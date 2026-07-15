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
    mandatory: bool = False


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


def is_web_gate_mandatory() -> bool:
    """Read the ``web_gate.mandatory`` config flag; defaults to False (opt-in).

    Reads the raw wiring mapping defensively rather than depending on full
    ``WebGateWiringConfig`` validation succeeding: ``mandatory: true`` should
    still enforce blocking even if the rest of the wiring (command/
    timeout_seconds) is separately broken -- in that case the downstream
    ``web_gate_tool()`` call for the actual URL fails closed anyway
    (``gate_invalid_config`` / ``gate_wiring_error`` / ...), so the tool call
    is still blocked. This flag read itself fails open to False so a config
    read glitch cannot silently lock out every user who never opted in.
    """
    try:
        wiring = _load_web_gate_wiring()
    except Exception:
        return False
    if not isinstance(wiring, Mapping):
        return False
    return bool(wiring.get("mandatory", False))


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def _urls_for_web_extract(args: Mapping[str, Any]) -> list[str]:
    urls = args.get("urls")
    if not isinstance(urls, list):
        return []
    return [u for u in urls[:5] if _is_http_url(u)]


def _urls_for_browser_navigate(args: Mapping[str, Any]) -> list[str]:
    url = args.get("url")
    return [url] if _is_http_url(url) else []


def _urls_for_vision_analyze(args: Mapping[str, Any]) -> list[str]:
    # image_url may be a local path or data: URL -- only http(s) is gated.
    url = args.get("image_url")
    return [url] if _is_http_url(url) else []


WEB_GATE_TARGET_TOOLS: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "web_extract": _urls_for_web_extract,
    "browser_navigate": _urls_for_browser_navigate,
    "vision_analyze": _urls_for_vision_analyze,
}


_REQUEST_SOURCE_BY_PLATFORM = {
    "telegram": "telegram",
    "webui": "webui",
    "web": "webui",
    "api_server": "webui",
    "tui": "webui",
    "desktop": "webui",
}


def _derive_web_gate_identity(session_id: str) -> tuple[str, str, str]:
    """Best-effort (actor, channel, request_source) from ambient session context.

    Uses ``gateway.session_context.get_session_env``, the existing
    concurrency-safe (contextvars) mirror of the legacy ``HERMES_SESSION_*``
    env vars, already populated per-request by the gateway and falling back
    to ``os.environ`` for CLI/cron. This avoids threading a new ``platform``
    parameter through every ``resolve_pre_tool_block`` call site.

    Platforms outside the mapped set collapse to ``request_source="cli"``
    (the contract only allows ``cli``/``telegram``/``webui``); ``channel``
    retains the real platform string regardless.
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return (session_id or "unknown", "cli", "cli")

    platform = (
        get_session_env("HERMES_SESSION_PLATFORM", "")
        or get_session_env("HERMES_SESSION_SOURCE", "")
        or "cli"
    ).strip().lower()
    actor = (
        get_session_env("HERMES_SESSION_USER_ID", "")
        or get_session_env("HERMES_SESSION_USER_NAME", "")
        or session_id
        or "unknown"
    ).strip() or "unknown"
    channel = platform or "cli"
    request_source = _REQUEST_SOURCE_BY_PLATFORM.get(platform, "cli")
    return actor, channel, request_source


def mandatory_web_gate_block_message(
    tool_name: str,
    args: Any,
    *,
    session_id: str = "",
) -> str | None:
    """Fail-closed mandatory interception for URL-bearing web-capable tools.

    Returns a block message (becomes the tool's result) when ``tool_name``
    is one of ``WEB_GATE_TARGET_TOOLS``, ``web_gate.mandatory`` is enabled,
    and web_gate denies (or errors evaluating) any http(s) URL the call
    would touch. Returns None to let the call proceed untouched in every
    other case (not a gated tool, flag off, no http(s) URL present, or
    web_gate allowed every URL).

    For multi-URL calls (web_extract), ANY denied URL blocks the WHOLE call
    -- the urls list is never silently filtered down to only-allowed URLs,
    so the model always sees exactly what it asked for.
    """
    if tool_name not in WEB_GATE_TARGET_TOOLS:
        return None
    try:
        if not is_web_gate_mandatory():
            return None
        urls = WEB_GATE_TARGET_TOOLS[tool_name](args if isinstance(args, Mapping) else {})
        if not urls:
            return None
        actor, channel, request_source = _derive_web_gate_identity(session_id)
        for url in urls:
            decision = web_gate_tool({
                "url": url,
                "tool": tool_name,
                "actor": actor,
                "channel": channel,
                "request_source": request_source,
            })
            if not decision.get("allowed"):
                reason = decision.get("reason", "gate_denied")
                return (
                    f"BLOCKED: web_gate denied {tool_name} access to "
                    f"{url!r} (reason={reason}). Mandatory web_gate "
                    f"interception is enabled; this call was not executed."
                )
    except Exception:
        # mandatory=True and something broke while evaluating a gated
        # tool's URL(s) -- fail closed rather than let it through.
        return (
            f"BLOCKED: web_gate mandatory-interception check errored while "
            f"evaluating {tool_name}; failing closed."
        )
    return None


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
