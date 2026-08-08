"""agentmemory memory plugin -- MemoryProvider interface.

Persistent memory via the agentmemory REST API (self-hosted, this DGX
Spark host's own instance at http://127.0.0.1:3111 by default -- same
box as the Hermes gateway, no network hop, no TLS needed).

Config via environment variables (profile-scoped via each profile's .env):
  AGENTMEMORY_SECRET  -- bearer token (required to authenticate; this
                          instance enforces auth even on localhost).
                          Raw token only -- "Bearer " is prepended when
                          building the Authorization header.
  AGENTMEMORY_URL      -- override for a remote instance
                          (default http://127.0.0.1:3111).

Config via config.yaml:
  memory:
    agentmemory:
      project: hermes   # tag written on every save

Working directory: $HERMES_HOME/agentmemory/ -- used only to persist a
collision-resistant instance_id across restarts (a UUID, not the bare
hostname, since hostnames aren't guaranteed unique across containers/VMs
-- see instance_id namespacing below). All actual memory content lives
in agentmemory itself, outside HERMES_HOME (see backup_paths()).

Instance namespacing (for future multi-Hermes scenarios): every write
tags a `concepts` entry with `hermes:<instance_id>` where instance_id
is a short UUID persisted to $HERMES_HOME/agentmemory/instance_id on
first run -- not the hostname, which can collide across cloned VMs or
respawned containers. session_id (from initialize()/on_memory_write's
metadata) is folded in alongside it where available. This is a minimum
viable scheme, not a full solution: there is no conflict-detection
registry and no session-level scoping of recall -- deliberately
deferred, not silently absent. A future second Hermes instance's setup
should just pick its own instance_id via this same mechanism, not
invent a new scheme.

Deliberately NOT implemented: mirroring on_memory_write(action="remove")
into agentmemory. The delete REST shape
(/agentmemory/governance/bulk-delete, DELETE /agentmemory/governance/memories)
was never verified against a live instance this session -- guessing a
destructive call against a shared store is worse than the gap. Ship
add/replace-only; a follow-up ticket can add verified deletion mirroring.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_PREFETCH_TIMEOUT = 3   # inline on the turn path -- must stay short
_WRITE_TIMEOUT = 5      # backgrounded -- can afford a little more
_MIN_QUERY_LEN = 8
_MIN_OUTPUT_CHARS = 20


def _load_plugin_config() -> Dict[str, Any]:
    """Read agentmemory's profile-scoped memory config (non-secret fields)."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
        memory_config = config.get("memory", {})
        if not isinstance(memory_config, dict):
            return {}
        provider_config = memory_config.get("agentmemory", {})
        return dict(provider_config) if isinstance(provider_config, dict) else {}
    except Exception:
        return {}


def _get_or_create_instance_id() -> str:
    """Return a short, collision-resistant instance id, persisted across restarts.

    Not the bare hostname -- hostnames aren't guaranteed unique across
    containers/VMs (a cloned VM or respawned container could collide).
    """
    try:
        from hermes_constants import get_hermes_home

        state_dir = get_hermes_home() / "agentmemory"
        state_dir.mkdir(parents=True, exist_ok=True)
        id_path = state_dir / "instance_id"
        if id_path.exists():
            existing = id_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        new_id = uuid.uuid4().hex[:12]
        id_path.write_text(new_id, encoding="utf-8")
        return new_id
    except Exception:
        # Best-effort -- a fresh id every process start is still correct,
        # just not stable across restarts. Never fatal.
        return uuid.uuid4().hex[:12]


def _post(base_url: str, secret: str, path: str, body: dict, timeout: int) -> Optional[dict]:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        logger.debug("agentmemory %s failed: %s", path, e)
        return None


RECALL_SCHEMA = {
    "name": "agentmemory_recall",
    "description": (
        "Search agentmemory's persistent, cross-session memory for relevant "
        "past context -- facts, decisions, user preferences, prior sessions. "
        "Distinct from the built-in memory tool: this searches the shared, "
        "long-term agentmemory store, not this session's MEMORY.md/USER.md."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "agentmemory_remember",
    "description": (
        "Store important information in agentmemory's persistent, "
        "cross-session memory -- architectural decisions, user preferences, "
        "facts worth remembering beyond this conversation. Distinct from "
        "the built-in memory tool: this writes to the shared, long-term "
        "agentmemory store, not this session's MEMORY.md/USER.md."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
            "concepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional short tags for this memory.",
            },
        },
        "required": ["content"],
    },
}


