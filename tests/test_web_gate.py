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


def test_missing_required_field_raises_validation_error():
    payload = {key: value for key, value in VALID_PAYLOAD.items() if key != "actor"}

    with pytest.raises(ValidationError):
        web_gate.web_gate_tool(payload)


@pytest.mark.parametrize(
    ("wiring", "reason"),
    [
        ({"wiring_version": "web_gate.wiring.v1"}, "gate_invalid_config"),
        (
            {
                "wiring_version": "web_gate.wiring.v1",
                "adapter_mode": "unknown-mode",
            },
            "gate_unknown_adapter_mode",
        ),
        (
            {
                "wiring_version": "web_gate.wiring.v2",
                "adapter_mode": "local_fake",
            },
            "gate_version_mismatch",
        ),
        ("not-a-mapping", "gate_invalid_config"),
    ],
)
def test_invalid_or_unknown_wiring_fails_closed(wiring, reason):
    assert web_gate.web_gate_tool(VALID_PAYLOAD, wiring=wiring) == {
        "allowed": False,
        "reason": reason,
    }


def test_wiring_factory_exception_fails_closed(monkeypatch):
    def exploding_factory(_config):
        raise RuntimeError("adapter wiring failed")

    monkeypatch.setitem(web_gate.WEB_GATE_ADAPTER_FACTORIES, "boom", exploding_factory)
    wiring = {
        "wiring_version": "web_gate.wiring.v1",
        "adapter_mode": "boom",
    }
    assert web_gate.web_gate_tool(VALID_PAYLOAD, wiring=wiring) == {
        "allowed": False,
        "reason": "gate_wiring_error",
    }


def subprocess_wiring(script, timeout_seconds=1):
    return {
        "wiring_version": web_gate.WEB_GATE_WIRING_VERSION,
        "adapter_mode": "subprocess_json",
        "command": [sys.executable, "-c", script],
        "timeout_seconds": timeout_seconds,
    }


def test_subprocess_json_allows_and_preserves_target_tool():
    script = (
        "import json,sys; request=json.load(sys.stdin); "
        "print(json.dumps({'allowed': request['contract_version'] == 'web_gate.v1'}))"
    )
    assert web_gate.web_gate_tool(VALID_PAYLOAD, wiring=subprocess_wiring(script)) == {
        "allowed": True,
        "next_tool": "web_extract",
    }


def test_subprocess_json_denies_with_reason():
    script = "print('{\"allowed\": false, \"reason\": \"policy_denied\"}')"
    assert web_gate.web_gate_tool(VALID_PAYLOAD, wiring=subprocess_wiring(script)) == {
        "allowed": False,
        "reason": "policy_denied",
    }


@pytest.mark.parametrize(
    "script",
    [
        "raise SystemExit(2)",
        "print('not-json')",
        "print('{\"allowed\": false}')",
        "print('{\"allowed\": true, \"unexpected\": 1}')",
        "print('{\"allowed\": true, \"contract_version\": \"web_gate.v2\"}')",
    ],
)
def test_subprocess_json_errors_fail_closed(script):
    assert web_gate.web_gate_tool(VALID_PAYLOAD, wiring=subprocess_wiring(script)) == {
        "allowed": False,
        "reason": "gate_adapter_error",
    }


def test_subprocess_json_timeout_fails_closed(monkeypatch):
    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(web_gate.subprocess, "run", time_out)
    assert web_gate.web_gate_tool(
        VALID_PAYLOAD, wiring=subprocess_wiring("print('{}')")
    ) == {"allowed": False, "reason": "gate_adapter_error"}


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
