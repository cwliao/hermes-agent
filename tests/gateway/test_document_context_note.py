"""Tests for the document context note prepended to user turns with attachments.

A user who attaches a PDF / DOCX in chat used to see the agent treat it as
"unreadable" because the context note told the model to "Ask the user what
they'd like you to do with it" — steering it away from extracting the text it
is perfectly capable of reading. These tests pin the contract:

- text documents: note confirms the (adapter-)inlined content + records path.
- binary documents (PDF/DOCX/…): note tells the agent to extract the text
  itself and never tells it to punt back to the user.
"""

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform

gateway_run = importlib.import_module("gateway.run")
_build_document_context_note = gateway_run._build_document_context_note
_PPTX_MTYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class TestTextDocumentNote:
    @pytest.mark.parametrize("mtype", ["text/plain", "text/markdown", "text/csv"])
    def test_text_note_mentions_included_content_and_path(self, mtype):
        note = _build_document_context_note("notes.txt", "/cache/doc_notes.txt", mtype)
        assert "text document" in note
        assert "notes.txt" in note
        assert "/cache/doc_notes.txt" in note
        assert "included below" in note


class TestBinaryDocumentNote:
    @pytest.mark.parametrize(
        "mtype",
        [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
        ],
    )
    def test_binary_note_guides_extraction(self, mtype):
        note = _build_document_context_note("contract.pdf", "/cache/doc_contract.pdf", mtype)
        # Records the path so the agent can open it.
        assert "/cache/doc_contract.pdf" in note
        # Tells the agent to read it by extracting the text...
        assert "extract" in note.lower()
        # ...and does NOT steer it into punting back to the user (the bug).
        assert "ask the user" not in note.lower()
        assert "paste" in note.lower()

    def test_binary_note_distinct_from_text_note(self):
        text_note = _build_document_context_note("a.txt", "/c/a.txt", "text/plain")
        pdf_note = _build_document_context_note("a.pdf", "/c/a.pdf", "application/pdf")
        assert text_note != pdf_note
        # The text path claims content is inlined; the binary path must not.
        assert "included below" in text_note
        assert "included below" not in pdf_note

    def test_binary_note_without_ingestion_signal_is_byte_for_byte_unchanged(self):
        note = _build_document_context_note("contract.pdf", "/cache/doc_contract.pdf", "application/pdf")
        expected = (
            "[The user sent a document: 'contract.pdf'. It is saved at: /cache/doc_contract.pdf. "
            "Its text is not inlined here (it's a binary format such as PDF or DOCX). "
            "To read it, extract the document's text yourself — for example with the "
            "terminal tool or the ocr-and-documents skill — before answering, instead "
            "of asking the user to paste the contents.]"
        )

        assert note == expected

    def test_pptx_note_directs_model_to_use_powerpoint_skill(self):
        note = _build_document_context_note(
            "presentation.pptx",
            "/cache/doc_presentation.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )

        assert "powerpoint" in note
        assert "Use the powerpoint skill to extract its text before answering." in note
        assert "terminal tool" not in note

    @pytest.mark.asyncio
    async def test_pptx_extraction_inlines_markitdown_output(self, monkeypatch):
        class _SuccessfulProcess:
            returncode = 0

            async def communicate(self):
                return b"# Quarterly Results\nRevenue grew 20%.", b""

        async def fake_create_subprocess_exec(*args, **kwargs):
            assert args[:3] == (gateway_run.sys.executable, "-m", "markitdown")
            assert args[3] == "/cache/presentation.pptx"
            return _SuccessfulProcess()

        monkeypatch.setattr(
            gateway_run.asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        extracted = await gateway_run._try_extract_pptx_text("/cache/presentation.pptx")
        note = _build_document_context_note(
            "presentation.pptx",
            "/agent-cache/presentation.pptx",
            _PPTX_MTYPE,
            extracted_text=extracted,
        )

        assert extracted == "# Quarterly Results\nRevenue grew 20%."
        assert "Revenue grew 20%." in note
        assert "Use the powerpoint skill to extract its text before answering." not in note

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["timeout", "nonzero"])
    async def test_pptx_extraction_failure_falls_back_to_powerpoint_skill(
        self, monkeypatch, failure, caplog
    ):
        class _Process:
            returncode = 1 if failure == "nonzero" else None

            async def communicate(self):
                if failure == "timeout" and not getattr(self, "killed", False):
                    await asyncio.sleep(3600)
                return b"ignored output", b""

            def kill(self):
                self.killed = True

        async def fake_create_subprocess_exec(*args, **kwargs):
            return _Process()

        monkeypatch.setattr(
            gateway_run.asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        extracted = await gateway_run._try_extract_pptx_text(
            "/cache/presentation.pptx", timeout=0.01
        )
        note = _build_document_context_note(
            "presentation.pptx",
            "/agent-cache/presentation.pptx",
            _PPTX_MTYPE,
            extracted_text=extracted,
        )

        assert extracted is None
        assert "Use the powerpoint skill to extract its text before answering." in note
        assert "ignored output" not in note
        assert [record.message for record in caplog.records].count(
            "PPTX text extraction failed for presentation.pptx"
        ) == 1

    @pytest.mark.asyncio
    async def test_pptx_extraction_enforces_timeout(self, monkeypatch):
        class _HungProcess:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*args, **kwargs):
            return _HungProcess()

        monkeypatch.setattr(
            gateway_run.asyncio,
            "create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        started = time.monotonic()
        assert await gateway_run._try_extract_pptx_text("/cache/presentation.pptx", timeout=0.01) is None
        assert time.monotonic() - started < 0.5

    def test_binary_note_without_matching_skill_keeps_generic_fallback(self):
        note = _build_document_context_note(
            "contract.docx",
            "/cache/doc_contract.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        assert "for example with the terminal tool or the ocr-and-documents skill" in note
        assert "powerpoint" not in note

    def test_successful_docubot_ingestion_softens_binary_note(self):
        note = _build_document_context_note(
            "contract.pdf",
            "/cache/doc_contract.pdf",
            "application/pdf",
            ingested=True,
        )

        assert "DocuBot has processed and stored the file" in note
        assert "Its content is not guaranteed to be available through klib's search tools" in note
        for tool_name in (
            "mcp__klib__search",
            "mcp__klib__semantic_search",
            "mcp__klib__list_resources",
        ):
            assert tool_name in note
        assert "can be queried directly" not in note
        assert "tell the user honestly that you can't find it right now" in note
        assert "do not use the terminal tool or write a script to parse the file yourself" in note
        assert "ocr-and-documents skill" not in note
        assert "extract" not in note.lower()

    def test_event_metadata_only_softens_matching_telegram_attachment(self):
        event = SimpleNamespace(
            source=SimpleNamespace(platform=Platform.TELEGRAM),
            metadata={
                "docubot_ingest_results": {
                    "/cache/doc_contract.pdf": {
                        "result": {"status": "accepted"},
                        "succeeded": True,
                    }
                }
            },
        )

        assert gateway_run._telegram_docubot_ingested_for_path(event, "/cache/doc_contract.pdf") is True
        assert gateway_run._telegram_docubot_ingested_for_path(event, "/cache/other.pdf") is False
        failed_event = SimpleNamespace(
            source=event.source,
            metadata={
                "docubot_ingest_results": {
                    "/cache/doc_contract.pdf": {
                        "result": {"status": "error"},
                        "succeeded": False,
                    }
                }
            },
        )
        assert gateway_run._telegram_docubot_ingested_for_path(failed_event, "/cache/doc_contract.pdf") is False
        assert gateway_run._telegram_docubot_ingested_for_path(
            SimpleNamespace(source=SimpleNamespace(platform=Platform.DISCORD), metadata=event.metadata),
            "/cache/doc_contract.pdf",
        ) is False
