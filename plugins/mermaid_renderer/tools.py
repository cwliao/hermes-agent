"""Agent-facing, bounded interface for the Mermaid renderer."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path
from uuid import uuid4

from plugins.mermaid_renderer import renderer


TASK_RENDER_TTL_SECONDS = 24 * 60 * 60
MAX_TASK_STATES = 4096
_PROCESS_TASK_SECRET = secrets.token_bytes(32)


class _TaskRenderGate:
    """Bounded, process-local one-successful-render gate keyed by task HMAC."""

    def __init__(
        self,
        *,
        process_secret: bytes,
        clock=time.monotonic,
        max_states: int = MAX_TASK_STATES,
        ttl_seconds: float = TASK_RENDER_TTL_SECONDS,
    ) -> None:
        self._process_secret = process_secret
        self._clock = clock
        self._max_states = max_states
        self._ttl_seconds = ttl_seconds
        self._states: dict[bytes, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def _key(self, task_id: str) -> bytes:
        return hmac.new(
            self._process_secret,
            task_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def _sweep_expired_completed(self, now: float) -> None:
        expired = [
            key
            for key, (state, expires_at) in self._states.items()
            if state == "completed" and expires_at is not None and expires_at <= now
        ]
        for key in expired:
            del self._states[key]

    def acquire(self, task_id: str) -> tuple[str, bytes | None]:
        """Atomically reserve a task, or report its existing bounded state."""
        key = self._key(task_id)
        now = self._clock()
        with self._lock:
            self._sweep_expired_completed(now)
            existing = self._states.get(key)
            if existing is not None:
                return existing[0], key
            if len(self._states) >= self._max_states:
                return "capacity_exceeded", None
            self._states[key] = ("in_progress", None)
            return "claimed", key

    def complete(self, key: bytes) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None and state[0] == "in_progress":
                self._states[key] = ("completed", self._clock() + self._ttl_seconds)

    def release(self, key: bytes) -> None:
        with self._lock:
            state = self._states.get(key)
            if state is not None and state[0] == "in_progress":
                del self._states[key]


_TASK_RENDER_GATE = _TaskRenderGate(process_secret=_PROCESS_TASK_SECRET)


RENDER_MERMAID_SCHEMA = {
    "name": "render_mermaid",
    "description": (
        "Render Mermaid diagram text to a PNG. Only Mermaid source and bounded "
        "dimensions are accepted; no URLs, HTML, filenames, or renderer options. "
        "Each task can render one PNG; request a new task for an alternate version."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Mermaid diagram source. Required.",
            },
            "width": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4096,
                "description": "Maximum PNG canvas width in pixels. Default 1600.",
            },
            "height": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4096,
                "description": "Maximum PNG canvas height in pixels. Default 1000.",
            },
        },
        "required": ["source"],
        "additionalProperties": False,
    },
}


def handle_render_mermaid(
    args: dict,
    **runtime_context,
) -> str:
    """Render registry arguments without exposing implementation detail."""
    if not isinstance(args, dict):
        return "status=failed\nerror=invalid_arguments"

    source = args.get("source")
    width = args.get("width", 1600)
    height = args.get("height", 1000)
    if (
        not isinstance(source, str)
        or not source.strip()
        or isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 1 <= width <= 4096
        or not 1 <= height <= 4096
    ):
        return "status=failed\nerror=invalid_arguments"

    task_id = runtime_context.get("task_id")
    runtime_context.clear()
    if not isinstance(task_id, str) or not task_id:
        return "status=failed\nerror=missing_task_context"

    gate_state, gate_key = _TASK_RENDER_GATE.acquire(task_id)
    del task_id
    if gate_state == "completed":
        return "status=skipped\nreason=render_already_completed"
    if gate_state == "in_progress":
        return "status=skipped\nreason=render_in_progress"
    if gate_state != "claimed" or gate_key is None:
        return "status=failed\nerror=render_capacity_exceeded"

    output_path = Path(renderer.MEDIA_ROOT) / f"{uuid4()}.png"
    try:
        result = renderer.render_mermaid_to_png(source, output_path, width=width, height=height)
    except Exception:
        _TASK_RENDER_GATE.release(gate_key)
        return "status=failed\nerror=render_failed"
    if not result.success:
        _TASK_RENDER_GATE.release(gate_key)
        return f"status=failed\nerror={result.error_code or 'render_failed'}"
    _TASK_RENDER_GATE.complete(gate_key)
    return f"MEDIA:{result.output_path}\nstatus=rendered"
