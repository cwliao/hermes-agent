"""
Tests for video cache utilities in gateway/platforms/base.py.

Covers: get_video_cache_dir, cleanup_video_cache.
"""

import os
import time

import pytest

from gateway.platforms.base import cleanup_video_cache, get_video_cache_dir


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Point the module-level VIDEO_CACHE_DIR to a fresh tmp_path."""
    monkeypatch.setattr(
        "gateway.platforms.base.VIDEO_CACHE_DIR", tmp_path / "video_cache"
    )


class TestGetVideoCacheDir:
    def test_creates_directory(self):
        cache_dir = get_video_cache_dir()
        assert cache_dir.exists()
        assert cache_dir.is_dir()

    def test_returns_existing_directory(self):
        first = get_video_cache_dir()
        second = get_video_cache_dir()
        assert first == second
        assert first.exists()


class TestCleanupVideoCache:
    def test_removes_old_files(self):
        cache_dir = get_video_cache_dir()
        old_file = cache_dir / "old.mp4"
        old_file.write_text("old")
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))

        removed = cleanup_video_cache(max_age_hours=24)
        assert removed == 1
        assert not old_file.exists()

    def test_keeps_recent_files(self):
        cache_dir = get_video_cache_dir()
        recent = cache_dir / "recent.mp4"
        recent.write_text("fresh")

        removed = cleanup_video_cache(max_age_hours=24)
        assert removed == 0
        assert recent.exists()

    def test_time_window_protects_recent_ingestion_boundary(self):
        cache_dir = get_video_cache_dir()
        now = time.time()
        in_flight = cache_dir / "in_flight.mp4"
        just_expired = cache_dir / "just_expired.mp4"
        in_flight.write_text("recent")
        just_expired.write_text("expired")
        os.utime(in_flight, (now - 23.9 * 3600, now - 23.9 * 3600))
        os.utime(just_expired, (now - 24.1 * 3600, now - 24.1 * 3600))

        removed = cleanup_video_cache(max_age_hours=24)

        assert removed == 1
        assert in_flight.exists()
        assert not just_expired.exists()

    def test_returns_removed_count(self):
        cache_dir = get_video_cache_dir()
        old_time = time.time() - 48 * 3600
        for i in range(3):
            f = cache_dir / f"old_{i}.mp4"
            f.write_text("x")
            os.utime(f, (old_time, old_time))

        assert cleanup_video_cache(max_age_hours=24) == 3

    def test_empty_cache_dir(self):
        assert cleanup_video_cache(max_age_hours=24) == 0
