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
    assert "X/Twitter" in sent["content"]
    assert (
        runner._pending_last30days_by_session[session_key]["topic"]
        == "Hermes Telegram applications"
    )
    assert runner._pending_last30days_by_session[session_key]["step"] == "choose_mode"


@pytest.mark.asyncio
async def test_last30days_quick_choice_runs_direct_engine(monkeypatch):
    runner = _make_runner()
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._pending_last30days_by_session[session_key] = {
        "source": source,
        "topic": "AI Agent 應用",
        "step": "choose_mode",
        "created_at": 1.0,
    }
    notices = []
    calls = []

    async def fake_notice(src, content):
        notices.append((src, content))

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "engine report"

    monkeypatch.setattr(runner, "_deliver_platform_notice", fake_notice)
    monkeypatch.setattr(runner, "_run_last30days_telegram_report", fake_run)

    result = await runner._handle_pending_last30days_choice(_text_event("1"))

    assert result == ""
    assert calls == [{"topic": "AI Agent 應用", "mode": "quick"}]
    assert "開始直接查詢「AI Agent 應用」" in notices[0][1]
    assert notices[1][1] == "engine report"
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
    assert "x = X/Twitter" in notices[-1][1]
    assert "g = GitHub" in notices[-1][1]

    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "source report"

    monkeypatch.setattr(runner, "_run_last30days_telegram_report", fake_run)

    second = await runner._handle_pending_last30days_choice(_text_event("r,g,h"))

    assert second == ""
    assert calls == [
        {
            "topic": "Hermes gateway",
            "mode": "quick",
            "sources": ["reddit", "github", "hackernews"],
        }
    ]
    assert notices[-1][1] == "source report"
    assert session_key not in runner._pending_last30days_by_session


def test_last30days_source_code_parser_dedupes_and_maps_web_to_grounding():
    sources = GatewayRunner._parse_last30days_source_codes("r, reddit, x, w, web, p")

    assert sources == ["reddit", "x", "grounding", "polymarket"]


def test_last30days_engine_output_cleanup_removes_bonus_and_ansi():
    raw = (
        "\x1b[95mProcessing\x1b[0m research\n"
        "Bonus: TikTok and Instagram are available with a free key.\n"
        "# Production Brief: 漁電共生\n"
        "- Sources: reddit, x\n"
    )

    cleaned = GatewayRunner._clean_last30days_engine_output(raw)

    assert "\x1b" not in cleaned
    assert "TikTok" not in cleaned
    assert "Instagram" not in cleaned
    assert "Production Brief" in cleaned
