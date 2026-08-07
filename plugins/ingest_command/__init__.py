"""Telegram ``/ingest`` command for pasted text and Markdown.

The command writes the current message to a short-lived Markdown file and
reuses Telegram's existing DocuBot multipart ingestion path.  It is kept in
its own plugin so the Telegram adapter's single file-upload call site remains
unchanged.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from gateway.session_context import get_session_env
from plugins.platforms.telegram.docubot_mcp_gateway import (
    build_telegram_document_stable_key,
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
            # Reuse the same chat-scoped stable-key builder the file-upload
            # path uses (see adapter.py's _ingest_cached_doc) so retries of
            # the same pasted text within the same chat/message share one
            # idempotency key. "inline-text" stands in for file_unique_id,
            # which doesn't exist for pasted content.
            stable_key=build_telegram_document_stable_key(
                chat_id, message_id, "inline-text"
            ),
            multipart=True,
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
    ctx.register_command(
        "ingest",
        handler=_handle_ingest,
        description="Ingest pasted text or Markdown into the DocuBot knowledge pipeline.",
        args_hint="<content>",
    )
