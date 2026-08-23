"""Fail-closed guard for exact stale model-answer replay.

This module deliberately contains no provider calls and no transcript writes.
It only identifies the narrow, deterministic case where an action-oriented
turn returned the exact previous answer without a tool execution receipt.
The conversation loop owns the bounded nudge/fallback state machine; SessionDB
owns the durable CAS ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


NORMALIZATION_VERSION = "normalization_v1"
ACTION_IDENTITY_VERSION = "action_identity_v1"
TOOL_EXECUTION_VERSION = "tool_execution_v1"
REGISTRY_VERSION = "hermes-tool-registry-v1"

REPLAY_NUDGE = (
    "[Internal recovery: re-evaluate the current request from the live state. "
    "Do not copy the previous answer. If the request requires a tool, execute "
    "the required read-only or idempotent tool now, then report only its fresh result.]"
)

_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def normalize_replay_text(value: Any) -> str:
    """Apply the fixed normalization_v1 transport-only normalization."""
    text = "" if value is None else str(value)
    text = _CSI_RE.sub("", text)
    text = _OSC_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    return unicodedata.normalize("NFC", text).strip()


def _finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, Mapping):
        return {str(k): _finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(v) for v in value]
    return value


def action_identity_digest(metadata: Mapping[str, Any]) -> Optional[str]:
    """Return a digest of structured action metadata, never the raw metadata."""
    if not isinstance(metadata, Mapping) or not metadata:
        return None
    try:
        canonical = json.dumps(
            _finite_json(metadata),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(
        (ACTION_IDENTITY_VERSION + "\0").encode("ascii") + canonical
    ).hexdigest()


def tool_registry_digest(
    idempotent_tools: Sequence[str], mutating_tools: Sequence[str]
) -> str:
    payload = json.dumps(
        {
            "version": REGISTRY_VERSION,
            "idempotent": sorted(str(name) for name in idempotent_tools),
            "mutating": sorted(str(name) for name in mutating_tools),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_synthetic(message: Mapping[str, Any]) -> bool:
    return any(
        bool(message.get(key))
        for key in (
            "_empty_recovery_synthetic",
            "_verification_stop_synthetic",
            "_pre_verify_synthetic",
            "_kanban_stop_synthetic",
            "_dropped_toolcall_nudge",
            "_model_replay_guard_synthetic",
        )
    )


def _tool_name(call: Any) -> Optional[str]:
    if isinstance(call, Mapping):
        function = call.get("function") or {}
        return str(function.get("name") or call.get("name") or "") or None
    function = getattr(call, "function", None)
    return str(getattr(function, "name", None) or getattr(call, "name", None) or "") or None


def _message_tool_names(message: Mapping[str, Any]) -> list[str]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        return []
    return [name for call in calls if (name := _tool_name(call))]


def _explicit_execution_metadata(agent: Any) -> Optional[dict[str, Any]]:
    """Use existing explicit execution configuration; never classify prose."""
    supplied = getattr(agent, "_replay_action_metadata", None)
    if isinstance(supplied, Mapping) and supplied:
        return dict(supplied)
    enforcement = str(getattr(agent, "_tool_use_enforcement", "") or "").strip().lower()
    guidance = str(getattr(agent, "_execution_guidance", "") or "").strip().lower()
    if enforcement in {"", "auto", "never", "off", "disabled"} and guidance in {
        "",
        "auto",
        "never",
        "off",
        "disabled",
    }:
        return None
    return {
        "tool_use_enforcement": enforcement,
        "execution_guidance": guidance,
        "platform": str(getattr(agent, "platform", "") or ""),
        "model_tool_surface": sorted(
            str(tool.get("function", {}).get("name") or tool.get("name") or "")
            for tool in (getattr(agent, "tools", None) or [])
            if isinstance(tool, Mapping)
        ),
    }


@dataclass(frozen=True)
class ReplayCandidate:
    logical_turn_key: str
    action_identity: str
    previous_answer: str
    current_answer: str
    previous_tool_names: tuple[str, ...]
    branch_id: str


@dataclass(frozen=True)
class ReplayEvidence:
    """Coordinator-produced evidence; missing/false complete is ineligible."""

    version: str
    logical_turn_key: str
    session_id: str
    branch_id: str
    generation: int
    complete: bool
    zero_calls_proven: bool
    closure_version: str = "closure_v1"
    calls: tuple[Mapping[str, Any], ...] = ()
    cutoff_sequence: int = 0


def find_candidate(
    messages: Sequence[Mapping[str, Any]],
    current_user_idx: int,
    current_answer: Any,
    agent: Any,
    evidence: ReplayEvidence,
    *,
    idempotent_tools: frozenset[str],
    mutating_tools: frozenset[str],
) -> Optional[ReplayCandidate]:
    """Find only an adjacent same-session/branch exact replay candidate."""
    if (
        evidence.version != TOOL_EXECUTION_VERSION
        or evidence.closure_version != "closure_v1"
        or not evidence.complete
        or not evidence.zero_calls_proven
        or evidence.cutoff_sequence <= 0
        or evidence.calls
        or not evidence.session_id
        or evidence.logical_turn_key != str(getattr(agent, "_current_turn_id", "") or "")
        or not (0 <= current_user_idx < len(messages))
    ):
        return None
    current_user = messages[current_user_idx]
    if not isinstance(current_user, Mapping) or current_user.get("role") != "user" or _is_synthetic(current_user):
        return None
    metadata = _explicit_execution_metadata(agent)
    if metadata is None:
        return None
    current_identity = current_user.get("_action_identity")
    if isinstance(current_identity, Mapping):
        current_digest = action_identity_digest(current_identity)
    else:
        current_digest = action_identity_digest(
            {**metadata, "request_digest": hashlib.sha256(
                normalize_replay_text(current_user.get("content")).encode("utf-8")
            ).hexdigest()}
        )
    if not current_digest:
        return None

    previous_user_idx = None
    for idx in range(current_user_idx - 1, -1, -1):
        message = messages[idx]
        if isinstance(message, Mapping) and message.get("role") == "user" and not _is_synthetic(message):
            previous_user_idx = idx
            break
    if previous_user_idx is None:
        return None
    previous_user = messages[previous_user_idx]
    previous_identity = previous_user.get("_action_identity")
    if isinstance(previous_identity, Mapping):
        previous_digest = action_identity_digest(previous_identity)
    else:
        previous_digest = action_identity_digest(
            {**metadata, "request_digest": hashlib.sha256(
                normalize_replay_text(previous_user.get("content")).encode("utf-8")
            ).hexdigest()}
        )
    if not previous_digest or previous_digest != current_digest:
        return None

    previous_tool_names: list[str] = []
    previous_answer = None
    for message in messages[previous_user_idx + 1 : current_user_idx]:
        if not isinstance(message, Mapping) or _is_synthetic(message):
            continue
        previous_tool_names.extend(_message_tool_names(message))
        if message.get("role") == "assistant" and not _message_tool_names(message):
            content = normalize_replay_text(message.get("content"))
            if content:
                previous_answer = content
    current_normalized = normalize_replay_text(current_answer)
    if not previous_answer or not current_normalized or previous_answer != current_normalized:
        return None
    if not previous_tool_names:
        return None
    if any(name in mutating_tools or name not in idempotent_tools for name in previous_tool_names):
        return None
    branch_id = str(getattr(agent, "_replay_branch_id", "") or getattr(agent, "session_id", "") or "")
    if not branch_id:
        return None
    return ReplayCandidate(
        logical_turn_key=evidence.logical_turn_key,
        action_identity=current_digest,
        previous_answer=previous_answer,
        current_answer=current_normalized,
        previous_tool_names=tuple(previous_tool_names),
        branch_id=branch_id,
    )


__all__ = [
    "ACTION_IDENTITY_VERSION",
    "NORMALIZATION_VERSION",
    "REGISTRY_VERSION",
    "REPLAY_NUDGE",
    "ReplayCandidate",
    "ReplayEvidence",
    "TOOL_EXECUTION_VERSION",
    "action_identity_digest",
    "find_candidate",
    "normalize_replay_text",
    "tool_registry_digest",
]
