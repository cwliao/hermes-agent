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


# ---------------------------------------------------------------------------
# Mandatory interception
# ---------------------------------------------------------------------------


def test_is_web_gate_mandatory_defaults_false_when_unset(monkeypatch):
    monkeypatch.setattr(
        web_gate,
        "_load_web_gate_wiring",
        lambda: {"wiring_version": "web_gate.wiring.v1", "adapter_mode": "local_fake"},
    )
    assert web_gate.is_web_gate_mandatory() is False


def test_is_web_gate_mandatory_reads_true(monkeypatch):
    monkeypatch.setattr(
        web_gate,
        "_load_web_gate_wiring",
        lambda: {
            "wiring_version": "web_gate.wiring.v1",
            "adapter_mode": "local_fake",
            "mandatory": True,
        },
    )
    assert web_gate.is_web_gate_mandatory() is True


def test_is_web_gate_mandatory_fails_closed_to_false_on_bad_config(monkeypatch):
    def boom():
        raise RuntimeError("config load failed")

    monkeypatch.setattr(web_gate, "_load_web_gate_wiring", boom)
    assert web_gate.is_web_gate_mandatory() is False


def test_is_web_gate_mandatory_false_for_non_mapping_wiring(monkeypatch):
    monkeypatch.setattr(web_gate, "_load_web_gate_wiring", lambda: "not-a-mapping")
    assert web_gate.is_web_gate_mandatory() is False


def test_urls_for_web_extract_filters_non_http():
    args = {
        "urls": [
            "https://a.example",
            "/etc/passwd",
            "ftp://x.example",
            "data:text/plain;base64,aGk=",
            "http://b.example",
        ]
    }
    assert web_gate._urls_for_web_extract(args) == [
        "https://a.example",
        "http://b.example",
    ]


def test_urls_for_web_extract_truncates_to_first_five_before_filtering():
    # Mirrors web_extract_tool's own args.get("urls", [])[:5] truncation --
    # a 6th http(s) URL must never be gated, since it will never execute.
    args = {
        "urls": [
            "https://a.example",
            "https://b.example",
            "https://c.example",
            "https://d.example",
            "https://e.example",
            "https://f.example",
        ]
    }
    assert web_gate._urls_for_web_extract(args) == [
        "https://a.example",
        "https://b.example",
        "https://c.example",
        "https://d.example",
        "https://e.example",
    ]


def test_urls_for_web_extract_non_list_returns_empty():
    assert web_gate._urls_for_web_extract({"urls": "https://a.example"}) == []
    assert web_gate._urls_for_web_extract({}) == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com", ["https://example.com"]),
        ("http://example.com", ["http://example.com"]),
        ("/local/path", []),
        ("", []),
        (None, []),
    ],
)
def test_urls_for_browser_navigate(url, expected):
    assert web_gate._urls_for_browser_navigate({"url": url}) == expected


@pytest.mark.parametrize(
    ("image_url", "expected"),
    [
        ("https://example.com/x.png", ["https://example.com/x.png"]),
        ("/tmp/local.png", []),
        ("data:image/png;base64,aGk=", []),
        (None, []),
    ],
)
def test_urls_for_vision_analyze_excludes_non_http(image_url, expected):
    assert web_gate._urls_for_vision_analyze({"image_url": image_url}) == expected


def test_mandatory_check_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: False)
    adapter = FakeAdapter(adapter_response(decision="deny", reason="policy_denied"))
    monkeypatch.setattr(
        web_gate, "resolve_web_gate_adapter", lambda wiring=None: (adapter, None)
    )
    result = web_gate.mandatory_web_gate_block_message(
        "browser_navigate", {"url": "https://example.com"}
    )
    assert result is None
    assert adapter.requests == []


def test_mandatory_check_noop_for_ungated_tool(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: True)
    adapter = FakeAdapter(adapter_response(decision="deny", reason="policy_denied"))
    monkeypatch.setattr(
        web_gate, "resolve_web_gate_adapter", lambda wiring=None: (adapter, None)
    )
    result = web_gate.mandatory_web_gate_block_message(
        "web_search", {"query": "hello"}
    )
    assert result is None
    assert adapter.requests == []


