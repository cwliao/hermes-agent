import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_swarm import (
    DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS,
    DEFAULT_SYNTHESIZER_MAX_RUNTIME_SECONDS,
    DEFAULT_WORKER_MAX_RUNTIME_SECONDS,
    MULTI_AGENT_LANE_IDS,
    SwarmWorkerSpec,
    _completion_call_example,
    _default_worker_max_runtime_seconds,
    _swarm_context,
    create_swarm,
    excuse_blocked_workers_below_quorum,
    extract_contract,
    latest_blackboard,
    parse_worker_arg,
    post_blackboard_update,
    validate_completion,
)
import pytest


def test_swarm_context_names_kanban_comment_and_rules_out_observed_failure_modes():
    """SWARM-CLAUDE-GROK-LANE-TIMEOUT-RECURRENCE-001 retest (2026-08-21):
    two independent lanes each burned most of their runtime budget trying
    to post a result via hand-written SQL (bash quoting bug) or
    execute_code (blocked outright), instead of the kanban_comment tool
    they already had. Lock the fix in: the swarm context must name the
    tool explicitly and rule out both observed failure modes."""
    context = _swarm_context("t_root123", "test goal")
    assert "kanban_comment" in context
    assert 'task_id="t_root123"' in context
    assert "execute_code" in context
    assert "sqlite3" in context


