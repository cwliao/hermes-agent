"""Pure tool-call guardrail primitive tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
    format_tool_outcome_footer,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)


def test_default_config_is_soft_warning_only_with_hard_stop_disabled():
    cfg = ToolCallGuardrailConfig()

    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False
    assert cfg.non_interactive_hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_gateway_platform_defaults_to_hard_stop_without_changing_interactive_defaults():
    interactive_configs = [
        ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        for platform in ("cli", "tui", "desktop", "acp")
    ]
    telegram_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="telegram")
    cron_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="cron")

    assert all(cfg.hard_stop_enabled is False for cfg in interactive_configs)
    assert telegram_cfg.hard_stop_enabled is True
    assert cron_cfg.hard_stop_enabled is True


def test_non_interactive_hard_stop_can_be_disabled_explicitly():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"non_interactive_hard_stop_enabled": False},
        platform="telegram",
    )

    assert cfg.hard_stop_enabled is False
    assert cfg.non_interactive_hard_stop_enabled is False


def test_unattended_explicit_soft_value_requires_opt_in():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": False}, default_hard_stop_enabled=True,
    )
    assert cfg.hard_stop_enabled is True
    assert "unattended_soft_mode=true" in cfg.configuration_warning
    soft_cfg = ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": False, "unattended_soft_mode": True},
        default_hard_stop_enabled=True,
    )
    assert soft_cfg.hard_stop_enabled is False
    assert soft_cfg.configuration_warning == ""


def test_auto_hard_stop_follows_runtime_default():
    assert ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": "auto"}, default_hard_stop_enabled=True,
    ).hard_stop_enabled is True
    assert ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_enabled": "auto"}, default_hard_stop_enabled=False,
    ).hard_stop_enabled is False


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2














def test_skill_read_tools_are_idempotent_and_block_repeated_identical_success_output():
    cases = [
        (
            "skill_view",
            {"name": "gui-agent-ml-operations"},
            '{"success":true,"name":"gui-agent-ml-operations","content":"same"}',
        ),
        (
            "skills_list",
            {"category": "mlops"},
            '{"success":true,"skills":[{"name":"gui-agent-ml-operations"}]}',
        ),
    ]

    for tool_name, args, result in cases:
        controller = ToolCallGuardrailController(
            ToolCallGuardrailConfig(
                hard_stop_enabled=True,
                no_progress_warn_after=2,
                no_progress_block_after=2,
            )
        )

        assert controller.before_call(tool_name, args).action == "allow"
        assert controller.after_call(tool_name, args, result, failed=False).action == "allow"
        assert controller.before_call(tool_name, args).action == "allow"
        warn = controller.after_call(tool_name, args, result, failed=False)
        assert warn.action == "warn"
        assert warn.code == "idempotent_no_progress_warning"

        blocked = controller.before_call(tool_name, args)
        assert blocked.action == "block"
        assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_identical_call_streak_halts_any_tool_when_hard_stop_enabled():
    # #89069 / #100849 bundle: a model replaying the same SUCCESSFUL
    # terminal/skill_view call with a byte-identical result is not covered by
    # the idempotent_tools no-progress block. The consecutive-identical
    # streak (observe_call) is tool-agnostic; under hard_stop it must halt.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"command": "hermes config get memory.provider"}
    for i in range(1, 5):
        controller.after_call("terminal", args, "local\n", failed=False)
        controller.observe_call("terminal", args, "local\n", failed=False)
        assert controller.halt_decision is None, f"halted early at {i}"

    controller.after_call("terminal", args, "local\n", failed=False)
    controller.observe_call("terminal", args, "local\n", failed=False)
    halt = controller.halt_decision
    assert halt is not None and halt.should_halt
    assert halt.code == "identical_call_streak_halt"
    assert halt.tool_name == "terminal" and halt.count == 5


def test_identical_call_streak_never_halts_when_hard_stop_disabled_or_for_pollers():
    soft = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False, no_progress_block_after=2)
    )
    for _ in range(6):
        soft.observe_call("terminal", {"command": "ls"}, "a\nb\n", failed=False)
    assert soft.halt_decision is None  # notice-only in interactive sessions

    hard = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    )
    for _ in range(6):
        hard.observe_call("process_manage", {"action": "poll", "session_id": "p1"}, "running", failed=False)
    assert hard.halt_decision is None  # an unchanged poll is legitimate progress

    # A changed result resets the streak.
    for i in range(6):
        hard.observe_call("terminal", {"command": "date"}, f"t{i}", failed=False)
    assert hard.halt_decision is None






# ── Per-turn runaway-loop caps (Claude Code v2.1.212, Week 29) ──────────────

from agent.tool_guardrails import LoopCapConfig  # noqa: E402






def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == 50
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == 50


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True












# ── Legitimate flows must survive hard stops (Teknium, Sep 2026) ────────────
# Hard stops default ON for unattended platforms. These pin the flows that
# must NEVER be cut off there: edit -> re-run loops, diagnostic sweeps of
# distinct red commands, and browser retry-after-action — while the pure
# replay (same call, nothing changed between attempts) is still stopped.

_HARD = lambda: ToolCallGuardrailController(  # noqa: E731
    ToolCallGuardrailConfig(hard_stop_enabled=True)
)
_PYTEST = {"command": "pytest tests/test_x.py -q"}
_RED = '{"output": "1 failed", "exit_code": 1}'


def _run_red(c, args=_PYTEST):
    assert c.before_call("terminal", args).allows_execution
    return c.after_call("terminal", args, _RED, failed=True)


def test_fix_retest_loop_is_never_hard_stopped():
    c = _HARD()
    for i in range(12):
        d = _run_red(c)
        assert not d.should_halt, f"halted on red run {i + 1}"
        # the model edits between runs — a landed mutation is progress
        c.after_call("patch", {"path": "x.py", "old_string": "a", "new_string": f"b{i}"},
                     '{"success": true, "diff": "..."}', failed=False)
    assert c.halt_decision is None
    assert c.before_call("terminal", _PYTEST).allows_execution


def test_pure_replay_with_no_intervening_change_is_still_blocked():
    c = _HARD()
    for _ in range(5):
        _run_red(c)
    d = c.before_call("terminal", _PYTEST)
    assert d.action == "block" and d.code == "repeated_exact_failure_block"


def test_intervening_mutation_resets_the_replay_streak_only_once():
    # 4 reds, one edit, then 4 reds with NO edit: the second run of 4 is a
    # fresh streak, and the 5th unchanged retry after it is blocked.
    c = _HARD()
    for _ in range(4):
        _run_red(c)
    c.after_call("write_file", {"path": "x.py", "content": "y"}, '{"bytes_written": 1}', failed=False)
    for _ in range(5):
        assert c.before_call("terminal", _PYTEST).allows_execution
        c.after_call("terminal", _PYTEST, _RED, failed=True)
    assert c.before_call("terminal", _PYTEST).action == "block"


def test_distinct_failing_terminal_commands_warn_but_never_halt():
    # A diagnostic sweep: grep with no matches, missing binaries, red builds.
    c = _HARD()
    for i in range(12):
        args = {"command": f"grep -q needle{i} haystack.txt"}
        d = c.after_call("terminal", args, _RED, failed=True)
        assert not d.should_halt, f"same_tool halt on distinct command #{i + 1}"
    assert c.halt_decision is None
    # ...while a non-tolerant tool failing 8 distinct ways still halts.
    c2 = _HARD()
    last = None
    for i in range(8):
        last = c2.after_call("send_message", {"to": f"u{i}"}, '{"error": "no route"}', failed=True)
    assert last.should_halt and last.code == "same_tool_failure_halt"


def test_browser_retry_after_action_is_not_a_replay():
    c = _HARD()
    nav = {"url": "https://example.test/app"}
    for _ in range(8):
        assert c.before_call("browser_navigate", nav).allows_execution
        c.after_call("browser_navigate", nav, '{"error": "timeout"}', failed=True)
        c.after_call("browser_click", {"selector": "#retry"}, '{"ok": true}', failed=False)
    assert c.halt_decision is None


def test_supervised_task_platforms_keep_warning_only_default():
    for platform in ("subagent", "api_server", "cli"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is False, platform
    for platform in ("telegram", "discord", "cron", "kanban"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is True, platform


def test_cross_turn_deterministic_blocker_is_target_scoped_and_session_bounded():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, cross_turn_failure_halt_after=2,
        same_tool_failure_halt_after=99,
    ))
    first_args = {"path": "/tmp/blocked.txt", "content": "secret-one"}
    second_args = {"path": "/tmp/blocked.txt", "content": "secret-two"}
    failure = '{"error":"No such file or directory"}'
    controller.after_call("write_file", first_args, failure, failed=True)
    controller.reset_for_turn()
    controller.after_call("write_file", second_args, failure, failed=True)
    blocked = controller.before_call("write_file", second_args)
    assert blocked.action == "block"
    assert blocked.code == "cross_turn_deterministic_blocker"
    assert "secret-one" not in json.dumps(blocked.to_metadata())
    assert "secret-two" not in json.dumps(blocked.to_metadata())
    assert controller.before_call("write_file", {"path": "/tmp/other.txt"}).action == "allow"
    controller.reset_for_session()
    assert controller.before_call("write_file", second_args).action == "allow"


def test_cross_turn_success_resets_target_blocker_progress():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, cross_turn_failure_halt_after=2,
        same_tool_failure_halt_after=99,
    ))
    args = {"path": "/tmp/recover.txt"}
    controller.after_call("write_file", args, '{"error":"Permission denied"}', failed=True)
    controller.reset_for_turn()
    controller.after_call("write_file", args, '{"bytes_written":1}', failed=False)
    controller.reset_for_turn()
    controller.after_call("write_file", args, '{"error":"Permission denied"}', failed=True)
    assert controller.before_call("write_file", args).action == "allow"


def test_cross_turn_ledger_serializes_concurrent_failures():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, cross_turn_failure_halt_after=4,
        same_tool_failure_halt_after=99,
    ))
    args = {"path": "/tmp/concurrent.txt"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            lambda _index: controller.after_call(
                "write_file", args, '{"error":"Permission denied"}', failed=True),
            range(4),
        ))
    assert controller.before_call("write_file", args).code == "cross_turn_deterministic_blocker"


def test_cross_turn_ledger_expires_entries():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, cross_turn_failure_halt_after=2,
        cross_turn_ttl_seconds=10, same_tool_failure_halt_after=99,
    ))
    args = {"path": "/tmp/expired.txt"}
    controller.after_call("write_file", args, '{"error":"Permission denied"}', failed=True)
    target_key = next(iter(controller._cross_turn_failures))
    controller._cross_turn_failures[target_key] = (2, 0.0, "permission")
    with patch("agent.tool_guardrails.time.monotonic", return_value=11.0):
        assert controller.before_call("write_file", args).action == "allow"


def test_cross_turn_ledger_evicts_oldest_entry_when_bounded():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, cross_turn_failure_halt_after=2,
        cross_turn_ledger_max_entries=2, same_tool_failure_halt_after=99,
    ))
    failure = '{"error":"Permission denied"}'
    paths = [{"path": f"/tmp/{name}.txt"} for name in ("first", "second", "third")]
    for args in paths[:2]:
        controller.after_call("write_file", args, failure, failed=True)
        controller.after_call("write_file", args, failure, failed=True)
    controller.after_call("write_file", paths[2], failure, failed=True)
    assert len(controller._cross_turn_failures) == 2
    assert controller.before_call("write_file", paths[0]).action == "allow"


# --- FABRICATION-REMEDY-001: turn tool-outcome tally and footer ---------------
#
# The 2026-08-19 incident: two tool calls, both failed, and the response
# asserted a detailed success -- four lanes, per-lane runtimes to 10ms, a
# verifier pass -- that no store recorded. The existing counters measure
# repetition and correctly stayed silent at two failures. These cover the
# property that went unguarded: what this turn's calls actually did.


def _controller(*, unattended=False):
    return ToolCallGuardrailController(
        ToolCallGuardrailConfig(unattended=unattended)
    )


def _record(controller, tool_name, *, failed):
    controller.after_call(tool_name, {"command": "x"}, "result", failed=failed)


def test_all_calls_failed_is_reported_on_every_surface():
    """A1. The incident shape, and the reason this is not gated on
    `unattended`: if no tool call succeeded, nothing the response says about
    their results can be true, wherever it is read."""
    for unattended in (True, False):
        c = _controller(unattended=unattended)
        _record(c, "terminal", failed=True)
        _record(c, "terminal", failed=True)
        outcome = c.turn_tool_outcome()
        assert outcome.all_failed
        footer = format_tool_outcome_footer(outcome, unattended=unattended)
        assert "All 2 tool calls this turn failed" in footer
        assert "terminal" in footer


def test_partial_failure_is_reported_only_where_nobody_watched():
    """B. An interactive user saw the calls scroll past; a Telegram reader
    saw only the prose."""
    for unattended, expected in ((True, True), (False, False)):
        c = _controller(unattended=unattended)
        _record(c, "terminal", failed=True)
        _record(c, "read_file", failed=False)
        _record(c, "read_file", failed=False)
        footer = format_tool_outcome_footer(
            c.turn_tool_outcome(), unattended=unattended
        )
        assert bool(footer) is expected
        if expected:
            assert "1 of 3 tool calls" in footer


def test_no_failure_and_no_calls_are_both_silent():
    c = _controller(unattended=True)
    assert format_tool_outcome_footer(c.turn_tool_outcome(), unattended=True) == ""
    _record(c, "read_file", failed=False)
    assert format_tool_outcome_footer(c.turn_tool_outcome(), unattended=True) == ""


def test_a_single_failed_call_is_all_failed():
    """Boundary: one call, failed. `all_failed` must not require a plural."""
    c = _controller()
    _record(c, "terminal", failed=True)
    footer = format_tool_outcome_footer(c.turn_tool_outcome(), unattended=False)
    assert "All 1 tool call this turn failed" in footer


def test_failed_tools_are_deduplicated_in_order():
    c = _controller(unattended=True)
    _record(c, "terminal", failed=True)
    _record(c, "read_file", failed=True)
    _record(c, "terminal", failed=True)
    assert c.turn_tool_outcome().failed_tools == ("terminal", "read_file")


def test_tally_resets_between_turns():
    """Without this the footer would accuse a clean turn of the previous
    turn's failures."""
    c = _controller(unattended=True)
    _record(c, "terminal", failed=True)
    c.reset_for_turn()
    _record(c, "read_file", failed=False)
    outcome = c.turn_tool_outcome()
    assert (outcome.attempted, outcome.failed) == (1, 0)
    assert format_tool_outcome_footer(outcome, unattended=True) == ""


