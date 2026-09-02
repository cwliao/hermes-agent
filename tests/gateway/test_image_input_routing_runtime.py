import json
import time
from pathlib import Path
from types import SimpleNamespace

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
        return "[ocr translated]\n\n翻譯這張圖"

    monkeypatch.setattr(runner, "_enrich_message_with_vision", fake_enrich)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    session_key = runner._session_key_for_source(source)
    assert result == "[ocr translated]\n\n翻譯這張圖"
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
    assert result == ""
    assert sent["source"] == source
    assert "1. OCR + 整理文字" in sent["content"]
    assert "2. 整理名片" in sent["content"]
    assert "3. 整理新聞" in sent["content"]
    assert runner._pending_image_ocr_by_session[session_key]["image_paths"] == ["/tmp/cashback.png"]


@pytest.mark.asyncio
async def test_image_ocr_purpose_appends_later_media_group_before_choice(monkeypatch):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    notices = []

    async def fake_notice(_source, content):
        notices.append(content)

    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)

    await runner._prompt_for_image_ocr_purpose(
        source=source,
        session_key=session_key,
        image_paths=["/tmp/album-1-a.jpg", "/tmp/album-1-b.jpg"],
    )
    await runner._prompt_for_image_ocr_purpose(
        source=source,
        session_key=session_key,
        image_paths=["/tmp/album-2-a.jpg"],
    )

    assert runner._pending_image_ocr_by_session[session_key]["image_paths"] == [
        "/tmp/album-1-a.jpg",
        "/tmp/album-1-b.jpg",
        "/tmp/album-2-a.jpg",
    ]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_image_ocr_purpose_replaces_stale_pending_batch_and_resends_choice(
    monkeypatch,
):
    from gateway.run import _IMAGE_OCR_CHOICE_TTL_SECS

    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._pending_image_ocr_by_session[session_key] = {
        "source": source,
        "image_paths": ["/tmp/stale-card.jpg"],
        "created_at": time.time() - _IMAGE_OCR_CHOICE_TTL_SECS - 1,
    }
    notices = []

    async def fake_notice(_source, content):
        notices.append(content)

    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)

    await runner._prompt_for_image_ocr_purpose(
        source=source,
        session_key=session_key,
        image_paths=["/tmp/fresh-card.jpg"],
    )

    pending = runner._pending_image_ocr_by_session[session_key]
    assert pending["image_paths"] == ["/tmp/fresh-card.jpg"]
    assert pending["created_at"] > time.time() - _IMAGE_OCR_CHOICE_TTL_SECS
    assert len(notices) == 1
    assert "請選擇這張圖片要怎麼處理" in notices[0]


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

    async def fake_news_reply(ocr_text):
        return f"📰 新聞 OCR / 整理\n\n修正版：{ocr_text}"

    monkeypatch.setattr(runner, "_extract_images_text_with_tesseract", lambda paths: "台積電新聞標題")
    monkeypatch.setattr(runner, "_format_news_ocr_reply", fake_news_reply)
    monkeypatch.setattr(runner, "_enrich_message_with_vision", fail_enrich)
    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)

    result = await runner._handle_pending_image_ocr_choice(event)

    assert result == ""
    assert sent["source"] == source
    assert sent["already_formatted"] is True
    assert "新聞 OCR / 整理" in sent["reply"]
    assert "台積電新聞標題" in sent["reply"]
    assert session_key not in runner._pending_image_ocr_by_session


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["1", "2", "3"])
async def test_expired_image_choice_replies_instead_of_falling_through(monkeypatch, choice):
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text=choice,
        message_type=MessageType.TEXT,
        source=source,
    )
    sent = {}

    async def fake_direct_reply(src, enriched_text, *, already_formatted=False):
        sent["source"] = src
        sent["reply"] = enriched_text
        sent["already_formatted"] = already_formatted

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)

    result = await runner._handle_pending_image_ocr_choice(event)

    assert result == ""
    assert sent == {
        "source": source,
        "reply": "圖片選單已過期，麻煩重新傳一次圖片。",
        "already_formatted": True,
    }


