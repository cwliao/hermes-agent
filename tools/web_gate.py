"""Versioned, fail-closed interface for an external web gate.

The default adapter is a local deterministic fake. It performs no network
requests and always denies until a production adapter is configured by a
separate integration.
"""

from collections.abc import Mapping
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from tools.registry import registry


WEB_GATE_CONTRACT_VERSION = "web_gate.v1"
WEB_GATE_WIRING_VERSION = "web_gate.wiring.v1"
WEB_GATE_ADAPTER_FACTORIES: dict[str, Callable[[], "WebGateAdapter"]] = {}
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


WEB_GATE_ADAPTER_FACTORIES["local_fake"] = LocalFakeWebGateAdapter


class WebGateWiringConfig(BaseModel):
    """Local wiring config used to select a repo-local web gate adapter."""

    model_config = ConfigDict(extra="forbid")

    wiring_version: str
    adapter_mode: str


def resolve_web_gate_adapter(
    wiring: Mapping[str, Any] | None = None,
) -> tuple[WebGateAdapter | None, str | None]:
    """Resolve the repo-local adapter selection, failing closed on bad wiring."""
    candidate = wiring if wiring is not None else WEB_GATE_WIRING_CONFIG
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
        adapter = factory()
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
