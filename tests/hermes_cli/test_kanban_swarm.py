import pytest

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_swarm import (
    MULTI_AGENT_LANE_IDS,
    SwarmWorkerSpec,
    create_swarm,
    extract_contract,
    latest_blackboard,
    parse_worker_arg,
    post_blackboard_update,
    validate_completion,
)
import pytest


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
        assert all(task.max_runtime_seconds == 120 for task in workers)
        assert [extract_contract(task.body)["expected_lane_id"] for task in workers] == list(MULTI_AGENT_LANE_IDS)
        assert extract_contract(verifier.body)["expected_lane_count"] == 4
        assert extract_contract(synthesizer.body)["verifier_id"] == created.verifier_id
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
            conn, created.synthesizer_id, result="The synthesized joke.",
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
            result = "final deliverable" if contract["role"] == "synthesizer" else None
            reason = validate_completion(task, metadata=metadata, result=result)
            assert reason is None, f"{contract['role']}: {reason}"
    finally:
        conn.close()


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
            result = "final deliverable" if role == "synthesizer" else None
            for dropped in full:
                partial = {k: v for k, v in full.items() if k != dropped}
                reason = validate_completion(task, metadata=partial, result=result)
                assert reason is not None, (
                    f"{role}: dropping {dropped!r} was accepted, so the body "
                    "states a key the kernel does not enforce"
                )
    finally:
        conn.close()
