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
# These are exact, user-facing action names whose result must come from the
# live report tool even when transcript compression left no clean baseline.
# Keep this registry narrow and explicit; do not replace it with prose or
# timestamp heuristics.
FRESHNESS_REQUIRED_ACTIONS = frozenset({"webboard"})

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
            "_kanban_execution_guard_synthetic",
        )
    )


def _tool_name(call: Any) -> Optional[str]:
    if isinstance(call, Mapping):
        function = call.get("function") or {}
        return str(function.get("name") or call.get("name") or "") or None
    function = getattr(call, "function", None)
    return str(getattr(function, "name", None) or getattr(call, "name", None) or "") or None


def is_read_only_webboard_report_call(call: Any) -> bool:
    """Recognize only the repository-owned, read-only Webboard report call."""
    if isinstance(call, Mapping):
        function = call.get("function") or {}
        name = str(function.get("name") or call.get("name") or "")
        arguments = function.get("arguments") or call.get("arguments") or {}
    else:
        function = getattr(call, "function", None)
        name = str(getattr(function, "name", None) or getattr(call, "name", None) or "")
        arguments = getattr(function, "arguments", None) or getattr(call, "arguments", None) or {}
    if name != "terminal":
        return False
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return False
    if not isinstance(arguments, Mapping):
        return False
    command = normalize_replay_text(arguments.get("command"))
    return command in {
        "bash ~/.hermes/scripts/hermes_webboard_report.sh",
        "bash /home/cwliao/.hermes/scripts/hermes_webboard_report.sh",
    }


def replay_tool_call_is_safe(
    call: Any, idempotent_tools: Sequence[str], mutating_tools: Sequence[str]
) -> bool:
    """Apply the same registry fence at both candidate and receipt time."""
    name = _tool_name(call)
    if not name:
        return False
    if name in idempotent_tools and name not in mutating_tools:
        return True
    return is_read_only_webboard_report_call(call)


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


def _resolved_action_metadata(agent: Any) -> Optional[dict[str, Any]]:
    """Return deterministic action metadata for the current runtime surface."""
    metadata = _explicit_execution_metadata(agent)
    if metadata is not None:
        return metadata
    tool_surface = getattr(agent, "valid_tool_names", None)
    if not isinstance(tool_surface, (set, frozenset, list, tuple)):
        tool_surface = [
            str(tool.get("function", {}).get("name") or tool.get("name") or "")
            for tool in (getattr(agent, "tools", None) or [])
            if isinstance(tool, Mapping)
        ]
    tool_surface = sorted(name for name in (str(v) for v in tool_surface) if name)
    if not tool_surface:
        return None
    return {
        "platform": str(getattr(agent, "platform", "") or ""),
        "model": str(getattr(agent, "model", "") or ""),
        "model_tool_surface": tool_surface,
    }


def _action_identity_for_user(
    message: Mapping[str, Any], metadata: Mapping[str, Any]
) -> Optional[str]:
    supplied_identity = message.get("_action_identity")
    if isinstance(supplied_identity, Mapping):
        return action_identity_digest(supplied_identity)
    return action_identity_digest(
        {
            **metadata,
            "request_digest": hashlib.sha256(
                normalize_replay_text(message.get("content")).encode("utf-8")
            ).hexdigest(),
        }
    )


def _has_tool_surface(agent: Any, tool_name: str) -> bool:
    metadata = _resolved_action_metadata(agent)
    if not metadata:
        return False
    names = metadata.get("model_tool_surface") or ()
    return tool_name in {str(name) for name in names}


