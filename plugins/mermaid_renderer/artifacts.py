"""Bounded lifecycle helpers for Mermaid PNG artifacts."""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from plugins.mermaid_renderer.renderer import MEDIA_ROOT as RENDERER_MEDIA_ROOT


MEDIA_ROOT = RENDERER_MEDIA_ROOT
RETENTION_HOURS = 24
RETENTION_SECONDS = RETENTION_HOURS * 60 * 60
UUID_PNG = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.png$"
)


class ArtifactRootError(RuntimeError):
    """Raised when the dedicated artifact root is unsafe to inspect."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ArtifactStatus:
    eligible_count: int
    eligible_bytes: int
    retained_count: int
    retained_bytes: int
    ignored_count: int


@dataclass(frozen=True)
class CleanupResult:
    mode: str
    eligible_count: int
    eligible_bytes: int
    deleted_count: int
    deleted_bytes: int
    skipped_count: int


@dataclass(frozen=True)
class _Candidate:
    path: Path
    device: int
    inode: int
    size: int
    mtime: float


def _validate_root() -> Path:
    try:
        entry = MEDIA_ROOT.lstat()
    except OSError as exc:
        raise ArtifactRootError("media_root_unavailable") from exc
    if (
        not stat.S_ISDIR(entry.st_mode)
        or stat.S_ISLNK(entry.st_mode)
        or entry.st_uid != os.getuid()
        or stat.S_IMODE(entry.st_mode) != 0o700
    ):
        raise ArtifactRootError("media_root_insecure")
    return MEDIA_ROOT


def _scan(now: float) -> tuple[ArtifactStatus, list[_Candidate]]:
    root = _validate_root()
    cutoff = now - RETENTION_SECONDS
    eligible_count = eligible_bytes = retained_count = retained_bytes = ignored_count = 0
    candidates: list[_Candidate] = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise ArtifactRootError("media_root_unavailable") from exc

    for path in entries:
        try:
            entry = path.lstat()
        except OSError:
            ignored_count += 1
            continue
        if (
            not stat.S_ISREG(entry.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or entry.st_uid != os.getuid()
            or not UUID_PNG.fullmatch(path.name)
        ):
            ignored_count += 1
            continue
        if entry.st_mtime <= cutoff:
            eligible_count += 1
            eligible_bytes += entry.st_size
            candidates.append(_Candidate(path, entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime))
        else:
            retained_count += 1
            retained_bytes += entry.st_size
    return (
        ArtifactStatus(
            eligible_count=eligible_count,
            eligible_bytes=eligible_bytes,
            retained_count=retained_count,
            retained_bytes=retained_bytes,
            ignored_count=ignored_count,
        ),
        candidates,
    )


def inspect_artifacts(*, now: float | None = None) -> ArtifactStatus:
    """Return bounded artifact counts without mutating the media root."""
    status, _ = _scan(time.time() if now is None else now)
    return status


def _candidate_is_current(candidate: _Candidate, cutoff: float) -> bool:
    try:
        entry = candidate.path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(entry.st_mode)
        and not stat.S_ISLNK(entry.st_mode)
        and entry.st_uid == os.getuid()
        and UUID_PNG.fullmatch(candidate.path.name) is not None
        and entry.st_dev == candidate.device
        and entry.st_ino == candidate.inode
        and entry.st_size == candidate.size
        and entry.st_mtime == candidate.mtime
        and entry.st_mtime <= cutoff
    )


def cleanup_artifacts(*, apply: bool = False, now: float | None = None) -> CleanupResult:
    """Preview or explicitly delete only expired, revalidated renderer PNGs."""
    current_time = time.time() if now is None else now
    status, candidates = _scan(current_time)
    if not apply:
        return CleanupResult(
            mode="dry-run",
            eligible_count=status.eligible_count,
            eligible_bytes=status.eligible_bytes,
            deleted_count=0,
            deleted_bytes=0,
            skipped_count=0,
        )

    cutoff = current_time - RETENTION_SECONDS
    deleted_count = deleted_bytes = skipped_count = 0
    for candidate in candidates:
        if not _candidate_is_current(candidate, cutoff):
            skipped_count += 1
            continue
        try:
            candidate.path.unlink()
        except OSError:
            skipped_count += 1
            continue
        deleted_count += 1
        deleted_bytes += candidate.size
    return CleanupResult(
        mode="apply",
        eligible_count=status.eligible_count,
        eligible_bytes=status.eligible_bytes,
        deleted_count=deleted_count,
        deleted_bytes=deleted_bytes,
        skipped_count=skipped_count,
    )