@pytest.mark.asyncio
async def test_image_choice_falls_through_when_last30days_choice_is_pending(monkeypatch):
    runner = _make_runner()
    source = _source()
    runner._pending_last30days_choices()[runner._last30days_choice_key(source)] = {
        "source": source,
        "topic": "AI agent",
        "step": "choose_mode",
        "created_at": 1.0,
    }
    event = MessageEvent(
        text="1",
        message_type=MessageType.TEXT,
        source=source,
    )

    async def fail_direct_reply(*_args, **_kwargs):
        pytest.fail("last30days choices must not receive an expired OCR reply")

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fail_direct_reply)

    assert await runner._handle_pending_image_ocr_choice(event) is None


@pytest.mark.asyncio
async def test_non_choice_message_still_falls_through_when_image_choice_is_missing(monkeypatch):
    runner = _make_runner()
    event = MessageEvent(
        text="這是一則普通訊息",
        message_type=MessageType.TEXT,
        source=_source(),
    )

    async def fail_direct_reply(*_args, **_kwargs):
        pytest.fail("non-choice messages must not receive an expired-menu reply")

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fail_direct_reply)

    assert await runner._handle_pending_image_ocr_choice(event) is None


@pytest.mark.asyncio
async def test_news_ocr_postprocess_normalizes_simplified_chinese(monkeypatch):
    runner = _make_runner()

    class _Resp:
        choices = [type("Choice", (), {"message": type("Message", (), {"content": "导入数位变生，环境部发表会议"})()})()]

    async def fake_call_llm(**kwargs):
        assert kwargs["task"] == "title_generation"
        assert kwargs["temperature"] == 0.0
        assert "不得輸出簡體字" in kwargs["messages"][0]["content"]
        return _Resp()

    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake_call_llm)

    out = await runner._format_news_ocr_reply("导入数位变生，环境部发表会议")

    assert "導入數位變生" in out
    assert "環境部發表會議" in out
    assert "导" not in out
    assert "环境" not in out


def _business_card_fields() -> dict[str, str]:
    return {
        "姓名": "王小明",
        "公司名稱": "綠能科技",
        "電話": "02-1234-5678",
        "手機": "0912-345-678",
        "Email": "ming@example.com",
        "傳真": "",
        "地址": "台北市信義區松仁路100號",
        "統編": "12345678",
        "備註": "業務經理",
    }


def _auxiliary_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeNotionResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeNotionClient:
    def __init__(
        self,
        *,
        duplicate: bool = False,
        fail: bool = False,
        upload_status: str = "uploaded",
        page_id: str = "new-page-123",
    ):
        self.duplicate = duplicate
        self.fail = fail
        self.upload_status = upload_status
        self.page_id = page_id
        self.calls: list[dict] = []
        self.sent_file_bytes = None

    async def __aenter__(self) -> "_FakeNotionClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> _FakeNotionResponse:
        self.calls.append({"url": url, **kwargs})
        if self.fail:
            raise RuntimeError("simulated Notion network failure")
        if url.endswith("/query"):
            results = []
            if self.duplicate:
                results = [
                    {
                        "url": "https://www.notion.so/existing-card",
                        "properties": {
                            "名片名稱": {
                                "title": [{"plain_text": "既有客戶名片"}]
                            }
                        },
                    }
                ]
            return _FakeNotionResponse({"results": results})
        if url.endswith("/file_uploads"):
            return _FakeNotionResponse(
                {
                    "id": "file-upload-123",
                    "status": self.upload_status,
                    "upload_url": "https://uploads.example.com/file-upload-123",
                }
            )
        if url == "https://uploads.example.com/file-upload-123":
            file_tuple = kwargs["files"]["file"]
            self.sent_file_bytes = file_tuple[1].read()
            return _FakeNotionResponse({"status": "uploaded"})
        if url.endswith("/pages"):
            return _FakeNotionResponse(
                {"id": self.page_id, "url": "https://www.notion.so/new-card"}
            )
        raise AssertionError(f"unexpected Notion URL: {url}")