@dataclass(frozen=True)
class ReplayCandidate:
    logical_turn_key: str
    action_identity: str
    previous_answer: str
    current_answer: str
    previous_tool_names: tuple[str, ...]
    branch_id: str
    recovery_safe: bool = True
    unsafe_reason: str = ""
    baseline_missing: bool = False


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
    """Find an exact replay candidate, including compressed transcript tails.

    Compression can preserve the original tool-backed answer while inserting
    summary/session metadata rows and duplicate user/assistant projections
    between that answer and the current user message.  Looking only at the
    immediately preceding user turn therefore misses a real replay.  We still
    require an exact answer and an action-equivalent user message, but walk
    backwards until we find the most recent matching answer with its own
    tool-call evidence.  The coordinator decides whether an unsafe tool is
    eligible for automatic recovery; returning it as a candidate lets runtime
    logging make the blocked decision observable instead of silently passing.
    """
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
    metadata = _resolved_action_metadata(agent)
    if metadata is None:
        return None
    current_digest = _action_identity_for_user(current_user, metadata)
    if not current_digest:
        return None

    current_normalized = normalize_replay_text(current_answer)
    if not current_normalized:
        return None

    def _tool_result_for_call(
        span: Sequence[Mapping[str, Any]], call: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        call_id = str(call.get("id") or call.get("call_id") or "")
        if not call_id:
            return None
        for item in span:
            if (
                isinstance(item, Mapping)
                and item.get("role") == "tool"
                and str(item.get("tool_call_id") or "") == call_id
            ):
                return item
        return None

    def _tool_result_failed(result: Mapping[str, Any]) -> bool:
        content = result.get("content")
        if isinstance(content, Mapping):
            return content.get("success") is False or bool(content.get("error"))
        if isinstance(content, str):
            stripped = content.lstrip().lower()
            if (
                stripped.startswith("tool '")
                or stripped.startswith("tool \"")
                or stripped.startswith("[duplicate tool output")
            ):
                return True
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, Mapping):
                return decoded.get("success") is False or bool(decoded.get("error"))
        return False

    def _collect_tool_evidence(
        span: Sequence[Mapping[str, Any]],
    ) -> tuple[list[str], list[Any], list[str]]:
        names: list[str] = []
        calls_seen: list[Any] = []
        missing_receipts: list[str] = []
        for message in span:
            if not isinstance(message, Mapping) or _is_synthetic(message):
                continue
            calls = message.get("tool_calls") or []
            if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
                continue
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                name = _tool_name(call)
                if not name:
                    continue
                result = _tool_result_for_call(span, call)
                if result is None:
                    # A call without its terminal tool row is incomplete
                    # telemetry. Keep it visible to the coordinator so the
                    # candidate can be blocked, never treated as idempotent.
                    calls_seen.append(call)
                    names.append(name)
                    missing_receipts.append(name)
                    continue
                # A failed/unknown tool proposal is not execution evidence.
                # In particular, Webboard's old transcript contains a failed
                # phantom ``webboard`` call followed by the real read-only
                # terminal report.
                if result is not None and _tool_result_failed(result):
                    continue
                calls_seen.append(call)
                names.append(name)
        return names, calls_seen, missing_receipts

    # Walk backward over compressed/duplicated projections.  A candidate is
    # valid only when its own matching user turn has a preceding assistant
    # tool-call receipt and the answer is exact; intervening repeated answers
    # without a tool receipt are intentionally skipped.
    for answer_idx in range(current_user_idx - 1, -1, -1):
        answer_message = messages[answer_idx]
        if not isinstance(answer_message, Mapping) or _is_synthetic(answer_message):
            continue
        if answer_message.get("role") != "assistant" or _message_tool_names(answer_message):
            continue
        previous_answer = normalize_replay_text(answer_message.get("content"))
        if not previous_answer or previous_answer != current_normalized:
            continue

        previous_user_idx = None
        previous_tool_names: list[str] = []
        previous_tool_calls: list[Any] = []
        previous_missing_receipts: list[str] = []
        # Prefer the nearest matching user that has a receipt.  If compression
        # inserted duplicate user/assistant projections, continue through the
        # same action's older users until the original tool-backed segment is
        # found; do not let a no-tool duplicate hide it.
        for idx in range(answer_idx - 1, -1, -1):
            message = messages[idx]
            if not (
                isinstance(message, Mapping)
                and message.get("role") == "user"
                and not _is_synthetic(message)
            ):
                continue
            previous_digest = _action_identity_for_user(message, metadata)
            if not previous_digest or previous_digest != current_digest:
                continue
            names, calls, missing = _collect_tool_evidence(
                messages[idx + 1 : answer_idx]
            )
            if names:
                previous_user_idx = idx
                previous_tool_names = names
                previous_tool_calls = calls
                previous_missing_receipts = missing
                break
        if not previous_tool_names:
            continue

        unsafe_names = [
            name for name in previous_tool_names
            if not any(
                replay_tool_call_is_safe(call, idempotent_tools, mutating_tools)
                for call in previous_tool_calls
                if _tool_name(call) == name
            )
        ]
        if previous_missing_receipts:
            unsafe_names.extend(
                "missing_receipt:" + name for name in previous_missing_receipts
            )
        branch_id = str(
            getattr(agent, "_replay_branch_id", "")
            or getattr(agent, "session_id", "")
            or ""
        )
        if not branch_id:
            continue
        return ReplayCandidate(
            logical_turn_key=evidence.logical_turn_key,
            action_identity=current_digest,
            previous_answer=previous_answer,
            current_answer=current_normalized,
            previous_tool_names=tuple(previous_tool_names),
            branch_id=branch_id,
            recovery_safe=not unsafe_names,
            unsafe_reason=(
                "unsafe_or_unknown_tools:" + ",".join(sorted(set(unsafe_names)))
                if unsafe_names else ""
            ),
        )
    return None


