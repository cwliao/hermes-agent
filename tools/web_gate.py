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
