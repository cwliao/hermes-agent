"""
Tests for document cache utilities in gateway/platforms/base.py.

Covers: get_document_cache_dir, cache_document_from_bytes,
        cleanup_document_cache, SUPPORTED_DOCUMENT_TYPES.
"""

import os
import time
from pathlib import Path

import pytest

from gateway.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    cleanup_document_cache,
    get_document_cache_dir,
)

# ---------------------------------------------------------------------------
# Fixture: redirect DOCUMENT_CACHE_DIR to a temp directory for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Point the module-level DOCUMENT_CACHE_DIR to a fresh tmp_path."""
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    monkeypatch.setattr(
        "gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path / "audio_cache"
    )


# ---------------------------------------------------------------------------
# TestGetDocumentCacheDir
# ---------------------------------------------------------------------------

class TestGetDocumentCacheDir:
    def test_creates_directory(self, tmp_path):
        cache_dir = get_document_cache_dir()
        assert cache_dir.exists()
        assert cache_dir.is_dir()


# ---------------------------------------------------------------------------
# TestCacheDocumentFromBytes
# ---------------------------------------------------------------------------

class TestCacheDocumentFromBytes:
    def test_basic_caching(self):
        data = b"hello world"
        path = cache_document_from_bytes(data, "test.txt")
        assert os.path.exists(path)
        assert Path(path).read_bytes() == data

    def test_filename_preserved_in_path(self):
        path = cache_document_from_bytes(b"data", "report.pdf")
        assert "report.pdf" in os.path.basename(path)

    def test_empty_filename_uses_fallback(self):
        path = cache_document_from_bytes(b"data", "")
        assert "document" in os.path.basename(path)


# ---------------------------------------------------------------------------
# TestCleanupDocumentCache
# ---------------------------------------------------------------------------

class TestCleanupDocumentCache:
    def test_removes_old_files(self, tmp_path):
        cache_dir = get_document_cache_dir()
        old_file = cache_dir / "old.txt"
        old_file.write_text("old")
        # Set modification time to 48 hours ago
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))

        removed = cleanup_document_cache(max_age_hours=24)
        assert removed == 1
        assert not old_file.exists()

    def test_keeps_recent_files(self):
        cache_dir = get_document_cache_dir()
        recent = cache_dir / "recent.txt"
        recent.write_text("fresh")

        removed = cleanup_document_cache(max_age_hours=24)
        assert removed == 0
        assert recent.exists()

    def test_time_window_protects_recent_ingestion_boundary(self):
        cache_dir = get_document_cache_dir()
        now = time.time()
        in_flight = cache_dir / "in_flight.txt"
        just_expired = cache_dir / "just_expired.txt"
        in_flight.write_text("recent")
        just_expired.write_text("expired")
        os.utime(in_flight, (now - 23.9 * 3600, now - 23.9 * 3600))
        os.utime(just_expired, (now - 24.1 * 3600, now - 24.1 * 3600))

        # cache_document_from_bytes() writes the file before _ingest_cached_doc()
        # starts its single bounded DocuBot HTTP/RPC call.  Therefore a normal
        # in-flight ingestion is protected by this 24-hour window; reaching
        # this boundary would require a stuck/leaked call, not normal latency.
        removed = cleanup_document_cache(max_age_hours=24)

        assert removed == 1
        assert in_flight.exists()
        assert not just_expired.exists()

    def test_returns_removed_count(self):
        cache_dir = get_document_cache_dir()
        old_time = time.time() - 48 * 3600
        for i in range(3):
            f = cache_dir / f"old_{i}.txt"
            f.write_text("x")
            os.utime(f, (old_time, old_time))

        assert cleanup_document_cache(max_age_hours=24) == 3

    def test_empty_cache_dir(self):
        assert cleanup_document_cache(max_age_hours=24) == 0


# ---------------------------------------------------------------------------
# TestSupportedDocumentTypes
# ---------------------------------------------------------------------------

class TestSupportedDocumentTypes:
    def test_all_extensions_have_mime_types(self):
        for ext, mime in SUPPORTED_DOCUMENT_TYPES.items():
            assert ext.startswith("."), f"{ext} missing leading dot"
            assert "/" in mime, f"{mime} is not a valid MIME type"


# ---------------------------------------------------------------------------
# TestCacheMediaBytes — the unified, platform-agnostic caching primitive
# ---------------------------------------------------------------------------

# 1x1 transparent PNG (passes cache_image_from_bytes validation)
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


class TestCacheMediaBytes:
    def test_pdf_routes_to_document(self):
        from gateway.platforms.base import cache_media_bytes
        result = cache_media_bytes(b"%PDF-1.4 body", filename="report.pdf", mime_type="application/pdf")
        assert result is not None
        assert result.kind == "document"
        assert result.media_type == "application/pdf"
        assert "report.pdf" in result.display_name
        assert os.path.exists(result.path)
        assert "report.pdf" in result.context_note()

    def test_png_routes_to_image(self):
        from gateway.platforms.base import cache_media_bytes
        result = cache_media_bytes(_PNG_1PX, filename="photo.png", mime_type="image/png")
        assert result is not None
        assert result.kind == "image"
        assert result.media_type == "image/png"
        assert os.path.exists(result.path)


    def test_unknown_document_cached_as_octet_stream(self):
        """Unknown file types are cached (not dropped) so the agent can inspect them.

        Authorization to message the agent is the gate, not the file extension.
        """
        from gateway.platforms.base import cache_media_bytes
        result = cache_media_bytes(b"MZ", filename="program.exe", mime_type="application/x-msdownload")
        assert result is not None
        assert result.kind == "document"
        # Caller-supplied MIME is preserved when present.
        assert result.media_type == "application/x-msdownload"
        assert os.path.exists(result.path)


