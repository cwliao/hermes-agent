"""KANBAN-SWARM-002 regression coverage: synthesizer attempt lifecycle,
ownership fencing, and recovery invariants.

Scope note (see docs/plans/2026-08-24-kanban-swarm-result-delivery-001.md,
"consensus-final revision", and the follow-up cross-review consensus):
the swarm root's ``status`` is intentionally flipped to ``done`` at
graph-creation time (``_activate_root_inline``) and is NOT touched by this
ticket -- five independent reviewers agreed that making root wait on the
synthesizer would deadlock ``recompute_ready``'s worker-promotion
precondition. What this ticket fixes is the synthesizer role's OWN
attempt lifecycle: bounded retries, confirmed termination before retry,
no same-tick respawn, an overall wall-clock deadline, and a typed
``block_kind`` on exhaustion -- all scoped to tasks whose body contains
``role = "synthesizer"`` (``kb._is_synthesizer_role``), so every other
role's timeout/retry behaviour is provably unchanged.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

SYNTH_BODY = (
    "Completion contract (the kernel rejects a completion that omits any "
    "of these):\n  role = \"synthesizer\"\n  root_id = \"t_deadbeef\""
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_synth_task(conn, *, max_retries=1, max_runtime_seconds=300):
    tid = kb.create_task(
        conn, title="synth", body=SYNTH_BODY, assignee="worker",
        max_runtime_seconds=max_runtime_seconds, max_retries=max_retries,
    )
    kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, os.getpid())
    return tid


def _backdate_run_start(conn, tid, seconds_ago):
    old_started = int(time.time()) - seconds_ago
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (old_started, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )
    return old_started


# ---------------------------------------------------------------------------
# Scenario 1: timeout, then a successful retry
# ---------------------------------------------------------------------------


def test_synthesizer_first_timeout_then_retry_succeeds(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)  # confirmed dead
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        assert tid in timed_out

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert task.block_kind is None  # not blocked yet -- one retry left
        assert task.retry_not_before is not None
        assert task.retry_not_before > int(time.time())

        events = [e.kind for e in kb.list_events(conn, tid)]
        assert events.count("timed_out") == 1
        assert "gave_up" not in events

        # Backoff must actually be enforced: claiming immediately fails.
        assert kb.claim_task(conn, tid) is None

        # After backoff elapses, the retry can be claimed and can succeed.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET retry_not_before = ? WHERE id = ?",
                (int(time.time()) - 1, tid),
            )
        run_id = kb.claim_task(conn, tid)
        assert run_id is not None
        assert kb.complete_task(conn, tid, result="final deliverable text")
        task = kb.get_task(conn, tid)
        assert task.status == "done"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 2: repeated timeout exhausts the retry budget -> blocked
# ---------------------------------------------------------------------------


def test_synthesizer_second_timeout_exhausts_budget(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        # Simulate the retry being claimed and itself timing out.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET retry_not_before = NULL WHERE id = ?", (tid,),
            )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "synthesizer_retry_exhausted"
        assert task.consecutive_failures == 2

        events = [e.kind for e in kb.list_events(conn, tid)]
        assert events.count("timed_out") == 2
        assert events.count("gave_up") == 1

        # Terminal: no dispatcher tick may spawn another run.
        assert kb.claim_task(conn, tid) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 3: failed termination -> termination_pending, no retry
# ---------------------------------------------------------------------------


def test_synthesizer_unconfirmed_termination_blocks_without_retry(
    kanban_home, monkeypatch,
):
    # SIGKILL "landed" (no exception) but the PID is still reported alive --
    # e.g. a zombie/defunct process or a permission failure on signal
    # delivery.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        assert tid in timed_out

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "termination_pending"
        # Not a counted failure -- this isn't the retry budget's business,
        # it's an unresolved kill that needs confirmation first.
        assert task.consecutive_failures == 0

        events = [e.kind for e in kb.list_events(conn, tid)]
        assert "timed_out" in events
        assert "gave_up" not in events  # breaker never engaged

        # No automatic retry while termination is unconfirmed.
        assert kb.claim_task(conn, tid) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 4: overall 3600s deadline trips the breaker even with budget left
# ---------------------------------------------------------------------------


def test_synthesizer_overall_deadline_forces_exhaustion(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        # max_retries=2 -- matching create_swarm()'s real synthesizer default
        # (see kanban_swarm.py's DEFAULT_SYNTHESIZER_MAX_RUNTIME_SECONDS *
        # max_retries=2 == this 3600s deadline) -- plenty of budget by count
        # (only 1 of 2 failures gets counted below), but the first-ever
        # attempt started 3700s ago (> the 3600s deadline), so this single
        # timeout must trip the breaker immediately.
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=3700)

        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "synthesizer_retry_exhausted"
        # force_trip path: counted once, not compared against the 2-budget.
        assert task.consecutive_failures == 1

        events = kb.list_events(conn, tid)
        gave_up = next(e for e in events if e.kind == "gave_up")
        assert gave_up.payload.get("deadline_exceeded") is True
    finally:
        conn.close()


def test_synthesizer_deadline_checked_after_termination_polling(
    kanban_home, monkeypatch,
):
    """The overall deadline includes time spent confirming worker death."""
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        loop_start = int(time.time())
        started_at = loop_start - 3599
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (started_at, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (started_at, tid),
            )

        fake_now = [loop_start]
        monkeypatch.setattr(kb.time, "time", lambda: fake_now[0])
        monkeypatch.setattr(
            kb.time, "sleep", lambda seconds: fake_now.__setitem__(
                0, fake_now[0] + seconds,
            ),
        )
        alive_calls = [0]

        def slow_pid_alive(pid):
            alive_calls[0] += 1
            # Keep the first SIGTERM polling window occupied for its full
            # 15s, then confirm death during the SIGKILL polling window.
            return alive_calls[0] <= 31

        monkeypatch.setattr(kb, "_pid_alive", slow_pid_alive)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "synthesizer_retry_exhausted"
        gave_up = next(e for e in kb.list_events(conn, tid) if e.kind == "gave_up")
        assert gave_up.payload.get("deadline_exceeded") is True
        assert fake_now[0] >= loop_start + 15
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 5: duplicate dispatcher tick / event replay is idempotent
# ---------------------------------------------------------------------------


def test_enforce_max_runtime_duplicate_tick_is_idempotent(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)

        first = kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        assert tid in first
        # Immediately call again in the "same tick" -- the task is no
        # longer 'running' (it's 'ready' with claim_lock/worker_pid
        # cleared), so enforce_max_runtime's own WHERE clause excludes it.
        # This is the idempotency guarantee: one timeout produces exactly
        # one timed_out event and one failure-counter increment, never two.
        second = kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        assert tid not in second

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        events = [e.kind for e in kb.list_events(conn, tid)]
        assert events.count("timed_out") == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 6: late old-run completion after a timeout is rejected
# ---------------------------------------------------------------------------


def test_late_old_run_completion_rejected_after_timeout(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        old_run_id = kb.get_task(conn, tid).current_run_id
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        # The old run is closed and no longer current. A completion attempt
        # carrying the OLD run id must be rejected -- it must not resurrect
        # the old attempt or overwrite the (now-ready-for-retry) task.
        ok = kb.complete_task(
            conn, tid, result="stale result from the old attempt",
            expected_run_id=old_run_id,
        )
        assert ok is False

        task = kb.get_task(conn, tid)
        assert task.status == "ready"  # unchanged by the rejected completion
        assert task.result is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 7: mixed timeout/crash sequence still exhausts correctly
# ---------------------------------------------------------------------------


def test_synthesizer_mixed_timeout_then_crash_exhausts_budget(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        assert kb.get_task(conn, tid).status == "ready"

        # Retry claimed, then "crashes" (worker exits without completing) --
        # exercised via the same unified failure-counter path a crash
        # detector would use, confirming the budget is shared across
        # different failure kinds for the same task, not per-kind.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET retry_not_before = NULL WHERE id = ?", (tid,),
            )
        kb.claim_task(conn, tid)
        tripped = kb._record_task_failure(
            conn, tid, error="worker process exited without completing",
            outcome="crashed", release_claim=True, end_run=True,
            block_kind="synthesizer_retry_exhausted",
        )
        assert tripped is True

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "synthesizer_retry_exhausted"
        assert task.consecutive_failures == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 8: root propagation -- root stays 'done' at t=0, verifier/
# synthesizer state is what downstream consumers must actually check.
# ---------------------------------------------------------------------------


def test_root_status_unaffected_by_synthesizer_exhaustion(kanban_home, monkeypatch):
    """Consensus-validated invariant: root.status is intentionally 'done'
    from graph-creation time onward (planning-root semantics) and must NOT
    be redefined to track the synthesizer's outcome -- doing so would
    deadlock recompute_ready's worker-promotion precondition. This test
    guards against a future change accidentally coupling the two.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        root_id = kb.create_task(conn, title="swarm root", initial_status="blocked")
        from hermes_cli import kanban_swarm as ks
        assert ks._activate_root_inline(
            conn, root_id, summary="planned", metadata={},
        )
        assert kb.get_task(conn, root_id).status == "done"

        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET retry_not_before = NULL WHERE id = ?", (tid,),
            )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        synth = kb.get_task(conn, tid)
        assert synth.status == "blocked"
        assert synth.block_kind == "synthesizer_retry_exhausted"

        # Root is unrelated by construction -- still 'done', as it has been
        # since before the synthesizer ever ran. A caller that wants "is
        # the swarm's deliverable actually ready" must check the
        # synthesizer's own status, never root.status.
        assert kb.get_task(conn, root_id).status == "done"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 9: notifier semantics -- blocked/gave_up events carry a truthful,
