"""T0213 Objective #1: bounded auto-restart for a narrow systemd --user allowlist.

``~/.hermes/scripts/failed_unit_watch.sh`` is deliberately general and
deliberately non-repairing: restarting an arbitrary failed unit would
suppress the five-day-signal it exists to surface
(``failed_unit_watch.sh:22-24``). This module does NOT modify that script
and does not read or write any of its state files. It is a separate,
narrower companion that restarts only a small, explicit allowlist of units
already verified (T0213 design section 1) to be stateless/read-mostly HTTP
daemons that already carry their own ``Restart=on-failure``:

    klib-query.service, klib-brain-query.service, docbot.service,
    dochelper.service, kmdaily-api.service

Deliberately excluded from this allowlist (see T0213.md section 1 for the
full per-unit reasoning, cited individually):

  - Anything that touches Gmail send, OAuth, credential handoff, or digest
    composition (trend-mail-auth-watch.timer / auth_watch.py,
    kmdaily-gmail-sync.service, trend-mail-remote-auth-handoff.service,
    kmdaily.service, kmdaily-digest.service, kmdaily-daily-report.service).
    The risk is in what the process does when it (re)starts mid-operation,
    not in whether the process itself is stateless.
  - hermes-gateway.service -- already owned by
    ``hermes_cli.calendar_guard``'s claim/lock recovery
    (``hermes-gateway-recovery.timer``). A second, uncoordinated restart
    path for the same unit would race the existing one and defeat the
    point of that claim lock.
  - The Ollama container target -- already owned by
    ``~/bin/ollama-gpu-healthcheck``'s own ``restart_ollama()``. Same
    "don't build a second uncoordinated repair path" reasoning.

The claim/lock/backoff shape below is the same one
``hermes_cli.calendar_guard`` already implements for
hermes-gateway.service recovery (``_claim_recovery`` /
``_restart_user_service``, ``calendar_guard.py:324-457``): a per-unit state
file tracking attempts within a sliding ``RECOVERY_WINDOW_SECONDS`` window,
exponential backoff seeded at ``COOLDOWN_SECONDS`` and capped at 3600s, and
``MAX_ATTEMPTS`` before the unit is left failed for a human. The constants
are imported directly from ``calendar_guard`` rather than redefined, so the
two mechanisms cannot silently drift apart.

Coverage for "what gets logged/alerted regardless of whether the repair
succeeds" (the ticket's Scope requirement) is deliberately NOT a new
--failed-state alerting path duplicating failed_unit_watch.sh's own --
per the design's own correction, that coverage already exists via
failed_unit_watch.sh's independent journal-based flap detector, which sees
every "Failed with result" line a restart attempt here produces regardless
of outcome. This module only adds one Telegram line per *attempted*
allowlist repair (success or exhaustion), using the same
TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL .env convention
failed_unit_watch.sh's own send_telegram() and mcp_health_check.sh already
use -- not a new notification path.
"""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.calendar_guard import (
    COOLDOWN_SECONDS,
    MAX_ATTEMPTS,
    RECOVERY_WINDOW_SECONDS,
    _atomic_json,
    _exclusive_lock,
    _load_json,
)

# Kept as an explicit, ordered tuple rather than pattern-matched -- adding a
# unit here must be a visible, reviewed decision (same rationale
# failed_unit_watch.sh documents for IGNORE_UNITS).
ALLOWLIST: tuple[str, ...] = (
    "klib-query.service",
    "klib-brain-query.service",
    "docbot.service",
    "dochelper.service",
    "kmdaily-api.service",
)


def _state_dir(home: Path) -> Path:
    return home / "gateway" / "failed_unit_allowlist_state"


def _state_path(home: Path, unit: str) -> Path:
    return _state_dir(home) / f"{unit}.json"


def _lock_path(home: Path, unit: str) -> Path:
    return _state_dir(home) / f"{unit}.lock"


