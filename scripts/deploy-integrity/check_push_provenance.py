#!/usr/bin/env python3
"""Check whether a commit is present on any origin remote-tracking branch."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


COMMIT_INPUT = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def check(repo: Path, commit: str, remote: str) -> int:
    if not COMMIT_INPUT.fullmatch(commit):
        print("PUSH_PROVENANCE: UNDETERMINED", file=sys.stderr)
        print("commit must be a hexadecimal hash (7-40 characters)", file=sys.stderr)
        return 2

    fetch = _git(repo, "fetch", remote)
    if fetch.stdout:
        print(fetch.stdout, end="")
    if fetch.stderr:
        print(fetch.stderr, end="", file=sys.stderr)
    if fetch.returncode != 0:
        print("PUSH_PROVENANCE: UNDETERMINED", file=sys.stderr)
        print(
            f"unable to determine provenance: git fetch {remote!r} failed "
            f"with exit code {fetch.returncode}",
            file=sys.stderr,
        )
        return 2

    resolved = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved.returncode != 0:
        print("PUSH_PROVENANCE: UNDETERMINED", file=sys.stderr)
        print("commit is not available as a local commit object after fetch", file=sys.stderr)
        return 2
    sha = resolved.stdout.strip()

    branches = _git(repo, "branch", "-r", "--contains", sha)
    if branches.returncode != 0:
        print("PUSH_PROVENANCE: UNDETERMINED", file=sys.stderr)
        print("git branch -r --contains failed", file=sys.stderr)
        if branches.stderr:
            print(branches.stderr, end="", file=sys.stderr)
        return 2

    origin_branches = []
    for line in branches.stdout.splitlines():
        name = line.strip().lstrip("* ").strip()
        if name.startswith(f"{remote}/"):
            origin_branches.append(name)
    if origin_branches:
        print("PUSH_PROVENANCE: PRESENT")
        print(f"commit: {sha}")
        print("origin branches: " + ", ".join(origin_branches))
        print("This proves origin provenance only; it does not prove deployment.")
        return 0

    print("PUSH_PROVENANCE: ABSENT")
    print(f"commit: {sha}")
    print(f"no {remote}/ remote-tracking branch contains this commit")
    print("This proves origin provenance only; it does not prove deployment.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "AC2: fetch a remote and check whether a commit exists on any of its "
            "branches. This is push provenance only, not deployment verification; "
            "AC1 verifies baked content and AC3 verifies running security content."
        )
    )
    parser.add_argument("commit", help="7-40 character commit hash")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="git repository (default: cwd)")
    parser.add_argument("--remote", default="origin", help="remote to fetch/check (default: origin)")
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"PUSH_PROVENANCE: UNDETERMINED\nrepository does not exist: {repo}", file=sys.stderr)
        return 2
    return check(repo, args.commit, args.remote)


if __name__ == "__main__":
    raise SystemExit(main())
