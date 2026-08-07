#!/usr/bin/env python3
"""Bake a release directory, verify its contents, and write its manifest.

This is a report/fail-loud wrapper around the existing rsync baked-release
workflow.  It verifies content, not just source metadata.  It does not deploy,
restart, or otherwise mutate a service.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = ".release-manifest.json"


def _fail(message: str) -> int:
    print(f"bake-release: ERROR: {message}", file=sys.stderr)
    return 1


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[str, str | None]]:
    """Return a deterministic content snapshot, excluding .git/manifest."""

    result: dict[str, tuple[str, str | None]] = {}

    def visit(directory: Path, relative: str = "") -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name == ".git":
                    continue
                if not relative and name == MANIFEST_NAME:
                    continue
                child_relative = f"{relative}/{name}" if relative else name
                child = directory / name
                if entry.is_symlink():
                    result[child_relative] = ("symlink", os.readlink(child))
                elif entry.is_dir(follow_symlinks=False):
                    result[child_relative] = ("directory", None)
                    visit(child, child_relative)
                elif entry.is_file(follow_symlinks=False):
                    result[child_relative] = ("file", _sha256(child))
                else:
                    raise RuntimeError(f"unsupported filesystem entry: {child}")

    visit(root)
    return result


def _verify_content(source: Path, destination: Path) -> list[str]:
    source_snapshot = _snapshot(source)
    destination_snapshot = _snapshot(destination)
    differences: list[str] = []
    for relative in sorted(set(source_snapshot) | set(destination_snapshot)):
        expected = source_snapshot.get(relative)
        actual = destination_snapshot.get(relative)
        if expected != actual:
            differences.append(
                f"{relative}: source={expected!r}, release={actual!r}"
            )
    return differences


def _manifest(source: Path) -> dict[str, object]:
    branch = _git(source, "symbolic-ref", "--short", "-q", "HEAD") or "DETACHED"
    commit = _git(source, "rev-parse", "--verify", "HEAD^{commit}")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise RuntimeError(f"git returned an invalid commit SHA: {commit!r}")
    return {
        "source_worktree": str(source),
        "source_branch": branch,
        "source_commit": commit,
        "baked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "baked_by": getpass.getuser() or os.environ.get("USER", "unknown"),
        "content_verification": {"method": "checksum", "passed": True},
    }


def bake(source: Path, destination: Path) -> int:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        return _fail(f"source worktree does not exist or is not a directory: {source}")
    if destination == source or source in destination.parents:
        return _fail("destination must not be the source worktree or inside it")

    try:
        _git(source, "rev-parse", "--show-toplevel")
        manifest_path = destination / MANIFEST_NAME
        destination.mkdir(parents=True, exist_ok=True)
        # A failed replacement must not leave a stale success marker behind.
        if manifest_path.exists() or manifest_path.is_symlink():
            manifest_path.unlink()

        command = [
            "rsync",
            "--archive",
            "--delete",
            "--exclude=.git",
            f"--exclude={MANIFEST_NAME}",
            f"{source}{os.sep}",
            f"{destination}{os.sep}",
        ]
        print("running:", " ".join(command))
        rsync = subprocess.run(command, check=False, capture_output=True, text=True)
        if rsync.stdout:
            print(rsync.stdout, end="")
        if rsync.stderr:
            print(rsync.stderr, end="", file=sys.stderr)
        if rsync.returncode != 0:
            return _fail(f"rsync failed with exit code {rsync.returncode}; manifest withheld")
        if rsync.stderr.strip():
            return _fail("rsync wrote to stderr; manifest withheld")

        differences = _verify_content(source, destination)
        if differences:
            print("content differences:", file=sys.stderr)
            for difference in differences[:50]:
                print(f"  {difference}", file=sys.stderr)
            if len(differences) > 50:
                print(f"  ... {len(differences) - 50} more", file=sys.stderr)
            return _fail("content verification failed; manifest withheld")

        manifest = _manifest(source)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f"{MANIFEST_NAME}.", suffix=".tmp", dir=destination
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary_name, manifest_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        print(f"verified content and wrote {manifest_path}")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return _fail(f"{exc}; manifest withheld")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "AC1: rsync a release snapshot, checksum-verify every non-.git file, "
            "then write .release-manifest.json. This is a general deployment "
            "provenance/content check, not the AC3 security-only audit; it never "
            "restarts or switches a service."
        )
    )
    parser.add_argument("source_worktree", type=Path, help="source git worktree")
    parser.add_argument("release_dir", type=Path, help="destination release directory")
    args = parser.parse_args()
    return bake(args.source_worktree, args.release_dir)


if __name__ == "__main__":
    raise SystemExit(main())