def test_mandatory_check_noop_when_no_http_url(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: True)
    adapter = FakeAdapter(adapter_response(decision="deny", reason="policy_denied"))
    monkeypatch.setattr(
        web_gate, "resolve_web_gate_adapter", lambda wiring=None: (adapter, None)
    )
    result = web_gate.mandatory_web_gate_block_message(
        "vision_analyze", {"image_url": "/local/file.png"}
    )
    assert result is None
    assert adapter.requests == []


def test_mandatory_check_allows_when_gate_allows(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: True)
    adapter = FakeAdapter(adapter_response(decision="allow", reason="policy_allowed"))
    monkeypatch.setattr(
        web_gate, "resolve_web_gate_adapter", lambda wiring=None: (adapter, None)
    )
    monkeypatch.setattr(
        web_gate, "_derive_web_gate_identity", lambda session_id: ("me", "telegram", "telegram")
    )
    result = web_gate.mandatory_web_gate_block_message(
        "browser_navigate", {"url": "https://example.com"}, session_id="sess-1"
    )
    assert result is None
    assert adapter.requests == [
        {
            "contract_version": "web_gate.v1",
            "url": "https://example.com",
            "tool": "browser_navigate",
            "actor": "me",
            "channel": "telegram",
            "request_source": "telegram",
        }
    ]


def test_mandatory_check_blocks_when_gate_denies(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: True)
    adapter = FakeAdapter(adapter_response(decision="deny", reason="https_required"))
    monkeypatch.setattr(
        web_gate, "resolve_web_gate_adapter", lambda wiring=None: (adapter, None)
    )
    result = web_gate.mandatory_web_gate_block_message(
        "browser_navigate", {"url": "http://example.com"}
    )
    assert result is not None
    assert "browser_navigate" in result
    assert "http://example.com" in result
    assert "https_required" in result


def test_mandatory_check_web_extract_any_deny_blocks_whole_call(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: True)

    class SequencedAdapter:
        def __init__(self):
            self.requests = []

        def evaluate(self, request):
            self.requests.append(request)
            if request["url"] == "https://b.example":
                return adapter_response(decision="deny", reason="blocked_b")
            return adapter_response(decision="allow", reason="policy_allowed")

    adapter = SequencedAdapter()
    monkeypatch.setattr(
        web_gate, "resolve_web_gate_adapter", lambda wiring=None: (adapter, None)
    )
    result = web_gate.mandatory_web_gate_block_message(
        "web_extract", {"urls": ["https://a.example", "https://b.example", "https://c.example"]}
    )
    assert result is not None
    assert "https://b.example" in result
    # Short-circuits: never evaluates the 3rd URL once the 2nd is denied.
    assert [r["url"] for r in adapter.requests] == ["https://a.example", "https://b.example"]


def test_mandatory_check_fails_closed_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(web_gate, "is_web_gate_mandatory", lambda: True)

    def boom(payload, adapter=None, wiring=None):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(web_gate, "web_gate_tool", boom)
    result = web_gate.mandatory_web_gate_block_message(
        "browser_navigate", {"url": "https://example.com"}
    )
    assert result is not None
    assert "browser_navigate" in result


@pytest.mark.parametrize(
    ("platform", "expected_request_source"),
    [
        ("telegram", "telegram"),
        ("webui", "webui"),
        ("web", "webui"),
        ("api_server", "webui"),
        ("tui", "webui"),
        ("desktop", "webui"),
        ("whatsapp", "cli"),
        ("", "cli"),
    ],
)
def test_derive_web_gate_identity_maps_known_platforms(platform, expected_request_source):
    from gateway.session_context import set_session_vars, clear_session_vars

    tokens = set_session_vars(platform=platform, user_id="user-42")
    try:
        actor, channel, request_source = web_gate._derive_web_gate_identity("sess-1")
    finally:
        clear_session_vars(tokens)

    assert actor == "user-42"
    assert channel == (platform or "cli")
    assert request_source == expected_request_source


def test_derive_web_gate_identity_falls_back_to_session_id_and_unknown():
    from gateway.session_context import set_session_vars, clear_session_vars

    tokens = set_session_vars(platform="", user_id="")
    try:
        actor, channel, request_source = web_gate._derive_web_gate_identity("sess-42")
    finally:
        clear_session_vars(tokens)

    assert actor == "sess-42"
    assert channel == "cli"
    assert request_source == "cli"
