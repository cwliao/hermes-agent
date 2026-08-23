import json
from types import SimpleNamespace

from agent.kanban_execution_guard import (
    KANBAN_EXECUTION_GUARD_SYNTHETIC,
    try_finalization,
    request_requires_four_lane_swarm,
)


PROMPT = (
    "四條 lane (native_hermes / claude / grok / agy) 各自獨立產出一句秋天諧音梗。"
    "Verifier 驗證；Synthesizer 整理。"
)


def _agent():
    return SimpleNamespace(
        _kanban_execution_guard_phase="",
        valid_tool_names={"kanban_swarm"},
        _emit_status=lambda _message: None,
        _session_messages=[],
    )


def _receipt_messages():
    return [
        {"role": "user", "content": PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "swarm-call",
                "function": {"name": "kanban_swarm", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "swarm-call",
            "content": json.dumps({
                "ok": True,
                "root_id": "t_root",
                "worker_ids": ["t_a", "t_b", "t_c", "t_d"],
                "verifier_id": "t_verify",
                "synthesizer_id": "t_synth",
            }),
        },
    ]


def test_request_classifier_is_narrow():
    assert request_requires_four_lane_swarm(PROMPT)
    assert not request_requires_four_lane_swarm("請說明 kanban swarm 是什麼")
    assert not request_requires_four_lane_swarm(
        "native_hermes 和 claude 各寫一句笑話"
    )


def test_success_requires_current_turn_swarm_receipt():
    agent = _agent()
    messages = _receipt_messages()
    final_msg = {"role": "assistant", "content": "real result"}
    outcome = try_finalization(agent, messages, 0, "real result", final_msg, list.append)
    assert outcome == "pass"
    assert agent._kanban_execution_guard_phase == ""


def test_fake_task_ids_without_tool_call_are_nudged_then_blocked():
    agent = _agent()
    messages = [{"role": "user", "content": PROMPT}]
    final_msg = {
        "role": "assistant",
        "content": "created t_l3n98rub and t_f6ry2vln",
    }
    outcome = try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "nudge"
    assert messages[-1][KANBAN_EXECUTION_GUARD_SYNTHETIC]
    assert agent._kanban_execution_guard_phase == "nudge_dispatched"

    second = {"role": "assistant", "content": "still fabricated"}
    outcome = try_finalization(agent, messages, 0, "still fabricated", second, list.append)
    assert outcome == "blocked"
    assert "could not verify" in second["content"]


def test_failed_mutation_receipt_is_blocked_without_retry():
    agent = _agent()
    messages = [
        {"role": "user", "content": PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "swarm-call",
                "function": {"name": "kanban_swarm", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "swarm-call", "content": '{"error":"failed"}'},
    ]
    final_msg = {"role": "assistant", "content": "I created the swarm"}
    assert try_finalization(agent, messages, 0, final_msg["content"], final_msg, list.append) == "blocked"


def test_old_receipt_before_current_user_does_not_count():
    agent = _agent()
    messages = _receipt_messages() + [
        {"role": "user", "content": PROMPT},
    ]
    final_msg = {"role": "assistant", "content": "old IDs reused"}
    assert try_finalization(agent, messages, 3, final_msg["content"], final_msg, list.append) == "nudge"


def test_control_escape_is_not_delivered_even_after_success_receipt():
    agent = _agent()
    messages = _receipt_messages()
    final_msg = {"role": "assistant", "content": "assign \\0fake-profile"}
    assert try_finalization(agent, messages, 0, final_msg["content"], final_msg, list.append) == "blocked"
    assert "could not verify" in final_msg["content"]
