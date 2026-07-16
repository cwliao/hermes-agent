from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from requests import Response, Session

from tools.registry import registry

logger = logging.getLogger(__name__)


def _normalize_token(value: str, *, max_len: int = 32) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip("/-_"))
    text = text.strip("-_.")
    return text[:max_len] if text else "event"


def build_idempotency_key(
    source: str,
    action: str,
    *,
    stable_key: Optional[str] = None,
    timestamp: Optional[str | datetime] = None,
    fallback_uuid: Optional[str] = None,
) -> str:
    """Build deterministic Idempotency-Key:

    - stable_key has highest priority so retries share one key.
    - else formatted timestamp/ISO-8601 UTC time.
    - else UUID fallback.
    """
    safe_source = _normalize_token(source)
    safe_action = _normalize_token(action)

    if stable_key:
        suffix = _normalize_token(stable_key, max_len=32)
    elif timestamp:
        if isinstance(timestamp, datetime):
            when = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
            suffix = when.strftime("%Y%m%dT%H%M%SZ")
        else:
            try:
                when = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                suffix = when.strftime("%Y%m%dT%H%M%SZ")
            except Exception:
                suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    else:
        suffix = (fallback_uuid or "").replace("-", "") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return f"{safe_source}-{safe_action}-{suffix}"


def _docubot_url() -> str:
    return os.getenv("DOCUBOT_INGEST_URL", "").strip()


def _docubot_headers() -> Dict[str, str]:
    token = (
        os.getenv("DOCUBOT_INGEST_TOKEN")
        or os.getenv("DOCUBOT_INGEST_API_TOKEN")
        or os.getenv("DOCUBOT_BEARER_TOKEN")
        or ""
    )
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _docubot_timeout() -> int:
    raw = os.getenv("DOCUBOT_INGEST_TIMEOUT_SEC", "20")
    try:
        return int(raw)
    except Exception:
        return 20


def ingest_document_to_docubot(
    *,
    source: str,
    action: str,
    metadata: Optional[Dict[str, Any]],
    local_path: Optional[str] = None,
    stable_key: Optional[str] = None,
    timestamp: Optional[str | datetime] = None,
    timeout_sec: Optional[int] = None,
) -> Dict[str, Any]:
    """Call DocuBot ingestion endpoint and return parsed JSON / structured result."""
    endpoint = _docubot_url()
    if not endpoint:
        logger.warning("DOCUBOT_INGEST_URL is not configured; skipping ingest request")
        return {
            "status": "skipped",
            "reason": "DOCUBOT_INGEST_URL not configured",
        }

    idempotency_key = build_idempotency_key(
        source=source,
        action=action,
        stable_key=stable_key,
        timestamp=timestamp,
    )

    path = Path(local_path) if local_path else None
    if path is not None and not path.exists():
        return {
            "status": "error",
            "error": f"local path not found: {path}",
            "idempotency_key": idempotency_key,
        }

    headers = _docubot_headers()
    headers.setdefault("Idempotency-Key", idempotency_key)

    payload: Dict[str, Any] = {
        "source": source,
        "action": action,
        "idempotency_key": idempotency_key,
    }
    if metadata:
        payload["metadata"] = metadata
    if path:
        payload["document_path"] = str(path)

    use_multipart = os.getenv("DOCUBOT_INGEST_MULTIPART", "0").strip() not in {
        "",
        "0",
        "false",
        "False",
        "FALSE",
    }
    timeout = timeout_sec or _docubot_timeout()

    try:
        with Session() as session:
            if path and use_multipart:
                with path.open("rb") as fp:
                    response: Response = session.post(
                        endpoint,
                        data={"payload": json.dumps(payload)},
                        files={"file": (path.name, fp, "application/octet-stream")},
                        headers={k: v for k, v in headers.items() if k.lower() != "content-type"},
                        timeout=timeout,
                    )
            else:
                response: Response = session.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
    except Exception as exc:
        logger.error("DocuBot ingest request failed before HTTP response: %s", exc)
        return {"status": "error", "error": str(exc), "idempotency_key": idempotency_key}

    response_text = (response.text or "").strip()
    try:
        parsed = response.json() if response_text else {"ok": response.ok}
    except Exception:
        parsed = {"raw": response_text}
        if isinstance(parsed.get("raw"), str):
            parsed["raw"] = parsed["raw"][:4096]

    if not isinstance(parsed, dict):
        parsed = {"result": parsed}

    parsed.setdefault("idempotency_key", idempotency_key)
    parsed["http_status"] = response.status_code
    if not (200 <= response.status_code < 300):
        parsed.setdefault("error", response_text or f"DocuBot request failed ({response.status_code})")
        parsed["status"] = "error"
    return parsed


