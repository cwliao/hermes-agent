"""Tests for reply-to pointer injection in _prepare_inbound_message_text.

The `[Replying to: "..."]` prefix is a *disambiguation pointer*, not
deduplication. It must always be injected when the user explicitly replies
to a prior message — even when the quoted text already exists somewhere
in the conversation history. History can contain the same or similar text
multiple times, and without an explicit pointer the agent has to guess
which prior message the user is referencing.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )


@pytest.mark.asyncio
async def test_reply_prefix_injected_when_text_absent_from_history():
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="What's the best time to go?",
        source=source,
        reply_to_message_id="42",
        reply_to_text="Japan is great for culture, food, and efficiency.",
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[{"role": "user", "content": "unrelated"}],
    )

    assert result is not None
    assert result.startswith(
        '[Replying to: "Japan is great for culture, food, and efficiency."]'
    )
    assert result.endswith("What's the best time to go?")


@pytest.mark.asyncio
async def test_telegram_long_reply_reaches_prompt_without_losing_later_items():
    """The native reply already has the full message; preparation must not trim it."""
    from gateway.platforms.event import MessageType
    from tests.gateway.test_telegram_reply_quote import _make_adapter, _make_message

    quoted = "\n".join(
        f"{index}. {company}: " + "Evidence from the supplied list. " * 12
        for index, company in enumerate(
            ["GoCar", "Urban Drive", "DubCar", "GRPS", "Halucar"], 1
        )
    )
    event = _make_adapter()._build_message_event(
        _make_message(text="Review all five companies.", reply_to_text=quoted),
        MessageType.TEXT,
    )
    history = [{"role": "user", "content": "Previous request"}]
    result = await _make_runner()._prepare_inbound_message_text(
        event=event, source=event.source, history=history,
    )
    assert result is not None
    assert quoted in result
    assert result.endswith("Review all five companies.")
    assert history == [{"role": "user", "content": "Previous request"}]


@pytest.mark.asyncio
async def test_quoted_reply_references_stay_literal_while_typed_ones_expand(tmp_path, monkeypatch):
    """The replied-to author's ``@file:`` is quoted text, not the replier's request: no local read.
    The same reference typed in the new message still expands (positive control)."""
    import threading

    payload = tmp_path / "notes.txt"
    payload.write_text("LOCAL-FILE-MARKER", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    runner = _make_runner()
    runner._session_model_overrides, runner._last_resolved_model = {}, {}
    runner._agent_cache, runner._agent_cache_lock = {}, threading.Lock()
    runner._resolve_session_agent_runtime = lambda **kw: ("openai/gpt-4.1-mini", {"base_url": None, "api_key": ""})
    source = _source()

    quoted = ("x " * 300) + f"\nsee @file:{payload.name} for details"
    quoted_ref = MessageEvent(text="what does this say?", source=source, reply_to_message_id="7", reply_to_text=quoted)
    result = await runner._prepare_inbound_message_text(event=quoted_ref, source=source, history=[])
    assert quoted in result
    assert "LOCAL-FILE-MARKER" not in result

    typed_ref = MessageEvent(text=f"read @file:{payload.name}", source=source, reply_to_message_id="7", reply_to_text="short")
    result = await runner._prepare_inbound_message_text(event=typed_ref, source=source, history=[])
    assert result.startswith('[Replying to: "short"]')
    assert "LOCAL-FILE-MARKER" in result


@pytest.mark.asyncio
async def test_reply_prefix_still_injected_when_text_in_history():
    """Regression test: the pointer must survive even when the quoted text
    already appears in history. Previously a `found_in_history` guard
    silently dropped the prefix, leaving the agent to guess which prior
    message the user was referencing."""
    runner = _make_runner()
    source = _source()
    quoted = "Japan is great for culture, food, and efficiency."
    event = MessageEvent(
        text="What's the best time to go?",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
    )

    history = [
        {"role": "user", "content": "I'm thinking of going to Japan or Italy."},
        {
            "role": "assistant",
            "content": (
                f"{quoted} Italy is better if you prefer a relaxed pace."
            ),
        },
        {"role": "user", "content": "How long should I stay?"},
        {"role": "assistant", "content": "For Japan, 10-14 days is ideal."},
    ]

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=history,
    )

    assert result is not None
    assert result.startswith(f'[Replying to: "{quoted}"]')
    assert result.endswith("What's the best time to go?")


@pytest.mark.asyncio
async def test_unmatched_goal_status_reply_falls_through_to_generic_reply_context():
    """An unrelated reply target must survive the goal-status intercept and keep quote context."""
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="please continue",
        source=source,
        reply_to_message_id="random-message-id",
        reply_to_text="A normal bot reply",
        reply_to_is_own_message=True,
    )
    session_key = "agent:main:telegram:123"

    class _NoMatchingNotice:
        def match_status_notice(self, message_id, *, chat_id=None):
            assert message_id == "random-message-id"
            assert chat_id == source.chat_id
            return None

    runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
    runner._hm_estop_gate = lambda event, source, is_internal: None
    runner._session_key_for_source = lambda source: session_key
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._check_slash_access = lambda source, command: None
    runner._get_goal_manager_for_event = AsyncMock(
        return_value=(_NoMatchingNotice(), SimpleNamespace(session_id="unmatched")),
    )

    async def _run_in_executor(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _run_in_executor
    runner._is_pending_image_ocr_choice = lambda event: False
    runner._handle_pending_last30days_choice = AsyncMock(return_value=None)
    runner._handle_pending_namecard_correction = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = lambda key: None
    runner._is_session_running = lambda key: False
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._is_telegram_topic_root_lobby = lambda source: False
    runner._external_drain_active = False
    runner._claim_active_session_slot = lambda key, source: (None, None)
    runner._hm_rescue_orphaned_fifo = lambda event, source, is_internal, key: (
        event, source, is_internal,
    )
    turn_state = SimpleNamespace(turn=SimpleNamespace(lease=None, agent=None, started_ts=None))
    runner._session_state = lambda key: turn_state
    runner._persist_active_agents = lambda: None
    runner._begin_session_run_generation = lambda key: 1

    async def _handle_with_agent(event, source, key, generation):
        return await runner._prepare_inbound_message_text(
            event=event, source=source, history=[], session_key=key,
        )

    runner._handle_message_with_agent = _handle_with_agent
    runner._run_post_turn_hooks = AsyncMock()
    runner._restore_pending_one_turn_model_override = lambda key, generation: None
    runner._clear_durable_active_turn = AsyncMock(return_value=True)
    runner._release_running_agent_state = lambda key, run_generation=None: None
    runner._release_turn_lease = lambda key, generation: None
    runner._consume_pending_native_image_paths = lambda key: []

    result = await runner._handle_message(event)

    assert result == '[Replying to your previous message: "A normal bot reply"]\n\nplease continue'