def find_baseline_less_candidate(
    messages: Sequence[Mapping[str, Any]],
    current_user_idx: int,
    current_answer: Any,
    agent: Any,
    evidence: ReplayEvidence,
) -> Optional[ReplayCandidate]:
    """Require a fresh result for an exact live action with no clean baseline.

    This is intentionally narrower than replay detection.  It only covers the
    Telegram ``Webboard`` action while a terminal tool surface is present.  A
    missing baseline is not evidence that the answer is stale; it is a reason
    to refuse silently accepting the answer and request one bounded fresh
    execution instead.
    """
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
    if (
        not isinstance(current_user, Mapping)
        or current_user.get("role") != "user"
        or _is_synthetic(current_user)
        or normalize_replay_text(current_user.get("content")).casefold()
        not in FRESHNESS_REQUIRED_ACTIONS
    ):
        return None
    if str(getattr(agent, "platform", "") or "").casefold() != "telegram":
        return None
    if not _has_tool_surface(agent, "terminal"):
        return None
    current_normalized = normalize_replay_text(current_answer)
    if not current_normalized:
        return None
    metadata = _resolved_action_metadata(agent)
    if metadata is None:
        return None
    branch_id = str(
        getattr(agent, "_replay_branch_id", "")
        or getattr(agent, "session_id", "")
        or ""
    )
    if not branch_id:
        return None
    current_digest = _action_identity_for_user(current_user, metadata)
    if not current_digest:
        return None
    return ReplayCandidate(
        logical_turn_key=evidence.logical_turn_key,
        action_identity=current_digest,
        previous_answer="",
        current_answer=current_normalized,
        previous_tool_names=(),
        branch_id=branch_id,
        recovery_safe=True,
        baseline_missing=True,
    )


__all__ = [
    "ACTION_IDENTITY_VERSION",
    "NORMALIZATION_VERSION",
    "REGISTRY_VERSION",
    "FRESHNESS_REQUIRED_ACTIONS",
    "REPLAY_NUDGE",
    "ReplayCandidate",
    "ReplayEvidence",
    "TOOL_EXECUTION_VERSION",
    "action_identity_digest",
    "find_candidate",
    "find_baseline_less_candidate",
    "is_read_only_webboard_report_call",
    "normalize_replay_text",
    "replay_tool_call_is_safe",
    "tool_registry_digest",
]
