"""Tests for the fail-closed web gate skeleton."""

import pytest
from pydantic import ValidationError

from tools import web_gate


VALID_PAYLOAD = {
    "url": "https://example.com/resource",
    "tool": "web_extract",
    "actor": "test-user",
    "channel": "local-test",
    "request_source": "cli",
}


def test_valid_input_is_denied_by_stub():
    assert web_gate.web_gate_tool(VALID_PAYLOAD) == {
        "allowed": False,
        "reason": "gate_not_configured",
    }


def test_missing_required_field_raises_validation_error():
    payload = {key: value for key, value in VALID_PAYLOAD.items() if key != "actor"}

    with pytest.raises(ValidationError):
        web_gate.web_gate_tool(payload)


def test_adapter_exception_fails_closed(monkeypatch):
    def raise_adapter_error(payload):
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr(web_gate, "_stub_gate_adapter", raise_adapter_error)

    assert web_gate.web_gate_tool(VALID_PAYLOAD) == {
        "allowed": False,
        "reason": "gate_error",
    }
