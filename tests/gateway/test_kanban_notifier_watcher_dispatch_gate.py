"""Notifier polling stays active when another gateway owns dispatching."""

import asyncio
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    return runner


def test_notifier_watcher_polls_without_dispatch_ownership():
    """A profile gateway still polls its profile-owned subscriptions."""
    runner = _make_runner(with_adapter=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Stop after the initial delay + first per-interval sleep so the loop
        # body runs exactly once.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch.object(
        _kb, "list_boards",
        side_effect=lambda *a, **kw: past_gate.append(True) or [],
    ):
        with patch("asyncio.sleep", side_effect=fake_sleep):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, (
        "gateways without the dispatch lock must still poll owned subscriptions"
    )


def test_notifier_watcher_skips_board_not_owned_by_current_gateway(monkeypatch):
    """If a board declares dispatcher_owner, only the owning gateway processes it."""
    runner = _make_runner(with_adapter=True)
    monkeypatch.setenv("GATEWAY_RELAY_ID", "gw-primary")

    sleep_calls = []
    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb
    connect_calls = []

    def fake_connect(board=None):
        connect_calls.append(board)
        m = MagicMock()
        m.close.return_value = None
        return m

    with patch("hermes_cli.config.load_config", return_value={"kanban": {"dispatch_in_gateway": True}}):
        with patch.object(
            _kb,
            "list_boards",
            side_effect=lambda *a, **kw: [
                {"slug": "other", "db_path": "ignore-other", "dispatcher_owner": "gw-other"},
                {"slug": "owner", "db_path": "owner-db", "dispatcher_owner": "gw-primary"},
            ],
        ):
            with patch.object(_kb, "connect", side_effect=fake_connect):
                with patch.object(_kb, "list_notify_subs", return_value=[]):
                    with patch.object(_kb, "count_notify_subs", return_value=1):
                        with patch("asyncio.sleep", side_effect=fake_sleep):
                            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                                asyncio.run(runner._kanban_notifier_watcher())

    assert connect_calls == ["owner"], connect_calls
