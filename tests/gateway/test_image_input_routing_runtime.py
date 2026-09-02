import json
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
    ):
        self.duplicate = duplicate
        self.fail = fail
        self.upload_status = upload_status
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
                {"id": "new-page-123", "url": "https://www.notion.so/new-card"}
            )
        raise AssertionError(f"unexpected Notion URL: {url}")


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
    client = _FakeNotionClient()
    fields, image_path, hermes_home = _patch_business_card_dependencies(
        monkeypatch, tmp_path, client
    )
    ocr_text = "王小明 綠能科技 02-1234-5678 ming@example.com"

    reply = await runner._process_business_card_ocr(ocr_text, [str(image_path)])

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

    reply = await runner._process_business_card_ocr(
        "王小明 綠能科技 02-1234-5678 ming@example.com", [str(image_path)]
    )

    record_path = next((hermes_home / "namecards" / "records").rglob("*.md"))
    local_image = next((hermes_home / "namecards" / "images").iterdir())
    assert fields["姓名"] in record_path.read_text(encoding="utf-8")
    assert local_image.read_bytes() == b"fake business card image"
    assert str(record_path) in reply
    assert "⚠️ Google Drive 備份失敗" in reply
    assert "https://www.notion.so/new-card" in reply


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
async def test_extract_business_card_fields_raises_on_malformed_json(monkeypatch):
    runner = _make_runner()

    async def fake_call_llm(**_kwargs):
        return _auxiliary_response("{not valid json")

    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake_call_llm)

    with pytest.raises(ValueError, match="invalid JSON"):
        await runner._extract_business_card_fields("王小明 綠能科技")
