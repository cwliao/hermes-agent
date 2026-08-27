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

NON_MATCHING_PROMPT = "跑一個顏色 swarm 測試"


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


def test_launch_receipt_cannot_claim_completion_while_downstream_is_pending(monkeypatch):
    import agent.kanban_execution_guard as guard

    monkeypatch.setattr(
        guard,
        "_read_swarm_completion_state",
        lambda _payload: {
            "complete": False,
            "verifier_status": "todo",
            "synthesizer_status": "todo",
        },
    )
    agent = _agent()
    messages = _receipt_messages()
    final_msg = {"role": "assistant", "content": "full workflow complete"}
    outcome = guard.try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert "not complete yet" in final_msg["content"]


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


def test_mixed_success_and_failure_receipts_are_blocked():
    agent = _agent()
    messages = [
        {"role": "user", "content": PROMPT},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "create-1", "function": {"name": "kanban_create", "arguments": "{}"}},
                {"id": "create-2", "function": {"name": "kanban_create", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"ok": true, "task_id": "t_one"}'},
        {"role": "tool", "tool_call_id": "create-2", "content": '{"error": "title is required"}'},
    ]
    final_msg = {"role": "assistant", "content": "Both tasks were created."}
    assert try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    ) == "blocked"
    assert "could not verify" in final_msg["content"]


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


def test_swarm_trigger_reproduces_incident_with_non_matching_prose(monkeypatch):
    import agent.kanban_execution_guard as guard

    monkeypatch.setattr(
        guard,
        "_read_swarm_completion_state",
        lambda _payload: {
            "complete": False,
            "verifier_status": "todo",
            "synthesizer_status": "todo",
        },
    )
    agent = _agent()
    messages = [{"role": "user", "content": NON_MATCHING_PROMPT}] + _receipt_messages()[1:]
    final_msg = {"role": "assistant", "content": "full workflow complete, colors summarized"}
    outcome = guard.try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert "not complete yet" in final_msg["content"]


def test_swarm_trigger_does_not_fire_for_plain_successful_kanban_create():
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-1",
                "function": {"name": "kanban_create", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"ok": true, "task_id": "t_one"}'},
    ]
    final_msg = {"role": "assistant", "content": "created the task"}
    outcome = try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert final_msg["content"] == "created the task"
    assert agent._kanban_execution_guard_phase == ""


def test_swarm_trigger_does_not_fire_for_failed_kanban_create():
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-1",
                "function": {"name": "kanban_create", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"error": "title is required"}'},
    ]
    final_msg = {"role": "assistant", "content": "the task creation failed"}
    outcome = try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert final_msg["content"] == "the task creation failed"
    assert agent._kanban_execution_guard_phase == ""


def test_ordinary_conversation_passes_immediately():
    agent = _agent()
    messages = [
        {"role": "user", "content": "今天天氣如何？"},
        {"role": "assistant", "content": "晴天"},
        {"role": "user", "content": "謝謝"},
    ]
    final_msg = {"role": "assistant", "content": "不客氣"}
    outcome = try_finalization(agent, messages, 2, final_msg["content"], final_msg, list.append)
    assert outcome == "pass"
    assert final_msg["content"] == "不客氣"
    assert agent._kanban_execution_guard_phase == ""


def test_swarm_trigger_blocks_on_failed_kanban_swarm_with_non_matching_prose():
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "swarm-call",
                "function": {"name": "kanban_swarm", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "swarm-call", "content": '{"error":"failed"}'},
    ]
    final_msg = {"role": "assistant", "content": "the swarm ran successfully"}
    outcome = try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "blocked"
    assert "could not verify" in final_msg["content"]


def test_swarm_trigger_blocks_on_non_four_lane_swarm_with_non_matching_prose():
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
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
                "worker_ids": ["t_a", "t_b"],
                "verifier_id": "t_verify",
                "synthesizer_id": "t_synth",
            }),
        },
    ]
    final_msg = {"role": "assistant", "content": "two-lane swarm complete"}
    outcome = try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "blocked"
    assert "could not verify" in final_msg["content"]


