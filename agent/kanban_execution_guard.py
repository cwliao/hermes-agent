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
_KANBAN_MUTATION_WORDS = (
    "create", "建立", "創建", "新增", "assign", "指派", "dispatch",
    "建立任務", "建立工作", "建立 swarm", "建立 任務",
)

KANBAN_EXECUTION_NUDGE = (
    "[Internal execution check: the current user request explicitly requires "
    "a real four-lane Kanban swarm. Call kanban_swarm now with one worker per "
    "requested lane. Do not describe a plan or invent task IDs. Only report "
    "IDs and execution results returned by the successful tool receipt. If the "
    "tool is unavailable or fails, state that the request is blocked.]\n"
)

KANBAN_EXECUTION_BLOCKED = (
    "I could not verify that a new Kanban swarm was created for this request. "
    "A Kanban mutation may have failed or only partially completed, so no task "
    "IDs or lane results are being reported as completed. Please inspect the "
    "board and retry after the Kanban tool is available."
)

KANBAN_EXECUTION_PENDING = (
    "The four-lane Kanban swarm was created, but the workflow is not complete "
    "yet. The verifier and synthesizer must finish and produce a non-empty "
    "result before a final answer can be reported."
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


def request_requires_transactional_delivery(value: Any) -> bool:
    """Recognize turns whose Kanban mutation prose must not stream early.

    The four-lane classifier remains the strict execution-proof gate. This
    wider delivery classifier also covers ordinary requests such as
    "建立一個 Kanban task" without making the execution guard invent a
    receipt requirement for read-only Kanban questions.
    """
    text = _text(value).casefold()
    if request_requires_four_lane_swarm(value):
        return True
    if not any(term in text for term in ("kanban", "task", "任務", "swarm")):
        return False
    if any(
        phrase in text
        for phrase in ("不要建立", "不建立", "不要 create", "do not create", "don't create")
    ):
        return False
    return any(term.casefold() in text for term in _KANBAN_MUTATION_WORDS)


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


def _read_swarm_completion_state(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Read live downstream state before allowing a launch receipt to look final.

    A successful kanban_swarm receipt proves graph creation only. When the
    gateway can read the referenced board, a final user-facing response is
    accepted as complete only after the synthesizer is done with a non-empty
    result. Unknown test/fallback boards return None and retain the historical
    receipt behavior.
    """
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            verifier = kb.get_task(conn, str(payload["verifier_id"]))
            synthesizer = kb.get_task(conn, str(payload["synthesizer_id"]))
            if verifier is None or synthesizer is None:
                return {
                    "complete": False,
                    "verifier_status": "unknown",
                    "synthesizer_status": "unknown",
                }
            synth_result = (synthesizer.result or "").strip()
            return {
                "complete": synthesizer.status == "done" and bool(synth_result),
                "verifier_status": verifier.status,
                "synthesizer_status": synthesizer.status,
            }
        finally:
            conn.close()
    except Exception:
        logger.debug("kanban execution guard: downstream state probe failed", exc_info=True)
        return {
            "complete": False,
            "verifier_status": "unknown",
            "synthesizer_status": "unknown",
        }


def _mutation_attempted(messages: Sequence[Mapping[str, Any]], current_user_idx: int) -> bool:
    for message in messages[current_user_idx + 1 :]:
        if isinstance(message, Mapping) and any(
            _call_name(call) in KANBAN_MUTATION_TOOLS for call in _calls(message)
        ):
            return True
    return False


def _call_args(call: Mapping[str, Any]) -> Mapping[str, Any]:
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, Mapping) else call.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, Mapping) else {}


def _referenced_task_ids(args: Mapping[str, Any]) -> set[str]:
    """Collect task ids a kanban_create/kanban_link call's arguments touch."""
    ids: set[str] = set()
    parents = args.get("parents")
    if isinstance(parents, Sequence) and not isinstance(parents, (str, bytes)):
        ids.update(str(item).strip() for item in parents if str(item).strip())
    for key in ("parent_id", "child_id"):
        value = args.get(key)
        if value and str(value).strip():
            ids.add(str(value).strip())
    return ids


def _swarm_topology_mutation_attempted(
    messages: Sequence[Mapping[str, Any]],
    current_user_idx: int,
    active_swarms: Sequence[Mapping[str, Any]],
) -> bool:
    """True if this turn's kanban_create/kanban_link targets an active swarm's own nodes.

    Mirrors ``tools/kanban_tools.py``'s ``_reject_in_flight_swarm_topology_mutation``
    signal: a mutation is only in-scope for this guard if it structurally
    references a worker/verifier/synthesizer id of a swarm still in flight in
    this session, not merely "some mutation happened while some swarm exists".
    """
    if not active_swarms:
        return False
    topology_ids: set[str] = set()
    for swarm in active_swarms:
        topology_ids.add(swarm.get("synthesizer_id") or "")
        topology_ids.add(swarm.get("verifier_id") or "")
        topology_ids.update(swarm.get("worker_ids") or [])
    topology_ids.discard("")
    if not topology_ids:
        return False
    for message in messages[current_user_idx + 1 :]:
        if not isinstance(message, Mapping):
            continue
        for call in _calls(message):
            if _call_name(call) not in ("kanban_create", "kanban_link"):
                continue
            if _referenced_task_ids(_call_args(call)) & topology_ids:
                return True
    return False


def _swarm_attempted(messages: Sequence[Mapping[str, Any]], current_user_idx: int) -> bool:
    for message in messages[current_user_idx + 1 :]:
        if isinstance(message, Mapping) and any(
            _call_name(call) == KANBAN_SWARM_TOOL for call in _calls(message)
        ):
            return True
    return False


def _failed_mutation_tools(
    messages: Sequence[Mapping[str, Any]], current_user_idx: int
) -> list[str]:
    """Return mutation tool names with an explicit failed receipt.

    ``_mutation_attempted`` is deliberately broad and catches a missing
    receipt. This companion keeps the stronger evidence separate: a model
    turn that contains one successful ``kanban_create`` and one failed one is
    still known-bad, even if another receipt exists in the same turn.
    """
    calls_by_id: dict[str, Mapping[str, Any]] = {}
    for message in messages[current_user_idx + 1 :]:
        if not isinstance(message, Mapping):
            continue
        for call in _calls(message):
            if _call_id(call):
                calls_by_id[_call_id(call)] = call
    failed: list[str] = []
    for message in messages[current_user_idx + 1 :]:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        call = calls_by_id.get(str(message.get("tool_call_id") or ""))
        name = _call_name(call or {})
        if name not in KANBAN_MUTATION_TOOLS:
            continue
        payload = _decode_result(message.get("content"))
        if payload is None or payload.get("ok") is not True:
            failed.append(name)
    return failed


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


def _find_active_swarms_for_session() -> list[dict[str, Any]]:
    """Return active (non-terminal) swarm topologies belonging to the current session."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_swarm import find_active_swarms_for_session

        conn = kb.connect()
        try:
            return find_active_swarms_for_session(conn)
        finally:
            conn.close()
    except Exception:
        logger.debug("kanban execution guard: active swarm probe failed", exc_info=True)
        return []


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
    valid_current_user = 0 <= current_user_idx < len(messages) and isinstance(current_user, Mapping)
    prose_trigger = valid_current_user and request_requires_four_lane_swarm(current_user.get("content"))
    swarm_trigger = valid_current_user and _swarm_attempted(messages, current_user_idx)
    active_swarms = _find_active_swarms_for_session() if valid_current_user else []
    active_swarm_trigger = valid_current_user and _swarm_topology_mutation_attempted(
        messages, current_user_idx, active_swarms
    )
    if not prose_trigger and not swarm_trigger and not active_swarm_trigger:
        agent._kanban_execution_guard_phase = ""
        return "pass"

    if active_swarm_trigger and not prose_trigger and not swarm_trigger:
        # Defense-in-depth: tools/kanban_tools.py's
        # _reject_in_flight_swarm_topology_mutation should already have hard-
        # rejected a kanban_create/kanban_link that structurally targets an
        # active swarm's own worker/verifier/synthesizer nodes. This is the
        # redundant net for the (should-be-unreachable) case where a
        # topology-targeting mutation still reached a successful receipt.
        agent._kanban_execution_guard_phase = "blocked"
        final_msg["content"] = KANBAN_EXECUTION_BLOCKED
        logger.warning(
            "kanban_execution_guard decision=blocked "
            "reason=swarm_topology_mutation_in_session"
        )
        return "blocked"

    failed_mutations = _failed_mutation_tools(messages, current_user_idx)
    if failed_mutations:
        agent._kanban_execution_guard_phase = "blocked"
        final_msg["content"] = KANBAN_EXECUTION_BLOCKED
        logger.warning(
            "kanban_execution_guard decision=blocked "
            "reason=known_mutation_failure tools=%s",
            ",".join(sorted(set(failed_mutations))),
        )
        return "blocked"

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
        state = _read_swarm_completion_state(payload)
        if not state.get("complete"):
            agent._kanban_execution_guard_phase = ""
            final_msg["content"] = KANBAN_EXECUTION_PENDING
            logger.info(
                "kanban_execution_guard decision=pass_pending "
                "reason=swarm_created_downstream_incomplete verifier_status=%s "
                "synthesizer_status=%s",
                state.get("verifier_status"),
                state.get("synthesizer_status"),
            )
            return "pass"
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
    "KANBAN_EXECUTION_PENDING",
    "_find_active_swarms_for_session",
    "request_requires_four_lane_swarm",
    "request_requires_transactional_delivery",
    "try_finalization",
]
