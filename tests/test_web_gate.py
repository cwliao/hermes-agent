"""Tests for the versioned, fail-closed web gate adapter contract."""

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


class FakeAdapter:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def evaluate(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def adapter_response(decision="deny", reason="policy_denied", **overrides):
    response = {
        "contract_version": web_gate.WEB_GATE_CONTRACT_VERSION,
        "decision": decision,
        "reason": reason,
    }
    response.update(overrides)
    return response


def test_default_local_fake_denies_without_external_configuration():
    assert web_gate.web_gate_tool(VALID_PAYLOAD) == {
        "allowed": False,
        "reason": "gate_not_configured",
    }


def test_adapter_receives_exact_versioned_request_contract():
    adapter = FakeAdapter(adapter_response())

    assert web_gate.web_gate_tool(VALID_PAYLOAD, adapter=adapter) == {
        "allowed": False,
        "reason": "policy_denied",
    }
    assert adapter.requests == [
        {
            "contract_version": "web_gate.v1",
            **VALID_PAYLOAD,
        }
    ]


def test_allow_uses_original_requested_tool():
    adapter = FakeAdapter(adapter_response(decision="allow", reason="policy_allowed"))

    assert web_gate.web_gate_tool(VALID_PAYLOAD, adapter=adapter) == {
        "allowed": True,
        "next_tool": "web_extract",
    }


def test_missing_required_field_raises_validation_error():
    payload = {key: value for key, value in VALID_PAYLOAD.items() if key != "actor"}

    with pytest.raises(ValidationError):
        web_gate.web_gate_tool(payload)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            adapter_response(contract_version="web_gate.v2"),
            "gate_version_mismatch",
        ),
        (
            {"contract_version": "web_gate.v1", "decision": "deny"},
            "gate_invalid_response",
        ),
        (adapter_response(decision="unknown"), "gate_invalid_response"),
        (adapter_response(unexpected="value"), "gate_invalid_response"),
        ("not-a-mapping", "gate_invalid_response"),
    ],
)
def test_invalid_adapter_responses_fail_closed(response, reason):
    assert web_gate.web_gate_tool(VALID_PAYLOAD, adapter=FakeAdapter(response)) == {
        "allowed": False,
        "reason": reason,
    }


def test_adapter_exception_fails_closed():
    adapter = FakeAdapter(error=RuntimeError("adapter unavailable"))

    assert web_gate.web_gate_tool(VALID_PAYLOAD, adapter=adapter) == {
        "allowed": False,
        "reason": "gate_adapter_error",
    }
