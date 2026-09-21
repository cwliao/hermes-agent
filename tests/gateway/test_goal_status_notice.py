from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.callbacks = {}
        self._active_sessions = {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(success=True)

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.callbacks[session_key] = (generation, callback)


def _goal_continuation_event(source, goal="finish the task"):
    return MessageEvent(
        text=CONTINUATION_PROMPT_TEMPLATE.format(goal=goal),
        message_type=MessageType.TEXT,
        source=source,
    )


@pytest.mark.asyncio
async def test_goal_status_notice_defers_until_post_delivery_callback():
    """Regression: goal status must appear after the agent's visible reply.

    _post_turn_goal_continuation runs before BasePlatformAdapter sends the
    returned final response. It should therefore register a post-delivery
    callback, not send the judge status immediately.
    """
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel",
        thread_id="thread-123",
        user_id="user-1",
    )

    await runner._defer_goal_status_notice_after_delivery(source, "✓ Goal achieved: done")

    assert adapter.calls == []
    assert len(adapter.callbacks) == 1

    _, callback = next(iter(adapter.callbacks.values()))
    result = callback()
    if hasattr(result, "__await__"):
        await result

    assert adapter.calls == [
        {
            "chat_id": "parent-channel",
            "content": "✓ Goal achieved: done",
            "reply_to": None,
            "metadata": {"thread_id": "thread-123"},
        }
    ]


class NoticeAdapter(FakeAdapter):
    def __init__(self, message_id="tg-100"):
        super().__init__()
        self._pending_messages = {}
        self.message_id = message_id

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        return SimpleNamespace(success=True, message_id=self.message_id)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import goals

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    try:
        reset_hermes_home_override(token)
    except Exception:
        pass
    goals._DB_CACHE.clear()


def _paused_mgr(session_id, goal="finish the task"):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id)
    mgr.set(goal, max_turns=5)
    mgr.pause("judged unachievable")
    return mgr


def _intercept_runner(adapter, mgr):
    from gateway.config import GatewayConfig, PlatformConfig
    from unittest.mock import AsyncMock

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._queued_events = {}

    async def _exec(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = _exec
    runner._get_goal_manager_for_event = AsyncMock(return_value=(mgr, SimpleNamespace(session_id=mgr.session_id)))
    runner._check_slash_access = lambda source, cmd: None
    runner._adapter_and_key_for = lambda event: (adapter, "agent:main:telegram:chat:goal-reply")
    runner._delivery_adapter_for = lambda source: adapter
    return runner


def _reply_event(*, text="use the staging key", reply_id="tg-100", own=True, user_id="user-1"):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="private",
        user_id=user_id,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        reply_to_message_id=reply_id,
        reply_to_text="⏸ Goal paused",
        reply_to_is_own_message=own,
    )


@pytest.mark.asyncio
async def test_send_persists_notice_identity(hermes_home):
    from hermes_cli.goals import GoalManager

    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = NoticeAdapter(message_id="tg-42")
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False, multiplex_profiles=False)
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: None

    sid = "sid-persist-notice"
    mgr = _paused_mgr(sid)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", user_id="user-1")
    await runner._send_goal_status_notice(
        source, "🚫 Goal judged unachievable — paused", notice_type="blocked", session_id=sid,
    )

    reloaded = GoalManager(sid)
    assert reloaded.state.status_notice_message_id == "tg-42"
    assert reloaded.state.status_notice_type == "blocked"
    assert reloaded.state.status_notice_chat_id == "chat-1"
    assert reloaded.state.status_notice_sent_at > 0
    assert mgr.state.status == "paused"


@pytest.mark.asyncio
async def test_reply_to_pause_notice_resumes_with_context(hermes_home):
    adapter = NoticeAdapter()
    mgr = _paused_mgr("sid-reply-pause")
    mgr.record_status_notice("tg-100", "blocked", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)
    event = _reply_event(text="use the staging API key")

    result = await runner._hm_goal_status_notice_reply(event, event.source, "agent:main:telegram:chat:goal-reply")

    assert result is not None
    assert "resume" in result.lower() or "Goal" in result
    assert mgr.state.status == "active"
    pending = adapter._pending_messages.get("agent:main:telegram:chat:goal-reply")
    assert pending is not None
    assert pending.text.startswith("[Continuing toward your standing goal]")
    assert "use the staging API key" in pending.text
    assert GatewayRunner._is_goal_continuation_event(pending)


