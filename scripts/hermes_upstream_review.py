#!/usr/bin/env python3
"""Create a review-only upstream candidate by rebasing in isolation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from hermes_upstream_preflight import (  # noqa: E402
    DEFAULT_REVIEW_TTL_SECONDS,
    _git,
    _parse_time,
    run_preflight,
)


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str, *, blocked: bool = False) -> None:
        self.code = code
        self.message = message
        self.blocked = blocked
        super().__init__(message)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _update_lock(state_dir: Path, run_id: str, now: datetime) -> Iterator[None]:
    """Hold the JSON lock for the complete review transaction."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "update.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError("LOCKED", "另一個 upstream updater 正在執行。", blocked=True) from exc
        handle.seek(0)
        existing = handle.read()
        if existing.strip():
            try:
                old = json.loads(existing)
            except json.JSONDecodeError:
                old = None
            expires = _parse_time(old.get("expires_at")) if isinstance(old, dict) else None
            if expires is not None and expires > now:
                raise ReviewError("LOCKED", "已有未過期的 upstream update lock。", blocked=True)
        lock = {
            "schema_version": "1.0",
            "owner": "hermes-upstream-review",
            "run_id": run_id,
            "pid": os.getpid(),
            "created_at_utc": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now.replace(microsecond=0)).isoformat().replace("+00:00", "Z"),
        }
        # The lease is intentionally short-lived in the metadata; the OS flock
        # is the real exclusion mechanism. A future long-running apply can set
        # a longer expiry explicitly.
        lock["expires_at"] = (now.timestamp() + 30 * 60)
        lock["expires_at"] = datetime.fromtimestamp(lock["expires_at"], timezone.utc).isoformat().replace("+00:00", "Z")
        handle.seek(0)
        handle.truncate()
        json.dump(lock, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _git_try(repo: Path, args: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=600,
    )
    return completed.returncode, completed.stdout.strip()


def _base_metadata(run_id: str, now: datetime, source_sha: str, upstream_sha: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "mode": "review-only",
        "status": "FAILED",
        "created_at_utc": now.isoformat().replace("+00:00", "Z"),
        "upstream_sha": upstream_sha,
        "source_sha": source_sha,
        "parent_sha": None,
        "candidate_sha": None,
        "local_base_sha": None,
        "replayed_local_commit_count": 0,
        "local_commit_ids": [],
        "rewritten_local_commit_ids": [],
        "release_id": f"hermes-upstream-{run_id}",
        "review_branch": f"refs/upstream/review/{run_id}",
        "lock_id": run_id,
        "checks": {
            "preflight_ok": True,
            "rebase_ok": False,
            "tests_ok": False,
            "noop": False,
        },
        "error_code": None,
        "retry_count": 0,
        "approval": {"approved_by": None, "approved_at_utc": None, "approval_token": None},
        "push_status": "not_attempted",
    }


def _run_id(args: argparse.Namespace, now: datetime) -> str:
    return args.run_id or now.strftime("%Y%m%d-%H%M%S")


def review(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    now = _parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("--now must be an ISO-8601 timestamp")
    repo = Path(args.repo).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    run_id = _run_id(args, now)
    candidate_path = state_dir / "candidates" / f"{run_id}.json"

    preflight_args = argparse.Namespace(
        repo=str(repo),
        state_dir=str(state_dir),
        mode="review",
        run_id=None,
        upstream_remote=args.upstream_remote,
        upstream_ref=args.upstream_ref,
        review_ttl_seconds=args.review_ttl_seconds,
        now=now.isoformat().replace("+00:00", "Z"),
    )
    preflight_code, preflight = run_preflight(preflight_args)
    if preflight_code:
        preflight["phase"] = "review"
        preflight["review_run_id"] = run_id
        return 1, preflight

    metadata: dict[str, Any] = {}
    worktree: Path | None = None
    review_ref = f"refs/upstream/review/{run_id}"
    try:
        with _update_lock(state_dir, run_id, now):
            source_sha = _git(repo, ["rev-parse", "refs/heads/main"])
            fetch_code, _ = _git_try(repo, ["fetch", args.upstream_remote, args.upstream_ref, "--prune"])
            if fetch_code:
                raise ReviewError("UPSTREAM_FETCH_FAIL", "fetch upstream/main 失敗。")
            upstream_sha = _git(repo, ["rev-parse", f"refs/remotes/{args.upstream_remote}/{args.upstream_ref}"])
            metadata = _base_metadata(run_id, now, source_sha, upstream_sha)
            metadata["local_base_sha"] = _git(repo, ["merge-base", upstream_sha, source_sha])
            local_ids = _git(repo, ["rev-list", "--reverse", f"{metadata['local_base_sha']}..{source_sha}"])
            metadata["local_commit_ids"] = local_ids.splitlines() if local_ids else []

            worktree_root = Path(args.worktree_root).expanduser().resolve() if args.worktree_root else state_dir / "worktrees"
            worktree_root.mkdir(parents=True, exist_ok=True)
            worktree = worktree_root / run_id
            if worktree.exists():
                raise ReviewError("STALE_REVIEW", f"review worktree 已存在：{worktree}。", blocked=True)
            add_code, _ = _git_try(repo, ["worktree", "add", "--detach", str(worktree), source_sha])
            if add_code:
                raise ReviewError("REVIEW_WORKTREE_CREATE_FAIL", "無法建立 isolated review worktree。")

            noop_code, _ = _git_try(repo, ["merge-base", "--is-ancestor", upstream_sha, source_sha])
            if noop_code == 0:
                candidate_sha = source_sha
                metadata["checks"]["noop"] = True
            else:
                rebase_code, _ = _git_try(worktree, ["rebase", f"{args.upstream_remote}/{args.upstream_ref}"])
                if rebase_code:
                    _git_try(worktree, ["rebase", "--abort"])
                    raise ReviewError("REBASE_CONFLICT", "rebase upstream/main 發生衝突；已 abort，不建立 review ref。", blocked=True)
                candidate_sha = _git(worktree, ["rev-parse", "HEAD"])
            metadata["candidate_sha"] = candidate_sha
            metadata["parent_sha"] = _git(worktree, ["rev-parse", f"{candidate_sha}^"])
            metadata["rewritten_local_commit_ids"] = _git(worktree, ["rev-list", "--reverse", f"{upstream_sha}..{candidate_sha}"]).splitlines()
            metadata["replayed_local_commit_count"] = len(metadata["rewritten_local_commit_ids"])
            metadata["status"] = "PENDING"
            metadata["checks"]["rebase_ok"] = True
            _write_json(candidate_path, metadata)
            update_code, _ = _git_try(repo, ["update-ref", review_ref, candidate_sha])
            if update_code:
                metadata["status"] = "FAILED"
                metadata["error_code"] = "REVIEW_REF_CREATE_FAIL"
                _write_json(candidate_path, metadata)
                raise ReviewError("REVIEW_REF_CREATE_FAIL", "無法建立 review ref。")
    except ReviewError as exc:
        if not metadata:
            metadata = {
                "schema_version": "1.0",
                "run_id": run_id,
                "mode": "review-only",
                "status": "BLOCKED" if exc.blocked else "FAILED",
                "created_at_utc": now.isoformat().replace("+00:00", "Z"),
                "release_id": f"hermes-upstream-{run_id}",
                "review_branch": review_ref,
                "checks": {"preflight_ok": True, "rebase_ok": False, "tests_ok": False, "noop": False},
                "error_code": exc.code,
            }
        else:
            metadata["status"] = "BLOCKED" if exc.blocked else "FAILED"
            metadata["error_code"] = exc.code
        _write_json(candidate_path, metadata)
        _git_try(repo, ["update-ref", "-d", review_ref])
        return 1, {"phase": "review", "status": metadata["status"], "run_id": run_id, "candidate": metadata, "next_step": "依 error_code 修正後重新執行 review-only；不要 apply。"}
    finally:
        if worktree is not None:
            _git_try(repo, ["worktree", "remove", "--force", str(worktree)])
            shutil.rmtree(worktree, ignore_errors=True)

    return 0, {"phase": "review", "status": "PENDING", "run_id": run_id, "candidate": metadata, "next_step": f"檢查 candidate diff，人工核准後以 run_id={run_id} 執行 apply。"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--worktree-root")
    parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--upstream-ref", default="main")
    parser.add_argument("--review-ttl-seconds", type=int, default=DEFAULT_REVIEW_TTL_SECONDS)
    parser.add_argument("--now")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.review_ttl_seconds <= 0:
        parser.error("--review-ttl-seconds must be positive")
    try:
        code, result = review(args)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({"phase": "review", "status": "FAILED", "error_code": "REVIEW_RUNTIME_FAIL", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
