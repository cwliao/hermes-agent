"""Deterministic proof gate for user-requested Kanban swarms.

The model may describe a swarm convincingly without ever calling the mutation
tool.  This module does not infer completion from prose or task-id-shaped
strings: a lane-bound request is accepted only after a successful
``kanban_swarm`` tool receipt in the current logical turn.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

KANBAN_EXECUTION_GUARD_SYNTHETIC = "_kanban_execution_guard_synthetic"
KANBAN_SWARM_TOOL = "kanban_swarm"
KANBAN_MUTATION_TOOLS = frozenset({"kanban_swarm", "kanban_create"})
LANE_IDS = ("native_hermes", "claude", "grok", "agy")

KANBAN_EXECUTION_NUDGE = (
    "[Internal execution check: the current user request explicitly requires "
    "a real four-lane Kanban swarm. Call kanban_swarm now with one worker per "
    "requested lane. Do not describe a plan or invent task IDs. Only report "
    "IDs and execution results returned by the successful tool receipt. If the "
    "tool is unavailable or fails, state that the request is blocked.]\n"
)

KANBAN_EXECUTION_BLOCKED = (
    "I could not verify that a new Kanban swarm was created for this request. "
    "No task IDs or lane results are being reported as completed. Please retry "
    "after the Kanban tool is available."
)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in value
            if isinstance(item, Mapping)
        )
    return "" if value is None else str(value)


def request_requires_four_lane_swarm(value: Any) -> bool:
    """Recognize only the narrow four-lane request shape we can enforce."""
    text = _text(value).casefold()
    lane_hits = sum(1 for lane in LANE_IDS if lane in text)
    has_lane_shape = "lane" in text and ("四條" in text or "4" in text)
    has_independent_outputs = "各自獨立" in text or "獨立產出" in text or "獨立産出" in text
    has_swarm_stage = any(word in text for word in ("verifier", "synthesizer", "kanban", "swarm"))
    return lane_hits >= 3 and has_lane_shape and has_independent_outputs and has_swarm_stage


def _calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = message.get("tool_calls")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _call_name(call: Mapping[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "")
    return str(call.get("name") or "")


def _call_id(call: Mapping[str, Any]) -> str:
    return str(call.get("id") or call.get("call_id") or "")


def _decode_result(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _successful_swarm_payload(messages: Sequence[Mapping[str, Any]], current_user_idx: int) -> Mapping[str, Any] | None:
    calls_by_id: dict[str, Mapping[str, Any]] = {}
    for message in messages[current_user_idx + 1 :]:
        if not isinstance(message, Mapping):
            continue
        for call in _calls(message):
            if _call_id(call):
                calls_by_id[_call_id(call)] = call
    for message in messages[current_user_idx + 1 :]:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        call = calls_by_id.get(call_id)
        if call is None or _call_name(call) != KANBAN_SWARM_TOOL:
            continue
        payload = _decode_result(message.get("content"))
        if not payload or payload.get("ok") is not True:
            continue
        worker_ids = payload.get("worker_ids")
        if (
            not isinstance(payload.get("root_id"), str)
            or not payload["root_id"].strip()
            or not isinstance(worker_ids, list)
            or len(worker_ids) < 4
            or not all(isinstance(item, str) and item.strip() for item in worker_ids)
            or not isinstance(payload.get("verifier_id"), str)
            or not isinstance(payload.get("synthesizer_id"), str)
        ):
            continue
        return payload
    return None


def _mutation_attempted(messages: Sequence[Mapping[str, Any]], current_user_idx: int) -> bool:
    for message in messages[current_user_idx + 1 :]:
        if isinstance(message, Mapping) and any(
            _call_name(call) in KANBAN_MUTATION_TOOLS for call in _calls(message)
        ):
            return True
    return False


def _has_tool(agent: Any, name: str) -> bool:
    names = getattr(agent, "valid_tool_names", None)
    if isinstance(names, (set, frozenset, list, tuple)):
        return name in {str(item) for item in names}
    for tool in getattr(agent, "tools", None) or ():
        if isinstance(tool, Mapping):
            function = tool.get("function")
            if isinstance(function, Mapping) and function.get("name") == name:
                return True
    return False


def _has_control_escape(value: Any) -> bool:
    text = _text(value)
    # The incident used the two-character escaped spelling ``\\0`` rather
    # than a byte NUL. Reject both forms at this user-visible boundary.
    return "\x00" in text or re.search(r"\\0(?=[A-Za-z])", text) is not None


def try_finalization(
    agent: Any,
    messages: list[dict[str, Any]],
    current_user_idx: int,
    final_response: str,
    final_msg: dict[str, Any],
    append_message_fn: Any,
) -> str:
    """Return ``pass``, ``nudge`` or ``blocked`` at the finalization choke point."""
    current_user = messages[current_user_idx] if 0 <= current_user_idx < len(messages) else {}
    if not isinstance(current_user, Mapping) or not request_requires_four_lane_swarm(current_user.get("content")):
        agent._kanban_execution_guard_phase = ""
        return "pass"

    payload = _successful_swarm_payload(messages, current_user_idx)
    phase = str(getattr(agent, "_kanban_execution_guard_phase", "") or "")
    if payload is not None:
        if _has_control_escape(final_response):
            agent._kanban_execution_guard_phase = "blocked"
            final_msg["content"] = KANBAN_EXECUTION_BLOCKED
            logger.warning(
                "kanban_execution_guard decision=blocked "
                "reason=control_escape_in_final_response"
            )
            return "blocked"
        agent._kanban_execution_guard_phase = ""
        logger.info(
            "kanban_execution_guard decision=pass reason=successful_swarm_receipt "
            "root=%s workers=%s",
            payload.get("root_id"), len(payload.get("worker_ids") or []),
        )
        return "pass"

    if _mutation_attempted(messages, current_user_idx):
        agent._kanban_execution_guard_phase = "blocked"
        final_msg["content"] = KANBAN_EXECUTION_BLOCKED
        logger.warning("kanban_execution_guard decision=blocked reason=mutation_without_successful_receipt")
        return "blocked"

    if phase in {"nudge_dispatched", "blocked"}:
        agent._kanban_execution_guard_phase = "blocked"
        final_msg["content"] = KANBAN_EXECUTION_BLOCKED
        logger.warning("kanban_execution_guard decision=blocked reason=no_receipt_after_bounded_nudge")
        return "blocked"

    if not _has_tool(agent, KANBAN_SWARM_TOOL):
        agent._kanban_execution_guard_phase = "blocked"
        final_msg["content"] = KANBAN_EXECUTION_BLOCKED
        logger.warning("kanban_execution_guard decision=blocked reason=kanban_swarm_unavailable")
        return "blocked"

    agent._kanban_execution_guard_phase = "nudge_dispatched"
    final_msg[KANBAN_EXECUTION_GUARD_SYNTHETIC] = True
    # The conversation loop owns append_message; this callback keeps the
    # helper pure and makes it straightforward to unit-test.
    if not callable(append_message_fn):
        raise RuntimeError("kanban execution guard append callback is not installed")
    append_message_fn(messages, final_msg)
    append_message_fn(messages, {
        "role": "user",
        "content": KANBAN_EXECUTION_NUDGE,
        KANBAN_EXECUTION_GUARD_SYNTHETIC: True,
    })
    agent._session_messages = messages
    logger.warning("kanban_execution_guard decision=nudge reason=no_current_turn_swarm_receipt")
    agent._emit_status("⚠️ Kanban request requires a real swarm tool call — asking the model to execute it")
    return "nudge"


__all__ = [
    "KANBAN_EXECUTION_BLOCKED",
    "KANBAN_EXECUTION_GUARD_SYNTHETIC",
    "KANBAN_EXECUTION_NUDGE",
    "request_requires_four_lane_swarm",
    "try_finalization",
]
