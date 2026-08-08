"""Tests for gateway code-skew detection (stale-checkout guard).

Companion to ``tests/test_stale_utils_module_import.py``: that test proves the
crash; these prove the guard that turns it into a clear "restart the gateway"
message before a model switch can hit it.
"""

import pytest

from gateway import code_skew


@pytest.fixture(autouse=True)
def _reset_boot_fingerprint(monkeypatch):
    """Each test starts with no recorded boot fingerprint."""
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)


class TestDetectCodeSkew:
    def test_no_boot_fingerprint_means_no_skew(self, monkeypatch):
        # Nothing recorded (e.g. non-git install) -> never a false positive.
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:def456")
        assert code_skew.detect_code_skew() is None


    def test_drift_is_detected_with_short_revs(self, monkeypatch):
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")
        code_skew.record_boot_fingerprint()

        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:def4567890123")
        skew = code_skew.detect_code_skew()
        assert skew == ("abc1234567", "def4567890")



    def test_record_writes_boot_fingerprint_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")

        import hermes_constants

        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        code_skew.record_boot_fingerprint()

        assert (tmp_path / "gateway_boot_fingerprint").read_text() == "git:refs/heads/main:abc1234567890\n"

    def test_record_writes_boot_fingerprint_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")

        import hermes_constants

        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        code_skew.record_boot_fingerprint()

        assert (tmp_path / "gateway_boot_fingerprint").read_text() == "git:refs/heads/main:abc1234567890\n"


class TestReleaseMarkerFingerprint:
    """T0140: release-dir snapshots have no .git, so the bake-time
    RELEASE_COMMIT marker file is the fingerprint source there."""

    def test_reads_marker_file_when_present(self, tmp_path):
        (tmp_path / code_skew.RELEASE_MARKER_FILENAME).write_text("5863e9d5e8a1\n")
        assert code_skew._release_marker_fingerprint(tmp_path) == "release:5863e9d5e8a1"

    def test_missing_marker_file_is_none(self, tmp_path):
        assert code_skew._release_marker_fingerprint(tmp_path) is None

    def test_empty_marker_file_is_none(self, tmp_path):
        (tmp_path / code_skew.RELEASE_MARKER_FILENAME).write_text("   \n")
        assert code_skew._release_marker_fingerprint(tmp_path) is None

    def test_fingerprint_prefers_marker_over_git(self, tmp_path, monkeypatch):
        (tmp_path / code_skew.RELEASE_MARKER_FILENAME).write_text("abc123\n")
        monkeypatch.setattr(code_skew, "_PROJECT_ROOT", tmp_path)

        def _fail_if_called(_root):
            raise AssertionError("git reader should not be reached when marker exists")

        monkeypatch.setattr(
            "hermes_cli.main._read_git_revision_fingerprint", _fail_if_called
        )
        assert code_skew._fingerprint() == "release:abc123"

    def test_fingerprint_falls_back_to_git_when_no_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(code_skew, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            "hermes_cli.main._read_git_revision_fingerprint",
            lambda root: "git:refs/heads/main:def456",
        )
        assert code_skew._fingerprint() == "git:refs/heads/main:def456"

    def test_short_renders_release_marker_like_git_fingerprint(self):
        assert code_skew._short("release:5863e9d5e8a123456") == "5863e9d5e8"


class TestShort:
    def test_shortens_long_sha(self):
        assert code_skew._short("git:refs/heads/main:abcdef0123456789") == "abcdef0123"

    def test_keeps_unresolved_marker(self):
        assert code_skew._short("git:refs/heads/main:unresolved") == "unresolved"

    def test_passes_short_sha_through_untruncated(self):
        assert code_skew._short("git:HEAD:abc1234") == "abc1234"


class TestModelSwitchSkewGuard:
    def test_guard_returns_none_without_skew(self, monkeypatch):
        from gateway import slash_commands

        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)
        assert slash_commands._model_switch_skew_guard() is None

    def test_guard_message_names_revs_and_restart(self, monkeypatch):
        from gateway import slash_commands

        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))
        msg = slash_commands._model_switch_skew_guard()
        assert msg is not None
        assert "abc1234567" in msg
        assert "def4567890" in msg
        assert "hermes gateway restart" in msg
