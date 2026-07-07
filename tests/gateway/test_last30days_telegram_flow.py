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
    runner._pending_last30days_by_session = {}
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="273403055",
        chat_type="dm",
        user_id="42",
        user_name="Keven",
    )


def _text_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
    )


@pytest.mark.asyncio
async def test_last30days_prompt_stores_topic_and_shows_three_options(monkeypatch):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    sent = {}

    async def fake_notice(src, content):
        sent["source"] = src
        sent["content"] = content

    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)

    await runner._prompt_for_last30days_mode(
        source=source,
        session_key=session_key,
        topic="Hermes Telegram applications",
    )

    assert sent["source"] == source
    assert "要怎麼查「Hermes Telegram applications」" in sent["content"]
    assert "1. 快速摘要" in sent["content"]
    assert "2. 深入整理" in sent["content"]
    assert "3. 指定來源" in sent["content"]
    assert (
        runner._pending_last30days_by_session[session_key]["topic"]
        == "Hermes Telegram applications"
    )
    assert runner._pending_last30days_by_session[session_key]["step"] == "choose_mode"


@pytest.mark.asyncio
async def test_last30days_quick_choice_rewrites_to_quick_skill_invocation(monkeypatch):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._pending_last30days_by_session[session_key] = {
        "source": source,
        "topic": "AI Agent 應用",
        "step": "choose_mode",
        "created_at": 1.0,
    }

    async def fail_notice(*_args, **_kwargs):
        pytest.fail("valid quick choice should not send another prompt")

    monkeypatch.setattr(runner, "_deliver_platform_notice", fail_notice)

    event = _text_event("1")
    result = await runner._handle_pending_last30days_choice(event)

    assert result == "/last30days AI Agent 應用 --quick --auto-resolve"
    assert getattr(event, "_last30days_choice_resolved") is True
    assert session_key not in runner._pending_last30days_by_session


@pytest.mark.asyncio
async def test_last30days_source_choice_asks_then_rewrites_with_search_sources(
    monkeypatch,
):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._pending_last30days_by_session[session_key] = {
        "source": source,
        "topic": "Hermes gateway",
        "step": "choose_mode",
        "created_at": 1.0,
    }
    notices = []

    async def fake_notice(src, content):
        notices.append((src, content))

    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)

    first = await runner._handle_pending_last30days_choice(_text_event("3"))

    assert first == ""
    assert runner._pending_last30days_by_session[session_key]["step"] == "choose_sources"
    assert "r = Reddit" in notices[-1][1]
    assert "g = GitHub" in notices[-1][1]

    event = _text_event("r,g,h")
    second = await runner._handle_pending_last30days_choice(event)

    assert (
        second
        == "/last30days Hermes gateway --quick --auto-resolve --search=reddit,github,hackernews"
    )
    assert getattr(event, "_last30days_choice_resolved") is True
    assert session_key not in runner._pending_last30days_by_session


def test_last30days_source_code_parser_dedupes_and_maps_web_to_grounding():
    sources = GatewayRunner._parse_last30days_source_codes("r, reddit, w, web, p")

    assert sources == ["reddit", "grounding", "polymarket"]
