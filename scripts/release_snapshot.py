"""Build an isolated Hermes release snapshot with a canonical identity marker.

The DGX deployer can call ``build_snapshot`` and then install the returned
directory. The operation refuses to overwrite an existing snapshot.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from hermes_cli.release_markers import stamp_release_marker


def build_snapshot(source: Path, destination: Path, source_sha: str) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    stamp_release_marker(destination, source_sha)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a Hermes release snapshot")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("source_sha")
    args = parser.parse_args(argv)
    print(build_snapshot(args.source, args.destination, args.source_sha))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