class _FakeCorrectionNotionClient:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_FakeCorrectionNotionClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def patch(self, url: str, **kwargs: object) -> _FakeNotionResponse:
        self.calls.append({"url": url, **kwargs})
        if self.fail:
            import httpx

            request = httpx.Request("PATCH", url)
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError(
                "simulated Notion PATCH failure", request=request, response=response
            )
        return _FakeNotionResponse({"id": "page-123"})


def _patch_namecard_correction_dependencies(monkeypatch, client, extracted):
    calls = []

    async def fake_call_llm(**kwargs):
        calls.append(kwargs)
        response_format = kwargs["extra_body"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "business_card_correction"
        payload = {
            field: extracted.get(field)
            for field in _business_card_fields()
        }
        return _auxiliary_response(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake_call_llm)
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: client)
    monkeypatch.setenv("NOTION_API_KEY", "test-notion-key")
    return calls


def _set_pending_namecard_correction(runner, tmp_path, *, saved_at=None):
    source = _source()
    record_path = tmp_path / "namecard.md"
    record_path.write_text("原始名片紀錄\n", encoding="utf-8")
    runner._pending_namecard_correction_by_session = {
        runner._image_ocr_choice_key(source): {
            "page_id": "page-123",
            "fields": {"公司名稱": "舊公司", "Email": "old@example.com"},
            "record_path": str(record_path),
            "saved_at": time.time() if saved_at is None else saved_at,
        }
    }
    return source, record_path


def _text_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


def _patch_business_card_dependencies(monkeypatch, tmp_path: Path, client):
    fields = _business_card_fields()
    hermes_home = tmp_path / "hermes-home"
    gdrive_root = tmp_path / "gdrive-namecards"
    image_path = tmp_path / "business-card.png"
    image_path.write_bytes(b"fake business card image")

    async def fake_call_llm(**kwargs):
        assert kwargs["task"] == "title_generation"
        assert kwargs["temperature"] == 0.0
        response_format = kwargs["extra_body"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "business_card_fields"
        assert response_format["json_schema"]["strict"] is True
        assert response_format["json_schema"]["schema"] == {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                field: {"type": ["string", "null"]}
                for field in fields
            },
            "required": list(fields),
        }
        return _auxiliary_response(json.dumps(fields, ensure_ascii=False))

    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake_call_llm)
    monkeypatch.setattr("gateway.run.get_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr("gateway.run._NAMECARD_GDRIVE_DIR", gdrive_root)
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: client)
    monkeypatch.setenv("NOTION_API_KEY", "test-notion-key")
    return fields, image_path, hermes_home


