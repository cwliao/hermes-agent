from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm as ks
from hermes_cli.kanban_swarm import (
    MULTI_AGENT_LANE_IDS,
    SwarmWorkerSpec,
    create_swarm,
    extract_contract,
    latest_blackboard,
    post_blackboard_update,
    validate_completion,
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


def _worker_body(root_id, *, lane_id="claude", skill_id="kanban-worker"):
    contract = {
        "version": 1,
        "role": "worker",
        "root_id": root_id,
        "expected_lane_id": lane_id,
        "preflight_skill_id": skill_id,
    }
    return "work\n[swarm:contract] " + json.dumps(contract)


def _make_deadline_worker(conn, *, status="ready", overdue=True):
    root_id = kb.create_task(
        conn, title="root", body="swarm root", assignee="root",
    )
    worker_id = kb.create_task(
        conn,
        title="worker",
        body=_worker_body(root_id),
        assignee="worker",
    )
    post_blackboard_update(
        conn,
        root_id,
        author="test",
        key="topology",
        value={"worker_ids": [worker_id]},
    )
    if overdue:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                (int(time.time()) - 661, root_id),
            )
    if status == "running":
        assert kb.claim_task(
            conn, worker_id, claimer="test-host:worker",
        ) is not None
    elif status == "blocked":
        assert kb.claim_task(
            conn, worker_id, claimer="test-host:worker",
        ) is not None
        assert kb.block_task(
            conn, worker_id, reason="waiting for operator input", kind="needs_input",
        )
    elif status == "todo":
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ?", (worker_id,)
            )
    else:
        assert status == "ready"
    return root_id, worker_id


def _lane_specs():
    return [
        SwarmWorkerSpec(
            profile=lane,
            title=f"{lane} work",
            body="Return one bounded result.",
            skills=[] if lane == "native_hermes" else ["kanban-worker"],
            lane_id=lane,
        )
        for lane in MULTI_AGENT_LANE_IDS
    ]


def test_overdue_running_worker_is_confirmed_dead_and_excused(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        root_id, worker_id = _make_deadline_worker(conn, status="running")
        monkeypatch.setattr(kb, "_claimer_id", lambda: "test-host:dispatcher")
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(kb.os, "kill", lambda pid, sig: None)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (424242, worker_id),
            )

        assert ks.excuse_overdue_workers(conn) == 1
        assert kb.get_task(conn, worker_id).status == "archived"
        assert latest_blackboard(conn, root_id)["excused_worker_ids"] == [worker_id]
    finally:
        conn.close()


def test_overdue_running_worker_that_survives_termination_is_deferred(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        root_id, worker_id = _make_deadline_worker(conn, status="running")
        monkeypatch.setattr(kb, "_claimer_id", lambda: "test-host:dispatcher")
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(kb.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(kb.time, "sleep", lambda seconds: None)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (424242, worker_id),
            )

        assert ks.excuse_overdue_workers(conn) == 0
        task = kb.get_task(conn, worker_id)
        assert task.status == "running"
        assert latest_blackboard(conn, root_id).get("excused_worker_ids") is None
        assert any(
            event.kind == "reclaim_deferred"
            for event in kb.list_events(conn, worker_id)
        )
    finally:
        conn.close()


def test_overdue_sticky_needs_input_worker_emits_root_excuse_event(
    kanban_home,
):
    conn = kb.connect()
    try:
        root_id, worker_id = _make_deadline_worker(conn, status="blocked")

        assert ks.excuse_overdue_workers(conn) == 1
        assert kb.get_task(conn, worker_id).status == "archived"
        event = next(
            event for event in kb.list_events(conn, root_id)
            if event.kind == "worker_excused_needs_input"
        )
        assert event.payload == {
            "task_id": worker_id,
            "lane_id": "claude",
            "skill_id": "kanban-worker",
            "reason": "waiting for operator input",
        }
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["ready", "todo"])
def test_overdue_ready_or_todo_worker_is_excused_without_event(
    kanban_home, status,
):
    conn = kb.connect()
    try:
        root_id, worker_id = _make_deadline_worker(conn, status=status)
        before = len(kb.list_events(conn, root_id))

        assert ks.excuse_overdue_workers(conn) == 1
        assert kb.get_task(conn, worker_id).status == "archived"
        assert latest_blackboard(conn, root_id)["excused_worker_ids"] == [worker_id]
        root_events = kb.list_events(conn, root_id)
        assert len(root_events) == before + 1  # blackboard update audit event
        assert not any(
            event.kind == "worker_excused_needs_input" for event in root_events
        )
    finally:
        conn.close()


