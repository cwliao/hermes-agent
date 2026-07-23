"""cli_bridge -- subprocess plumbing for the coding-cli plugin.

Runs the real ``codex``/``claude`` CLIs non-interactively (one prompt in,
one response out, per call) and persists a per-chat resume id so
consecutive Telegram messages continue the same underlying CLI
conversation. See plugins/coding-cli/__init__.py for the slash-command
handlers that call into this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover -- plugin may load before constants resolves
    import os

    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()

logger = logging.getLogger(__name__)


class CliTurnError(Exception):
    """Raised when a codex/claude CLI turn fails, times out, or exits non-zero."""


_STATE_LOCK = threading.Lock()


def resolve_profile_home(profile_home: str | Path) -> Optional[Path]:
    """Return a private profile directory, or None when it is unsafe."""
    try:
        path = Path(profile_home).expanduser().resolve()
        mode = stat.S_IMODE(path.stat().st_mode)
    except (OSError, TypeError, ValueError):
        return None
    if not path.is_dir() or mode & 0o077:
        return None
    return path


def build_subprocess_env(profile_home: str | Path) -> dict[str, str]:
    """Build a minimal environment for an external coding CLI."""
    profile = resolve_profile_home(profile_home)
    if profile is None:
        raise CliTurnError(
            "external_cli.profile_home must be an existing private directory"
        )
    env: dict[str, str] = {}
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env["HOME"] = str(profile)
    env["XDG_CONFIG_HOME"] = str(profile / "config")
    env["XDG_DATA_HOME"] = str(profile / "data")
    env["XDG_CACHE_HOME"] = str(profile / "cache")
    return env


def _state_path(profile_home: str | Path | None = None) -> Path:
    if profile_home is None:
        return get_hermes_home() / "coding-cli-sessions.json"
    profile = resolve_profile_home(profile_home)
    if profile is None:
        raise ValueError("external_cli.profile_home is not a private directory")
    return profile / "coding-cli-sessions.json"


def _load_state(profile_home: str | Path | None = None) -> dict[str, Any]:
    path = _state_path(profile_home)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("coding-cli: failed to read state file %s; starting fresh", path)
        return {}


def _save_state(state: dict[str, Any], profile_home: str | Path | None = None) -> None:
    path = _state_path(profile_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(path)


def get_chat_state(chat_key: str, *, profile_home: str | Path | None = None) -> dict[str, Any]:
    """Return the persisted {"resume_id": ..., "cwd": ...} for a chat/backend key."""
    with _STATE_LOCK:
        return dict(_load_state(profile_home).get(chat_key, {}))


def set_chat_state(chat_key: str, *, profile_home: str | Path | None = None, **updates: Any) -> None:
    """Merge ``updates`` into the persisted state for ``chat_key``."""
    with _STATE_LOCK:
        state = _load_state(profile_home)
        entry = dict(state.get(chat_key, {}))
        entry.update(updates)
        state[chat_key] = entry
        _save_state(state, profile_home)


def clear_resume_id(chat_key: str, *, profile_home: str | Path | None = None) -> None:
    """Drop the stored resume id for ``chat_key`` (keeps ``cwd``)."""
    with _STATE_LOCK:
        state = _load_state(profile_home)
        entry = dict(state.get(chat_key, {}))
        entry.pop("resume_id", None)
        state[chat_key] = entry
        _save_state(state, profile_home)


def resolve_allowed_dir(requested: str, allowed_roots: list[str]) -> Optional[Path]:
    """Resolve ``requested`` and confirm it's under one of ``allowed_roots``.

    Returns the resolved Path on success, or None if the path doesn't
    exist, isn't a directory, or escapes every allowed root.
    """
    try:
        candidate = Path(requested).expanduser().resolve()
    except Exception:
        return None
    if not candidate.is_dir():
        return None

    for root in allowed_roots:
        try:
            resolved_root = Path(root).expanduser().resolve()
        except Exception:
            continue
        if candidate == resolved_root or resolved_root in candidate.parents:
            return candidate
    return None


def _resolve_bin(configured: str) -> str:
    found = shutil.which(configured)
    return found or configured


async def _run_subprocess(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    profile_home: str | Path,
) -> tuple[str, str]:
    """Run argv, returning (stdout, stderr). Raises CliTurnError on timeout/failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_subprocess_env(profile_home),
        )
    except Exception as exc:
        raise CliTurnError(f"failed to start {argv[0]!r}: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise CliTurnError(f"{argv[0]} timed out after {timeout:.0f}s")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        tail = stderr.strip()[-2000:] or stdout.strip()[-2000:]
        raise CliTurnError(f"{argv[0]} exited {proc.returncode}: {tail}")

    return stdout, stderr


def _parse_codex_jsonl(stdout: str) -> tuple[str, Optional[str]]:
    """Parse codex exec --json output. Returns (response_text, thread_id)."""
    thread_id: Optional[str] = None
    messages: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid:
                thread_id = tid
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    messages.append(text)

    if not thread_id:
        raise CliTurnError("codex output did not include a thread_id")
    if not messages:
        raise CliTurnError("codex produced no agent_message output")
    return "\n\n".join(messages), thread_id


async def run_codex_turn(
    prompt: str,
    cwd: str,
    resume_id: Optional[str],
    *,
    sandbox: str,
    timeout: float,
    profile_home: str | Path,
    codex_bin: str = "codex",
) -> tuple[str, str]:
    """Run one codex CLI turn. Returns (response_text, new_resume_id)."""
    binary = _resolve_bin(codex_bin)
    if resume_id:
        argv = [
            binary, "exec", "resume", resume_id, prompt,
            "--json", "--skip-git-repo-check", "-C", cwd, "-s", sandbox,
        ]
    else:
        argv = [
            binary, "exec", prompt,
            "--json", "--skip-git-repo-check", "-C", cwd, "-s", sandbox,
        ]
    stdout, _stderr = await _run_subprocess(argv, cwd=cwd, timeout=timeout, profile_home=profile_home)
    return _parse_codex_jsonl(stdout)


def _parse_claude_json(stdout: str) -> tuple[str, str]:
    """Parse claude -p --output-format json output. Returns (response_text, session_id)."""
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise CliTurnError(f"claude produced invalid JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise CliTurnError("claude output was not a JSON object")
    if payload.get("is_error"):
        raise CliTurnError(f"claude reported an error: {payload.get('result')!r}")

    result = payload.get("result")
    session_id = payload.get("session_id")
    if not isinstance(result, str) or not result:
        raise CliTurnError("claude output had no result text")
    if not isinstance(session_id, str) or not session_id:
        raise CliTurnError("claude output did not include a session_id")
    return result, session_id


async def run_claude_turn(
    prompt: str,
    cwd: str,
    resume_id: Optional[str],
    *,
    permission_mode: str,
    timeout: float,
    profile_home: str | Path,
    claude_bin: str = "claude",
) -> tuple[str, str]:
    """Run one claude CLI turn. Returns (response_text, new_resume_id)."""
    binary = _resolve_bin(claude_bin)
    argv = [
        binary, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", permission_mode,
    ]
    if resume_id:
        argv += ["--resume", resume_id]
    stdout, _stderr = await _run_subprocess(argv, cwd=cwd, timeout=timeout, profile_home=profile_home)
    return _parse_claude_json(stdout)
