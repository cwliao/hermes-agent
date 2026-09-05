#!/usr/bin/env python3
"""Read-only preflight gate for the Hermes upstream updater.

The review and apply phases deliberately share this gate. It may write a JSON
receipt under the configured state directory, but it never changes a Git ref,
the working tree, a deployment, or a remote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_REVIEW_TTL_SECONDS = 7 * 24 * 60 * 60
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
REJECTED_PUSH_STATES = {"rejected", "push_rejected", "push-rejected"}
ACTIVE_CANDIDATE_STATES = {"PENDING", "APPROVED"}


@dataclass(frozen=True)
class Issue:
    code: str
    status: str
    message: str
    next_steps: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "next_steps": list(self.next_steps),
        }


class GitProbeError(RuntimeError):
    """Raised only for an unavailable Git probe, without exposing stderr."""

    def __init__(self, args: Iterable[str], returncode: int | None = None) -> None:
        self.args_list = tuple(args)
        self.returncode = returncode
        super().__init__("git probe failed")


def _git(repo: Path, args: Iterable[str], *, timeout: float = 45.0) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitProbeError(command) from exc
    if completed.returncode != 0:
        raise GitProbeError(command, completed.returncode)
    return completed.stdout.strip()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA_RE.fullmatch(value))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _review_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    ref = value.strip()
    return ref if ref.startswith("refs/") else f"refs/{ref}"


def _issue(
    issues: list[Issue],
    code: str,
    status: str,
    message: str,
    *next_steps: str,
) -> None:
    issues.append(Issue(code, status, message, tuple(next_steps)))


def _check_repository(repo: Path, issues: list[Issue]) -> dict[str, Any]:
    try:
        branch = _git(repo, ["branch", "--show-current"]) or "(detached HEAD)"
    except GitProbeError:
        _issue(
            issues,
            "NOT_A_GIT_REPOSITORY",
            "FAILED",
            "無法讀取 Hermes repo 的 Git 狀態。",
            f"確認 repo 路徑存在且可執行：git -C {repo} status",
        )
        return {"branch": None, "dirty": None}

    if branch != "main":
        _issue(
            issues,
            "NON_MAIN_BRANCH",
            "BLOCKED",
            f"目前分支是 {branch}；upstream updater 只允許從 main 開始。",
            f"先檢查後切回 main：git -C {repo} switch main",
        )

    try:
        porcelain = _git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    except GitProbeError:
        _issue(
            issues,
            "GIT_STATUS_FAIL",
            "FAILED",
            "無法讀取 working tree 狀態。",
            f"重試：git -C {repo} status --short",
        )
        return {"branch": branch, "dirty": None}

    dirty_entries = [line for line in porcelain.splitlines() if line.strip()]
    if dirty_entries:
        _issue(
            issues,
            "DIRTY_WORKTREE",
            "BLOCKED",
            f"working tree 不乾淨（{len(dirty_entries)} 個變更）；為避免遺失本地檔案，流程停止。",
            f"先檢查：git -C {repo} status --short",
            "完成 commit、stash 或人工處理後重新執行 preflight。",
        )
    return {"branch": branch, "dirty": bool(dirty_entries), "dirty_count": len(dirty_entries)}


def _check_upstream(
    repo: Path,
    remote: str,
    ref: str,
    issues: list[Issue],
) -> dict[str, Any]:
    try:
        url = _git(repo, ["remote", "get-url", remote])
    except GitProbeError:
        _issue(
            issues,
            "UPSTREAM_FETCH_FAIL",
            "FAILED",
            f"找不到可用的 {remote} remote，無法驗證 {remote}/{ref}。",
            f"檢查 remote：git -C {repo} remote -v",
            f"修正後重試：git -C {repo} ls-remote --heads {remote} {ref}",
        )
        return {"remote": remote, "ref": ref, "reachable": False}

    # ls-remote verifies network, credentials, and the exact branch while
    # leaving all local refs untouched.
    try:
        output = _git(repo, ["ls-remote", "--exit-code", "--heads", remote, ref])
    except GitProbeError:
        _issue(
            issues,
            "UPSTREAM_FETCH_FAIL",
            "FAILED",
            f"無法讀取 {remote}/{ref}；未更新任何 local ref。",
            f"重試：git -C {repo} ls-remote --heads {remote} {ref}",
            "確認網路、DNS、remote 權限與 upstream branch 名稱後再重跑。",
        )
        return {"remote": remote, "ref": ref, "url": url, "reachable": False}

    upstream_sha = output.split()[0] if output.split() else None
    if not _valid_sha(upstream_sha):
        _issue(
            issues,
            "UPSTREAM_FETCH_FAIL",
            "FAILED",
            f"{remote}/{ref} 回傳格式無法辨識；未更新任何 local ref。",
            f"人工檢查：git -C {repo} ls-remote --heads {remote} {ref}",
        )
        return {"remote": remote, "ref": ref, "url": url, "reachable": False}
    return {
        "remote": remote,
        "ref": ref,
        "url": url,
        "reachable": True,
        "remote_sha": upstream_sha,
    }


def _candidate_files(candidate_dir: Path, run_id: str | None) -> list[Path]:
    if run_id:
        return [candidate_dir / f"{run_id}.json"]
    if not candidate_dir.is_dir():
        return []
    return sorted(candidate_dir.glob("*.json"))


def _check_candidates(
    repo: Path,
    state_dir: Path,
    mode: str,
    run_id: str | None,
    now: datetime,
    ttl_seconds: int,
    issues: list[Issue],
) -> tuple[list[tuple[Path, dict[str, Any]]], dict[str, Any]]:
    candidate_dir = state_dir / "candidates"
    active: list[tuple[Path, dict[str, Any]]] = []
    inspected = 0
    for path in _candidate_files(candidate_dir, run_id):
        if not path.exists():
            if mode == "apply" and run_id:
                _issue(
                    issues,
                    "NO_APPROVED_CANDIDATE",
                    "BLOCKED",
                    f"找不到指定 candidate metadata：{path}。",
                    "先完成 review-only 並取得仍有效的 candidate run_id。",
                )
            continue
        inspected += 1
        metadata = _load_json(path)
        if metadata is None:
            _issue(
                issues,
                "STALE_REVIEW_CANDIDATE",
                "BLOCKED",
                f"candidate metadata 無法解析：{path}；未自動刪除，需人工檢查。",
                f"檢查 JSON：python -m json.tool {path}",
                "確認後重新產生 candidate，或由 operator 依 runbook 清理。",
            )
            continue

        status = str(metadata.get("status") or "").upper()
        if status not in ACTIVE_CANDIDATE_STATES:
            if mode == "apply" and run_id:
                _issue(
                    issues,
                    "NO_APPROVED_CANDIDATE",
                    "BLOCKED",
                    f"candidate {path.name} 狀態是 {status or 'MISSING'}，不是 APPROVED。",
                    "只可對人工核准且 metadata 未過期的 candidate 執行 apply。",
                )
            continue

        created = _parse_time(metadata.get("created_at_utc"))
        expires = _parse_time(metadata.get("expires_at_utc"))
        if expires is None and created is not None:
            expires = created + timedelta(seconds=ttl_seconds)
        if expires is None or expires <= now:
            _issue(
                issues,
                "STALE_REVIEW_CANDIDATE",
                "BLOCKED",
                f"candidate {path.name} 已過期或缺少有效的 created_at_utc。",
                "重新執行 review-only 產生新的 candidate，不要直接沿用舊 marker。",
            )

        required = ("candidate_sha", "source_sha", "parent_sha", "release_id", "review_branch")
        missing = [key for key in required if not metadata.get(key)]
        invalid_sha = [
            key for key in ("candidate_sha", "source_sha", "parent_sha")
            if not _valid_sha(metadata.get(key))
        ]
        if missing or invalid_sha:
            detail = []
            if missing:
                detail.append("缺少 " + ", ".join(missing))
            if invalid_sha:
                detail.append("SHA 格式錯誤 " + ", ".join(invalid_sha))
            _issue(
                issues,
                "STALE_REVIEW_CANDIDATE",
                "BLOCKED",
                f"candidate {path.name} metadata 不完整（{'；'.join(detail)}）。",
                "重新產生 candidate metadata，保留完整 SHA、parent 與 release_id。",
            )

        review_ref = _review_ref(metadata.get("review_branch"))
        if review_ref:
            try:
                _git(repo, ["rev-parse", "--verify", f"{review_ref}^{{commit}}"])
            except GitProbeError:
                _issue(
                    issues,
                    "STALE_REVIEW_CANDIDATE",
                    "BLOCKED",
                    f"candidate {path.name} 指向的 review ref 不存在：{review_ref}。",
                    "重新執行 review-only，或依 runbook 檢查候選 ref 與 metadata 是否一致。",
                )

        push_status = str(metadata.get("push_status") or "").lower()
        if push_status in REJECTED_PUSH_STATES or metadata.get("push_rejected") is True:
            _issue(
                issues,
                "PUSH_REJECTED",
                "BLOCKED",
                f"candidate {path.name} 已記錄 origin push rejected；不視為成功。",
                "確認 origin/main 是否前進，重新 review candidate 後再重試 push。",
            )

        active.append((path, metadata))

    # Reserved review refs must always have a matching metadata record. This
    # check is read-only and intentionally leaves orphan refs for review.
    try:
        refs = _git(repo, ["for-each-ref", "--format=%(refname)", "refs/upstream/review"]).splitlines()
    except GitProbeError:
        refs = []
    known_refs = {
        _review_ref(metadata.get("review_branch"))
        for _, metadata in active
        if _review_ref(metadata.get("review_branch"))
    }
    for ref in refs:
        if ref not in known_refs:
            _issue(
                issues,
                "STALE_REVIEW_CANDIDATE",
                "BLOCKED",
                f"發現沒有 metadata 對應的 review ref：{ref}；未自動刪除。",
                "檢查 candidate metadata 後依 runbook 清理 orphan ref，或重新產生 review。",
            )

    # The old updater used a single branch outside the new namespace. Report it
    # rather than silently removing it so an operator can inspect it.
    try:
        _git(repo, ["rev-parse", "--verify", "refs/heads/upstream-review-pending^{commit}"])
    except GitProbeError:
        pass
    else:
        _issue(
            issues,
            "STALE_REVIEW_CANDIDATE",
            "BLOCKED",
            "發現 legacy upstream-review-pending branch；新流程不會自動沿用。",
            "檢查 branch 與候選內容後，重新產生新格式 review candidate。",
        )

    if mode == "apply":
        approved = [item for item in active if str(item[1].get("status")).upper() == "APPROVED"]
        if run_id and len(approved) != 1:
            _issue(
                issues,
                "NO_APPROVED_CANDIDATE",
                "BLOCKED",
                "apply 必須精確對應一份 APPROVED candidate metadata。",
                "指定唯一 run_id，並重新確認人工 approval 與 candidate SHA。",
            )
        elif not run_id and len(approved) != 1:
            _issue(
                issues,
                "NO_APPROVED_CANDIDATE",
                "BLOCKED",
                f"目前有 {len(approved)} 份 APPROVED candidate；apply 不會自行猜測。",
                "明確指定唯一 run_id 後重新執行 apply preflight。",
            )

    return active, {"directory": str(candidate_dir), "inspected": inspected, "active": len(active)}


def _check_lock(state_dir: Path, now: datetime, issues: list[Issue]) -> dict[str, Any]:
    path = state_dir / "update.lock"
    if not path.exists():
        return {"path": str(path), "present": False}
    metadata = _load_json(path)
    if metadata is None:
        _issue(
            issues,
            "STALE_LOCK",
            "BLOCKED",
            f"upstream update lock 不是有效 JSON：{path}；不會自動清除。",
            "確認沒有活躍 updater 後，依 runbook 人工處理 stale lock。",
        )
        return {"path": str(path), "present": True, "valid": False}
    expires = _parse_time(metadata.get("expires_at"))
    required = ("owner", "run_id", "pid", "expires_at")
    missing = [key for key in required if metadata.get(key) in (None, "")]
    if missing or expires is None or expires <= now:
        reason = "已過期" if expires is not None and expires <= now else "欄位缺失或時間格式無效"
        _issue(
            issues,
            "STALE_LOCK",
            "BLOCKED",
            f"upstream update lock {reason}：{path}；不會自動清除。",
            "確認 owner/pid 沒有活躍流程後，依 runbook 人工清鎖並重跑。",
        )
    return {
        "path": str(path),
        "present": True,
        "valid": not missing and expires is not None,
        "expires_at": metadata.get("expires_at"),
    }


def _check_marker(repo: Path, state_dir: Path, issues: list[Issue]) -> dict[str, Any]:
    path = state_dir / "apply.marker.json"
    if not path.exists():
        return {"path": str(path), "present": False}
    marker = _load_json(path)
    required = ("run_id", "candidate_sha", "release_id", "metadata_sha256")
    if marker is None or any(not marker.get(key) for key in required):
        _issue(
            issues,
            "MARKER_SIGNATURE_MISMATCH",
            "BLOCKED",
            f"apply marker 缺少完整簽章欄位：{path}；不會自動覆寫。",
            "重新驗證 candidate metadata，必要時由 operator 依 runbook 重建 marker。",
        )
        return {"path": str(path), "present": True, "valid": False}

    candidate_path = state_dir / "candidates" / f"{marker['run_id']}.json"
    candidate = _load_json(candidate_path)
    actual_digest = None
    try:
        actual_digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    except OSError:
        pass
    mismatches: list[str] = []
    if candidate is None:
        mismatches.append("candidate metadata 不存在或無法解析")
    else:
        if candidate.get("candidate_sha") != marker.get("candidate_sha"):
            mismatches.append("candidate_sha 不一致")
        if candidate.get("release_id") != marker.get("release_id"):
            mismatches.append("release_id 不一致")
    if actual_digest != marker.get("metadata_sha256"):
        mismatches.append("metadata_sha256 不一致")
    if not _valid_sha(marker.get("candidate_sha")):
        mismatches.append("candidate_sha 格式錯誤")
    else:
        try:
            _git(repo, ["cat-file", "-e", f"{marker['candidate_sha']}^{{commit}}"])
        except GitProbeError:
            mismatches.append("candidate_sha 不存在於 repo object database")
    if mismatches:
        _issue(
            issues,
            "MARKER_SIGNATURE_MISMATCH",
            "BLOCKED",
            f"apply marker 與 candidate 不一致：{'；'.join(mismatches)}。",
            "停止 apply，重新產生 candidate/marker；不要以手動覆寫方式跳過驗證。",
        )
    return {"path": str(path), "present": True, "valid": not mismatches, "run_id": marker.get("run_id")}


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
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


def run_preflight(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    repo = Path(args.repo).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    now = _parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("--now must be an ISO-8601 timestamp")
    run_id = args.run_id or now.strftime("%Y%m%d-%H%M%S")
    issues: list[Issue] = []

    repository = _check_repository(repo, issues)
    upstream = _check_upstream(repo, args.upstream_remote, args.upstream_ref, issues)
    _, candidates = _check_candidates(
        repo,
        state_dir,
        args.mode,
        args.run_id,
        now,
        args.review_ttl_seconds,
        issues,
    )
    lock = _check_lock(state_dir, now, issues)
    marker = _check_marker(repo, state_dir, issues)

    statuses = {issue.status for issue in issues}
    if "FAILED" in statuses:
        status = "FAILED"
    elif issues:
        status = "BLOCKED"
    else:
        status = "READY"
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "phase": args.mode,
        "status": status,
        "created_at_utc": now.isoformat().replace("+00:00", "Z"),
        "repo": str(repo),
        "repository": repository,
        "upstream": upstream,
        "candidates": candidates,
        "lock": lock,
        "marker": marker,
        "issues": [issue.as_dict() for issue in issues],
        "read_only_contract": {
            "git_ref_updates": False,
            "working_tree_mutation": False,
            "deployment_mutation": False,
            "remote_push": False,
        },
    }
    receipt = state_dir / "preflight" / f"{run_id}.json"
    try:
        result["artifact_path"] = str(receipt)
        _write_receipt(receipt, result)
    except OSError:
        _issue(
            issues,
            "STATUS_ARTIFACT_WRITE_FAIL",
            "FAILED",
            f"無法寫入 preflight status artifact：{receipt}。",
            "確認 state directory 權限與磁碟空間後重試。",
        )
        result["status"] = "FAILED"
        result["issues"] = [issue.as_dict() for issue in issues]
    return (0 if result["status"] == "READY" else 1), result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Hermes development checkout")
    parser.add_argument("--state-dir", required=True, help="upstream state directory")
    parser.add_argument("--mode", choices=("review", "apply"), default="review")
    parser.add_argument("--run-id", help="candidate run id required by apply")
    parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--upstream-ref", default="main")
    parser.add_argument("--review-ttl-seconds", type=int, default=DEFAULT_REVIEW_TTL_SECONDS)
    parser.add_argument("--now", help="ISO-8601 time, primarily for deterministic tests")
    parser.add_argument("--json", action="store_true", help="emit the structured receipt (default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.review_ttl_seconds <= 0:
        parser.error("--review-ttl-seconds must be positive")
    try:
        code, result = run_preflight(args)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