def test_tally_is_independent_of_the_repetition_thresholds():
    """The incident's two failures are below every warn/halt threshold. The
    footer must not inherit that gating -- that is the whole point."""
    c = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            unattended=True,
            warnings_enabled=False,
            hard_stop_enabled=False,
        )
    )
    _record(c, "terminal", failed=True)
    _record(c, "terminal", failed=True)
    assert c.halt_decision is None
    footer = format_tool_outcome_footer(c.turn_tool_outcome(), unattended=True)
    assert "All 2 tool calls this turn failed" in footer


def test_one_success_defeats_a1_on_an_attended_surface():
    """Pins the acknowledged gap rather than leaving it to the docstring.

    Three failures and one success: `all_failed` is false, so A1 does not
    fire, and B is unattended-only -- an attended turn gets nothing. If this
    ever starts producing a footer, the scoping changed and the docstring is
    stale.
    """
    c = _controller(unattended=False)
    _record(c, "terminal", failed=True)
    _record(c, "terminal", failed=True)
    _record(c, "terminal", failed=True)
    _record(c, "read_file", failed=False)
    outcome = c.turn_tool_outcome()
    assert not outcome.all_failed
    assert format_tool_outcome_footer(outcome, unattended=False) == ""
    # The same turn on an unattended surface is covered by B.
    assert "3 of 4 tool calls" in format_tool_outcome_footer(outcome, unattended=True)
