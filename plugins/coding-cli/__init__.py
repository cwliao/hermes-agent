"""coding-cli plugin -- drive the codex/claude CLIs from Telegram.

Registers two slash commands, ``/codex`` and ``/claude``, that shell out to
the real codex/claude CLI binaries non-interactively (one prompt in, one
response out per call), resuming the same underlying CLI conversation
across consecutive messages in a chat via each CLI's own session/thread id.

Off by default -- requires ``external_cli.enabled: true`` and at least one
``external_cli.allowed_roots`` entry in config.yaml before it will run a
prompt (fail-closed, mirrors the ``web_gate``/``security.tirith_*`` pattern
elsewhere in this repo).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import cli_bridge

logger = logging.getLogger(__name__)


_HELP_TEMPLATE = """\
/{name} -- run a prompt through the {name} CLI (resumable per chat)

Usage:
  /{name} <prompt>       Send a prompt (resumes this chat's session if one exists)
  /{name} dir <path>     Set the working directory for this chat (must be under
                          an external_cli.allowed_roots entry in config.yaml)
  /{name} reset          Start a fresh {name} session next time (keeps the dir)

Requires external_cli.enabled: true and a configured allowed_roots in
config.yaml, and a working directory set via `/{name} dir <path>` before
the first prompt.
"""


def _chat_state_key(backend: str) -> str:
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "") or "cli"
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "") or "default"
    return f"{platform}:{chat_id}:{backend}"


def _load_external_cli_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        block = config.get("external_cli")
        return block if isinstance(block, dict) else {}
    except Exception:
        logger.warning("coding-cli: failed to load config; treating as disabled")
        return {}


def _resolve_profile_home(cfg: dict[str, Any]):
    raw = cfg.get("profile_home")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return cli_bridge.resolve_profile_home(raw)


def _profile_home_error(backend: str) -> str:
    return (
        f"{backend}: external_cli.profile_home must be an existing private "
        f"directory (mode 700 or stricter)."
    )

async def _handle_backend_command(backend: str, raw_args: str) -> str:
    args = raw_args.strip()

    if not args or args in {"help", "-h", "--help"}:
        return _HELP_TEMPLATE.format(name=backend)

    chat_key = _chat_state_key(backend)

    if args == "reset":
        profile_home = _resolve_profile_home(_load_external_cli_config())
        if profile_home is None:
            return _profile_home_error(backend)
        cli_bridge.clear_resume_id(chat_key, profile_home=profile_home)
        return f"{backend}: session reset. Next prompt starts a fresh conversation."

    if args.startswith("dir "):
        requested = args[len("dir "):].strip()
        if not requested:
            return f"Usage: /{backend} dir <path>"
        cfg = _load_external_cli_config()
        allowed_roots = cfg.get("allowed_roots")
        allowed_roots = allowed_roots if isinstance(allowed_roots, list) else []
        if not allowed_roots:
            return (
                f"{backend}: no external_cli.allowed_roots configured. "
                f"An admin must add at least one directory to config.yaml first."
            )
        profile_home = _resolve_profile_home(cfg)
        if profile_home is None:
            return _profile_home_error(backend)
        resolved = cli_bridge.resolve_allowed_dir(requested, allowed_roots)
        if resolved is None:
            return (
                f"{backend}: {requested!r} is not an existing directory under "
                f"an allowed root. Allowed roots: {allowed_roots}"
            )
        cli_bridge.set_chat_state(chat_key, profile_home=profile_home, cwd=str(resolved))
        return f"{backend}: working directory set to {resolved}"

    # Otherwise: treat the whole argument string as a prompt.
    cfg = _load_external_cli_config()
    if not cfg.get("enabled"):
        return (
            f"{backend}: external_cli.enabled is false in config.yaml. "
            f"An admin must enable it (and set allowed_roots) first."
        )

    allowed_roots = cfg.get("allowed_roots")
    allowed_roots = allowed_roots if isinstance(allowed_roots, list) else []
    if not allowed_roots:
        return (
            f"{backend}: no external_cli.allowed_roots configured. "
            f"An admin must add at least one directory to config.yaml first."
        )

    profile_home = _resolve_profile_home(cfg)
    if profile_home is None:
        return _profile_home_error(backend)
    state = cli_bridge.get_chat_state(chat_key, profile_home=profile_home)
    cwd = state.get("cwd")
    if not cwd:
        return (
            f"{backend}: no working directory set for this chat yet. "
            f"Run `/{backend} dir <path>` first."
        )
    # Re-validate on every prompt: allowed_roots can change after `dir` was set.
    if cli_bridge.resolve_allowed_dir(cwd, allowed_roots) is None:
        return (
            f"{backend}: this chat's working directory ({cwd}) is no longer "
            f"under an allowed root. Run `/{backend} dir <path>` again."
        )

    resume_id: Optional[str] = state.get("resume_id")
    timeout = float(cfg.get("timeout_seconds", 180))

    try:
        if backend == "codex":
            text, new_resume_id = await cli_bridge.run_codex_turn(
                args, cwd, resume_id,
                sandbox=str(cfg.get("codex_sandbox", "workspace-write")),
                timeout=timeout,
                profile_home=str(profile_home),
                codex_bin=str(cfg.get("codex_bin", "codex")),
            )
        else:
            text, new_resume_id = await cli_bridge.run_claude_turn(
                args, cwd, resume_id,
                permission_mode=str(cfg.get("claude_permission_mode", "acceptEdits")),
                timeout=timeout,
                profile_home=str(profile_home),
                claude_bin=str(cfg.get("claude_bin", "claude")),
            )
    except cli_bridge.CliTurnError as exc:
        return f"{backend}: {exc}"

    cli_bridge.set_chat_state(chat_key, profile_home=profile_home, resume_id=new_resume_id)
    return f"🤖 {backend}:\n{text}"


async def _handle_codex(raw_args: str) -> str:
    return await _handle_backend_command("codex", raw_args)


async def _handle_claude(raw_args: str) -> str:
    return await _handle_backend_command("claude", raw_args)


def register(ctx) -> None:
    ctx.register_command(
        "codex",
        handler=_handle_codex,
        description="Run a prompt through the codex CLI (resumable per chat).",
        args_hint="<prompt> | dir <path> | reset",
    )
    ctx.register_command(
        "claude",
        handler=_handle_claude,
        description="Run a prompt through the claude CLI (resumable per chat).",
        args_hint="<prompt> | dir <path> | reset",
    )
