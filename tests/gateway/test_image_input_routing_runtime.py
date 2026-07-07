import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")}
    )
    runner.adapters = {}
    runner._pending_native_image_paths_by_session = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_image_ocr_by_session = {}
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="273403055",
        chat_type="dm",
        user_id="42",
        user_name="Maxim",
    )


def _image_event(text: str = "look") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO,
        source=_source(),
        media_urls=["/tmp/cashback.png"],
        media_types=["image/png"],
    )


def _auto_config() -> dict:
    return {
        "agent": {"image_input_mode": "auto"},
        "auxiliary": {"vision": {"provider": "auto", "model": "", "base_url": ""}},
        "model": {"provider": "xiaomi", "default": "mimo-v2.5-pro"},
    }


def test_pre_turn_named_custom_provider_identity_selects_vision_override(monkeypatch):
    """Gateway preprocessing must use the name retained by runtime resolution."""
    runner = _make_runner()
    cfg = {
        "agent": {"image_input_mode": "auto"},
        "model": {"provider": "default-proxy", "default": "shared-model"},
        "custom_providers": [
            {
                "name": "default-proxy",
                "models": {"shared-model": {"supports_vision": False}},
            },
            {
                "name": "vision-provider",
                "models": {"shared-model": {"supports_vision": True}},
            },
        ],
    }
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_: (
            "shared-model",
            {
                "provider": "custom",
                "requested_provider": "vision-provider",
            },
        ),
    )

    assert runner._decide_image_input_mode(
        source=_source(),
        user_config=cfg,
    ) == "native"


@pytest.mark.asyncio
async def test_prepare_route_identity_check_keeps_event_loop_responsive(monkeypatch):
    """A slow route-identity check must not block gateway heartbeats."""
    import asyncio
    import threading
    from types import SimpleNamespace

    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="inspect @AGENTS.md",
        message_type=MessageType.TEXT,
        source=source,
    )
    started = threading.Event()
    released_by_event_loop = threading.Event()
    seen = {}
    main_thread = threading.current_thread()

    cfg = {
        "model": {
            "default": "test-model",
            "provider": "test-provider",
            "base_url": "https://example.invalid/v1",
            "context_length": 128000,
        }
    }
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_kwargs: (
            "test-model",
            {
                "provider": "test-provider",
                "base_url": "https://example.invalid/v1",
                "api_key": "",
            },
        ),
    )

    def blocking_route_identity_check(*_args):
        seen["thread"] = threading.current_thread()
        started.set()
        seen["event_loop_progressed"] = released_by_event_loop.wait(timeout=2)
        return False

    monkeypatch.setattr(
        "hermes_cli.route_identity.should_clear_context_pin",
        blocking_route_identity_check,
    )

    async def fake_context_length(*_args, **_kwargs):
        return 128000

    async def fake_preprocess(message, **_kwargs):
        return SimpleNamespace(
            blocked=False,
            expanded=False,
            message=message,
            warnings=[],
        )

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length_async", fake_context_length
    )
    monkeypatch.setattr(
        "agent.context_references.preprocess_context_references_async",
        fake_preprocess,
    )

    async def heartbeat_ticker():
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        released_by_event_loop.set()

    heartbeat = asyncio.create_task(heartbeat_ticker())
    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[]
    )
    await heartbeat

    assert result == "inspect @AGENTS.md"
    assert seen["event_loop_progressed"] is True
    assert seen["thread"] is not main_thread


@pytest.mark.asyncio
async def test_telegram_image_ocr_translate_preempts_native_routing(monkeypatch):
    """Configured Telegram OCR should produce text even for vision-capable models."""
    runner = _make_runner()
    source = _source()
    event = _image_event("翻譯這張圖")
    cfg = _auto_config()
    cfg["gateway"] = {
        "image_ocr_translate": {
            "enabled": True,
            "platforms": ["telegram"],
            "target_language": "Traditional Chinese",
        }
    }

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("agent.auxiliary_client._read_main_provider", lambda: "openai-codex")
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "gpt-5.5")
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **_: ("gpt-5.5", {"provider": "openai-codex"}),
    )
    monkeypatch.setattr("agent.image_routing._lookup_supports_vision", lambda *_: True)

    async def fake_enrich(user_text, image_paths, *, ocr_translate=False):
        assert user_text == "翻譯這張圖"
        assert image_paths == ["/tmp/cashback.png"]
        assert ocr_translate is True
        return "[ocr translated]

翻譯這張圖"

    monkeypatch.setattr(runner, "_enrich_message_with_vision", fake_enrich)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    session_key = runner._session_key_for_source(source)
    assert result == "[ocr translated]

翻譯這張圖"
    assert runner._pending_native_image_paths_by_session.get(session_key) is None

@pytest.mark.asyncio
async def test_telegram_image_only_ocr_prompts_for_purpose(monkeypatch):
    runner = _make_runner()
    source = _source()
    event = _image_event("")
    cfg = _auto_config()
    cfg["gateway"] = {
        "image_ocr_translate": {
            "enabled": True,
            "platforms": ["telegram"],
            "target_language": "Traditional Chinese",
        }
    }

    sent = {}

    async def fake_notice(src, content):
        sent["source"] = src
        sent["content"] = content

    async def fail_enrich(*_args, **_kwargs):
        pytest.fail("upload should only ask for purpose, not OCR immediately")

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)
    monkeypatch.setattr(runner, "_enrich_message_with_vision", fail_enrich)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    session_key = runner._session_key_for_source(source)
    assert result is None
    assert sent["source"] == source
    assert "1. OCR + 整理文字" in sent["content"]
    assert "2. 整理名片" in sent["content"]
    assert "3. 整理新聞" in sent["content"]
    assert runner._pending_image_ocr_by_session[session_key]["image_paths"] == ["/tmp/cashback.png"]


@pytest.mark.asyncio
async def test_telegram_image_choice_news_uses_tesseract_and_skips_vision(monkeypatch):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._pending_image_ocr_by_session[session_key] = {
        "source": source,
        "image_paths": ["/tmp/news.png"],
        "created_at": 1.0,
    }
    event = MessageEvent(
        text="3",
        message_type=MessageType.TEXT,
        source=source,
    )
    sent = {}

    async def fail_enrich(*_args, **_kwargs):
        pytest.fail("vision fallback should not run when Tesseract returns text")

    async def fake_direct_reply(src, enriched_text, *, already_formatted=False):
        sent["source"] = src
        sent["reply"] = enriched_text
        sent["already_formatted"] = already_formatted

    monkeypatch.setattr(runner, "_extract_images_text_with_tesseract", lambda paths: "台積電新聞標題")
    monkeypatch.setattr(runner, "_enrich_message_with_vision", fail_enrich)
    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)

    result = await runner._handle_pending_image_ocr_choice(event)

    assert result is None
    assert sent["source"] == source
    assert sent["already_formatted"] is True
    assert "新聞 OCR / 整理" in sent["reply"]
    assert "台積電新聞標題" in sent["reply"]
    assert "未呼叫 LLM" in sent["reply"]
    assert session_key not in runner._pending_image_ocr_by_session