@pytest.mark.asyncio
async def test_reply_to_continue_notice_does_not_resume(hermes_home):
    adapter = NoticeAdapter()
    mgr = _paused_mgr("sid-reply-continue")
    mgr.record_status_notice("tg-100", "continue", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)

    result = await runner._hm_goal_status_notice_reply(
        _reply_event(), _reply_event().source, "k",
    )

    assert result is None
    assert mgr.state.status == "paused"
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_reply_to_loop_notice_does_not_resume(hermes_home):
    adapter = NoticeAdapter()
    mgr = _paused_mgr("sid-reply-loop")
    mgr.record_status_notice("tg-100", "loop", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)

    result = await runner._hm_goal_status_notice_reply(
        _reply_event(), _reply_event().source, "k",
    )

    assert result is None
    assert mgr.state.status == "paused"


@pytest.mark.asyncio
async def test_reply_to_done_notice_does_not_resume(hermes_home):
    adapter = NoticeAdapter()
    mgr = _paused_mgr("sid-reply-done")
    mgr.record_status_notice("tg-100", "done", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)

    result = await runner._hm_goal_status_notice_reply(
        _reply_event(), _reply_event().source, "k",
    )

    assert result is None
    assert mgr.state.status == "paused"


@pytest.mark.asyncio
async def test_reply_to_stale_notice_does_not_resume(hermes_home):
    from hermes_cli.goals import STATUS_NOTICE_TTL_S

    adapter = NoticeAdapter()
    mgr = _paused_mgr("sid-reply-stale")
    mgr.record_status_notice("tg-100", "pause", chat_id="chat-1")
    mgr.state.status_notice_sent_at -= (STATUS_NOTICE_TTL_S + 10)
    mgr._save()
    runner = _intercept_runner(adapter, mgr)

    result = await runner._hm_goal_status_notice_reply(
        _reply_event(), _reply_event().source, "k",
    )

    assert result is None
    assert mgr.state.status == "paused"
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_unauthorized_reply_does_not_resume(hermes_home):
    adapter = NoticeAdapter()
    mgr = _paused_mgr("sid-reply-unauth")
    mgr.record_status_notice("tg-100", "blocked", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)
    runner._check_slash_access = lambda source, cmd: "⛔ /goal is admin-only here."

    result = await runner._hm_goal_status_notice_reply(
        _reply_event(), _reply_event().source, "k",
    )

    assert result is None
    assert mgr.state.status == "paused"
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_reply_to_someone_elses_message_does_not_resume(hermes_home):
    adapter = NoticeAdapter()
    adapter._is_reply_to_bot = lambda raw: False
    mgr = _paused_mgr("sid-reply-not-bot")
    mgr.record_status_notice("tg-100", "blocked", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)
    event = _reply_event(own=False)
    event.raw_message = object()

    result = await runner._hm_goal_status_notice_reply(
        event, event.source, "k",
    )

    assert result is None
    assert mgr.state.status == "paused"


@pytest.mark.asyncio
async def test_waiting_notice_reply_unparks_with_context(hermes_home):
    from hermes_cli.goals import GoalManager

    adapter = NoticeAdapter()
    mgr = GoalManager("sid-reply-wait")
    mgr.set("ship it", max_turns=8)
    mgr.state.turns_used = 3
    mgr.wait_for_seconds(600, reason="cooldown")
    mgr.record_status_notice("tg-100", "waiting", chat_id="chat-1")
    runner = _intercept_runner(adapter, mgr)

    result = await runner._hm_goal_status_notice_reply(
        _reply_event(text="CI is green, continue"), _reply_event().source, "k",
    )

    assert result is not None
    assert mgr.state.status == "active"
    assert not mgr.is_waiting()
    assert mgr.state.turns_used == 3
    pending = adapter._pending_messages.get("agent:main:telegram:chat:goal-reply")
    assert pending is not None
    assert "CI is green, continue" in pending.text

