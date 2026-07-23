"""Operator-only CLI for Mermaid PNG artifact lifecycle management."""

from __future__ import annotations

import argparse

from plugins.mermaid_renderer import artifacts


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Attach the ``hermes mermaid-renderer`` subcommand tree."""
    subcommands = parser.add_subparsers(dest="mermaid_renderer_command")
    subcommands.add_parser("status", help="Show bounded Mermaid artifact retention status")
    cleanup = subcommands.add_parser("cleanup", help="Preview or apply expired Mermaid artifact cleanup")
    cleanup.add_argument("--apply", action="store_true", help="Delete only expired revalidated renderer PNGs")
    parser.set_defaults(func=mermaid_renderer_command)


def _print_status(status: artifacts.ArtifactStatus) -> None:
    print(f"root={artifacts.MEDIA_ROOT}")
    print(f"retention_hours={artifacts.RETENTION_HOURS}")
    print(f"eligible_count={status.eligible_count}")
    print(f"eligible_bytes={status.eligible_bytes}")
    print(f"retained_count={status.retained_count}")
    print(f"retained_bytes={status.retained_bytes}")
    print(f"ignored_count={status.ignored_count}")


def _print_cleanup(result: artifacts.CleanupResult) -> None:
    print(f"mode={result.mode}")
    print(f"retention_hours={artifacts.RETENTION_HOURS}")
    print(f"eligible_count={result.eligible_count}")
    print(f"eligible_bytes={result.eligible_bytes}")
    print(f"deleted_count={result.deleted_count}")
    print(f"deleted_bytes={result.deleted_bytes}")
    print(f"skipped_count={result.skipped_count}")


def mermaid_renderer_command(args: argparse.Namespace) -> int:
    """Run a bounded status or cleanup command without exposing filenames."""
    command = getattr(args, "mermaid_renderer_command", None)
    try:
        if command == "status":
            _print_status(artifacts.inspect_artifacts())
            return 0
        if command == "cleanup":
            _print_cleanup(artifacts.cleanup_artifacts(apply=bool(getattr(args, "apply", False))))
            return 0
    except artifacts.ArtifactRootError as exc:
        print("status=failed")
        print(f"error={exc.code}")
        return 1
    print("usage: hermes mermaid-renderer {status,cleanup [--apply]}")
    return 2