def _systemctl_is_failed(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-failed", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Cannot confirm failure -- do not claim an attempt on a unit we
        # cannot even query. Fail closed (skip), consistent with this
        # module's bounded/never-unlimited-retry design.
        return False
    return result.stdout.strip() == "failed"


def _systemctl_restart(unit: str, *, timeout: float = 90) -> None:
    subprocess.run(
        ["systemctl", "--user", "restart", unit],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _send_telegram(text: str, *, home: Path | None = None) -> bool:
    """Best-effort Telegram notification.

    Reimplements the exact endpoint/env-var/field shape
    failed_unit_watch.sh's own send_telegram() and mcp_health_check.sh
    already use (TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL from
    $HERMES_HOME/.env, POST to api.telegram.org's sendMessage) -- not a new
    notification path, just the same one called from a Python caller
    instead of bash. A delivery failure must not raise: the notification
    is best-effort and secondary to the restart attempt itself.
    """
    home = (home or get_hermes_home()).resolve()
    env_file = home / ".env"
    if not env_file.is_file():
        return False
    bot_token = ""
    home_channel = ""
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                bot_token = line.split("=", 1)[1].strip().strip("'\"")
            elif line.startswith("TELEGRAM_HOME_CHANNEL="):
                home_channel = line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        return False
    if not bot_token or not home_channel:
        return False
    try:
        subprocess.run(
            [
                "curl",
                "-sf",
                "-m",
                "10",
                "-X",
                "POST",
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                "-d",
                f"chat_id={home_channel}",
                "--data-urlencode",
                f"text={text}",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def _claim_attempt(home: Path, unit: str, now: float) -> tuple[str, int]:
    """Claim one bounded restart attempt for ``unit``, or report its status.

    Returns (status, attempts) where status is one of:
      "claimed"   -- go ahead and restart, this is attempt number `attempts`
      "cooldown"  -- still inside backoff from a previous attempt; do nothing
      "exhausted" -- MAX_ATTEMPTS reached within RECOVERY_WINDOW_SECONDS;
                     leave the unit failed for a human
    """
    with _exclusive_lock(_lock_path(home, unit)):
        state = _load_json(_state_path(home, unit))
        if not isinstance(state, dict):
            state = {}
        recovery_attempts = [
            float(value)
            for value in state.get("recovery_attempts", [])
            if isinstance(value, (int, float))
            and float(value) >= now - RECOVERY_WINDOW_SECONDS
        ]
        # Exhaustion is checked before cooldown (not after, as a naive
        # ordering would do): a unit that just hit MAX_ATTEMPTS also has a
        # future next_attempt_at from its last claimed attempt, and a
        # cooldown-first check would report "cooldown" (silent) instead of
        # "exhausted" (notified) for that unit's remaining time in-window --
        # exactly the notification this ticket's Scope requires ("what gets
        # logged/alerted regardless of whether the repair succeeds").
        if len(recovery_attempts) >= MAX_ATTEMPTS:
            if state.get("recovery_exhausted") is True:
                # Already reported this exhaustion; stay silent until the
                # sliding window ages the old attempts out, matching
                # calendar_guard._claim_recovery's own
                # already-exhausted-stays-quiet behavior.
                return "cooldown", len(recovery_attempts)
            state.update(
                {
                    "recovery_attempts": recovery_attempts,
                    "recovery_exhausted": True,
                    "last_outcome": "exhausted",
                    "last_checked_at": now,
                }
            )
            _atomic_json(_state_path(home, unit), state)
            return "exhausted", len(recovery_attempts)
        if float(state.get("next_attempt_at", 0) or 0) > now:
            return "cooldown", len(recovery_attempts)
        attempts = len(recovery_attempts) + 1
        recovery_attempts.append(now)
        state.update(
            {
                "recovery_attempts": recovery_attempts,
                "next_attempt_at": now
                + min(3600, COOLDOWN_SECONDS * (2 ** (attempts - 1))),
                "recovery_exhausted": False,
                "last_outcome": "claimed",
                "last_attempt_at": now,
            }
        )
        _atomic_json(_state_path(home, unit), state)
        return "claimed", attempts


def repair_once(
    *,
    home: Path | None = None,
    now: float | None = None,
    units: tuple[str, ...] = ALLOWLIST,
    is_failed=None,
    restart=None,
    notify=None,
) -> list[str]:
    """Attempt exactly one bounded restart per currently-failed allowlisted unit.

    Injectable ``is_failed``/``restart``/``notify`` are for tests; production
    callers should leave them as the systemctl/Telegram defaults.
    """
    home = (home or get_hermes_home()).resolve()
    now = time.time() if now is None else now
    is_failed = is_failed or _systemctl_is_failed
    restart = restart or _systemctl_restart
    notify = notify or (lambda text: _send_telegram(text, home=home))

    messages: list[str] = []
    for unit in units:
        if not is_failed(unit):
            continue
        status, attempts = _claim_attempt(home, unit, now)
        if status == "cooldown":
            continue
        if status == "exhausted":
            msg = (
                f"failed-unit-allowlist-repair: {unit} exhausted its retry "
                f"budget ({MAX_ATTEMPTS} attempts within "
                f"{RECOVERY_WINDOW_SECONDS}s) -- left failed for a human."
            )
            notify(msg)
            messages.append(msg)
            continue
        try:
            restart(unit)
        except Exception as exc:  # noqa: BLE001 -- report, do not raise
            msg = (
                f"failed-unit-allowlist-repair: restart of {unit} failed "
                f"(attempt {attempts}/{MAX_ATTEMPTS}): {exc}"
            )
        else:
            msg = (
                f"failed-unit-allowlist-repair: restarted {unit} "
                f"(attempt {attempts}/{MAX_ATTEMPTS})."
            )
        notify(msg)
        messages.append(msg)
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "T0213 Objective #1: bounded auto-restart companion for a "
            "narrow failed-unit-watch allowlist"
        )
    )
    parser.parse_args(argv)
    for message in repair_once():
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
