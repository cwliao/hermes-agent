"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway is a single long-lived process; its ``sys.modules`` is frozen at
boot. If the checkout is updated underneath it (a manual ``git pull``, or the
window before ``hermes update``'s graceful restart fires), a first-time lazy
import on a new code path can resolve a freshly-pulled consumer module against a
stale cached dependency -> ImportError (see
``tests/test_stale_utils_module_import.py`` for the exact failure).

We snapshot the checkout revision at gateway startup and compare on demand, so
risky callers (e.g. ``/model`` switching) can refuse with a clear "restart the
gateway" message instead of crashing on a cryptic import error.

If the revision can't be read (non-git install, IO error), the boot snapshot
stays ``None`` and skew detection no-ops — it never produces a false positive.

Production (DGX) does not run out of a git checkout at all: the systemd
drop-in points ``WorkingDirectory``/``PYTHONPATH`` at
``~/.hermes/releases/<version>-<label>-<hash>/``, a plain ``rsync
--exclude=.git`` snapshot of a worktree with no ``.git`` directory (see
``.ai/tickets/T0129.md`` in the klib repo for how this snapshot is produced —
it's a manual step, not tooling this repo owns). Against a snapshot dir,
``_read_git_revision_fingerprint()`` always returns ``None``, which silently
disabled this entire module for every release-dir deploy (T0140). To keep
working there, ``_fingerprint()`` first looks for a ``RELEASE_COMMIT``
marker file at the project root — a plain-text file containing the commit
hash the release was baked from, written by hand as the last step of the
manual bake (see ``docs/`` for the exact command). When present, its content
*is* the fingerprint, independent of directory naming. When absent (dev
checkouts, or a release baked before this marker existed), we fall back to
the git-based reader unchanged.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None

RELEASE_MARKER_FILENAME = "RELEASE_COMMIT"


def _release_marker_fingerprint(project_root: Path) -> str | None:
    """Read the bake-time version marker, if this checkout has one.

    The marker is a single line of plain text (the commit hash the release
    directory was rsynced from) written by hand at the end of the manual
    release-bake SOP. Missing/empty/unreadable -> ``None`` (fail-soft, same
    contract as the git-based reader below), e.g. while a release directory
    is still mid-rsync and the marker hasn't been written yet.
    """
    try:
        content = (project_root / RELEASE_MARKER_FILENAME).read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        return None
    if not content:
        return None
    return f"release:{content}"


def _fingerprint() -> str | None:
    """Current checkout fingerprint.

    Prefers the bake-time ``RELEASE_COMMIT`` marker (works for both
    release-dir snapshots and, if someone drops the file in, a dev checkout).
    Falls back to the CLI's git-rev reader, reused so we don't duplicate its
    worktree-aware ref resolution. ``hermes_cli.main`` is always already
    imported in a gateway process (it's the entry point), so this import is
    free.
    """
    marker = _release_marker_fingerprint(_PROJECT_ROOT)
    if marker is not None:
        return marker
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def _write_boot_fingerprint_file(fingerprint: str | None) -> None:
    if not fingerprint:
        return
    try:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "gateway_boot_fingerprint"
        path.write_text(fingerprint + "\n")
    except Exception:
        pass


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()
    _write_boot_fingerprint_file(_boot_fingerprint)


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    if sha and sha != "unresolved" and len(sha) > 10:
        return sha[:10]
    return sha or fingerprint


def detect_code_skew() -> tuple[str, str] | None:
    """Return ``(boot_rev, disk_rev)`` short labels if the checkout drifted
    since boot, else ``None``."""
    if _boot_fingerprint is None:
        return None
    current = _fingerprint()
    if current is None or current == _boot_fingerprint:
        return None
    return _short(_boot_fingerprint), _short(current)
