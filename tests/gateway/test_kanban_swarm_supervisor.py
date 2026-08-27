"""M1 swarm supervisor watcher: read-only stall diagnostics.

Covers lock gating, stall_key dedup, heartbeat health, and the invariant
that this watcher never mutates task status.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def runner():
    obj = object.__new__(GatewayKanbanWatchersMixin)
    obj._kanban_dispatcher_lock_handle = object()
    return obj


def _supervisor_events(conn, task_id: str):
    return [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == "verifier_gate_rejected"
        and isinstance(event.payload, dict)
        and event.payload.get("source") == "swarm_supervisor"
    ]


def _make_swarm_with_worker_budget(conn, *, max_runtime_seconds: int = 30):
    return create_swarm(
        conn,
        goal="M1 swarm supervisor fixture",
        workers=[
            SwarmWorkerSpec(
                profile="worker-a",
                title="Lane A",
                body="Do the work",
                max_runtime_seconds=max_runtime_seconds,
            )
        ],
        verifier_assignee="verifier",
        synthesizer_assignee="synthesizer",
    )


def _set_worker_heartbeat(conn, worker_id: str, last_heartbeat_at: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (last_heartbeat_at, worker_id),
        )


def test_stalled_worker_records_one_diagnostic_without_blocking(
    kanban_home, runner, monkeypatch
):
    conn = kb.connect()
    created = _make_swarm_with_worker_budget(conn, max_runtime_seconds=30)
    worker_id = created.worker_ids[0]
    now = int(time.time())
    _set_worker_heartbeat(conn, worker_id, now - 120)
    before_status = kb.get_task(conn, worker_id).status
    conn.close()

    calls: list[dict] = []
    real = kb.record_swarm_stall_diagnostic

    def _spy(conn, task_id, **kwargs):
        calls.append({"task_id": task_id, **kwargs})
        return real(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "record_swarm_stall_diagnostic", _spy)

    runner._kanban_swarm_supervisor_tick(now=now)

    worker_calls = [call for call in calls if call["task_id"] == worker_id]
    assert len(worker_calls) == 1
    assert worker_calls[0]["block"] is False
    assert worker_calls[0]["source"] == "swarm_supervisor"

    conn = kb.connect()
    events = _supervisor_events(conn, worker_id)
    assert len(events) == 1
    assert events[0].payload["stall_key"].startswith(
        f"swarm-stall:{created.root_id}:{worker_id}:"
    )
    after = kb.get_task(conn, worker_id)
    assert after.status == before_status
    conn.close()


def test_same_stall_is_deduped_across_ticks(kanban_home, runner):
    conn = kb.connect()
    created = _make_swarm_with_worker_budget(conn, max_runtime_seconds=30)
    worker_id = created.worker_ids[0]
    now = int(time.time())
    _set_worker_heartbeat(conn, worker_id, now - 120)
    conn.close()

    runner._kanban_swarm_supervisor_tick(now=now)
    runner._kanban_swarm_supervisor_tick(now=now)

    conn = kb.connect()
    assert len(_supervisor_events(conn, worker_id)) == 1
    conn.close()


def test_healthy_worker_produces_no_diagnostic(kanban_home, runner, monkeypatch):
    conn = kb.connect()
    created = _make_swarm_with_worker_budget(conn, max_runtime_seconds=30)
    worker_id = created.worker_ids[0]
    now = int(time.time())
    _set_worker_heartbeat(conn, worker_id, now - 5)
    conn.close()

    calls: list[dict] = []
    real = kb.record_swarm_stall_diagnostic

    def _spy(conn, task_id, **kwargs):
        calls.append({"task_id": task_id, **kwargs})
        return real(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "record_swarm_stall_diagnostic", _spy)

    runner._kanban_swarm_supervisor_tick(now=now)

    assert [call for call in calls if call["task_id"] == worker_id] == []
    conn = kb.connect()
    assert _supervisor_events(conn, worker_id) == []
    conn.close()


def test_watcher_noops_without_dispatcher_lock(kanban_home, runner, monkeypatch):
    conn = kb.connect()
    created = _make_swarm_with_worker_budget(conn, max_runtime_seconds=30)
    worker_id = created.worker_ids[0]
    now = int(time.time())
    _set_worker_heartbeat(conn, worker_id, now - 120)
    conn.close()

    runner._kanban_dispatcher_lock_handle = None

    calls: list[dict] = []
    real = kb.record_swarm_stall_diagnostic

    def _spy(conn, task_id, **kwargs):
        calls.append({"task_id": task_id, **kwargs})
        return real(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "record_swarm_stall_diagnostic", _spy)

    runner._kanban_swarm_supervisor_tick(now=now)

    assert calls == []
    conn = kb.connect()
    assert _supervisor_events(conn, worker_id) == []
    conn.close()


def test_stall_tick_never_mutates_task_status(kanban_home, runner):
    conn = kb.connect()
    created = _make_swarm_with_worker_budget(conn, max_runtime_seconds=30)
    worker_id = created.worker_ids[0]
    now = int(time.time())
    _set_worker_heartbeat(conn, worker_id, now - 120)
    statuses_before = {
        task_id: kb.get_task(conn, task_id).status
        for task_id in [
            created.root_id,
            worker_id,
            created.verifier_id,
            created.synthesizer_id,
        ]
    }
    conn.close()

    runner._kanban_swarm_supervisor_tick(now=now)

    conn = kb.connect()
    for task_id, status in statuses_before.items():
        assert kb.get_task(conn, task_id).status == status
    assert _supervisor_events(conn, worker_id)
    conn.close()