class AgentMemoryProvider(MemoryProvider):
    """agentmemory persistent memory via its REST API."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = dict(config) if config is not None else _load_plugin_config()
        self._base_url = os.environ.get("AGENTMEMORY_URL") or "http://127.0.0.1:3111"
        self._secret = os.environ.get("AGENTMEMORY_SECRET", "")
        self._project = self._config.get("project", "hermes")
        self._instance_id = _get_or_create_instance_id()
        self._session_id = ""
        self._sync_thread: Optional[threading.Thread] = None
        self._write_thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str:
        return "agentmemory"

    def is_available(self) -> bool:
        # No network calls -- config presence only, per the ABC contract.
        return bool(self._base_url)

    def get_config_schema(self):
        return [
            {
                "key": "secret",
                "description": "agentmemory bearer token (docker exec agentmemory cat /data/.hmac)",
                "secret": True,
                "env_var": "AGENTMEMORY_SECRET",
            },
            {
                "key": "base_url",
                "description": "agentmemory REST base URL",
                "default": "http://127.0.0.1:3111",
                "env_var": "AGENTMEMORY_URL",
            },
            {
                "key": "project",
                "description": "Project tag written on every save",
                "default": "hermes",
            },
        ]

    def _instance_tags(self) -> List[str]:
        tags = [self._project, f"hermes:{self._instance_id}"]
        if self._session_id:
            tags.append(f"session:{self._session_id}")
        return tags

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id

    def system_prompt_block(self) -> str:
        return (
            "# agentmemory\n"
            "Active. Persistent cross-session memory server (self-hosted). "
            "Use agentmemory_recall to search past context, "
            "agentmemory_remember to store important facts."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Synchronous recall before the turn's first LLM call.

        Kept to a short (3s) timeout deliberately -- this runs inline on
        the turn path with no manager-enforced timeout of its own, so an
        unbounded call here would hang every gateway turn, not just
        degrade this one plugin (agentmemory is a shared host that has
        hung before).
        """
        if not query or len(query.strip()) < _MIN_QUERY_LEN:
            return ""
        result = _post(
            self._base_url, self._secret, "/agentmemory/smart-search",
            {"query": query.strip()[:2000], "limit": 5},
            timeout=_PREFETCH_TIMEOUT,
        )
        if not result:
            return ""
        items = result.get("results") or []
        if not items:
            return ""
        lines = [f"- {item.get('title') or item.get('type') or 'memory'}" for item in items[:5]]
        text = "\n".join(lines)
        if len(text) < _MIN_OUTPUT_CHARS:
            return ""
        return f"## agentmemory context\n{text}"

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist a completed turn in the background (non-blocking)."""
        if len(user_content.strip()) < _MIN_QUERY_LEN:
            return

        def _sync():
            content = f"User: {user_content[:2000]}\nAssistant: {assistant_content[:2000]}"
            _post(
                self._base_url, self._secret, "/agentmemory/remember",
                {
                    "content": content,
                    "concepts": self._instance_tags() + ["hermes-turn"],
                },
                timeout=_WRITE_TIMEOUT,
            )

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="agentmemory-sync")
        self._sync_thread.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory add/replace writes to agentmemory.

        Deliberately backgrounds itself -- MemoryManager does NOT
        background this hook automatically the way it does prefetch/
        sync_turn, so a synchronous call here would stall the tool
        response mid-turn if agentmemory is slow/unreachable.

        action == "remove" is intentionally not mirrored -- see the
        module docstring for why.
        """
        if action not in {"add", "replace"} or not content:
            return

        session_id = (metadata or {}).get("session_id") or self._session_id

        def _write():
            label = "User profile" if target == "user" else "Agent memory"
            tags = self._instance_tags() + ["hermes-builtin-mirror"]
            if session_id and f"session:{session_id}" not in tags:
                tags.append(f"session:{session_id}")
            _post(
                self._base_url, self._secret, "/agentmemory/remember",
                {"content": f"[{label}] {content}", "concepts": tags},
                timeout=_WRITE_TIMEOUT,
            )

        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5.0)
        self._write_thread = threading.Thread(target=_write, daemon=True, name="agentmemory-memwrite")
        self._write_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, REMEMBER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "agentmemory_recall":
            return self._tool_recall(args)
        elif tool_name == "agentmemory_remember":
            return self._tool_remember(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        for t in (self._sync_thread, self._write_thread):
            if t and t.is_alive():
                t.join(timeout=10.0)

    def backup_paths(self) -> List[str]:
        # Memory content lives in agentmemory itself, outside HERMES_HOME.
        # Only the persisted instance_id lives under HERMES_HOME, and
        # `hermes backup` already walks HERMES_HOME wholesale -- nothing
        # extra to declare here.
        return []

    # -- Tool implementations ------------------------------------------------

    def _tool_recall(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")
        result = _post(
            self._base_url, self._secret, "/agentmemory/smart-search",
            {"query": query.strip()[:2000], "limit": 8},
            timeout=_WRITE_TIMEOUT,
        )
        if result is None:
            return tool_error("agentmemory request failed")
        items = result.get("results") or []
        if not items:
            return json.dumps({"result": "No relevant memories found."})
        return json.dumps({"result": items})

    def _tool_remember(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")
        concepts = args.get("concepts") or []
        result = _post(
            self._base_url, self._secret, "/agentmemory/remember",
            {"content": content, "concepts": self._instance_tags() + list(concepts)},
            timeout=_WRITE_TIMEOUT,
        )
        if result is None:
            return tool_error("agentmemory save failed")
        return json.dumps({"result": "Memory saved successfully."})


def register(ctx) -> None:
    """Register agentmemory as a memory provider plugin."""
    ctx.register_memory_provider(AgentMemoryProvider())
