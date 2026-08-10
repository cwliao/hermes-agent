from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_message(text="/brain what is the release path?", user_id=101, chat_id=101):
    return SimpleNamespace(
        message_id=7,
        text=text,
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username="operator", first_name="Operator"),
        reply_to_message=None,
        entities=None,
        caption_entities=None,
        photo=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        animation=None,
        sticker=None,
        video_note=None,
    )


def _make_adapter(monkeypatch):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake", extra={"allow_from": ["101"]})
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter._build_message_event = lambda msg, message_type, update_id: SimpleNamespace(
        text=msg.text,
        message_type=message_type,
        channel_prompt=None,
    )
    adapter._clean_bot_trigger_text = lambda text: text
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_brain_success_dispatches_untrusted_prompt_to_handler(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    prompt = "<klib_untrusted_context>knowledge</klib_untrusted_context>"
    called = {}

    async def fake_brain(query, **kwargs):
        called["query"] = query
        called["kwargs"] = kwargs
        return {"status": "ok", "query": query, "channel_prompt": prompt}

    monkeypatch.setattr("plugins.platforms.telegram.adapter._handle_brain", fake_brain)
    await adapter._handle_command(
        SimpleNamespace(update_id=12, message=_make_message()),
        None,
    )

    assert called["query"] == "what is the release path?"
    assert called["kwargs"] == {"user_id": "101", "chat_id": "101", "chat_type": "dm"}
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "what is the release path?"
    assert event.message_type is MessageType.TEXT
    assert event.channel_prompt == prompt
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_brain_failure_sends_static_reply_without_agent_dispatch(monkeypatch):
    adapter = _make_adapter(monkeypatch)

    async def fake_brain(query, **kwargs):
        return {"status": "error", "code": "unavailable", "message": "KLIB Brain 暫時無法使用。"}

    monkeypatch.setattr("plugins.platforms.telegram.adapter._handle_brain", fake_brain)
    await adapter._handle_command(
        SimpleNamespace(update_id=13, message=_make_message()),
        None,
    )

    adapter.send.assert_awaited_once_with("101", "KLIB Brain 暫時無法使用。", reply_to="7")
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_brain_command_reaches_plugin_for_unauthorized_user(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    adapter.config = PlatformConfig(enabled=True, token="fake", extra={"allow_from": ["999"]})
    called = {}

    async def fake_brain(query, **kwargs):
        called.update(kwargs)
        return {"status": "error", "code": "unauthorized", "message": "KLIB Brain access denied."}

    monkeypatch.setattr("plugins.platforms.telegram.adapter._handle_brain", fake_brain)
    await adapter._handle_command(
        SimpleNamespace(update_id=14, message=_make_message()),
        None,
    )

    assert called == {"user_id": "101", "chat_id": "101", "chat_type": "dm"}
    adapter.send.assert_awaited_once_with("101", "KLIB Brain access denied.", reply_to="7")
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_brain_command_accepts_telegram_botname_suffix(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    called = {}

    async def fake_brain(query, **kwargs):
        called["query"] = query
        return {
            "status": "error",
            "code": "invalid_request",
            "message": "KLIB Brain query is invalid.",
        }

    monkeypatch.setattr("plugins.platforms.telegram.adapter._handle_brain", fake_brain)
    await adapter._handle_command(
        SimpleNamespace(update_id=15, message=_make_message("/brain@test_bot hello")),
        None,
    )

    assert called["query"] == "hello"
    adapter.send.assert_awaited_once_with("101", "KLIB Brain query is invalid.", reply_to="7")


@pytest.mark.asyncio
async def test_brain_channel_post_uses_sender_chat_identity(monkeypatch):
    adapter = _make_adapter(monkeypatch)
    called = {}

    async def fake_brain(query, **kwargs):
        called.update(kwargs)
        return {"status": "error", "code": "unauthorized", "message": "KLIB Brain access denied."}

    monkeypatch.setattr("plugins.platforms.telegram.adapter._handle_brain", fake_brain)
    message = _make_message()
    message.chat = SimpleNamespace(id=-1002, type="channel", is_forum=False)
    message.from_user = None
    message.sender_chat = SimpleNamespace(id=-1002, title="source")
    await adapter._handle_command(SimpleNamespace(update_id=16, message=message), None)

    assert called == {"user_id": "-1002", "chat_id": "-1002", "chat_type": "channel"}
    adapter.send.assert_awaited_once_with("-1002", "KLIB Brain access denied.", reply_to="7")