def test_matching_prose_with_only_kanban_create_stays_blocked_not_nudge():
    agent = _agent()
    messages = [
        {"role": "user", "content": PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-1",
                "function": {"name": "kanban_create", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"ok": true, "task_id": "t_one"}'},
    ]
    final_msg = {"role": "assistant", "content": "created the task"}
    outcome = try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "blocked"
    assert "could not verify" in final_msg["content"]


def test_invalid_current_user_idx_stays_pass_even_with_earlier_swarm_call():
    agent = _agent()
    messages = _receipt_messages() + [
        {"role": "user", "content": NON_MATCHING_PROMPT},
    ]
    final_msg = {"role": "assistant", "content": "unrelated reply"}
    outcome = try_finalization(
        agent, messages, -1, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert final_msg["content"] == "unrelated reply"
    assert agent._kanban_execution_guard_phase == ""


def test_non_matching_prose_with_kanban_create_blocked_when_targets_swarm_topology(monkeypatch):
    """A kanban_create parented into an active swarm's own topology node (e.g. its
    verifier) is blocked from finalizing, as defense-in-depth alongside the
    tool-level guard in tools/kanban_tools.py."""
    import agent.kanban_execution_guard as guard

    monkeypatch.setattr(
        guard,
        "_find_active_swarms_for_session",
        lambda: [{
            "root_id": "t_root_active",
            "synthesizer_id": "t_synth_active",
            "verifier_id": "t_verify_active",
            "worker_ids": ["t_a", "t_b", "t_c", "t_d"],
        }],
    )
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-1",
                "function": {
                    "name": "kanban_create",
                    "arguments": json.dumps({"title": "substitute", "parents": ["t_verify_active"]}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"ok": true, "task_id": "t_substitute"}'},
    ]
    final_msg = {"role": "assistant", "content": "I created a substitute task and here is the result"}
    outcome = guard.try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "blocked"
    assert "could not verify" in final_msg["content"]


def test_non_matching_prose_with_unrelated_kanban_create_passes_despite_active_swarm(monkeypatch):
    """An active swarm existing elsewhere in the session must NOT block a kanban_create
    for a genuinely unrelated task (i.e. one that doesn't reference the swarm's own
    topology node ids) -- this is the false-positive regression the guard must avoid."""
    import agent.kanban_execution_guard as guard

    monkeypatch.setattr(
        guard,
        "_find_active_swarms_for_session",
        lambda: [{
            "root_id": "t_root_active",
            "synthesizer_id": "t_synth_active",
            "verifier_id": "t_verify_active",
            "worker_ids": ["t_a", "t_b", "t_c", "t_d"],
        }],
    )
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-1",
                "function": {
                    "name": "kanban_create",
                    "arguments": json.dumps({"title": "unrelated task", "parents": ["t_unrelated_parent"]}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"ok": true, "task_id": "t_unrelated"}'},
    ]
    final_msg = {"role": "assistant", "content": "created the unrelated task"}
    outcome = guard.try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert final_msg["content"] == "created the unrelated task"


def test_non_matching_prose_with_kanban_create_passes_when_no_active_swarm_in_session(monkeypatch):
    """When no active swarm exists for the session, an ordinary kanban_create passes."""
    import agent.kanban_execution_guard as guard

    monkeypatch.setattr(guard, "_find_active_swarms_for_session", lambda: [])
    agent = _agent()
    messages = [
        {"role": "user", "content": NON_MATCHING_PROMPT},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-1",
                "function": {"name": "kanban_create", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "create-1", "content": '{"ok": true, "task_id": "t_legit"}'},
    ]
    final_msg = {"role": "assistant", "content": "created the legit task"}
    outcome = guard.try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert final_msg["content"] == "created the legit task"


def test_non_matching_prose_with_read_only_passes_even_with_active_swarm(monkeypatch):
    """When an active swarm exists, a status-checking turn calling read-only tools like
    kanban_show is allowed to pass and report status."""
    import agent.kanban_execution_guard as guard

    monkeypatch.setattr(
        guard,
        "_find_active_swarms_for_session",
        lambda: [{
            "root_id": "t_root_active",
            "synthesizer_id": "t_synth_active",
            "verifier_id": "t_verify_active",
            "worker_ids": ["t_a", "t_b", "t_c", "t_d"],
        }],
    )
    agent = _agent()
    messages = [
        {"role": "user", "content": "swarm 的進度如何？"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "show-1",
                "function": {"name": "kanban_show", "arguments": '{"task_id": "t_synth_active"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "show-1", "content": '{"task": {"id": "t_synth_active", "status": "running"}}'},
    ]
    final_msg = {"role": "assistant", "content": "Synthesizer 目前仍在 running 中，請稍候。"}
    outcome = guard.try_finalization(
        agent, messages, 0, final_msg["content"], final_msg, list.append
    )
    assert outcome == "pass"
    assert final_msg["content"] == "Synthesizer 目前仍在 running 中，請稍候。"


def test_find_active_swarms_for_session_end_to_end(monkeypatch, tmp_path):
    """End-to-end test verifying that _find_active_swarms_for_session correctly finds
    non-terminal swarms matching the session and ignores completed or archived swarms."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_swarm as ks
    import agent.kanban_execution_guard as guard

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        # Create an active swarm in session-A
        swarm_a = ks.create_swarm(
            conn,
            goal="Swarm A in flight",
            workers=[ks.SwarmWorkerSpec(profile="default", title="W1", body="W1 work")],
            verifier_assignee="default",
            synthesizer_assignee="default",
            origin={"origin_session_key": "session-A"},
        )
        # Create a completed swarm in session-B
        swarm_b = ks.create_swarm(
            conn,
            goal="Swarm B done",
            workers=[ks.SwarmWorkerSpec(profile="default", title="W2", body="W2 work")],
            verifier_assignee="default",
            synthesizer_assignee="default",
            origin={"origin_session_key": "session-B"},
        )
        for worker_id in swarm_b.worker_ids:
            kb.claim_task(conn, worker_id)
            kb.complete_task(conn, worker_id, summary="worker done", result="worker output")
        kb.claim_task(conn, swarm_b.verifier_id)
        kb.complete_task(conn, swarm_b.verifier_id, summary="verified", result="verified")
        kb.claim_task(conn, swarm_b.synthesizer_id)
        kb.complete_task(conn, swarm_b.synthesizer_id, summary="done", result="synth output")
    finally:
        conn.close()

    # Session A: should find active swarm A
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-A")
    active_a = guard._find_active_swarms_for_session()
    assert len(active_a) == 1
    assert active_a[0]["root_id"] == swarm_a.root_id
    assert active_a[0]["synthesizer_id"] == swarm_a.synthesizer_id

    # Session B: should find no active swarms
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-B")
    active_b = guard._find_active_swarms_for_session()
    assert len(active_b) == 0

    # Session C (unrelated): should find no active swarms
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-C")
    active_c = guard._find_active_swarms_for_session()
    assert len(active_c) == 0