def test_not_yet_overdue_worker_is_untouched(kanban_home):
    conn = kb.connect()
    try:
        root_id, worker_id = _make_deadline_worker(conn, overdue=False)
        worker_events = kb.list_events(conn, worker_id)
        root_events = kb.list_events(conn, root_id)

        assert ks.excuse_overdue_workers(conn) == 0
        assert kb.get_task(conn, worker_id).status == "ready"
        assert latest_blackboard(conn, root_id).get("excused_worker_ids") is None
        assert kb.list_events(conn, worker_id) == worker_events
        assert kb.list_events(conn, root_id) == root_events
    finally:
        conn.close()


def test_excuse_overdue_workers_is_idempotent_after_archiving(kanban_home):
    conn = kb.connect()
    try:
        root_id, worker_id = _make_deadline_worker(conn)

        assert ks.excuse_overdue_workers(conn) == 1
        first_events = kb.list_events(conn, root_id)
        assert ks.excuse_overdue_workers(conn) == 0
        assert kb.get_task(conn, worker_id).status == "archived"
        assert latest_blackboard(conn, root_id)["excused_worker_ids"] == [worker_id]
        assert kb.list_events(conn, root_id) == first_events
    finally:
        conn.close()


def test_dispatcher_reports_overdue_excuses(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        monkeypatch.setattr(ks, "excuse_overdue_workers", lambda connection: 3)
        monkeypatch.setattr(
            ks, "excuse_blocked_workers_below_quorum", lambda connection: 0,
        )
        result = kb._dispatch_once_locked(conn, reconcile_orphans=False)
        assert result.overdue_excused == 3
    finally:
        conn.close()


def test_dynamic_verifier_count_uses_excused_worker_ids_and_has_floor(
    kanban_home,
):
    conn = kb.connect()
    try:
        created = create_swarm(
            conn,
            goal="Generate four bounded results.",
            workers=_lane_specs(),
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        verifier = kb.get_task(conn, created.verifier_id)
        contract = extract_contract(verifier.body)
        assert contract["dynamic_expected_lane_count"] is True
        assert contract["expected_lane_count"] == 4

        post_blackboard_update(
            conn,
            created.root_id,
            author="test",
            key="excused_worker_ids",
            value=[created.worker_ids[0]],
        )
        accepted_at_three = validate_completion(
            verifier,
            conn=conn,
            metadata={
                "role": "verifier",
                "root_id": created.root_id,
                "gate": "pass",
                "expected_lane_count": 3,
                "verified_lane_count": 3,
            },
        )
        assert accepted_at_three is None

        post_blackboard_update(
            conn,
            created.root_id,
            author="test",
            key="excused_worker_ids",
            value=created.worker_ids[:3],
        )
        assert ks._effective_expected_lane_count(contract, conn) == 1
        accepted_at_floor = validate_completion(
            verifier,
            conn=conn,
            metadata={
                "role": "verifier",
                "root_id": created.root_id,
                "gate": "pass",
                "expected_lane_count": 1,
                "verified_lane_count": 1,
            },
        )
        assert accepted_at_floor is None
    finally:
        conn.close()


def test_complete_task_uses_dynamic_verifier_count_from_blackboard(kanban_home):
    conn = kb.connect()
    try:
        created = create_swarm(
            conn,
            goal="Generate four bounded results.",
            workers=_lane_specs(),
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        verifier = kb.get_task(conn, created.verifier_id)
        contract = extract_contract(verifier.body)
        excused_worker_id = created.worker_ids[0]
        post_blackboard_update(
            conn,
            created.root_id,
            author="swarm-deadline",
            key="excused_worker_ids",
            value=[excused_worker_id],
        )
        assert kb.archive_task(conn, excused_worker_id)

        for worker_id in created.worker_ids[1:]:
            worker = kb.get_task(conn, worker_id)
            worker_contract = extract_contract(worker.body)
            assert kb.complete_task(
                conn,
                worker_id,
                summary="bounded result verified",
                metadata={
                    "role": "worker",
                    "root_id": created.root_id,
                    "lane_id": worker_contract["expected_lane_id"],
                    "preflight_skill_id": worker_contract["preflight_skill_id"],
                    "outcome": "completed",
                    "verified_clean": True,
                },
            )

        assert contract["dynamic_expected_lane_count"] is True
        assert contract["expected_lane_count"] == 4
        assert kb.complete_task(
            conn,
            created.verifier_id,
            summary="three worker lanes verified",
            metadata={
                "role": "verifier",
                "root_id": created.root_id,
                "gate": "pass",
                "expected_lane_count": 3,
                "verified_lane_count": 3,
            },
        )
        assert kb.get_task(conn, created.verifier_id).status == "done"
    finally:
        conn.close()
