"""Tests for the versioned, fail-closed web gate adapter contract."""

import subprocess
import sys

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


def test_default_adapter_selection_uses_local_fake_and_denies(monkeypatch):
    monkeypatch.setattr(
        web_gate, "_load_web_gate_wiring", lambda: web_gate.WEB_GATE_WIRING_CONFIG
    )
    adapter, reason = web_gate.resolve_web_gate_adapter()

    assert isinstance(adapter, web_gate.LocalFakeWebGateAdapter)
    assert reason is None
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


