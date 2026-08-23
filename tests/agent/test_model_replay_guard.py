from types import SimpleNamespace

from agent.conversation_loop import _replay_guard_try_finalization
from agent.model_replay_guard import (
    ReplayEvidence,
    TOOL_EXECUTION_VERSION,
    action_identity_digest,
    find_baseline_less_candidate,
    find_candidate,
    normalize_replay_text,
    replay_tool_call_is_safe,
)
from agent.tool_guardrails import IDEMPOTENT_TOOL_NAMES, MUTATING_TOOL_NAMES
from hermes_state import SessionDB


def _agent(*, explicit=True):
    return SimpleNamespace(
        _current_turn_id="turn-current",
        session_id="session-1",
        platform="telegram",
        tools=[{"function": {"name": "read_file"}}],
        _tool_use_enforcement="required" if explicit else "auto",
        _execution_guidance="required" if explicit else "auto",
    )


def _evidence(agent, *, complete=True, zero=True):
    return ReplayEvidence(
        version=TOOL_EXECUTION_VERSION,
        logical_turn_key=agent._current_turn_id,
        session_id=agent.session_id,
        branch_id=agent.session_id,
        generation=1,
        complete=complete,
        zero_calls_proven=zero,
        cutoff_sequence=1,
    )


def _messages(answer="fresh timestamp: 08:03:29"):
    return [
        {"role": "user", "content": "check the board and report the timestamp"},
        {
            "role": "assistant",
            "content": "I checked it",
            "tool_calls": [{"id": "old-call", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "content": "timestamp: 08:03:29", "tool_call_id": "old-call"},
        {"role": "assistant", "content": answer},
        {"role": "user", "content": "check the board and report the timestamp"},
    ]


def test_normalization_v1_only_removes_transport_noise():
    assert normalize_replay_text("\x1b[31mA\x1b[0m\r\n") == "A"
    assert normalize_replay_text("\x1b]0;title\x07Ａ") == "Ａ"
    assert normalize_replay_text("  a  b  ") == "a  b"


def test_candidate_requires_explicit_action_signal_and_exact_answer():
    agent = _agent()
    messages = _messages()
    candidate = find_candidate(
        messages,
        4,
        "\x1b[32mfresh timestamp: 08:03:29\x1b[0m\r\n",
        agent,
        _evidence(agent),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    )
    assert candidate is not None
    assert candidate.previous_tool_names == ("read_file",)
    assert action_identity_digest({"a": 1})

    assert find_candidate(
        messages,
        4,
        "different answer",
        agent,
        _evidence(agent),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    ) is None
    inferred = find_candidate(
        messages,
        4,
        "fresh timestamp: 08:03:29",
        _agent(explicit=False),
        _evidence(_agent(explicit=False)),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    )
    assert inferred is not None
    assert inferred.recovery_safe


def test_candidate_fails_closed_for_incomplete_telemetry_or_mutation():
    agent = _agent()
    messages = _messages()
    assert find_candidate(
        messages, 4, "fresh timestamp: 08:03:29", agent,
        _evidence(agent, complete=False),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    ) is None
    messages[1]["tool_calls"][0]["function"]["name"] = "terminal"
    unsafe = find_candidate(
        messages, 4, "fresh timestamp: 08:03:29", agent,
        _evidence(agent),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    )
    assert unsafe is not None
    assert not unsafe.recovery_safe
    assert "terminal" in unsafe.unsafe_reason


def test_candidate_finds_tool_backed_answer_across_compression_duplicates():
    agent = _agent(explicit=False)
    answer = "Webboard report: timestamp 08:03:29"
    messages = [
        {"role": "user", "content": "Webboard"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "webboard-call",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command":"bash ~/.hermes/scripts/hermes_webboard_report.sh"}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "webboard-call", "content": "timestamp: 08:03:29"},
        {"role": "assistant", "content": answer},
        {"role": "session_meta", "content": "compression metadata"},
        {"role": "user", "content": "Webboard"},
        {"role": "assistant", "content": answer},
        {"role": "user", "content": "Webboard"},
    ]
    candidate = find_candidate(
        messages,
        7,
        answer,
        agent,
        _evidence(agent),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    )
    assert candidate is not None
    assert candidate.previous_tool_names == ("terminal",)
    assert candidate.recovery_safe


def test_baseline_less_webboard_requires_fresh_execution():
    agent = _agent(explicit=False)
    agent.tools = [{"function": {"name": "terminal"}}]
    stale = "Webboard report: timestamp 08:03:29 with AGENTS.md content"
    candidate = find_baseline_less_candidate(
        [{"role": "user", "content": "Webboard"}],
        0,
        stale,
        agent,
        _evidence(agent),
    )
    assert candidate is not None
    assert candidate.baseline_missing
    assert candidate.previous_answer == ""
    assert candidate.recovery_safe


def test_baseline_less_guard_does_not_classify_arbitrary_prose():
    agent = _agent(explicit=False)
    agent.tools = [{"function": {"name": "terminal"}}]
    assert find_baseline_less_candidate(
        [{"role": "user", "content": "give me a timestamp"}],
        0,
        "timestamp: 08:03:29",
        agent,
        _evidence(agent),
    ) is None


def test_webboard_read_only_receipt_is_safe_even_if_terminal_is_mutating():
    exact = {
        "id": "webboard-call",
        "function": {
            "name": "terminal",
            "arguments": '{"command":"bash ~/.hermes/scripts/hermes_webboard_report.sh"}',
        },
    }
    arbitrary = {
        "id": "other-call",
        "function": {
            "name": "terminal",
            "arguments": '{"command":"rm -f /tmp/example"}',
        },
    }
    assert replay_tool_call_is_safe(exact, frozenset(), frozenset({"terminal"}))
    assert not replay_tool_call_is_safe(arbitrary, frozenset(), frozenset({"terminal"}))


def test_conversation_loop_dispatches_baseline_less_webboard_nudge():
    class FakeDB:
        def claim_model_replay_attempt(self, **kwargs):
            return {"claim_token": kwargs["claim_token"], "state": "nudge_claimed"}

        def transition_model_replay_attempt(self, **kwargs):
            return True

    agent = _agent(explicit=False)
    agent.tools = [{"function": {"name": "terminal"}}]
    agent._session_db = FakeDB()
    agent._model_replay_guard_phase = ""
    agent._model_replay_guard_claim = None
    agent._model_replay_guard_previous_answer = ""
    agent._emit_status = lambda _message: None
    agent._session_messages = []
    messages = [{"role": "user", "content": "Webboard"}]
    final_msg = {"role": "assistant", "content": "stale 08:03:29"}

    outcome = _replay_guard_try_finalization(
        agent,
        messages,
        0,
        agent._current_turn_id,
        "stale 08:03:29",
        final_msg,
    )

    assert outcome == "nudge"
    assert agent._model_replay_guard_phase == "nudge_dispatched"
    assert messages[-1]["_model_replay_guard_synthetic"]


def test_candidate_blocks_missing_tool_receipt():
    agent = _agent(explicit=False)
    messages = [
        {"role": "user", "content": "check the board"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-without-result",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "assistant", "content": "board: unchanged"},
        {"role": "user", "content": "check the board"},
    ]
    candidate = find_candidate(
        messages,
        3,
        "board: unchanged",
        agent,
        _evidence(agent),
        idempotent_tools=IDEMPOTENT_TOOL_NAMES,
        mutating_tools=MUTATING_TOOL_NAMES,
    )
    assert candidate is not None
    assert not candidate.recovery_safe
    assert "missing_receipt:read_file" in candidate.unsafe_reason


def test_ledger_is_atomic_and_never_reopens_a_claim(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    first = db.claim_model_replay_attempt(
        logical_turn_key="turn-current",
        session_id="session-1",
        branch_id="session-1",
        action_identity="digest",
        attempt="nudge",
        claim_token="claim-1",
        invocation_id="invoke-1",
    )
    assert first["state"] == "nudge_claimed"
    assert db.claim_model_replay_attempt(
        logical_turn_key="turn-current",
        session_id="session-1",
        branch_id="session-1",
        action_identity="digest",
        attempt="nudge",
        claim_token="claim-2",
        invocation_id="invoke-2",
    ) is None
    assert db.transition_model_replay_attempt(
        logical_turn_key="turn-current",
        expected_state="nudge_claimed",
        new_state="nudge_dispatched",
        claim_token="claim-1",
    )
    assert db.transition_model_replay_attempt(
        logical_turn_key="turn-current",
        expected_state="nudge_dispatched",
        new_state="nudge_terminal_no_receipt",
        claim_token="claim-1",
    )
    fallback = db.claim_model_replay_attempt(
        logical_turn_key="turn-current",
        session_id="session-1",
        branch_id="session-1",
        action_identity="digest",
        attempt="fallback",
        claim_token="claim-fallback",
        invocation_id="invoke-fallback",
    )
    assert fallback["state"] == "fallback_claimed"
    assert db.claim_model_replay_attempt(
        logical_turn_key="turn-current",
        session_id="session-1",
        branch_id="session-1",
        action_identity="digest",
        attempt="fallback",
        claim_token="claim-fallback-2",
        invocation_id="invoke-fallback-2",
    ) is None
    assert db.transition_model_replay_attempt(
        logical_turn_key="turn-current",
        expected_state="fallback_claimed",
        new_state="fallback_dispatched",
        claim_token="claim-fallback",
    )
    assert db.record_model_replay_receipt(
        logical_turn_key="turn-current",
        session_id="session-1",
        branch_id="session-1",
        expected_state="fallback_dispatched",
        claim_token="claim-fallback",
        invocation_id="invoke-fallback",
        registry_digest="",
        tool_call_ids=["fresh-call-1"],
    )
    assert not db.record_model_replay_receipt(
        logical_turn_key="turn-current",
        session_id="session-1",
        branch_id="session-1",
        expected_state="fallback_dispatched",
        claim_token="claim-fallback",
        invocation_id="invoke-fallback",
        registry_digest="",
        tool_call_ids=["fresh-call-1"],
    )
    assert db.get_model_replay_attempt("turn-current")["state"] == "recovered"
    assert db.get_model_replay_attempt("turn-current")["fallback_count"] == 1
    db.close()
