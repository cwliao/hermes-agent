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

from types import SimpleNamespace

from agent.chat_completion_helpers import build_assistant_message


def _make_agent(request_overrides=None):
    agent = SimpleNamespace()
    agent.provider = "custom"
    agent.model = "drafter-active"
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent.request_overrides = request_overrides or {}
    agent._extract_reasoning = lambda msg: getattr(msg, "reasoning_content", None)
    agent._strip_think_blocks = lambda s: s
    agent._needs_thinking_reasoning_pad = lambda: False
    return agent


def _non_thinking_overrides():
    return {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }


def test_null_content_promoted_to_reasoning_when_sent_non_thinking() -> None:
    agent = _make_agent(request_overrides=_non_thinking_overrides())
    assistant_message = SimpleNamespace(
        content=None,
        reasoning_content="4.",
        tool_calls=None,
    )

    msg = build_assistant_message(agent, assistant_message, "stop")

    assert msg["content"] == "4."
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
