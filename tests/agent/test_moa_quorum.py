"""Tests for the default degraded quorum used by MoA references."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=None,
    )


def _output(label: str, text: str):
    from agent.moa_loop import _RefAccounting
    from agent.usage_pricing import CanonicalUsage

    return label, text, _RefAccounting(CanonicalUsage())


@pytest.mark.parametrize(
    ("reference_count", "required"),
    [(0, 0), (1, 1), (2, 1), (3, 2), (4, 3), (8, 7)],
)
def test_minimum_reference_successes_uses_degraded_n_minus_one_rule(
    reference_count, required
):
    from agent.moa_loop import _minimum_reference_successes

    assert _minimum_reference_successes(reference_count) == required


def test_empty_reference_output_does_not_count_as_usable():
    from agent.moa_loop import _is_failed_reference

    assert _is_failed_reference("") is True
    assert _is_failed_reference("  \n") is True
    # _run_reference normalizes a provider's empty content to this sentinel
    # before quorum classification.
    assert _is_failed_reference("(empty response)") is True
    assert _is_failed_reference("READY") is False


def test_four_references_below_quorum_hides_minority_advice(monkeypatch):
    from agent import moa_loop

    outputs = [
        _output("advisor-a", "minority advice A"),
        _output("advisor-b", "minority advice B"),
        _output("advisor-c", "[failed: provider down]"),
        _output("advisor-d", "[failed: timeout]"),
    ]
    aggregator_calls = []

    monkeypatch.setattr(moa_loop, "_run_references_parallel", lambda *a, **k: outputs)
    monkeypatch.setattr(
        moa_loop,
        "_slot_runtime",
        lambda slot: {"provider": slot["provider"], "model": slot["model"]},
    )
    monkeypatch.setattr(
        moa_loop,
        "call_llm",
        lambda **kwargs: (aggregator_calls.append(kwargs) or _response("synthesized")),
    )

    result = moa_loop.aggregate_moa_context(
        user_prompt="review this",
        api_messages=[{"role": "user", "content": "review this"}],
        reference_models=[
            {"provider": "p", "model": "a"},
            {"provider": "p", "model": "b"},
            {"provider": "p", "model": "c"},
            {"provider": "p", "model": "d"},
        ],
        aggregator={"provider": "p", "model": "aggregator"},
    )

    assert len(aggregator_calls) == 1
    prompt = aggregator_calls[0]["messages"][0]["content"]
    assert "minority advice A" not in prompt
    assert "minority advice B" not in prompt
    assert "Reference quorum not met: 2/4 usable; need 3" in prompt
    assert result.endswith("synthesized")


@pytest.mark.parametrize(
    ("reference_count", "successful_count"),
    [(3, 2), (2, 1)],
)
def test_degraded_quorum_accepts_n_minus_one_successes(
    monkeypatch, reference_count, successful_count
):
    from agent import moa_loop

    outputs = [
        _output(f"advisor-{index}", "usable advice")
        if index <= successful_count
        else _output(f"advisor-{index}", "[failed: unavailable]")
        for index in range(1, reference_count + 1)
    ]
    aggregator_calls = []
    monkeypatch.setattr(moa_loop, "_run_references_parallel", lambda *a, **k: outputs)
    monkeypatch.setattr(
        moa_loop,
        "_slot_runtime",
        lambda slot: {"provider": slot["provider"], "model": slot["model"]},
    )
    monkeypatch.setattr(
        moa_loop,
        "call_llm",
        lambda **kwargs: (aggregator_calls.append(kwargs) or _response("synthesized")),
    )

    moa_loop.aggregate_moa_context(
        user_prompt="review this",
        api_messages=[{"role": "user", "content": "review this"}],
        reference_models=[
            {"provider": "p", "model": str(index)}
            for index in range(1, reference_count + 1)
        ],
        aggregator={"provider": "p", "model": "aggregator"},
    )

    prompt = aggregator_calls[0]["messages"][0]["content"]
    assert "usable advice" in prompt
    assert "Reference quorum not met" not in prompt


def test_facade_below_quorum_drops_minority_guidance(monkeypatch, tmp_path):
    from agent import moa_loop

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  default_preset: review
  presets:
    review:
      reference_models:
        - provider: p
          model: a
        - provider: p
          model: b
        - provider: p
          model: c
        - provider: p
          model: d
      aggregator:
        provider: p
        model: aggregator
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    outputs = [
        _output("p:a", "minority A"),
        _output("p:b", "minority B"),
        _output("p:c", "[failed: unavailable]"),
        _output("p:d", "[failed: unavailable]"),
    ]
    monkeypatch.setattr(moa_loop, "_run_references_parallel", lambda *a, **k: outputs)

    facade = moa_loop.MoAChatCompletions("review")
    prepared = facade.create(
        messages=[{"role": "user", "content": "review this"}],
        tools=[],
        _moa_prepare_only=True,
    )

    assert "Reference quorum not met: 2/4 usable; need 3" in prepared["guidance"]
    assert "minority A" not in prepared["guidance"]
    assert "minority B" not in prepared["guidance"]