@pytest.mark.asyncio
async def test_business_card_ocr_success_saves_local_record_and_notion_page(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source = _source()
    client = _FakeNotionClient()
    fields, image_path, hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    ocr_text = "王小明 綠能科技 02-1234-5678 ming@example.com"

    reply = await runner._process_business_card_ocr(
        ocr_text, [str(image_path)], source=source
    )

    record_path = next((hermes_home / "namecards" / "records").rglob("*.md"))
    assert fields["姓名"] in reply
    assert fields["公司名稱"] in reply
    assert str(record_path) in reply
    assert "https://www.notion.so/new-card" in reply
    assert ocr_text in record_path.read_text(encoding="utf-8")
    assert [call["url"].rsplit("/", 1)[-1] for call in client.calls] == [
        "query",
        "file_uploads",
        "pages",
    ]

    page_call = next(call for call in client.calls if call["url"].endswith("/pages"))
    page_body = page_call["json"]
    assert page_body["parent"] == {
        "data_source_id": "c13e29d0-66e6-4ec0-9835-39a788617fa3"
    }
    properties = page_body["properties"]
    assert properties["名片名稱"] == {
        "title": [{"type": "text", "text": {"content": "王小明"}}]
    }
    assert properties["公司名稱"] == {
        "rich_text": [{"type": "text", "text": {"content": "綠能科技"}}]
    }
    assert properties["電話"] == {
        "rich_text": [{"type": "text", "text": {"content": "02-1234-5678"}}]
    }
    for field in ("手機", "地址", "統編", "備註"):
        assert properties[field] == {
            "rich_text": [
                {"type": "text", "text": {"content": fields[field]}}
            ]
        }
    assert properties["Email"] == {"email": "ming@example.com"}
    assert "傳真" not in properties
    assert properties["聯繫情形"] == {"select": {"name": "未聯繫"}}
    assert properties["名片圖檔"] == {
        "files": [
            {
                "type": "file_upload",
                "file_upload": {"id": "file-upload-123"},
                "name": "business-card.png",
            }
        ]
    }
    assert runner._pending_namecard_correction_by_session[
        runner._image_ocr_choice_key(source)
    ]["page_id"] == "new-page-123"


@pytest.mark.asyncio
async def test_business_card_batch_saves_each_image_as_separate_notion_page(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source = _source()
    runner._pending_namecard_correction_by_session = {}
    client = _FakeNotionClient()
    _fields, image_path, _hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    image_paths = [
        str(image_path),
        str(image_path.with_name("business-card-2.png")),
    ]
    Path(image_paths[1]).write_bytes(b"second business card image")

    monkeypatch.setattr(
        runner,
        "_extract_images_text_with_tesseract",
        lambda paths: f"OCR for {Path(paths[0]).name}",
    )

    reply = await runner._process_business_card_batch(image_paths, source=source)

    assert "共 2 張" in reply
    assert "成功 2 張" in reply
    assert "失敗 0 張" in reply
    page_calls = [call for call in client.calls if call["url"].endswith("/pages")]
    assert len(page_calls) == 2
    assert [
        call["json"]["properties"]["名片圖檔"]["files"][0]["name"]
        for call in page_calls
    ] == ["business-card.png", "business-card-2.png"]
    assert runner._image_ocr_choice_key(source) not in runner._pending_namecard_correction_by_session


@pytest.mark.asyncio
async def test_business_card_batch_continues_after_one_image_processing_failure(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source = _source()
    runner._pending_namecard_correction_by_session = {}
    client = _FakeNotionClient()
    _fields, image_path, _hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    image_paths = [
        str(image_path.with_name("business-card-1.png")),
        str(image_path.with_name("business-card-2.png")),
        str(image_path.with_name("business-card-3.png")),
    ]
    for path in image_paths:
        Path(path).write_bytes(path.encode())

    processed_paths = []

    def fake_extract(paths):
        processed_paths.append(paths[0])
        if Path(paths[0]).name == "business-card-2.png":
            raise RuntimeError("simulated OCR failure")
        return f"OCR for {Path(paths[0]).name}"

    monkeypatch.setattr(runner, "_extract_images_text_with_tesseract", fake_extract)

    reply = await runner._process_business_card_batch(image_paths, source=source)

    assert "共 3 張" in reply
    assert "成功 2 張" in reply
    assert "失敗 1 張" in reply
    assert "business-card-2.png" in reply
    assert "simulated OCR failure" in reply
    assert "ValueError" not in reply
    assert processed_paths == image_paths
    page_calls = [call for call in client.calls if call["url"].endswith("/pages")]
    assert len(page_calls) == 2
    assert [
        call["json"]["properties"]["名片圖檔"]["files"][0]["name"]
        for call in page_calls
    ] == ["business-card-1.png", "business-card-3.png"]
    assert runner._image_ocr_choice_key(source) not in runner._pending_namecard_correction_by_session


@pytest.mark.asyncio
async def test_business_card_ocr_duplicate_warns_but_still_creates_new_page(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    client = _FakeNotionClient(duplicate=True)
    _fields, image_path, _hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )

    reply = await runner._process_business_card_ocr(
        "王小明 綠能科技 02-1234-5678 ming@example.com", [str(image_path)]
    )

    assert "⚠️ 疑似重複" in reply
    assert "既有客戶名片" in reply
    assert "https://www.notion.so/existing-card" in reply
    assert "https://www.notion.so/new-card" in reply
    assert len([call for call in client.calls if call["url"].endswith("/pages")]) == 1


@pytest.mark.asyncio
async def test_business_card_upload_sends_bytes_when_create_upload_is_pending(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    client = _FakeNotionClient(upload_status="pending")
    _fields, image_path, _hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )

    reply = await runner._process_business_card_ocr(
        "王小明 綠能科技 02-1234-5678 ming@example.com", [str(image_path)]
    )

    assert "https://www.notion.so/new-card" in reply
    assert client.sent_file_bytes == b"fake business card image"
    assert [call["url"] for call in client.calls] == [
        "https://api.notion.com/v1/data_sources/c13e29d0-66e6-4ec0-9835-39a788617fa3/query",
        "https://api.notion.com/v1/file_uploads",
        "https://uploads.example.com/file-upload-123",
        "https://api.notion.com/v1/pages",
    ]


@pytest.mark.asyncio
async def test_business_card_gdrive_failure_does_not_block_local_backup(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    client = _FakeNotionClient()
    fields, image_path, hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    gdrive_root = tmp_path / "gdrive-namecards"
    gdrive_root.write_text("not a directory", encoding="utf-8")
    outcome = {}

    reply = await runner._process_business_card_ocr(
        "王小明 綠能科技 02-1234-5678 ming@example.com",
        [str(image_path)],
        _outcome=outcome,
    )

    record_path = next((hermes_home / "namecards" / "records").rglob("*.md"))
    local_image = next((hermes_home / "namecards" / "images").iterdir())
    assert fields["姓名"] in record_path.read_text(encoding="utf-8")
    assert local_image.read_bytes() == b"fake business card image"
    assert str(record_path) in reply
    assert "⚠️ Google Drive 備份失敗" in reply
    assert "https://www.notion.so/new-card" in reply
    assert outcome == {"success": True, "reason": ""}


@pytest.mark.asyncio
async def test_business_card_empty_notion_page_id_counts_as_failure(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    client = _FakeNotionClient(page_id="")
    _fields, image_path, _hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    outcome = {}

    await runner._process_business_card_ocr(
        "王小明 綠能科技 02-1234-5678 ming@example.com",
        [str(image_path)],
        _outcome=outcome,
    )

    assert outcome == {"success": False, "reason": "Notion 未回傳頁面 ID"}
    assert runner._pending_namecard_correction_choices() == {}


@pytest.mark.asyncio
async def test_business_card_batch_sends_processing_notice_before_work(monkeypatch):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._pending_image_ocr_by_session[session_key] = {
        "source": source,
        "image_paths": ["/tmp/card-1.jpg", "/tmp/card-2.jpg"],
        "created_at": time.time(),
    }
    event = _text_event("2", source)
    notices = []
    calls = []

    async def fake_notice(_source, content):
        notices.append(content)

    async def fake_batch(image_paths, *, source):
        calls.append((image_paths, source))
        return "批次完成"

    async def fake_direct_reply(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)
    monkeypatch.setattr(runner, "_process_business_card_batch", fake_batch)
    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)

    assert await runner._handle_pending_image_ocr_choice(event) == ""
    assert calls == [(["/tmp/card-1.jpg", "/tmp/card-2.jpg"], source)]
    assert notices == ["收到 2 張名片，正在批次處理中，請稍候..."]


@pytest.mark.asyncio
async def test_business_card_ocr_notion_failure_keeps_local_backup_and_ocr(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    client = _FakeNotionClient(fail=True)
    fields, image_path, hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    ocr_text = "王小明 綠能科技 02-1234-5678 ming@example.com"

    reply = await runner._process_business_card_ocr(ocr_text, [str(image_path)])

    record_path = next((hermes_home / "namecards" / "records").rglob("*.md"))
    assert str(record_path) in reply
    assert fields["姓名"] in reply
    assert fields["Email"] in reply
    assert ocr_text in reply
    assert "⚠️ Notion 未寫入" in reply
    assert record_path.is_file()


@pytest.mark.asyncio
async def test_pending_namecard_correction_patches_notion_and_clears_pointer(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source, record_path = _set_pending_namecard_correction(runner, tmp_path)
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(
        monkeypatch,
        client,
        {"公司名稱": "新綠能股份有限公司", "Email": "new@example.com"},
    )
    sent = {}

    async def fake_direct_reply(src, content, *, already_formatted=False):
        sent.update(source=src, reply=content, already_formatted=already_formatted)

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)
    event = _text_event(
        "名片的公司名稱應該改成新綠能股份有限公司，Email 改成 new@example.com",
        source,
    )

    assert await runner._handle_pending_namecard_correction(event) == ""

    assert len(llm_calls) == 1
    assert len(client.calls) == 1
    patch_call = client.calls[0]
    assert patch_call["url"] == "https://api.notion.com/v1/pages/page-123"
    assert patch_call["json"] == {
        "properties": {
            "公司名稱": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": "新綠能股份有限公司"},
                    }
                ]
            },
            "Email": {"email": "new@example.com"},
        }
    }
    assert sent["source"] == source
    assert sent["already_formatted"] is True
    assert "公司名稱：舊公司 → 新綠能股份有限公司" in sent["reply"]
    assert "Email：old@example.com → new@example.com" in sent["reply"]
    assert "https://www.notion.so/page123" in sent["reply"]
    assert runner._pending_namecard_correction_by_session == {}
    assert "## 修正紀錄" in record_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_pending_namecard_correction_requires_field_and_correction_cue(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source, _record_path = _set_pending_namecard_correction(runner, tmp_path)
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(monkeypatch, client, {})
    event = _text_event("這個人的電話我待會會打", source)

    assert await runner._handle_pending_namecard_correction(event) is None
    assert llm_calls == []
    assert client.calls == []
    assert runner._pending_namecard_correction_by_session


@pytest.mark.asyncio
async def test_pending_namecard_correction_ignores_slash_command(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source, _record_path = _set_pending_namecard_correction(runner, tmp_path)
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(
        monkeypatch, client, {"公司名稱": "新公司"}
    )
    event = _text_event("/plan 名片公司名稱改成新公司", source)

    assert await runner._handle_pending_namecard_correction(event) is None
    assert llm_calls == []
    assert client.calls == []
    assert runner._pending_namecard_correction_by_session


@pytest.mark.asyncio
async def test_pending_namecard_name_correction_updates_notion_title(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source, _record_path = _set_pending_namecard_correction(runner, tmp_path)
    runner._pending_namecard_correction_by_session[
        runner._image_ocr_choice_key(source)
    ]["fields"] = {"姓名": "舊姓名", "公司名稱": "舊公司"}
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(
        monkeypatch, client, {"姓名": "新姓名"}
    )

    async def fake_direct_reply(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)
    event = _text_event("名片姓名改成新姓名", source)

    assert await runner._handle_pending_namecard_correction(event) == ""

    assert len(llm_calls) == 1
    assert client.calls[0]["json"] == {
        "properties": {
            "姓名": {
                "rich_text": [
                    {"type": "text", "text": {"content": "新姓名"}}
                ]
            },
            "名片名稱": {
                "title": [{"type": "text", "text": {"content": "新姓名"}}]
            },
        }
    }


@pytest.mark.asyncio
async def test_pending_namecard_correction_ignores_non_correction_message(monkeypatch, tmp_path):
    runner = _make_runner()
    source, _record_path = _set_pending_namecard_correction(runner, tmp_path)
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(monkeypatch, client, {})
    event = _text_event("今天台北天氣不錯", source)

    assert await runner._handle_pending_namecard_correction(event) is None
    assert llm_calls == []
    assert client.calls == []
    assert runner._pending_namecard_correction_by_session


@pytest.mark.asyncio
async def test_namecard_correction_falls_through_without_pending_pointer(monkeypatch):
    runner = _make_runner()
    source = _source()
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(monkeypatch, client, {})
    event = _text_event("請幫我安排明天的會議", source)

    assert await runner._handle_pending_namecard_correction(event) is None
    assert llm_calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_strong_namecard_correction_without_pending_pointer_replies_not_found(
    monkeypatch,
):
    runner = _make_runner()
    source = _source()
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(monkeypatch, client, {})
    sent = {}

    async def fake_direct_reply(src, content, *, already_formatted=False):
        sent.update(source=src, reply=content, already_formatted=already_formatted)

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)
    event = _text_event("名片資料不對，請更正公司名稱", source)

    assert await runner._handle_pending_namecard_correction(event) == ""
    assert sent["source"] == source
    assert sent["already_formatted"] is True
    assert "找不到最近儲存的名片紀錄" in sent["reply"]
    assert llm_calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_namecard_correction_patch_failure_keeps_local_record_unchanged(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source, record_path = _set_pending_namecard_correction(runner, tmp_path)
    original_record = record_path.read_text(encoding="utf-8")
    client = _FakeCorrectionNotionClient(fail=True)
    _patch_namecard_correction_dependencies(
        monkeypatch, client, {"公司名稱": "新公司"}
    )
    sent = {}

    async def fake_direct_reply(src, content, *, already_formatted=False):
        sent.update(source=src, reply=content, already_formatted=already_formatted)

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)
    event = _text_event("名片公司名稱改成新公司", source)

    assert await runner._handle_pending_namecard_correction(event) == ""
    assert "名片修正失敗" in sent["reply"]
    assert "Notion 沒有更新" in sent["reply"]
    assert sent["already_formatted"] is True
    assert record_path.read_text(encoding="utf-8") == original_record
    assert runner._pending_namecard_correction_by_session
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_expired_namecard_correction_pointer_replies_not_found(monkeypatch, tmp_path):
    from gateway.run import _NAMECARD_CORRECTION_TTL_SECS

    runner = _make_runner()
    source, _record_path = _set_pending_namecard_correction(
        runner,
        tmp_path,
        saved_at=time.time() - _NAMECARD_CORRECTION_TTL_SECS - 1,
    )
    client = _FakeCorrectionNotionClient()
    llm_calls = _patch_namecard_correction_dependencies(monkeypatch, client, {})
    sent = {}

    async def fake_direct_reply(src, content, *, already_formatted=False):
        sent.update(source=src, reply=content, already_formatted=already_formatted)

    monkeypatch.setattr(runner, "_deliver_direct_image_ocr_reply", fake_direct_reply)
    event = _text_event("名片資料不對，請改成正確資料", source)

    assert await runner._handle_pending_namecard_correction(event) == ""
    assert "找不到最近儲存的名片紀錄" in sent["reply"]
    assert runner._pending_namecard_correction_by_session == {}
    assert llm_calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_extract_business_card_fields_raises_on_malformed_json(monkeypatch):
    runner = _make_runner()

    async def fake_call_llm(**_kwargs):
        return _auxiliary_response("{not valid json")

    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake_call_llm)

    with pytest.raises(ValueError, match="invalid JSON"):
        await runner._extract_business_card_fields("王小明 綠能科技")
