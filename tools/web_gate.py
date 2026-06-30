"""Fail-closed web gate skeleton pending connection to an external gate.

This module deliberately uses a local deny-only adapter. It performs no network
requests and must remain fail-closed until a production gate is configured by a
separate integration.
"""

from typing import Any, Literal

from pydantic import BaseModel

from tools.registry import registry


class WebGateTool(BaseModel):
    """Validated input for a web access gate decision."""

    url: str
    tool: str
    actor: str
    channel: str
    request_source: Literal["cli", "telegram", "webui"]

    def execute(self) -> dict[str, Any]:
        """Evaluate the request, denying on either rejection or adapter failure."""
        payload = self.model_dump()

        try:
            result = _stub_gate_adapter(payload)
            if result.get("decision") == "allow":
                return {"allowed": True, "next_tool": self.tool}
            return {
                "allowed": False,
                "reason": result.get("reason", "gate_denied"),
            }
        except Exception:
            return {"allowed": False, "reason": "gate_error"}


def _stub_gate_adapter(payload: dict[str, Any]) -> dict[str, str]:
    """Return the local placeholder verdict without making an external call."""
    del payload
    return {"decision": "deny", "reason": "gate_not_configured"}


def web_gate_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and evaluate a web gate request."""
    return WebGateTool.model_validate(payload).execute()


WEB_GATE_SCHEMA = {
    "name": "web_gate",
    "description": (
        "Check whether a web-capable tool may access a URL. This skeleton is "
        "fail-closed until an external gate is configured."
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
