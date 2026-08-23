"""Tests for the MOA progress indicator added in issue #59546.

The MoA facade (``MoAChatCompletions.create``) emits a sequence of display
events so a TUI / CLI / desktop surface can render progress as references
complete and the phase transitions from ``reference`` to ``aggregator``:

  - ``moa.progress`` — fired once per reference completion with
                       ``refs_done`` and ``refs_total`` (e.g. drives a
                       status-bar ``MOA: 2/3 refs done`` indicator)
  - ``moa.phase``    — fired once per phase transition (currently only the
                       ``phase="aggregator"`` transition right before the
                       aggregator acts)

These tests exercise the real callback surface end-to-end through the
display hook (``reference_callback``) — no mocks on the dispatch path.
The LLM is stubbed via ``call_llm`` so the test does not depend on any
real provider.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _response(content="ok"):
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None, model="fake")


@pytest.fixture
def moa_config(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  default_preset: closed
  presets:
    closed:
      enabled: true
      reference_models:
        - provider: openrouter
          model: anthropic/claude-opus-4.8
        - provider: openrouter
          model: openai/gpt-5.5
        - provider: openrouter
          model: google/gemini-3-pro
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _collect_emits(facade):
    """Pull every event the facade dispatches into a flat list of (event, kwargs)."""
    captured: list[tuple[str, dict]] = []

    def _capture(event: str, **kwargs):
        captured.append((event, kwargs))

    facade.reference_callback = _capture
    return captured




def test_moa_phase_transitions_to_aggregator(moa_config, monkeypatch):
    """A single ``moa.phase`` event with ``phase="aggregator"`` fires after the fan-out."""
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            return _response("advice")
        return _response("acted")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    facade = MoAChatCompletions("closed")
    captured = _collect_emits(facade)

    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "plan the migration"}],
    )

    phase_events = [(e, k) for (e, k) in captured if e == "moa.phase"]
    # Exactly one phase event per turn, identifying the aggregator.
    assert len(phase_events) == 1
    _event, kwargs = phase_events[0]
    assert kwargs["phase"] == "aggregator"
    assert kwargs["aggregator"] == "openrouter:anthropic/claude-opus-4.8"
    # counts match the configured reference count
    assert kwargs["refs_done"] == 3
    assert kwargs["refs_total"] == 3






def test_moa_progress_callback_none_safe(moa_config, monkeypatch):
    """A missing ``reference_callback`` does not break the fan-out or create()."""
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            return _response("advice")
        return _response("acted")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    # No callback attached — the facade's _emit is a no-op in that case.
    facade = MoAChatCompletions("closed")
    assert facade.reference_callback is None
    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "noop"}],
    )
    # Turn still resolved cleanly; aggregator slot populated as usual.
    assert facade.last_aggregator_slot is not None
    assert facade.last_aggregator_slot["model"] == "anthropic/claude-opus-4.8"


def test_moa_progress_counts_skipped_recursion_guard(moa_config, monkeypatch):
    """Skipped nested-MoA slots still advance terminal N/M progress."""
    from agent import moa_loop

    def fake_call_llm(**kwargs):
        return _response("advice")

    monkeypatch.setattr(moa_loop, "call_llm", fake_call_llm)

    refs = [
        {"provider": "openrouter", "model": "model-a"},
        {"provider": "moa", "model": "nested"},
        {"provider": "openrouter", "model": "model-b"},
    ]
    progress = []

    output = moa_loop._run_references_parallel(
        refs,
        [{"role": "user", "content": "synthetic"}],
        progress_callback=lambda done, total, label: progress.append(
            (done, total, label)
        ),
    )

    assert len(output) == 3
    assert [done for done, _total, _label in progress] == [1, 2, 3]
    assert all(total == 3 for _done, total, _label in progress)
    assert {label for _done, _total, label in progress} == {
        "openrouter:model-a",
        "moa:nested",
        "openrouter:model-b",
    }


def test_moa_progress_four_references_survives_one_failure(moa_config, monkeypatch):
    """Four references keep 3 usable outputs and complete N/M after one failure."""
    from agent import moa_loop

    def fake_call_llm(**kwargs):
        if kwargs.get("model") == "failing-model":
            raise RuntimeError("synthetic provider failure")
        return _response(f"advice-{kwargs.get('model')}")

    monkeypatch.setattr(moa_loop, "call_llm", fake_call_llm)
    monkeypatch.setattr(moa_loop, "get_transport", lambda *_args, **_kwargs: None)

    refs = [
        {"provider": "openrouter", "model": "model-a"},
        {"provider": "openrouter", "model": "model-b"},
        {"provider": "openrouter", "model": "model-c"},
        {"provider": "openrouter", "model": "failing-model"},
    ]
    progress = []

    output = moa_loop._run_references_parallel(
        refs,
        [{"role": "user", "content": "synthetic four-agent quorum"}],
        progress_callback=lambda done, total, label: progress.append(
            (done, total, label)
        ),
    )

    successful = [text for _label, text, _acct in output if not text.startswith("[failed:")]
    failed = [text for _label, text, _acct in output if text.startswith("[failed:")]
    assert len(output) == 4
    assert len(successful) == 3
    assert len(failed) == 1
    assert [done for done, _total, _label in progress] == [1, 2, 3, 4]
    assert all(total == 4 for _done, total, _label in progress)
    assert progress[-1][0:2] == (4, 4)
    assert [label for label, _text, _acct in output] == [
        "openrouter:model-a",
        "openrouter:model-b",
        "openrouter:model-c",
        "openrouter:failing-model",
    ]