def _tool_arg_candidates(schema: Dict[str, Any], query: str) -> Dict[str, Any]:
    wrapped = schema.get("function", {}) if isinstance(schema, dict) else {}
    params = wrapped.get("parameters") if isinstance(wrapped, dict) else None
    properties = {}
    if isinstance(params, dict):
        properties = params.get("properties") or {}
    elif isinstance(wrapped, dict):
        properties = wrapped.get("parameters", {}).get("properties", {}) if isinstance(wrapped.get("parameters"), dict) else {}

    keys = [
        "query",
        "input",
        "text",
        "search_query",
        "q",
        "statement",
        "prompt",
    ]
    for key in keys:
        if key in properties:
            return {key: query}

    for key, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "string":
            return {key: query}

    return {"query": query}


def _resolve_klib_tool_candidates() -> List[str]:
    names = registry.get_all_tool_names()
    candidates = [name for name in names if name.startswith("mcp__klib__")]
    # Prefer explicit search/query tools for better text semantics.
    def rank(name: str) -> int:
        lowered = name.lower()
        if "query" in lowered:
            return 3
        if "search" in lowered:
            return 2
        if "find" in lowered:
            return 1
        return 0

    return sorted(candidates, key=rank, reverse=True)


def query_via_klib_mcp(
    query: str,
    *,
    explicit_tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a query through available klib MCP tools, never through raw SQL."""
    if not query:
        return {"error": "query text is empty"}

    candidates = _resolve_klib_tool_candidates()
    if explicit_tool_name:
        if explicit_tool_name in candidates:
            candidates = [explicit_tool_name]
        else:
            return {"error": f"Specified klib tool not registered: {explicit_tool_name}"}

    if not candidates:
        return {"error": "No klib MCP tools are registered"}

    last_error = None
    for tool_name in candidates:
        entry = registry.get_entry(tool_name)
        schema = entry.schema if entry else {}
        args = _tool_arg_candidates(schema, query)
        result = registry.dispatch(tool_name, args)

        if result is None:
            last_error = f"Tool '{tool_name}' returned empty result"
            continue
        if isinstance(result, dict):
            if isinstance(result.get("error"), str) and result.get("error"):
                last_error = str(result["error"])
                continue
            return {
                "tool": tool_name,
                "result": result,
            }
        if isinstance(result, str):
            text = result.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {
                    "tool": tool_name,
                    "result": text,
                }
            if isinstance(parsed, dict) and parsed.get("error"):
                last_error = str(parsed.get("error"))
                continue
            return {
                "tool": tool_name,
                "result": parsed,
            }

        # Best-effort for simple scalar / JSON-like scalar returns.
        if isinstance(result, (list, tuple, int, float, bool)):
            return {
                "tool": tool_name,
                "result": result,
            }
        if isinstance(result, bytes):
            return {
                "tool": tool_name,
                "result": result.decode("utf-8", errors="replace"),
            }

        last_error = f"Tool '{tool_name}' returned unsupported type: {type(result).__name__}"

    if last_error:
        return {"error": str(last_error)}
    return {"error": "No klib MCP tool returned usable query output"}
