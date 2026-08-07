"""Tests for the Telegram ``/ingest`` slash-command plugin."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def _load_plugin_init():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "ingest_command"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.ingest_command",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        sys.modules["hermes_plugins"] = namespace
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_plugins.ingest_command"
    module.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.ingest_command"] = module
    spec.loader.exec_module(module)
    return module


def _run(coro):
    return asyncio.run(coro)


def _set_session(monkeypatch, module, *, chat_id="chat-1", message_id="message-1"):
    values = {
        "HERMES_SESSION_CHAT_ID": chat_id,
        "HERMES_SESSION_MESSAGE_ID": message_id,
    }
    monkeypatch.setattr(
        module,
        "get_session_env",
        lambda name, default="": values.get(name, default),
    )


class TestIngestCommand:
    def test_happy_path_writes_exact_content_and_cleans_temp_file(self, monkeypatch):
        module = _load_plugin_init()
        _set_session(monkeypatch, module)
        calls = []

        def fake_ingest(**kwargs):
            path = Path(kwargs["local_path"])
            calls.append((kwargs, path.read_text(encoding="utf-8")))
            assert path.suffix == ".md"
            return {"status": "queued"}

        monkeypatch.setattr(module, "ingest_document_to_docubot", fake_ingest)
        content = "# Pasted note\n\n**Keep this exact Markdown.**\n"

        reply = _run(module._handle_ingest(content))

        assert len(calls) == 1
        kwargs, written_content = calls[0]
        assert written_content == content
        assert kwargs["multipart"] is True
        assert kwargs["metadata"] == {
            "platform": "telegram",
            "chat_id": "chat-1",
            "message_id": "message-1",
            "file_name": "telegram-inline-message-1.md",
            "mime_type": "text/markdown",
        }
        assert not Path(kwargs["local_path"]).exists()
        assert "queued" in reply

    def test_same_content_different_message_ids_have_distinct_stable_keys(self, monkeypatch):
        module = _load_plugin_init()
        values = {
            "HERMES_SESSION_CHAT_ID": "chat-1",
            "HERMES_SESSION_MESSAGE_ID": "message-a",
        }
        monkeypatch.setattr(
            module,
            "get_session_env",
            lambda name, default="": values.get(name, default),
        )
        stable_keys = []

        def fake_ingest(**kwargs):
            stable_keys.append(kwargs["stable_key"])
            return {"status": "accepted"}

        monkeypatch.setattr(module, "ingest_document_to_docubot", fake_ingest)
        content = "same content"

        _run(module._handle_ingest(content))
        values["HERMES_SESSION_MESSAGE_ID"] = "message-b"
        _run(module._handle_ingest(content))

        assert stable_keys == ["chat-1-message-a-inline-text", "chat-1-message-b-inline-text"]

    def test_empty_content_returns_usage_without_ingesting(self, monkeypatch):
        module = _load_plugin_init()
        calls = []
        monkeypatch.setattr(module, "ingest_document_to_docubot", lambda **kwargs: calls.append(kwargs))

        reply = _run(module._handle_ingest(" \n\t "))

        assert reply == module._USAGE_REPLY
        assert calls == []

    def test_over_length_content_is_rejected_without_ingesting(self, monkeypatch):
        module = _load_plugin_init()
        calls = []
        monkeypatch.setattr(module, "ingest_document_to_docubot", lambda **kwargs: calls.append(kwargs))
        content = "x" * (module._MAX_INGEST_TEXT_BYTES + 1)

        reply = _run(module._handle_ingest(content))

        assert "rejected" in reply
        assert f"{len(content.encode('utf-8')):,}" in reply
        assert f"{module._MAX_INGEST_TEXT_BYTES:,}" in reply
        assert calls == []

    def test_temp_file_is_cleaned_when_ingestion_raises(self, monkeypatch):
        module = _load_plugin_init()
        _set_session(monkeypatch, module)
        paths = []

        def failing_ingest(**kwargs):
            paths.append(Path(kwargs["local_path"]))
            raise RuntimeError("DocuBot unavailable")

        monkeypatch.setattr(module, "ingest_document_to_docubot", failing_ingest)

        reply = _run(module._handle_ingest("will fail"))

        assert "failed" in reply
        assert "DocuBot unavailable" in reply
        assert len(paths) == 1
        assert not paths[0].exists()

    def test_register_registers_ingest_command(self):
        module = _load_plugin_init()
        registered = {}

        class Context:
            def register_command(self, name, **kwargs):
                registered["command"] = (name, kwargs)

        module.register(Context())

        assert registered["command"] == (
            "ingest",
            {
                "handler": module._handle_ingest,
                "description": "Ingest pasted text or Markdown into the DocuBot knowledge pipeline.",
                "args_hint": "<content>",
            },
        )

    def test_background_polling_success_sends_second_notice(self, monkeypatch):
        module = _load_plugin_init()
        _set_session(monkeypatch, module, chat_id="chat-99", message_id="msg-99")

        sent_notices = []

        class MockCtx:
            def register_command(self, name, **kwargs):
                pass

            async def send_to_chat(self, chat_id, reply_to, text, platform="telegram"):
                sent_notices.append({
                    "chat_id": chat_id,
                    "reply_to": reply_to,
                    "text": text,
                    "platform": platform,
                })
                return True

        ctx = MockCtx()
        module.register(ctx)

        monkeypatch.setattr(module, "ingest_document_to_docubot", lambda **kw: {"job_id": "job-success-1", "status": "accepted"})
        monkeypatch.setattr(module, "POLL_INTERVAL_SEC", 0.01)

        poll_calls = []

        def fake_status(job_id):
            poll_calls.append(job_id)
            if len(poll_calls) == 1:
                return {"status": "processing"}
            return {"status": "completed"}

        monkeypatch.setattr(module, "get_docubot_job_status", fake_status)

        async def run_test():
            first_reply = await module._handle_ingest("Content for success test")
            assert "accepted" in first_reply
            await asyncio.sleep(0.05)

        _run(run_test())

        assert poll_calls == ["job-success-1", "job-success-1"]
        assert len(sent_notices) == 1
        assert sent_notices[0]["chat_id"] == "chat-99"
        assert sent_notices[0]["reply_to"] == "msg-99"
        assert "completed" in sent_notices[0]["text"].lower()
        assert "job-success-1" in sent_notices[0]["text"]

    def test_background_polling_failure_sends_second_notice(self, monkeypatch):
        module = _load_plugin_init()
        _set_session(monkeypatch, module, chat_id="chat-88", message_id="msg-88")

        sent_notices = []

        class MockCtx:
            def register_command(self, name, **kwargs):
                pass

            async def send_to_chat(self, chat_id, reply_to, text, platform="telegram"):
                sent_notices.append({
                    "chat_id": chat_id,
                    "reply_to": reply_to,
                    "text": text,
                })
                return True

        module.register(MockCtx())

        monkeypatch.setattr(module, "ingest_document_to_docubot", lambda **kw: {"job_id": "job-fail-1", "status": "accepted"})
        monkeypatch.setattr(module, "POLL_INTERVAL_SEC", 0.01)
        monkeypatch.setattr(
            module,
            "get_docubot_job_status",
            lambda job_id: {
                "status": "failed",
                "error_code": "processing_failed",
                "error_message": "OCR quality check failed",
            },
        )

        async def run_test():
            first_reply = await module._handle_ingest("Content for fail test")
            assert "accepted" in first_reply
            await asyncio.sleep(0.05)

        _run(run_test())

        assert len(sent_notices) == 1
        assert sent_notices[0]["chat_id"] == "chat-88"
        assert sent_notices[0]["reply_to"] == "msg-88"
        assert "failed" in sent_notices[0]["text"].lower()
        assert "OCR quality check failed" in sent_notices[0]["text"]
        assert "job-fail-1" in sent_notices[0]["text"]

    def test_background_polling_timeout_sends_second_notice(self, monkeypatch):
        module = _load_plugin_init()
        _set_session(monkeypatch, module, chat_id="chat-77", message_id="msg-77")

        sent_notices = []

        class MockCtx:
            def register_command(self, name, **kwargs):
                pass

            async def send_to_chat(self, chat_id, reply_to, text, platform="telegram"):
                sent_notices.append({
                    "chat_id": chat_id,
                    "reply_to": reply_to,
                    "text": text,
                })
                return True

        module.register(MockCtx())

        monkeypatch.setattr(module, "ingest_document_to_docubot", lambda **kw: {"job_id": "job-timeout-1", "status": "accepted"})
        monkeypatch.setattr(module, "POLL_INTERVAL_SEC", 0.001)
        monkeypatch.setattr(module, "POLL_TIMEOUT_SEC", 0.01)
        monkeypatch.setattr(module, "get_docubot_job_status", lambda job_id: {"status": "processing"})

        async def run_test():
            first_reply = await module._handle_ingest("Content for timeout test")
            assert "accepted" in first_reply
            await asyncio.sleep(0.05)

        _run(run_test())

        assert len(sent_notices) == 1
        assert sent_notices[0]["chat_id"] == "chat-77"
        assert sent_notices[0]["reply_to"] == "msg-77"
        assert "timed out" in sent_notices[0]["text"].lower()
        assert "job-timeout-1" in sent_notices[0]["text"]


