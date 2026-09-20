"""Needle 3 (``cactus-needle``) local pre-filter: fast structured extraction of an
intent hint + entities from free-text, used by ``kanban_decompose.decompose_task``
to enrich (never replace) the prompt sent to the aux LLM.

Optional dependency (``pip install hermes-agent[needle]``); ``needle`` is imported
lazily inside :func:`_extract_sync` so a missing/broken install only degrades this
one pre-filter, never the caller. Each call builds a fresh ``Needle`` instance
(mirrors the package's own one-shot ``needle.extract()`` helper) so one ticket's
extraction never carries context into the next — Hermes decomposes many unrelated
tickets, not a single conversation.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Cactus's own reference points for confidence routing: ~0.1 is the engine's built-in
# suppression floor (below it the engine itself already discards calls as
# suppressed_calls), ~0.5 for low-stakes auto-actions, ~0.7 for general 'act'
# decisions, ~0.9 for high-stakes actions. Kanban triage enrichment is lower-stakes
# than any of those — a missed/skipped hint just falls through to today's
# unchanged behaviour — so we sit at the low end of "usable": >=0.6 enriches the
# prompt, anything below (including confidence=None, which LoRA-fine-tuned weights
# always report) is treated as no-signal. Retune here if false-negatives/positives
# in practice suggest otherwise.
CONFIDENCE_THRESHOLD = 0.6

# Needle 3 extraction is meant to run in double-digit milliseconds; this is a
# generous ceiling so a wedged native call can never hang the kanban dispatcher tick.
_TIMEOUT_SECONDS = 5.0

_TRIAGE_HINTS_TOOL = {
    "name": "triage_hints",
    "description": "Record the extracted intent and entities for a kanban ticket.",
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "short phrase for what the ticket is asking for",
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "notable named entities/keywords mentioned in the ticket",
            },
        },
        "required": ["intent"],
    },
}

_TRIAGE_SYSTEM_PROMPT = "Always call triage_hints with your extraction result."


def _extract_sync(text: str) -> Optional[dict]:
    """Blocking Needle 3 call; raises on any engine/import failure — caller handles it."""
    os.environ.setdefault("NEEDLE_TELEMETRY", "0")
    with contextlib.suppress(Exception):
        from tools.lazy_deps import ensure
        ensure("tool.needle", prompt=False)
    import needle  # optional extra ([needle]); imported lazily so it's never a hard dependency

    agent = needle.Needle(tools=[_TRIAGE_HINTS_TOOL], system=_TRIAGE_SYSTEM_PROMPT)
    try:
        response = agent.complete(text, 256)
    finally:
        agent.close()

    calls = response.get("function_calls") or response.get("suppressed_calls") or []
    if not calls:
        return None
    arguments = calls[0].get("arguments") or {}
    intent = arguments.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return None
    entities = arguments.get("entities")
    return {
        "intent": intent.strip(),
        "entities": [e for e in entities if isinstance(e, str)] if isinstance(entities, list) else [],
        "confidence": response.get("confidence"),
    }


def extract_triage_hints(text: str) -> Optional[dict]:
    """Best-effort ``{"intent", "entities", "confidence"}`` for kanban triage
    enrichment, or ``None`` on any failure, timeout, or low/absent confidence
    (``confidence=None`` — e.g. LoRA-fine-tuned weights — is fail-safe "needs
    review", never a pass-through). Callers treat ``None`` as "no enrichment
    available, proceed unchanged"; this never raises."""
    if not text or not text.strip():
        return None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="needle_worker")
    try:
        future = executor.submit(_extract_sync, text)
        result = future.result(timeout=_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning("needle_worker: extraction timed out after %.1fs", _TIMEOUT_SECONDS)
        return None
    except Exception as exc:
        logger.warning("needle_worker: extraction failed: %s", exc)
        return None
    finally:
        executor.shutdown(wait=False)

    if result is None:
        return None
    confidence = result.get("confidence")
    if confidence is None or confidence < CONFIDENCE_THRESHOLD:
        return None
    return result
