"""Abandoned swarm graphs are archived whole, children first.

KANBAN-CARD-GC-001. Two days of testing left 34 cards on the board, all
residue, nine of them non-terminal. `hermes kanban gc` already existed but
only cleaned up *after* something was archived; nothing decided a card was
dead.

The ordering is the part that is not obvious. `archive_task` runs
`recompute_ready` after every call, and an `archived` parent satisfies a
dependency exactly as a `done` one does, so archiving a dead worker before
its verifier promotes that verifier against a graph that produced nothing.
The ticket records an observed instance with the card's event trail.
`test_wrong_order_does_promote_it` reproduces it here.
"""

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB.

    Same shape as the fixture in test_kanban_db.py; it is defined per-file in
    this suite rather than shared.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _age(conn, task_ids, seconds):
    """Backdate every event on these tasks."""
    cutoff = int(time.time()) - seconds
    with kb.write_txn(conn):
        for tid in task_ids:
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ?",
                (cutoff, tid),
            )


def _swarm(conn, *, tenant="gc-test"):
    """Root -> two workers -> verifier, the shape a failed swarm leaves."""
    root = kb.create_task(conn, title="root", tenant=tenant)
    kb.complete_task(conn, root, summary="planned")
    w1 = kb.create_task(conn, title="worker 1", parents=[root], tenant=tenant)
    w2 = kb.create_task(conn, title="worker 2", parents=[root], tenant=tenant)
    verifier = kb.create_task(conn, title="verify", parents=[w1, w2], tenant=tenant)
    for w in (w1, w2):
        kb.block_task(conn, w, reason="gave up")
    return root, w1, w2, verifier


def test_children_are_archived_before_parents(kanban_home):
    """The property the whole design rests on."""
    with kb.connect() as conn:
        root, w1, w2, verifier = _swarm(conn)
        _age(conn, [root, w1, w2, verifier], 30 * 24 * 3600)

        graphs = kb.find_dead_graphs(conn, older_than_seconds=7 * 24 * 3600, tenant="gc-test")
        assert len(graphs) == 1
        order = graphs[0]
        assert order.index(verifier) < order.index(w1)
        assert order.index(verifier) < order.index(w2)
        assert order.index(w1) < order.index(root)


def test_archiving_the_graph_never_promotes_the_verifier(kanban_home):
    """The hazard, pinned. Archive in the returned order and the verifier
    must go straight from todo to archived -- never through ready."""
    with kb.connect() as conn:
        root, w1, w2, verifier = _swarm(conn)
        _age(conn, [root, w1, w2, verifier], 30 * 24 * 3600)

        order = kb.find_dead_graphs(conn, older_than_seconds=7 * 24 * 3600, tenant="gc-test")[0]
        kb.archive_graph(conn, order)

        assert kb.get_task(conn, verifier).status == "archived"
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (verifier,),
            ).fetchall()
        ]
        assert "promoted" not in kinds, (
            "verifier was promoted to ready by the cleanup that was archiving it"
        )


def test_wrong_order_does_promote_it(kanban_home):
    """Negative control for the ordering. Archive parents first and the
    promotion happens -- so the ordering above is load-bearing, not a
    stylistic choice."""
    with kb.connect() as conn:
        root, w1, w2, verifier = _swarm(conn)
        kb.archive_task(conn, w1)
        kb.archive_task(conn, w2)
        assert kb.get_task(conn, verifier).status == "ready", (
            "expected the hazard to reproduce when archiving upward"
        )


def test_a_live_card_keeps_its_graph(kanban_home):
    """One dispatchable card protects its siblings. A graph is abandoned
    together or not at all."""
    with kb.connect() as conn:
        root, w1, w2, verifier = _swarm(conn)
        kb.unblock_task(conn, w1)
        assert kb.get_task(conn, w1).status in ("ready", "todo")
        _age(conn, [root, w1, w2, verifier], 30 * 24 * 3600)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (w1,))
        assert kb.find_dead_graphs(conn, older_than_seconds=7 * 24 * 3600, tenant="gc-test") == []


def test_a_recent_graph_is_left_alone(kanban_home):
    with kb.connect() as conn:
        _swarm(conn)
        assert kb.find_dead_graphs(conn, older_than_seconds=7 * 24 * 3600, tenant="gc-test") == []


def test_a_fully_finished_graph_is_not_garbage(kanban_home):
    """Everything done is history, not residue. Archiving it would hide
    completed work from the board for no benefit."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root")
        child = kb.create_task(conn, title="child", parents=[root])
        kb.complete_task(conn, root, summary="ok")
        kb.complete_task(conn, child, summary="ok")
        _age(conn, [root, child], 30 * 24 * 3600)
        assert kb.find_dead_graphs(conn, older_than_seconds=7 * 24 * 3600, tenant="gc-test") == []


def test_a_graph_in_another_tenant_is_not_swept(kanban_home):
    """Scoping is the only thing separating a disposable graph from a parked
    one, since nothing on a card marks it disposable."""
    with kb.connect() as conn:
        root, w1, w2, verifier = _swarm(conn, tenant="someone-elses-backlog")
        _age(conn, [root, w1, w2, verifier], 30 * 24 * 3600)
        assert kb.find_dead_graphs(
            conn, older_than_seconds=7 * 24 * 3600, tenant="gc-test"
        ) == []


class TestGcCommandGuards:
    """`--dry-run` must not reach the destructive half of `gc`.

    An earlier version ran the workspace, event, and log purges *before*
    checking the flag, so `gc --dead-graphs --dry-run` deleted them and then
    reported that nothing had been touched. A reviewer found it.
    """

    @staticmethod
    def _args(**over):
        import argparse
        base = dict(event_retention_days=30, log_retention_days=30,
                    dead_graphs=False, dead_graph_days=7, dry_run=False,
                    tenant=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_dry_run_does_not_purge_events(self, kanban_home, capsys):
        from hermes_cli.kanban import _cmd_gc

        with kb.connect() as conn:
            root, w1, w2, verifier = _swarm(conn)
            _age(conn, [root, w1, w2, verifier], 30 * 24 * 3600)
            before = conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0]

        assert _cmd_gc(self._args(dead_graphs=True, dry_run=True,
                                  tenant="gc-test")) == 0

        with kb.connect() as conn:
            after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
            assert kb.get_task(conn, verifier).status != "archived"
        assert after == before, "a dry run deleted event rows"
        assert "would archive" in capsys.readouterr().out

    def test_dead_graphs_refuses_an_unscoped_sweep(self, kanban_home):
        """Without a tenant there is nothing distinguishing a dead test graph
        from a backlog somebody parked, so the command declines rather than
        guessing."""
        from hermes_cli.kanban import _cmd_gc

        assert _cmd_gc(self._args(dead_graphs=True)) == 2
