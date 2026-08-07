"""Telegram ``/ingest`` command for pasted text and Markdown.

The command writes the current message to a short-lived Markdown file and
reuses Telegram's existing DocuBot multipart ingestion path.  It is kept in
its own plugin so the Telegram adapter's single file-upload call site remains
unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import Any

from gateway.session_context import get_session_env
from plugins.platforms.telegram.docubot_mcp_gateway import (
    build_telegram_document_stable_key,
    get_docubot_job_status,
    ingest_document_to_docubot,
)

logger = logging.getLogger(__name__)

# A pasted message is expected to be substantially smaller than Telegram's
# 20 MiB file-upload baseline, while 500 KiB still accommodates long Markdown
# notes without allowing an accidentally huge message to occupy the gateway.
_MAX_INGEST_TEXT_BYTES = 500 * 1024

_USAGE_REPLY = (
    "Usage: /ingest <text or Markdown content>\n\n"
    "Pastes the content as a document into the DocuBot knowledge pipeline. "
    "Only paste content you're OK adding to the knowledge base."
)

_FAILURE_STATUSES = {"error", "failed", "failure", "skipped", "rejected"}

# In-memory polling parameters for completion notice: 2s interval, 45s total timeout
POLL_INTERVAL_SEC = 2.0
POLL_TIMEOUT_SEC = 45.0

# Module-level PluginContext handle captured during register(ctx)
_ctx: Any | None = None


def _ingest_result_reply(result: Any) -> str:
    """Turn the existing ingestion result into a useful user-facing reply."""
    if isinstance(result, dict):
        status = str(result.get("status", "")).strip().lower()
        if result.get("error") or result.get("ok") is False or status in _FAILURE_STATUSES:
            detail = result.get("error") or result.get("reason") or status or "unknown error"
            return f"/ingest failed: DocuBot did not accept the content ({detail})."
        if status:
            return f"/ingest received and queued for DocuBot ingestion (status: {status})."
        if result.get("ok") is True:
            return "/ingest received and queued for DocuBot ingestion (status: ok)."

    # The existing helper returns a dict, but keep an acknowledgement for a
    # compatible/mock implementation that returns no structured status.
    return "/ingest received and queued for DocuBot ingestion."


async def _poll_ingest_completion(chat_id: str, message_id: str, job_id: str) -> None:
    """Background task polling DocuBot job completion and sending proactive notice.

    Note: This is an in-memory task. If the gateway process restarts during the
    45-second polling window, this task will be lost and the second completion notice
    will not be delivered. This is an intentional design choice (no persistent queue).
    """
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed >= POLL_TIMEOUT_SEC:
            timeout_msg = (
                f"/ingest timed out: DocuBot did not finish within {int(POLL_TIMEOUT_SEC)} seconds. "
                f"You can track this request using Job ID: {job_id}."
            )
            await _send_completion_notice(chat_id, message_id, timeout_msg)
            return

        status_res = await asyncio.to_thread(get_docubot_job_status, job_id)

        if isinstance(status_res, dict):
            raw_status = str(status_res.get("status", "")).strip().lower()

            if raw_status in {
                "completed",
                "complete",
                "passed",
                "pass",
                "done",
                "success",
                "confirmed",
                "auto_confirmed",
                "auto-confirmed",
            }:
                success_msg = (
                    f"/ingest completed! Content has been successfully indexed into "
                    f"the knowledge base. (Job ID: {job_id})"
                )
                await _send_completion_notice(chat_id, message_id, success_msg)
                return

            if raw_status in {"failed", "error", "rejected", "skipped"} or (
                status_res.get("error")
                and raw_status not in {"pending", "processing", "accepted", "queued", "running", ""}
            ):
                reason = (
                    status_res.get("error")
                    or status_res.get("reason")
                    or raw_status
                    or "unknown error"
                )
                failure_msg = f"/ingest failed during processing: {reason}. (Job ID: {job_id})"
                await _send_completion_notice(chat_id, message_id, failure_msg)
                return

        await asyncio.sleep(POLL_INTERVAL_SEC)


async def _send_completion_notice(chat_id: str, message_id: str, text: str) -> bool:
    """Deliver the second notice via PluginContext or platform adapter fallback."""
    if _ctx is not None and hasattr(_ctx, "send_to_chat"):
        try:
            sent = await _ctx.send_to_chat(
                chat_id=chat_id,
                reply_to=message_id,
                text=text,
                platform="telegram",
            )
            if sent:
                return True
        except Exception as exc:
            logger.warning("/ingest second notice via ctx.send_to_chat failed: %s", exc)

    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if runner and hasattr(runner, "adapters"):
            for p, adapter in runner.adapters.items():
                if getattr(p, "value", str(p)).lower() == "telegram":
                    res = await adapter.send(chat_id, text, reply_to=message_id)
                    return bool(getattr(res, "ok", res))
    except Exception as exc:
        logger.warning("/ingest fallback notice failed: %s", exc)

    logger.warning("Could not deliver /ingest completion notice to chat %s", chat_id)
    return False


async def _handle_ingest(raw_args: str) -> str:
    """Handle ``/ingest`` and convert all failures into a reply."""
    temp_path: str | None = None
    try:
        content = raw_args if isinstance(raw_args, str) else str(raw_args)
        if not content.strip():
            return _USAGE_REPLY

        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_INGEST_TEXT_BYTES:
            limit_kib = _MAX_INGEST_TEXT_BYTES // 1024
            return (
                f"/ingest rejected: content is {content_bytes:,} bytes, which exceeds "
                f"the {_MAX_INGEST_TEXT_BYTES:,}-byte ({limit_kib} KiB) limit. "
                "Please paste a smaller document."
            )

        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
        message_id = str(get_session_env("HERMES_SESSION_MESSAGE_ID", "") or "").strip()
        if not chat_id or not message_id:
            return (
                "/ingest could not start: the current Telegram chat or message "
                "identifier is unavailable. Please try again from Telegram."
            )

        # Capture variables in closure so background polling task does not rely
        # on session contextvars after handler returns.
        captured_chat_id = chat_id
        captured_message_id = message_id

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(content)

        result = ingest_document_to_docubot(
            source="telegram",
            action="document_review",
            metadata={
                "platform": "telegram",
                "chat_id": chat_id,
                "message_id": message_id,
                "file_name": f"telegram-inline-{message_id}.md",
                "mime_type": "text/markdown",
            },
            local_path=temp_path,
            stable_key=build_telegram_document_stable_key(
                chat_id, message_id, "inline-text"
            ),
            multipart=True,
        )

        job_id = None
        is_error = False
        if isinstance(result, dict):
            job_id = result.get("job_id") or result.get("id") or result.get("job")
            status = str(result.get("status", "")).strip().lower()
            if result.get("error") or result.get("ok") is False or status in _FAILURE_STATUSES:
                is_error = True

        if job_id and not is_error:
            asyncio.create_task(
                _poll_ingest_completion(captured_chat_id, captured_message_id, str(job_id))
            )

        return _ingest_result_reply(result)
    except Exception as exc:
        logger.warning("/ingest command failed: %s", exc, exc_info=True)
        return f"/ingest failed while submitting the content to DocuBot: {exc}"
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except Exception:
                logger.warning("/ingest could not remove temporary file %s", temp_path, exc_info=True)


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_command(
        "ingest",
        handler=_handle_ingest,
        description="Ingest pasted text or Markdown into the DocuBot knowledge pipeline.",
        args_hint="<content>",
    )
