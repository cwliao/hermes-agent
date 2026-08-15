#!/usr/bin/env python3
"""Inspect an upstream candidate without mutating a checkout or runtime.

This tool deliberately stops at source metadata.  It does not fetch, merge,
reset, create a worktree, read ``~/.hermes``, or contact DGX.  The resulting
report is safe to attach to the HERMES-UPDATE-001 review packet because it
contains Git identity and path metadata only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Sequence


REPORT_SCHEMA = 1


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _category(path: str) -> str:
    first = path.split("/", 1)[0]
    if first in {"agent", "hermes_cli", "hermes_state.py", "hermes_state"}:
        return "agent-and-state"
    if first == "gateway" or path.startswith("plugins/platforms/"):
        return "gateway-and-platforms"
    if first in {"cron", "skills", "optional-skills"}:
        return "cron-and-skills"
    if first == "plugins":
        return "plugins"
    if first in {"scripts", ".github", "pyproject.toml", "uv.lock"}:
        return "build-and-ci"
    if first in {"apps", "ui-tui", "tui_gateway", "website"}:
        return "desktop-and-web"
    if first in {"docs", "README.md", "AGENTS.md"}:
        return "documentation"
    return "other"


def inspect_refs(repo: Path, private_ref: str, upstream_ref: str) -> dict:
    """Return deterministic, metadata-only comparison data for two refs."""

    repo = repo.resolve()
    if not repo.is_dir():
        raise ValueError(f"repo is not a directory: {repo}")

    private_sha = _git(repo, "rev-parse", "--verify", f"{private_ref}^{{commit}}")
    upstream_sha = _git(repo, "rev-parse", "--verify", f"{upstream_ref}^{{commit}}")
    merge_base = _git(repo, "merge-base", private_ref, upstream_ref)
    counts = _git(
        repo,
        "rev-list",
        "--left-right",
        "--count",
        f"{private_ref}...{upstream_ref}",
    ).split()
    if len(counts) != 2:
        raise ValueError("git returned an invalid symmetric-difference count")

    changed_paths = [
        path for path in _git(repo, "diff", "--name-only", private_ref, upstream_ref).splitlines() if path
    ]
    categories = Counter(_category(path) for path in changed_paths)
    return {
        "schema_version": REPORT_SCHEMA,
        "repository": str(repo),
        "private_ref": {"name": private_ref, "sha": private_sha},
        "upstream_ref": {"name": upstream_ref, "sha": upstream_sha},
        "merge_base": merge_base,
        "private_only_commits": int(counts[0]),
        "upstream_only_commits": int(counts[1]),
        "changed_file_count": len(changed_paths),
        "changed_path_categories": dict(sorted(categories.items())),
        "candidate_policy": "inspect-only; retain current private release until matrix PASS",
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--private-ref", default="origin/main")
    parser.add_argument("--upstream-ref", default="upstream/main")
    parser.add_argument(
        "--output",
        type=Path,
        help="write a new JSON report; existing files are never overwritten",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    output = args.output.resolve() if args.output is not None else None
    if output is not None and output.exists():
        raise SystemExit(f"refusing to overwrite existing report: {output}")
    report = inspect_refs(args.repo, args.private_ref, args.upstream_ref)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
        return 0
    assert output is not None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8", newline="\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
