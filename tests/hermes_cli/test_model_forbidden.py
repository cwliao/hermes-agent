"""Tests for user-configured forbidden models."""

from hermes_cli import model_switch
from hermes_cli.model_switch import switch_model


def test_switch_model_rejects_forbidden_model(monkeypatch):
    monkeypatch.setattr(
        model_switch,
        "_forbidden_models_from_config",
        lambda: {"llama3.3:70b"},
    )

    result = switch_model(
        raw_input="llama3.3:70b",
        current_provider="openai-api",
        current_model="ornith:9b",
        current_base_url="http://localhost:11434/v1",
        current_api_key="no-key-required",
    )

    assert not result.success
    assert "forbidden" in result.error_message
    assert result.new_model == "llama3.3:70b"
