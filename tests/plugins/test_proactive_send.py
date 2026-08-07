"""Unit tests for proactive-send interface (PluginContext.send_to_chat)."""

import asyncio
import pytest
from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager


def _run(coro):
    return asyncio.run(coro)


class MockAdapter:
    def __init__(self, platform_name="telegram", ok=True):
        self.platform = platform_name
        self.calls = []
        self._ok = ok

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return type("SendResult", (), {"ok": self._ok})()


class MockRunner:
    def __init__(self, adapters):
        self.adapters = adapters


class TestProactiveSendInterface:
    def test_send_to_chat_positional_signature(self):
        manager = PluginManager()
        manifest = PluginManifest(name="test_plugin")
        ctx = PluginContext(manifest, manager)

        received = []

        def mock_handler(chat_id, content, reply_to, platform, metadata):
            received.append({
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "platform": platform,
                "metadata": metadata,
            })
            return True

        manager._send_to_chat_handler = mock_handler

        result = _run(ctx.send_to_chat("chat-100", "msg-200", "Hello from proactive send!"))

        assert result is True
        assert len(received) == 1
        assert received[0] == {
            "chat_id": "chat-100",
            "content": "Hello from proactive send!",
            "reply_to": "msg-200",
            "platform": "telegram",
            "metadata": None,
        }

    def test_send_to_chat_keyword_signatures(self):
        manager = PluginManager()
        manifest = PluginManifest(name="test_plugin")
        ctx = PluginContext(manifest, manager)

        received = []
        manager._send_to_chat_handler = lambda **kwargs: received.append(kwargs) or True

        # Test signature: send_to_chat(chat_id, text, reply_to=message_id)
        res1 = _run(ctx.send_to_chat("chat-1", "Notice text", reply_to="msg-1"))
        assert res1 is True

        # Test signature: send_to_chat(chat_id=..., message_id=..., text=...)
        res2 = _run(ctx.send_to_chat(chat_id="chat-2", message_id="msg-2", text="Notice text 2"))
        assert res2 is True

        assert len(received) == 2
        assert received[0]["chat_id"] == "chat-1"
        assert received[0]["content"] == "Notice text"
        assert received[0]["reply_to"] == "msg-1"

        assert received[1]["chat_id"] == "chat-2"
        assert received[1]["content"] == "Notice text 2"
        assert received[1]["reply_to"] == "msg-2"

    def test_send_to_chat_dispatches_via_gateway_runner_adapter(self, monkeypatch):
        manager = PluginManager()
        manifest = PluginManifest(name="test_plugin")
        ctx = PluginContext(manifest, manager)

        mock_adapter = MockAdapter(platform_name="telegram", ok=True)
        # Match Platform enum or str platform
        enum_telegram = type("PlatformEnum", (), {"value": "telegram"})()
        runner = MockRunner({enum_telegram: mock_adapter})

        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        result = _run(
            ctx.send_to_chat(
                chat_id="12345",
                message_id="67890",
                text="DocuBot processing complete!",
                platform="telegram",
            )
        )

        assert result is True
        assert len(mock_adapter.calls) == 1
        assert mock_adapter.calls[0] == {
            "chat_id": "12345",
            "content": "DocuBot processing complete!",
            "reply_to": "67890",
            "metadata": None,
        }

    def test_send_to_chat_empty_content_or_chat_id_fails(self):
        manager = PluginManager()
        manifest = PluginManifest(name="test_plugin")
        ctx = PluginContext(manifest, manager)

        assert _run(ctx.send_to_chat("", "msg-1", "hello")) is False
        assert _run(ctx.send_to_chat("chat-1", "msg-1", "")) is False

    def test_send_to_chat_missing_adapter_returns_false(self, monkeypatch):
        manager = PluginManager()
        manifest = PluginManifest(name="test_plugin")
        ctx = PluginContext(manifest, manager)

        runner = MockRunner({})
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        assert _run(ctx.send_to_chat("chat-1", "msg-1", "hello", platform="discord")) is False
