"""Regression test: vLLM step3p5 reasoning-parser null-content fallback.

When a call is sent with extra_body.chat_template_kwargs.enable_thinking
false, vLLM's step3p5 reasoning-parser fallback logic
(vllm/reasoning/basic_parsers.py: "if end_token not in model_output:
return model_output, None") misclassifies a short answer with no
<think>/</think> tags as pure reasoning. The API response comes back with
content=null and the real answer sitting in reasoning/reasoning_content.

``build_assistant_message`` must promote reasoning into content in that
specific case — but ONLY when we can confirm this call was actually sent
non-thinking via the agent's resolved custom-provider extra_body. A
thinking-mode response with genuinely empty content must NOT be rewritten;
that's a real failure, not evidence the answer is in reasoning.
"""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import build_assistant_message
from agent.conversation_loop import (
    _promote_explicit_non_thinking_reasoning_response,
)
from agent.transports.types import NormalizedResponse
from gateway.run import _merge_gateway_request_overrides


def _make_agent(request_overrides=None):
    agent = SimpleNamespace()
    agent.provider = "custom"
    agent.model = "drafter-active"
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent.request_overrides = request_overrides or {}
    agent._extract_reasoning = lambda msg: (
        getattr(msg, "reasoning", None)
        or getattr(msg, "reasoning_content", None)
    )
    agent._strip_think_blocks = lambda s: s
    agent._needs_thinking_reasoning_pad = lambda: False
    return agent


def _non_thinking_overrides():
    return {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }


def test_null_content_promoted_to_content_when_sent_non_thinking() -> None:
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    assistant_message = SimpleNamespace(
        content=None,
        reasoning_content="4.",
        tool_calls=None,
    )

    msg = build_assistant_message(agent, assistant_message, "stop")

    assert msg["content"] == "4."
    assert msg["reasoning"] is None


def test_promoted_content_passes_through_secret_redaction() -> None:
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    assistant_message = SimpleNamespace(
        content=None,
        reasoning_content="SECRET",
        tool_calls=None,
    )

    with patch("agent.redact.redact_sensitive_text", return_value="[REDACTED]") as redact:
        msg = build_assistant_message(agent, assistant_message, "stop")

    redact.assert_called_once_with("SECRET")
    assert msg["content"] == "[REDACTED]"
    assert msg["reasoning"] is None


def test_empty_content_not_rewritten_when_thinking_not_disabled() -> None:
    """A genuine thinking-mode empty-content response must NOT be papered over —
    without confirmation the request was sent non-thinking, an empty content
    is a real failure and should surface as one."""
    agent = _make_agent(request_overrides={})
    assistant_message = SimpleNamespace(
        content=None,
        reasoning_content="some thinking trace",
        tool_calls=None,
    )

    msg = build_assistant_message(agent, assistant_message, "stop")

    assert not msg["content"]
    assert msg["reasoning"] == "some thinking trace"


def test_normal_content_untouched_when_sent_non_thinking() -> None:
    """The fallback must not fire at all when content is already present."""
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    assistant_message = SimpleNamespace(
        content="4.",
        reasoning_content=None,
        tool_calls=None,
    )

    msg = build_assistant_message(agent, assistant_message, "stop")

    assert msg["content"] == "4."
    assert msg["reasoning"] is None


def test_normalized_response_promoted_before_empty_response_handling() -> None:
    """The live loop must see content before entering thinking-prefill."""
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    response = NormalizedResponse(
        content=None,
        tool_calls=None,
        finish_reason="stop",
        reasoning="4.",
    )

    promoted = _promote_explicit_non_thinking_reasoning_response(agent, response)

    assert promoted is True
    assert response.content == "4."
    assert response.reasoning is None


def test_normalized_response_keeps_genuine_thinking_mode_empty() -> None:
    agent = _make_agent(request_overrides={})
    response = NormalizedResponse(
        content=None,
        tool_calls=None,
        finish_reason="stop",
        reasoning="some thinking trace",
    )

    promoted = _promote_explicit_non_thinking_reasoning_response(agent, response)

    assert promoted is False
    assert response.content is None
    assert response.reasoning == "some thinking trace"


def test_normalized_reasoning_content_is_promoted_and_cleared() -> None:
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    response = NormalizedResponse(
        content=None,
        tool_calls=None,
        finish_reason="stop",
        provider_data={"reasoning_content": "answer"},
    )

    promoted = _promote_explicit_non_thinking_reasoning_response(agent, response)

    assert promoted is True
    assert response.content == "answer"
    assert response.reasoning_content is None


def test_normalized_promotion_passes_through_secret_redaction() -> None:
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    response = NormalizedResponse(
        content=None,
        tool_calls=None,
        finish_reason="stop",
        reasoning="SECRET",
    )

    with patch("agent.redact.redact_sensitive_text", return_value="[REDACTED]") as redact:
        promoted = _promote_explicit_non_thinking_reasoning_response(agent, response)

    assert promoted is True
    redact.assert_called_once_with("SECRET")
    assert response.content == "[REDACTED]"
    assert response.reasoning is None


