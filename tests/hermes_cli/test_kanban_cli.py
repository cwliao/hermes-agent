"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kbc.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kbc.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kbc.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------



@pytest.mark.parametrize("alias", ["help", "--help", "-h", "?"])
def test_run_slash_help_aliases_match_bare(kanban_home, alias):
    """Every documented help alias produces the same curated output."""
    bare = kc.run_slash("")
    out = kc.run_slash(alias)
    assert out == bare


def test_run_slash_subcommand_help_returns_help_text(kanban_home):
    """`/kanban show -h` returns the actual subcommand help, not a
    fake `(usage error: 0)` sentinel."""
    out = kc.run_slash("show -h")
    assert "task_id" in out
    assert "/kanban show" in out
    assert not out.startswith("⚠")


def test_run_slash_unknown_action_friendly_error(kanban_home):
    """Unknown subcommand surfaces a single-line usage error prefixed
    with our marker — no `(usage error: 2)` wrapping, no doubled
    `kanban kanban` prog string."""
    out = kc.run_slash("frobnicate")
    assert "/kanban" in out
    assert "frobnicate" in out
    assert "/kanban-wrap" not in out
    assert "/kanban kanban" not in out
    assert "(usage error: " not in out


def test_run_slash_missing_required_arg_friendly_error(kanban_home):
    """Missing positional argument shows the subcommand-scoped usage
    line, not the top-level kanban tree."""
    out = kc.run_slash("show")
    assert "/kanban show" in out
    assert "task_id" in out


def test_run_slash_board_override_restores_prior_env(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")

    kc.run_slash("--board alpha list")

    assert os.environ.get("HERMES_KANBAN_BOARD") == "beta"


def test_run_slash_board_override_does_not_change_boards_show_current(kanban_home):
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.set_current_board("alpha")

    out = kc.run_slash("--board beta boards show")

    assert "Current board: alpha" in out


# ---------------------------------------------------------------------------
# Swarm auto-subscribe (GATE8-SWARM-COMPLETED-VERIFIER-RECOVERY-AND-
# DELIVERY-GAP-001, finding 2): `hermes kanban swarm` never registered a
# notification subscription for its own graph, unlike `kanban_create`. A
# completed synthesizer's result had nowhere to be delivered.
# ---------------------------------------------------------------------------

def _swarm_args(**overrides):
    base = dict(
        goal="four lane jokes",
        worker=["native:native joke", "peer:claude joke"],
        worker_lane=[],
        goal_max_turns=5,
        worker_max_runtime=120,
        verifier="verifier",
        synthesizer="synthesizer",
        tenant="swarm-notify-test",
        priority=0,
        created_by="test",
        idempotency_key=None,
        json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_swarm_subscribes_synthesizer_when_session_context_present(
    kanban_home, monkeypatch, capsys,
):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-gate8")

    rc = kc._cmd_swarm(_swarm_args())
    assert rc == 0
    created = json.loads(capsys.readouterr().out)

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, created["synthesizer_id"])
    finally:
        conn.close()
    assert len(subs) == 1, subs
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat-gate8"
    # Only the synthesizer is subscribed -- not every card in the graph,
    # which would turn one swarm into one notification per worker.
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, created["root_id"]) == []
        assert kb.list_notify_subs(conn, created["verifier_id"]) == []
    finally:
        conn.close()


def test_cmd_swarm_no_subscription_without_session_context(
    kanban_home, monkeypatch, capsys,
):
    """A bare CLI invocation (operator's terminal, cron) has no Telegram
    session to deliver to -- must stay a silent no-op, not an error."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)

    rc = kc._cmd_swarm(_swarm_args())
    assert rc == 0
    created = json.loads(capsys.readouterr().out)

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, created["synthesizer_id"])
    finally:
        conn.close()
    assert subs == []


def test_cmd_swarm_auto_subscribe_failure_does_not_fail_swarm_creation(
    kanban_home, monkeypatch, capsys,
):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-gate8")

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "add_notify_sub", _boom)

    rc = kc._cmd_swarm(_swarm_args())
    assert rc == 0
    created = json.loads(capsys.readouterr().out)
    assert created["synthesizer_id"]


def test_cmd_swarm_respects_auto_subscribe_on_create_false(
    kanban_home, monkeypatch, capsys,
):
    """A user who opted out of auto-subscription on the kanban_create path
    must not be silently re-subscribed via the swarm path -- the two entry
    points share one config knob."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-gate8")
    fake_config = {"kanban": {"auto_subscribe_on_create": False}}
    monkeypatch.setattr("hermes_cli.kanban.load_config", lambda: fake_config)

    rc = kc._cmd_swarm(_swarm_args())
    assert rc == 0
    created = json.loads(capsys.readouterr().out)

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, created["synthesizer_id"])
    finally:
        conn.close()
    assert subs == []


def test_cmd_swarm_tui_fallback_subscribes_via_session_key(
    kanban_home, monkeypatch, capsys,
):
    """TUI sessions clear the platform/chat_id ContextVars but still export
    HERMES_SESSION_KEY -- must subscribe as platform='tui', matching the
    kanban_create path's existing TUI behaviour."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-abc")

    rc = kc._cmd_swarm(_swarm_args())
    assert rc == 0
    created = json.loads(capsys.readouterr().out)

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, created["synthesizer_id"])
    finally:
        conn.close()
    assert len(subs) == 1, subs
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-session-abc"
