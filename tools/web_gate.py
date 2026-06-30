"""Versioned, fail-closed interface for an external web gate.

The default adapter is a local deterministic fake. It performs no network
requests and always denies until a production adapter is configured by a
separate integration.
"""

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from tools.registry import registry


WEB_GATE_CONTRACT_VERSION = "web_gate.v1"


class WebGateTool(BaseModel):
    """Validated input for a web access gate decision."""

    url: str
    tool: str
    actor: str
    channel: str
    request_source: Literal["cli", "telegram", "webui"]

    def execute(self, adapter: "WebGateAdapter | None" = None) -> dict[str, Any]:
        """Evaluate the request, denying on rejection or adapter failure."""
        adapter_request = WebGateAdapterRequest(
            contract_version=WEB_GATE_CONTRACT_VERSION,
            **self.model_dump(),
        ).model_dump()
        selected_adapter = adapter if adapter is not None else LocalFakeWebGateAdapter()

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


def web_gate_tool(
    payload: dict[str, Any], adapter: WebGateAdapter | None = None
) -> dict[str, Any]:
    """Validate and evaluate a web gate request."""
    return WebGateTool.model_validate(payload).execute(adapter=adapter)


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