# typed reason a notifier can render without inventing success language.
# ---------------------------------------------------------------------------


def test_gave_up_event_payload_is_notifier_truthful(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET retry_not_before = NULL WHERE id = ?", (tid,),
            )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _backdate_run_start(conn, tid, seconds_ago=400)
        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        events = kb.list_events(conn, tid)
        gave_up = next(e for e in events if e.kind == "gave_up")
        # gateway/kanban_watchers.py renders this event kind as
        # "gave up after repeated spawn failures" -- confirm the payload
        # never claims a spawn failure for what was actually a timeout, and
        # never omits the error/failure count a truthful message needs.
        assert gave_up.payload["trigger_outcome"] == "timed_out"
        assert gave_up.payload["failures"] == 2
        assert "error" in gave_up.payload and gave_up.payload["error"]

        task = kb.get_task(conn, tid)
        assert task.result is None or task.result == ""  # never a stale/fabricated result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 10: output-contract rejection is retried once, not treated as
# an immediate permanent failure.
# ---------------------------------------------------------------------------


def test_output_contract_rejection_is_retryable(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        tid = _make_synth_task(conn, max_retries=2)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET body = ? WHERE id = ?",
                (
                    SYNTH_BODY
                    + '\n[swarm:contract] '
                    + '{"role": "synthesizer", "root_id": "t_deadbeef"}',
                    tid,
                ),
            )
        # A status-only / contract-invalid completion attempt is rejected
        # by validate_completion (kanban_swarm.py) at the complete_task
        # boundary -- it raises rather than silently completing. This is
        # pre-existing behaviour (KANBAN-SWARM-RESULT-DELIVERY-001); this
        # test only confirms the task remains claimable/retryable
        # afterward rather than being wedged.
        with pytest.raises(ValueError):
            kb.complete_task(
                conn, tid,
                result="Work has been processed and an artifact was prepared.",
            )
        task = kb.get_task(conn, tid)
        assert task.status == "running"  # rejected completion, not silently accepted
        assert task.result is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 11: non-synthesizer roles are completely unaffected (grace
# period, backoff, and deadline are all synthesizer-scoped).
# ---------------------------------------------------------------------------


def test_non_synthesizer_role_keeps_legacy_timeout_behavior(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="worker", body="role = \"worker\"", assignee="w",
            max_runtime_seconds=300, max_retries=2,
        )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _backdate_run_start(conn, tid, seconds_ago=400)

        kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        # No synthesizer-only backoff applied to other roles.
        assert task.retry_not_before is None
        assert kb.claim_task(conn, tid) is not None
    finally:
        conn.close()