def test_create_swarm_builds_parallel_workers_verifier_and_synthesizer(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Map the target market and produce a decision memo.",
            workers=[
                SwarmWorkerSpec(profile="researcher-a", title="Market scan", body="Find competitors"),
                SwarmWorkerSpec(profile="researcher-b", title="Customer scan", body="Find customer pains"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            tenant="intel",
            created_by="orchestrator",
        )

        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root is not None
        assert all(task is not None for task in workers)
        workers = [task for task in workers if task is not None]
        assert verifier is not None
        assert synthesizer is not None
        assert root.status == "done"
        assert root.assignee == "orchestrator"
        assert [task.status for task in workers] == ["ready", "ready"]
        assert [task.assignee for task in workers] == ["researcher-a", "researcher-b"]
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"
        assert set(kb.parent_ids(conn, created.verifier_id)) == set(created.worker_ids)
        assert kb.parent_ids(conn, created.synthesizer_id) == [created.verifier_id]
        assert all(created.root_id in (task.body or "") for task in workers)
    finally:
        conn.close()


def test_create_swarm_verifier_does_not_carry_requesting_code_review(tmp_path):
    """Verifier tasks must not inherit requesting-code-review.

    That skill assumes git/terminal/file tools the kanban verifier worker
    does not have; attaching it causes unknown-tool exhaustion and a
    protocol-violation stall. Cover both create_swarm() entry shapes:
    non-lane (plain worker specs) and lane-bound (lane_id on every worker).
    """
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Map the target market and produce a decision memo.",
            workers=[
                SwarmWorkerSpec(profile="researcher-a", title="Market scan", body="Find competitors"),
                SwarmWorkerSpec(profile="researcher-b", title="Customer scan", body="Find customer pains"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            tenant="intel",
            created_by="orchestrator",
        )
        verifier = kb.get_task(conn, created.verifier_id)
        assert verifier is not None
        assert "requesting-code-review" not in (verifier.skills or [])

        created_lane = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        lane_verifier = kb.get_task(conn, created_lane.verifier_id)
        assert lane_verifier is not None
        assert "requesting-code-review" not in (lane_verifier.skills or [])
        assert extract_contract(lane_verifier.body)["role"] == "verifier"
    finally:
        conn.close()


def test_create_swarm_graph_is_atomic_and_rolls_back_partial_build(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "kanban.db"
    writer = kbc.connect(db_path)
    reader = kbc.connect(db_path)
    original_create = kb.create_task
    original_complete = kb.complete_task
    calls = 0

    def observed_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        task_id = original_create(*args, **kwargs)
        if calls == 1:
            # Releasing the nested create_task savepoint must not expose the
            # root before the whole graph's outer transaction commits.
            visible = reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            assert visible == 0
        if calls == 3:
            raise RuntimeError("synthetic graph-construction failure")
        return task_id

    monkeypatch.setattr(kb, "create_task", observed_create)
    try:
        with pytest.raises(RuntimeError, match="synthetic graph-construction failure"):
            create_swarm(
                writer,
                goal="Build atomically",
                workers=[
                    SwarmWorkerSpec(profile="worker-a", title="A", body="A"),
                    SwarmWorkerSpec(profile="worker-b", title="B", body="B"),
                ],
                verifier_assignee="reviewer",
                synthesizer_assignee="writer",
            )
        assert writer.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        monkeypatch.setattr(kb, "create_task", original_create)
        import hermes_cli.kanban_swarm as ks

        original_activate = ks._activate_root_inline
        monkeypatch.setattr(
            ks, "_activate_root_inline", lambda *args, **kwargs: False
        )
        with pytest.raises(RuntimeError, match="could not activate"):
            create_swarm(
                writer,
                goal="Fail activation atomically",
                workers=[
                    SwarmWorkerSpec(profile="worker-a", title="A", body="A"),
                ],
                verifier_assignee="reviewer",
                synthesizer_assignee="writer",
            )
        assert writer.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        hooks: list[tuple[str, bool]] = []
        monkeypatch.setattr(ks, "_activate_root_inline", original_activate)
        monkeypatch.setattr(
            kb,
            "_fire_kanban_lifecycle_hook",
            lambda event, *_args, **_kwargs: hooks.append(
                (event, writer.in_transaction)
            ),
        )
        create_swarm(
            writer,
            goal="Commit before lifecycle hook",
            workers=[SwarmWorkerSpec(profile="worker-a", title="A", body="A")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )
        assert hooks == [("kanban_task_completed", False)]
    finally:
        reader.close()
        writer.close()


def test_plain_write_txn_nesting_raises_and_allow_nested_composes(tmp_path):
    """B1 regression: nesting is explicit opt-in, never silent.

    Plain ``write_txn`` inside an open transaction must raise loudly (the
    historical invariant). ``allow_nested=True`` composes via a savepoint,
    and an outer rollback discards the inner work without any post-commit
    side effects having fired (the workspace directory survives).
    """
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        workspace = tmp_path / "scratch-ws"
        workspace.mkdir()
        tid = kb.create_task(conn, title="ws task", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(workspace), tid),
            )

        # 1) Plain nesting raises loudly.
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            with kb.write_txn(conn):
                with kb.write_txn(conn):
                    pass
        assert not conn.in_transaction

        # 2) allow_nested composes; outer rollback discards inner work
        #    and no side effects (workspace cleanup) fired meanwhile.
        with pytest.raises(RuntimeError, match="outer failure"):
            with kb.write_txn(conn):
                with kb.write_txn(conn, allow_nested=True):
                    conn.execute(
                        "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,)
                    )
                    kb._append_event(conn, tid, "completed", {"result_len": 0})
                # Inner savepoint released, but the outer txn now fails.
                raise RuntimeError("outer failure")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"  # inner 'done' flip was discarded
        assert not any(
            e.kind == "completed" for e in kb.list_events(conn, tid)
        )
        assert workspace.is_dir()  # no _cleanup_workspace side effect fired
    finally:
        conn.close()


def test_swarm_blackboard_merges_structured_updates(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Collect evidence.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Evidence", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        post_blackboard_update(
            conn,
            created.root_id,
            author="researcher",
            key="sources",
            value=["https://example.com/a"],
        )
        post_blackboard_update(
            conn,
            created.root_id,
            author="reviewer",
            key="risks",
            value={"missing_primary_source": True},
        )

        board = latest_blackboard(conn, created.root_id)
        assert board["sources"] == ["https://example.com/a"]
        assert board["risks"] == {"missing_primary_source": True}
        assert board["_authors"]["sources"] == "researcher"
    finally:
        conn.close()


def test_swarm_verifier_and_synthesis_are_dependency_gated(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Research two branches then verify and synthesize.",
            workers=[
                SwarmWorkerSpec(profile="a", title="Branch A", body="A"),
                SwarmWorkerSpec(profile="b", title="Branch B", body="B"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        kb.complete_task(
            conn,
            created.worker_ids[0],
            summary="A done",
            metadata={"confidence": 0.8},
        )
        kb.recompute_ready(conn)
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier is not None
        assert synthesizer is not None
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"

        kb.complete_task(conn, created.worker_ids[1], summary="B done")
        kb.recompute_ready(conn)
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier is not None
        assert synthesizer is not None
        assert verifier.status == "ready"
        assert synthesizer.status == "todo"

        kb.complete_task(
            conn,
            created.verifier_id,
            summary="Verified both branches",
            metadata={"gate": "pass"},
        )
        kb.recompute_ready(conn)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert synthesizer is not None
        assert synthesizer.status == "ready"
    finally:
        conn.close()


def test_lane_bound_swarm_persists_contracts_goal_budget_and_runtime(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root.goal_mode is True
        assert root.goal_max_turns == 5
        assert all(task.goal_mode is True for task in workers)
        assert all(task.goal_max_turns == 5 for task in workers)
        # SWARM-CLAUDE-GROK-LANE-TIMEOUT-RECURRENCE-001: lane-aware default when
        # worker_max_runtime_seconds is left unset -- native_hermes gets
        # DEFAULT_WORKER_MAX_RUNTIME_SECONDS; every external-CLI lane gets
        # DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS instead.
        by_lane = {
            extract_contract(task.body)["expected_lane_id"]: task for task in workers
        }
        assert by_lane["native_hermes"].max_runtime_seconds == DEFAULT_WORKER_MAX_RUNTIME_SECONDS
        for lane in ("claude", "grok", "agy"):
            assert by_lane[lane].max_runtime_seconds == DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS
        assert [extract_contract(task.body)["expected_lane_id"] for task in workers] == list(MULTI_AGENT_LANE_IDS)
        assert extract_contract(verifier.body)["expected_lane_count"] == 4
        assert extract_contract(synthesizer.body)["verifier_id"] == created.verifier_id
    finally:
        conn.close()


def test_default_worker_max_runtime_seconds_is_lane_aware():
    assert _default_worker_max_runtime_seconds("native_hermes") == DEFAULT_WORKER_MAX_RUNTIME_SECONDS
    assert _default_worker_max_runtime_seconds(None) == DEFAULT_WORKER_MAX_RUNTIME_SECONDS
    for lane in ("claude", "grok", "agy"):
        assert (
            _default_worker_max_runtime_seconds(lane)
            == DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS
        )
    assert DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS == DEFAULT_WORKER_MAX_RUNTIME_SECONDS


def test_explicit_worker_max_runtime_seconds_applies_uniformly_across_lanes(tmp_path):
    """An explicit swarm-wide override still wins over the lane-aware default,
    for every lane including native_hermes -- preserves the pre-existing
    behavior for callers that already pass this explicitly."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
            worker_max_runtime_seconds=300,
        )
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        assert all(task.max_runtime_seconds == 300 for task in workers)
    finally:
        conn.close()


def test_per_worker_max_runtime_seconds_still_beats_swarm_and_lane_defaults(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                    max_runtime_seconds=42 if lane == "claude" else None,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        by_lane = {
            extract_contract(kb.get_task(conn, tid).body)["expected_lane_id"]: kb.get_task(conn, tid)
            for tid in created.worker_ids
        }
        assert by_lane["claude"].max_runtime_seconds == 42
        assert by_lane["native_hermes"].max_runtime_seconds == DEFAULT_WORKER_MAX_RUNTIME_SECONDS
        assert by_lane["grok"].max_runtime_seconds == DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SWARM-PARTIAL-QUORUM-001: one permanently-blocked worker must not deadlock
# the verifier forever when the caller opts into a quorum.
# ---------------------------------------------------------------------------

def _make_quorum_swarm(conn, worker_quorum):
    return create_swarm(
        conn,
        goal="Four independent lanes each produce one joke.",
        workers=[
            SwarmWorkerSpec(
                profile=lane, title=f"{lane} joke", body="Return one joke.",
                skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
            )
            for lane in MULTI_AGENT_LANE_IDS
        ],
        verifier_assignee="verifier",
        synthesizer_assignee="synthesizer",
        tenant="quorum-test",
        worker_quorum=worker_quorum,
    )


def test_worker_quorum_out_of_range_rejected(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="worker_quorum must be between 1 and 4"):
            _make_quorum_swarm(conn, worker_quorum=5)
        with pytest.raises(ValueError, match="worker_quorum must be between 1 and 4"):
            _make_quorum_swarm(conn, worker_quorum=0)
    finally:
        conn.close()


def test_worker_quorum_requires_lane_mode(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="worker_quorum is only meaningful for lane-bound swarms"):
            create_swarm(
                conn,
                goal="Plain non-lane swarm.",
                workers=[SwarmWorkerSpec(profile="a", title="A", body="do it")],
                verifier_assignee="verifier",
                synthesizer_assignee="synthesizer",
                worker_quorum=1,
            )
    finally:
        conn.close()


def test_worker_quorum_sets_verifier_expected_lane_count_and_stores_topology(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=3)
        verifier = kb.get_task(conn, created.verifier_id)
        contract = extract_contract(verifier.body)
        assert contract["expected_lane_count"] == 3
        assert "quorum of 3 out of 4" in verifier.body
        topology = latest_blackboard(conn, created.root_id).get("topology")
        assert topology["worker_quorum"] == 3
    finally:
        conn.close()


def test_worker_quorum_none_keeps_full_lane_count(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=None)
        verifier = kb.get_task(conn, created.verifier_id)
        contract = extract_contract(verifier.body)
        assert contract["expected_lane_count"] == 4
        assert "quorum" not in verifier.body
        topology = latest_blackboard(conn, created.root_id).get("topology")
        assert topology["worker_quorum"] is None
    finally:
        conn.close()


def test_excuse_blocked_workers_below_quorum_archives_once_satisfied(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=3)
        by_lane = {
            extract_contract(kb.get_task(conn, tid).body)["expected_lane_id"]: tid
            for tid in created.worker_ids
        }
        # Three siblings genuinely complete.
        for lane in ("native_hermes", "claude", "grok"):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (by_lane[lane],))
        # The fourth exhausted its retries and the dispatcher gave up on it.
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (by_lane["agy"],))
        conn.commit()

        excused = excuse_blocked_workers_below_quorum(conn)

        assert excused == 1
        assert kb.get_task(conn, by_lane["agy"]).status == "archived"
        # Excusing must unblock the verifier via the ordinary
        # "every parent done or archived" promotion rule.
        assert kb.get_task(conn, created.verifier_id).status == "ready"
    finally:
        conn.close()


def test_excuse_blocked_workers_below_quorum_noop_below_quorum(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=3)
        by_lane = {
            extract_contract(kb.get_task(conn, tid).body)["expected_lane_id"]: tid
            for tid in created.worker_ids
        }
        # Only two siblings done -- below the quorum of 3.
        for lane in ("native_hermes", "claude"):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (by_lane[lane],))
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (by_lane["agy"],))
        conn.commit()

        excused = excuse_blocked_workers_below_quorum(conn)

        assert excused == 0
        assert kb.get_task(conn, by_lane["agy"]).status == "blocked"
        assert kb.get_task(conn, created.verifier_id).status == "todo"
    finally:
        conn.close()


def test_excuse_blocked_workers_below_quorum_noop_without_quorum_configured(tmp_path):
    """Swarms created without worker_quorum keep the strict all-workers
    behavior unchanged -- a blocked worker is never excused."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=None)
        by_lane = {
            extract_contract(kb.get_task(conn, tid).body)["expected_lane_id"]: tid
            for tid in created.worker_ids
        }
        for lane in ("native_hermes", "claude", "grok"):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (by_lane[lane],))
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (by_lane["agy"],))
        conn.commit()

        excused = excuse_blocked_workers_below_quorum(conn)

        assert excused == 0
        assert kb.get_task(conn, by_lane["agy"]).status == "blocked"
        assert kb.get_task(conn, created.verifier_id).status == "todo"
    finally:
        conn.close()


def test_excuse_blocked_workers_below_quorum_ignores_unrelated_blocked_tasks(tmp_path):
    """A blocked task with no swarm worker contract at all (an ordinary
    task that happens to also be blocked) must never be touched."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        plain_id = kb.create_task(conn, title="unrelated", body="just a task", assignee="someone")
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (plain_id,))
        conn.commit()

        excused = excuse_blocked_workers_below_quorum(conn)

        assert excused == 0
        assert kb.get_task(conn, plain_id).status == "blocked"
    finally:
        conn.close()


def test_excuse_blocked_workers_below_quorum_never_touches_sticky_block(tmp_path):
    """A worker/operator-initiated kanban_block (sticky -- caught by
    independent cross-review before merge) must never be auto-excused,
    even when siblings already satisfy the quorum. Only the dispatcher's
    own circuit-breaker gave_up path is eligible."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=3)
        by_lane = {
            extract_contract(kb.get_task(conn, tid).body)["expected_lane_id"]: tid
            for tid in created.worker_ids
        }
        for lane in ("native_hermes", "claude", "grok"):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (by_lane[lane],))
        # agy is still running and a worker deliberately asks for human
        # review -- block_task requires running/ready, so set that first.
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (by_lane["agy"],))
        conn.commit()
        assert kb.block_task(conn, by_lane["agy"], reason="need human input", kind="needs_input")
        assert kb.get_task(conn, by_lane["agy"]).status == "blocked"

        excused = excuse_blocked_workers_below_quorum(conn)

        assert excused == 0
        assert kb.get_task(conn, by_lane["agy"]).status == "blocked"
        assert kb.get_task(conn, created.verifier_id).status == "todo"
    finally:
        conn.close()


def test_excuse_blocked_workers_below_quorum_skips_task_no_longer_blocked(tmp_path):
    """Defense-in-depth: if the task transitioned away from 'blocked'
    between the initial scan and the archive attempt (e.g. a concurrent
    kanban unblock), it must not be archived anyway."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = _make_quorum_swarm(conn, worker_quorum=3)
        by_lane = {
            extract_contract(kb.get_task(conn, tid).body)["expected_lane_id"]: tid
            for tid in created.worker_ids
        }
        for lane in ("native_hermes", "claude", "grok"):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (by_lane[lane],))
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (by_lane["agy"],))
        conn.commit()

        # Simulate a concurrent kanban unblock landing between the initial
        # scan and this function's own pre-archive re-check by making the
        # (already-called-per-row, always-False-here) sticky-block lookup
        # revive the task as a side effect -- it runs earlier in the same
        # iteration than the re-check being tested.
        import hermes_cli.kanban_swarm as ks_module
        original = ks_module.kb._has_sticky_block

        def _sticky_check_that_revives(conn, task_id):
            if task_id == by_lane["agy"]:
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
            return original(conn, task_id)

        ks_module.kb._has_sticky_block = _sticky_check_that_revives
        try:
            excused = excuse_blocked_workers_below_quorum(conn)
        finally:
            ks_module.kb._has_sticky_block = original

        assert excused == 0
        assert kb.get_task(conn, by_lane["agy"]).status == "ready"
    finally:
        conn.close()


def test_lane_bound_swarm_allows_two_of_three_external_lanes(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask native Hermes plus two available external agents for one joke.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in ("native_hermes", "claude", "grok")
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        assert [extract_contract(task.body)["expected_lane_id"] for task in workers] == [
            "native_hermes", "claude", "grok",
        ]
        assert extract_contract(verifier.body)["expected_lane_count"] == 3
    finally:
        conn.close()


def test_lane_bound_swarm_rejects_missing_native_hermes_lane(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="native_hermes"):
            create_swarm(
                conn,
                goal="Missing the required native_hermes lane.",
                workers=[
                    SwarmWorkerSpec(
                        profile=lane, title=lane, body="Work.",
                        skills=["kanban-worker"], lane_id=lane,
                    )
                    for lane in ("claude", "grok", "agy")
                ],
                verifier_assignee="verifier",
                synthesizer_assignee="synthesizer",
            )
    finally:
        conn.close()


def test_lane_bound_swarm_rejects_fewer_than_two_external_lanes(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="at least 2"):
            create_swarm(
                conn,
                goal="Only one external lane is available.",
                workers=[
                    SwarmWorkerSpec(
                        profile="native_hermes", title="native_hermes", body="Work.",
                        skills=[], lane_id="native_hermes",
                    ),
                    SwarmWorkerSpec(
                        profile="claude", title="claude", body="Work.",
                        skills=["kanban-worker"], lane_id="claude",
                    ),
                ],
                verifier_assignee="verifier",
                synthesizer_assignee="synthesizer",
            )
    finally:
        conn.close()


def test_lane_bound_swarm_rejects_unknown_lane_id(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="only accept lane ids"):
            create_swarm(
                conn,
                goal="An unrecognized lane id is not a valid worker lane.",
                workers=[
                    SwarmWorkerSpec(
                        profile="native_hermes", title="native_hermes", body="Work.",
                        skills=[], lane_id="native_hermes",
                    ),
                    SwarmWorkerSpec(
                        profile="claude", title="claude", body="Work.",
                        skills=["kanban-worker"], lane_id="claude",
                    ),
                    SwarmWorkerSpec(
                        profile="grok", title="grok", body="Work.",
                        skills=["kanban-worker"], lane_id="mystery",
                    ),
                ],
                verifier_assignee="verifier",
                synthesizer_assignee="synthesizer",
            )
    finally:
        conn.close()


def test_lane_bound_completion_is_fail_closed_and_synth_requires_verifier_gate(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Run the four-lane joke test.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=lane, body="Work.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        for worker_id, lane in zip(created.worker_ids, MULTI_AGENT_LANE_IDS):
            with pytest.raises(ValueError, match="lane_id"):
                kb.complete_task(
                    conn, worker_id, summary="done",
                    metadata={
                        "role": "worker", "root_id": created.root_id,
                        "lane_id": "wrong", "preflight_skill_id": "" if lane == "native_hermes" else "kanban-worker",
                        "outcome": "completed", "verified_clean": True,
                    },
                )
            assert kb.complete_task(
                conn, worker_id, summary="done",
                metadata={
                    "role": "worker", "root_id": created.root_id,
                    "lane_id": lane, "preflight_skill_id": "" if lane == "native_hermes" else "kanban-worker",
                    "outcome": "completed", "verified_clean": True,
                },
            )
        assert kb.get_task(conn, created.verifier_id).status == "ready"
        assert kb.complete_task(
            conn, created.verifier_id, summary="verified",
            metadata={
                "role": "verifier", "root_id": created.root_id,
                "gate": "pass", "expected_lane_count": 4,
                "verified_lane_count": 4,
            },
        )
        assert kb.get_task(conn, created.synthesizer_id).status == "ready"
        assert kb.complete_task(
            conn, created.synthesizer_id, result="冬天的笑話。",
            metadata={
                "role": "synthesizer", "root_id": created.root_id,
                "outcome": "completed", "result_present": True,
            },
        )
        assert kb.get_task(conn, created.synthesizer_id).status == "done"
    finally:
        conn.close()


def test_swarm_worker_parser_keeps_third_segment_as_skill_only():
    spec = parse_worker_arg("claude:Return one bounded joke:kanban-worker")
    assert spec.profile == "claude"
    assert spec.body == "Return one bounded joke"
    assert spec.skills == ["kanban-worker"]
    assert spec.acceptance == ""


def test_swarm_worker_parser_keeps_two_segment_legacy_shape():
    spec = parse_worker_arg("claude:Return one bounded joke")
    assert spec.profile == "claude"
    assert spec.title == "Return one bounded joke"
    assert spec.body == "Return one bounded joke"
    assert spec.skills == []
    assert spec.acceptance == ""


def test_swarm_worker_parser_reads_optional_acceptance_segment():
    spec = parse_worker_arg(
        "claude:Return one bounded joke:kanban-worker:Include the punchline and keep it brief"
    )
    assert spec.profile == "claude"
    assert spec.title == "Return one bounded joke"
    assert spec.body == "Return one bounded joke"
    assert spec.skills == ["kanban-worker"]
    assert spec.acceptance == "Include the punchline and keep it brief"


def test_lane_bound_worker_contract_acceptance_prefers_explicit_body_then_title(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Run three independent worker checks.",
            workers=[
                SwarmWorkerSpec(
                    profile="native",
                    title="Explicit worker",
                    body="Detailed explicit worker instructions.",
                    acceptance="Explicit worker acceptance.",
                    lane_id="native_hermes",
                ),
                SwarmWorkerSpec(
                    profile="claude",
                    title="Rich worker",
                    body="Detailed worker instructions that are also the acceptance.",
                    skills=["kanban-worker"],
                    lane_id="claude",
                ),
                SwarmWorkerSpec(
                    profile="grok",
                    title="Title fallback worker",
                    body="",
                    skills=["kanban-worker"],
                    lane_id="grok",
                ),
                SwarmWorkerSpec(
                    profile="agy",
                    title="Fourth worker",
                    body="Fourth worker instructions.",
                    skills=["kanban-worker"],
                    lane_id="agy",
                ),
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        contracts = [extract_contract(task.body) for task in workers]
        by_lane = {
            contract["expected_lane_id"]: contract for contract in contracts
        }
        assert by_lane["native_hermes"]["acceptance"] == "Explicit worker acceptance."
        assert by_lane["claude"]["acceptance"] == (
            "Detailed worker instructions that are also the acceptance."
        )
        assert by_lane["grok"]["acceptance"] == "Title fallback worker"
    finally:
        conn.close()


def _metadata_from_body(body):
    """Read the completion metadata a compliant agent would send, by parsing
    the instruction text out of the task body.

    Deliberately parses the body rather than hardcoding the expected keys.
    A hardcoded dict would only prove that some dict passes; parsing proves
    that *the text the agent is given* passes. That is the property that was
    violated -- the verifier body named one of the five required keys, so an
    agent that obeyed it exactly was rejected.
    """

    metadata = {}
    in_block = False
    for line in body.splitlines():
        if line.startswith("Completion contract ("):
            in_block = True
            continue
        if not in_block:
            continue
        if line.startswith("Send these as completion metadata"):
            break
        stripped = line.strip()
        if not stripped.startswith("(") and " = " in stripped:
            key, _, raw = stripped.partition(" = ")
            metadata[key.strip()] = json.loads(raw.strip())
    return metadata


def test_completion_requirements_satisfy_validate_completion(tmp_path):
    """The body's stated contract must be exactly what the kernel accepts.

    This is the anti-drift check. If `_completion_requirements` and
    `validate_completion` ever disagree again, this fails -- whichever side
    moved.
    """

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )

        tasks = [kb.get_task(conn, tid) for tid in created.worker_ids]
        tasks.append(kb.get_task(conn, created.verifier_id))
        tasks.append(kb.get_task(conn, created.synthesizer_id))

        for task in tasks:
            contract = extract_contract(task.body)
            assert contract is not None
            metadata = _metadata_from_body(task.body)
            assert metadata, f"{contract['role']} body states no completion metadata"
            # The synthesizer additionally requires a non-empty task result;
            # every other role is metadata-only.
            result = "最終交付內容：冬天的笑話。" if contract["role"] == "synthesizer" else None
            reason = validate_completion(task, metadata=metadata, result=result)
            assert reason is None, f"{contract['role']}: {reason}"
    finally:
        conn.close()


def _metadata_and_result_from_example_call(example_text):
    """Parse the literal 'kanban_complete({...})' example line back into
    (metadata, result) for the anti-drift test below, mirroring
    _metadata_from_body's "parse what the agent is actually shown" spirit."""
    marker = "kanban_complete("
    start = example_text.index(marker) + len(marker)
    end = example_text.rindex(")")
    call = json.loads(example_text[start:end])
    return call.get("metadata"), call.get("result")


def test_completion_call_example_satisfies_validate_completion(tmp_path):
    """SWARM-LANE-TIMEOUT-RETEST-002 (2026-08-21): a real synthesizer
    failed kanban_complete 19 times over ~10 minutes, then self-blocked
    falsely claiming the kernel validator was buggy -- independently
    disproven by testing a correctly-shaped call against the very same
    task. This is the anti-drift check for the concrete example added in
    response: if _completion_call_example and validate_completion ever
    disagree, this fails -- whichever side moved."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        tasks = [kb.get_task(conn, tid) for tid in created.worker_ids]
        tasks.append(kb.get_task(conn, created.verifier_id))
        tasks.append(kb.get_task(conn, created.synthesizer_id))

        for task in tasks:
            contract = extract_contract(task.body)
            assert contract is not None
            example_lines = [
                line for line in task.body.splitlines()
                if line.startswith("  kanban_complete(")
            ]
            assert len(example_lines) == 1, task.body
            metadata, result = _metadata_and_result_from_example_call(example_lines[0])
            reason = validate_completion(task, metadata=metadata, result=result)
            assert reason is None, f"{contract['role']}: {reason}"
    finally:
        conn.close()


def test_completion_call_example_distinguishes_top_level_from_metadata_fields():
    """The concrete regression: result/summary/task_id must render as
    top-level kanban_complete parameters, never nested inside metadata --
    that exact ambiguity is what the real stuck synthesizer kept
    mis-resolving across 19 failed attempts."""
    worker_example = _completion_call_example({
        "role": "worker", "root_id": "t_root", "expected_lane_id": "claude",
        "preflight_skill_id": "claude-code",
    })
    assert '"summary"' in worker_example
    assert '"summary"' not in worker_example.split('"metadata"')[1]

    synth_example = _completion_call_example({"role": "synthesizer", "root_id": "t_root"})
    assert '"result": "冬天的笑話：雪人說，今天真冷。"' in synth_example
    assert '"result"' not in synth_example.split('"metadata"')[1]


def test_completion_requirements_reject_a_subset(tmp_path):
    """Negative control: dropping any single stated key must be rejected.

    Without this, the test above would still pass if the body listed keys the
    kernel does not actually enforce.
    """

    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        # Use an external-lane worker, not native_hermes. native_hermes has an
        # empty preflight_skill_id, and the kernel reads that key with a ""
        # default -- so omitting it is genuinely equivalent to sending it, and
        # the key is not load-bearing for that lane. Every key stated for an
        # external worker is.
        external_worker = created.worker_ids[list(MULTI_AGENT_LANE_IDS).index("claude")]
        for task_id in (created.verifier_id, created.synthesizer_id, external_worker):
            task = kb.get_task(conn, task_id)
            role = extract_contract(task.body)["role"]
            full = _metadata_from_body(task.body)
            result = "最終交付內容：冬天的笑話。" if role == "synthesizer" else None
            for dropped in full:
                partial = {k: v for k, v in full.items() if k != dropped}
                reason = validate_completion(task, metadata=partial, result=result)
                assert reason is not None, (
                    f"{role}: dropping {dropped!r} was accepted, so the body "
                    "states a key the kernel does not enforce"
                )
    finally:
        conn.close()


class TestNoPartialGraphOnValidationFailure:
    """SWARM-E2E-DEFECTS-001 Defect 1.

    The per-worker skill check used to run inside the creation loop, so an
    invalid later worker left a root and the earlier workers behind. Those
    cards were `ready`, so the dispatcher claimed and ran them -- work whose
    output no verifier would consume, because no verifier had been created.

    Observed in production on 2026-08-19: an agent's first `kanban swarm`
    invocation left root t_6109f004 and two workers, and they ran.
    """

    @staticmethod
    def _specs(bad_index):
        specs = []
        for i, lane in enumerate(MULTI_AGENT_LANE_IDS):
            skills = [] if lane == "native_hermes" else ["kanban-worker"]
            if i == bad_index:
                skills = []  # external lane with no skill -- the invalid case
            specs.append(
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=skills, lane_id=lane,
                )
            )
        return specs

    def _attempt(self, conn, bad_index):
        with pytest.raises(ValueError, match="requires a preflight skill id"):
            create_swarm(
                conn,
                goal="Ask four agents for one joke.",
                workers=self._specs(bad_index),
                verifier_assignee="verifier",
                synthesizer_assignee="synthesizer",
                tenant="atomicity-test",
            )

    def test_an_invalid_last_worker_leaves_no_cards(self, tmp_path):
        """The worst case: everything before it is valid, so under the old
        code the root and three workers were already committed."""
        conn = kb.connect(tmp_path / "kanban.db")
        try:
            self._attempt(conn, bad_index=len(MULTI_AGENT_LANE_IDS) - 1)
            rows = conn.execute("select count(*) from tasks").fetchone()[0]
            assert rows == 0, f"{rows} card(s) survived a rejected swarm"
        finally:
            conn.close()

    def test_an_invalid_middle_worker_leaves_no_cards(self, tmp_path):
        conn = kb.connect(tmp_path / "kanban.db")
        try:
            self._attempt(conn, bad_index=1)
            assert conn.execute("select count(*) from tasks").fetchone()[0] == 0
        finally:
            conn.close()

    def test_a_valid_swarm_is_unaffected(self, tmp_path):
        """The check must still let a correct graph through -- moving a
        validation earlier is only safe if it did not become stricter."""
        conn = kb.connect(tmp_path / "kanban.db")
        try:
            specs = [
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"],
                    lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ]
            created = create_swarm(
                conn,
                goal="Ask four agents for one joke.",
                workers=specs,
                verifier_assignee="verifier",
                synthesizer_assignee="synthesizer",
                tenant="atomicity-test",
            )
            assert len(created.worker_ids) == len(MULTI_AGENT_LANE_IDS)
            assert kb.get_task(conn, created.verifier_id) is not None
            assert kb.get_task(conn, created.synthesizer_id) is not None
        finally:
            conn.close()


def test_synthesizer_runtime_and_retry_budget_reaches_overall_deadline():
    """Regression guard for
    docs/plans/2026-08-27-synthesizer-failure-limit-vs-deadline-001.md: the
    synthesizer's create_task() call passes max_retries=2, so its per-attempt
    runtime cap times that retry budget must stay >= kanban_db's overall
    synthesizer deadline, or the generic 2-strike failure breaker trips
    before the deadline check ever gets a chance to fire."""
    synthesizer_max_retries = 2
    assert (
        DEFAULT_SYNTHESIZER_MAX_RUNTIME_SECONDS * synthesizer_max_retries
        >= kb._SYNTHESIZER_OVERALL_DEADLINE_SECONDS
    )
