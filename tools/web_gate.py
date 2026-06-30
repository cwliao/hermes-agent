"""Versioned, fail-closed interface for an external web gate.

The default adapter is a local deterministic fake. It performs no network
requests and always denies until a production adapter is configured by a
separate integration.
"""

import json
import subprocess
from collections.abc import Mapping
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.registry import registry


WEB_GATE_CONTRACT_VERSION = "web_gate.v1"
WEB_GATE_WIRING_VERSION = "web_gate.wiring.v1"
WEB_GATE_ADAPTER_FACTORIES: dict[
    str, Callable[["WebGateWiringConfig"], "WebGateAdapter"]
] = {}
WEB_GATE_WIRING_CONFIG: dict[str, Any] = {
    "wiring_version": WEB_GATE_WIRING_VERSION,
    "adapter_mode": "local_fake",
}


class WebGateTool(BaseModel):
    """Validated input for a web access gate decision."""

    url: str
    tool: str
    actor: str
    channel: str
    request_source: Literal["cli", "telegram", "webui"]

    def execute(
        self,
        adapter: "WebGateAdapter | None" = None,
        wiring: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate the request, denying on rejection or adapter failure."""
        adapter_request = WebGateAdapterRequest(
            contract_version=WEB_GATE_CONTRACT_VERSION,
            **self.model_dump(),
        ).model_dump()
        selected_adapter = adapter
        if selected_adapter is None:
            selected_adapter, wiring_reason = resolve_web_gate_adapter(wiring)
            if selected_adapter is None:
                return {"allowed": False, "reason": wiring_reason}

        try:
            raw_response = selected_adapter.evaluate(adapter_request)
        except Exception:
            return {"allowed": False, "reason": "gate_adapter_error"}

        if not isinstance(raw_response, Mapping):
            return {"allowed": False, "reason": "gate_invalid_response"}
        if raw_response.get("contract_version") != WEB_GATE_CONTRACT_VERSION:
            return {"allowed": False, "reason": "gate_version_mismatch"}

        try:
            response = WebGateAdapterResponse.model_validate(raw_response)
        except ValidationError:
            return {"allowed": False, "reason": "gate_invalid_response"}

        if response.decision == "allow":
            # The adapter decides only whether the original request is allowed;
            # it cannot redirect execution to a different tool.
            return {"allowed": True, "next_tool": self.tool}
        return {"allowed": False, "reason": response.reason}


class WebGateAdapterRequest(BaseModel):
    """Serialized request contract passed to a web gate adapter."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["web_gate.v1"]
    url: str
    tool: str
    actor: str
    channel: str
    request_source: Literal["cli", "telegram", "webui"]


class WebGateAdapterResponse(BaseModel):
    """Serialized response contract returned by a web gate adapter."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["web_gate.v1"]
    decision: Literal["allow", "deny"]
    reason: str


class WebGateAdapter(Protocol):
    """Transport-independent interface implemented by web gate adapters."""

    def evaluate(self, request: dict[str, Any]) -> Mapping[str, Any]: ...


class LocalFakeWebGateAdapter:
    """Deterministic deny-only adapter used until an integration is enabled."""

    def evaluate(self, request: dict[str, Any]) -> Mapping[str, Any]:
        WebGateAdapterRequest.model_validate(request)
        return {
            "contract_version": WEB_GATE_CONTRACT_VERSION,
            "decision": "deny",
            "reason": "gate_not_configured",
        }


class SubprocessJsonResponse(BaseModel):
    """Minimal response emitted by a local subprocess adapter command."""

    model_config = ConfigDict(extra="forbid", strict=True)

    allowed: bool
    reason: str | None = None
    contract_version: str | None = None


class SubprocessJsonWebGateAdapter:
    """Run a local argv command using JSON on stdin and stdout."""

    def __init__(self, command: tuple[str, ...], timeout_seconds: float):
        self.command = command
        self.timeout_seconds = timeout_seconds

    def evaluate(self, request: dict[str, Any]) -> Mapping[str, Any]:
        WebGateAdapterRequest.model_validate(request)
        result = subprocess.run(
            self.command,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("web gate subprocess failed")

        response = SubprocessJsonResponse.model_validate_json(result.stdout)
        if (
            response.contract_version is not None
            and response.contract_version != WEB_GATE_CONTRACT_VERSION
        ):
            raise ValueError("web gate subprocess returned an unsupported version")
        if not response.allowed and not response.reason:
            raise ValueError("web gate subprocess deny response requires a reason")

        return {
            "contract_version": WEB_GATE_CONTRACT_VERSION,
            "decision": "allow" if response.allowed else "deny",
            "reason": response.reason or "subprocess_allowed",
        }


class WebGateWiringConfig(BaseModel):
    """Local wiring config used to select a repo-local web gate adapter."""

    model_config = ConfigDict(extra="forbid")

    wiring_version: str
    adapter_mode: str
    command: tuple[str, ...] | None = None
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)


def _load_web_gate_wiring() -> Mapping[str, Any]:
    """Load non-secret web gate settings from config.yaml, defaulting closed."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        wiring = config.get("web_gate")
    except Exception:
        wiring = None
    return wiring if wiring is not None else WEB_GATE_WIRING_CONFIG


def _local_fake_factory(config: WebGateWiringConfig) -> WebGateAdapter:
    if config.command is not None:
        raise ValueError("local_fake does not accept a command")
    return LocalFakeWebGateAdapter()


def _subprocess_json_factory(config: WebGateWiringConfig) -> WebGateAdapter:
    if not config.command or any(not part for part in config.command):
        raise ValueError("subprocess_json requires a non-empty command")
    return SubprocessJsonWebGateAdapter(config.command, config.timeout_seconds)


WEB_GATE_ADAPTER_FACTORIES.update(
    {
        "local_fake": _local_fake_factory,
        "subprocess_json": _subprocess_json_factory,
    }
)


def resolve_web_gate_adapter(
    wiring: Mapping[str, Any] | None = None,
) -> tuple[WebGateAdapter | None, str | None]:
    """Resolve the repo-local adapter selection, failing closed on bad wiring."""
    candidate = wiring if wiring is not None else _load_web_gate_wiring()
    if not isinstance(candidate, Mapping):
        return None, "gate_invalid_config"

    try:
        wiring_config = WebGateWiringConfig.model_validate(candidate)
    except ValidationError:
        return None, "gate_invalid_config"

    if wiring_config.wiring_version != WEB_GATE_WIRING_VERSION:
        return None, "gate_version_mismatch"

    factory = WEB_GATE_ADAPTER_FACTORIES.get(wiring_config.adapter_mode)
    if factory is None:
        return None, "gate_unknown_adapter_mode"

    try:
        adapter = factory(wiring_config)
    except Exception:
        return None, "gate_wiring_error"

    if not callable(getattr(adapter, "evaluate", None)):
        return None, "gate_wiring_error"

    return adapter, None


def web_gate_tool(
    payload: dict[str, Any],
    adapter: WebGateAdapter | None = None,
    wiring: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and evaluate a web gate request."""
    return WebGateTool.model_validate(payload).execute(adapter=adapter, wiring=wiring)


WEB_GATE_SCHEMA = {
    "name": "web_gate",
    "description": (
        "Check whether a web-capable tool may access a URL. This tool uses a "
        "versioned, fail-closed adapter contract and remains disconnected "
        "from external endpoints by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Requested URL."},
            "tool": {"type": "string", "description": "Web-capable tool requesting access."},
            "actor": {"type": "string", "description": "Actor making the request."},
            "channel": {"type": "string", "description": "Conversation or delivery channel."},
            "request_source": {
                "type": "string",
                "enum": ["cli", "telegram", "webui"],
                "description": "Surface from which the request originated.",
            },
        },
        "required": ["url", "tool", "actor", "channel", "request_source"],
    },
}


registry.register(
    name="web_gate",
    toolset="web",
    schema=WEB_GATE_SCHEMA,
    handler=lambda args, **kw: web_gate_tool(args),
    emoji="🛡️",
)