def test_real_conversation_loop_promotes_before_thinking_prefill(
    tmp_path, monkeypatch
) -> None:
    """Exercise the real no-tool final-response control flow.

    This is the gap the original builder-only tests missed: the loop used to
    decide that raw content=null was thinking-only before the builder could
    promote the answer.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")

    from run_agent import AIAgent

    agent = AIAgent(
        model="drafter-active",
        api_key="no-key-required",
        base_url="https://example.invalid/v1",
        provider="custom",
        api_mode="chat_completions",
        request_overrides=_non_thinking_overrides(),
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        platform="cli",
    )
    agent._disable_streaming = True
    calls = 0

    def _reasoning_only(_api_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    reasoning="4.",
                    reasoning_content=None,
                    reasoning_details=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )],
            usage=None,
            model="drafter-active",
        )

    monkeypatch.setattr(agent, "_interruptible_api_call", _reasoning_only)
    try:
        result = agent.run_conversation("2+2?")
    finally:
        agent.close()

    assert result["final_response"] == "4."
    assert result.get("failed", False) is False
    assert calls == 1
    assert agent._thinking_prefill_retries == 0


def test_gateway_turn_overrides_preserve_custom_provider_non_thinking_flag() -> None:
    provider_overrides = _non_thinking_overrides()
    turn_overrides = {"service_tier": "priority"}

    merged = _merge_gateway_request_overrides(provider_overrides, turn_overrides)

    assert merged == {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
        },
        "service_tier": "priority",
    }
    assert provider_overrides == _non_thinking_overrides()


def test_gateway_turn_without_fast_mode_keeps_provider_overrides_only() -> None:
    provider_overrides = _non_thinking_overrides()

    merged = _merge_gateway_request_overrides(provider_overrides, {})

    assert merged == provider_overrides


def test_gateway_turn_runner_preserves_provider_override_at_api_call() -> None:
    """Exercise the real TurnRunner agent lifecycle, not only the merge helper.

    ``AIAgent`` normally merges the custom provider's ``extra_body`` during
    construction.  This stub mirrors that boundary, then records the final
    overrides visible when ``run_conversation`` makes the API call.
    """
    from gateway.config import Platform
    from gateway.run import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    class _CapturingAgent:
        last_instance = None

        def __init__(self, **kwargs):
            type(self).last_instance = self
            self.model = kwargs["model"]
            self.session_id = kwargs["session_id"]
            self.tools = []
            self.constructor_request_overrides = kwargs.get("request_overrides")
            self.request_overrides = _merge_gateway_request_overrides(
                _non_thinking_overrides(),
                self.constructor_request_overrides,
            )
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=65_536,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.seen_request_overrides = []
            self._fallback_activated = False

        def _restore_primary_runtime(self):
            return False

        def run_conversation(self, _message, **_kwargs):
            self.seen_request_overrides.append(copy.deepcopy(self.request_overrides))
            return {
                "final_response": "4.",
                "failed": False,
                "messages": [],
            }

    gateway_runner = MagicMock()
    gateway_runner.config = SimpleNamespace(streaming=None)
    gateway_runner._provider_routing = {}
    gateway_runner._agent_cache_lock = threading.Lock()
    gateway_runner._agent_cache = {}
    gateway_runner._session_db = None
    gateway_runner._prefill_messages = None
    gateway_runner._pending_model_notes = {}
    gateway_runner._pending_skills_reload_notes = {}
    gateway_runner.session_store._entries = {}
    gateway_runner._get_system_prompt_for_channel.return_value = None
    gateway_runner._resolve_session_agent_runtime.return_value = (
        "drafter-active",
        {},
    )
    gateway_runner._resolve_session_reasoning_config.return_value = None
    gateway_runner._resolve_session_service_tier.return_value = None
    gateway_runner._resolve_turn_agent_config.return_value = {
        "model": "drafter-active",
        "runtime": {},
        "request_overrides": {"service_tier": "priority"},
    }
    gateway_runner._agent_config_signature.return_value = ("test-signature",)
    gateway_runner._extract_cache_busting_config.return_value = {}
    gateway_runner._refresh_fallback_model.return_value = None
    gateway_runner._consume_pending_native_image_paths.return_value = []
    gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
    gateway_runner._is_telegram_topic_lane.return_value = False
    gateway_runner._is_discord_auto_thread_lane.return_value = False
    gateway_runner._is_relay_discord_channel_lane.return_value = False

    ctx = TurnContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="test-chat",
            user_id="test-user",
        ),
        message="2+2?",
        history=[],
        session_id="test-session",
        session_key="test-session-key",
        user_config={},
        AIAgent=_CapturingAgent,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )

    result = TurnRunner(gateway_runner, ctx).run_sync()

    assert result["final_response"] == "4."
    agent = _CapturingAgent.last_instance
    assert agent.constructor_request_overrides is None
    assert agent.seen_request_overrides == [{
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
        },
        "service_tier": "priority",
    }]

    # Reuse the same cached agent on a normal turn.  The transient fast-mode
    # option must disappear while the provider's non-thinking flag remains.
    gateway_runner._resolve_turn_agent_config.return_value = {
        "model": "drafter-active",
        "runtime": {},
        "request_overrides": {},
    }
    ctx.message = "and again"
    second_result = TurnRunner(gateway_runner, ctx).run_sync()

    assert second_result["final_response"] == "4."
    assert _CapturingAgent.last_instance is agent
    assert agent.seen_request_overrides == [
        {
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": False},
            },
            "service_tier": "priority",
        },
        {
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
    ]

    # Simulate a cross-provider fallback that remains active on the next
    # cached gateway turn because the primary is still in cooldown. The
    # primary's non-thinking flag must not be overlaid onto that fallback.
    agent._fallback_activated = True
    agent._gateway_active_provider_request_overrides = {}
    gateway_runner._resolve_turn_agent_config.return_value = {
        "model": "drafter-active",
        "runtime": {},
        "request_overrides": {},
    }
    ctx.message = "fallback still cooling down"

    third_result = TurnRunner(gateway_runner, ctx).run_sync()

    assert third_result["final_response"] == "4."
    assert agent.seen_request_overrides[-1] == {}
