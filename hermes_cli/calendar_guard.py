"""Calendar guard and supervisor-owned gateway recovery.

The hourly check is safe to run as a child of the gateway. It never restarts
the gateway itself; it writes a request consumed by a separate user-systemd
oneshot service. ``--recover`` is intended to run only from that service.

The guard keeps its own bounded fail-closed lock rather than calling the cron
jobs lock directly: the guard state lives outside ``cron/jobs.py`` and a lock
timeout must block recovery/reporting instead of degrading to an unlocked
write. Both paths use the same bounded ``flock`` shape and lock the complete
request/state critical section.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator

from hermes_constants import get_hermes_home

from gateway.code_skew import read_boot_record
from hermes_cli.gateway_identity import (
    GatewayIdentity,
    GatewayIdentityError,
    SERVICE_NAME,
    active_gateway_identity,
    short_fingerprint,
)

STATE_SCHEMA = 1
MAX_ATTEMPTS = 3
COOLDOWN_SECONDS = 300
RECOVERY_TIMEOUT_SECONDS = 120
POLL_SECONDS = 2


def _state_path(home: Path) -> Path:
    return home / "gateway" / "calendar_guard_state.json"


def _request_path(home: Path) -> Path:
    return home / "gateway" / "calendar_guard_request.json"


def _lock_path(home: Path) -> Path:
    return home / "gateway" / "calendar_guard.lock"


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


@contextlib.contextmanager
def _exclusive_lock(path: Path, timeout: float = 5.0) -> Iterator[None]:
    """Use the same bounded flock shape as ``cron.jobs._jobs_lock``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    try:
        try:
            import fcntl

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            if not locked:
                raise TimeoutError(f"timed out waiting for {path}")
        except ImportError:
            # Windows has no fcntl; the supervisor path is only supported on
            # POSIX, but keeping the context usable makes unit tests portable.
            locked = True
        yield
    finally:
        if locked:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        handle.close()


def _incident_key(boot: dict[str, object] | None, identity: GatewayIdentity, reason: str) -> str:
    boot_fp = str((boot or {}).get("fingerprint", "missing"))
    material = "|".join((boot_fp, identity.fingerprint, reason))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _state(home: Path) -> dict[str, object]:
    value = _load_json(_state_path(home))
    return value if value.get("schema") == STATE_SCHEMA else {"schema": STATE_SCHEMA}


def _save_state(home: Path, state: dict[str, object]) -> None:
    state["schema"] = STATE_SCHEMA
    _atomic_json(_state_path(home), state)


def _write_request(home: Path, request: dict[str, object]) -> None:
    _atomic_json(_request_path(home), request)


def _format_issue(identity: GatewayIdentity | None, message: str) -> str:
    prefix = "Hermes Calendar Guard"
    if identity is not None:
        prefix += f" ({short_fingerprint(identity.fingerprint)})"
    return f"{prefix}: {message}"


def check_once(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    now: float | None = None,
    service_name: str = SERVICE_NAME,
    runner=None,
) -> str:
    """Run the hourly check and return output for the cron delivery target."""
    home = (home or get_hermes_home()).resolve()
    project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    now = time.time() if now is None else now
    identity: GatewayIdentity | None = None
    try:
        identity = active_gateway_identity(
            home, project_root=project_root, service_name=service_name, runner=runner
        )
        props = identity.service_properties
        if props.get("ActiveState") != "active" or props.get("SubState") != "running":
            raise GatewayIdentityError(
                f"gateway service is {props.get('ActiveState')}/{props.get('SubState')}"
            )
        boot = read_boot_record(home)
        if not boot:
            raise GatewayIdentityError("gateway boot fingerprint is missing or invalid")
        if boot.get("fingerprint") != identity.fingerprint:
            raise GatewayIdentityError(
                "gateway boot identity differs: "
                f"boot {short_fingerprint(str(boot.get('fingerprint')))}, "
                f"active {short_fingerprint(identity.fingerprint)}"
            )
        if identity.release_path and boot.get("release_path"):
            if Path(str(boot["release_path"])).resolve() != identity.release_path.resolve():
                raise GatewayIdentityError("gateway boot release path differs from active release")
        return ""
    except GatewayIdentityError as exc:
        reason = str(exc)
        key = _incident_key(
            read_boot_record(home),
            identity or GatewayIdentity("unknown", "unknown", None, None, {}),
            reason,
        )
        try:
            with _exclusive_lock(_lock_path(home)):
                state = _state(home)
                same_incident = state.get("incident_key") == key
                if same_incident and state.get("recovery_exhausted") is True:
                    return ""
                if same_incident and float(
                    state.get("notification_cooldown_until", 0) or 0
                ) > now:
                    return ""
                _write_request(
                    home,
                    {
                        "schema": STATE_SCHEMA,
                        "incident_key": key,
                        "service": service_name,
                        "reason": reason,
                        "requested_at": now,
                    },
                )
                if not same_incident:
                    state.update(
                        {"attempts": 0, "next_attempt_at": 0, "recovery_exhausted": False}
                    )
                state.update(
                    {
                        "incident_key": key,
                        "last_outcome": "QUEUED",
                        "notification_at": now,
                        "notification_cooldown_until": now + COOLDOWN_SECONDS,
                    }
                )
                _save_state(home, state)
        except OSError as lock_error:
            return "WARNING: " + _format_issue(
                identity, f"{reason}; state lock unavailable: {lock_error}"
            )
        return "WARNING: " + _format_issue(identity, f"{reason}; recovery queued")


def _restart_user_service(service_name: str) -> None:
    env = os.environ.copy()
    env.pop("_HERMES_GATEWAY", None)
    env.pop("HERMES_GATEWAY_SESSION", None)
    subprocess.run(
        ["systemctl", "--user", "restart", service_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )


def recover_once(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    now: float | None = None,
    service_name: str = SERVICE_NAME,
    runner=None,
    restart=None,
    sleep=time.sleep,
) -> str:
    """Consume one request from the supervisor-owned recovery service."""
    home = (home or get_hermes_home()).resolve()
    project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    now = time.time() if now is None else now
    try:
        with _exclusive_lock(_lock_path(home)):
            request = _load_json(_request_path(home))
            if not request:
                return ""
            state = _state(home)
            if int(state.get("attempts", 0) or 0) >= MAX_ATTEMPTS:
                if state.get("recovery_exhausted") is True:
                    return ""
                state.update({"last_outcome": "BLOCKED", "last_error": "retry limit exceeded"})
                state["recovery_exhausted"] = True
                _save_state(home, state)
                return "WARNING: Hermes Calendar Guard: recovery blocked after retry limit"
            if float(state.get("next_attempt_at", 0) or 0) > now:
                return ""
            before = active_gateway_identity(
                home, project_root=project_root, service_name=service_name, runner=runner
            )
            (restart or _restart_user_service)(service_name)
            deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
            after = None
            boot = None
            while time.monotonic() < deadline:
                try:
                    after = active_gateway_identity(
                        home, project_root=project_root, service_name=service_name, runner=runner
                    )
                    boot = read_boot_record(home)
                    if (
                        after.service_properties.get("MainPID") != before.service_properties.get("MainPID")
                        and boot
                        and boot.get("fingerprint") == after.fingerprint
                        and (
                            not after.release_path
                            or Path(str(boot.get("release_path", ""))).resolve()
                            == after.release_path.resolve()
                        )
                    ):
                        _request_path(home).unlink(missing_ok=True)
                        state.update(
                            {
                                "last_outcome": "RECOVERED",
                                "resolved_at": now,
                                "next_attempt_at": 0,
                                "notification_cooldown_until": 0,
                                "recovery_exhausted": False,
                            }
                        )
                        _save_state(home, state)
                        return "OK: Hermes Calendar Guard: gateway recovery verified"
                except GatewayIdentityError:
                    pass
                sleep(POLL_SECONDS)
            raise GatewayIdentityError("post-restart identity verification timed out")
    except (GatewayIdentityError, OSError, subprocess.SubprocessError) as exc:
        state = _state(home)
        attempts = int(state.get("attempts", 0) or 0) + 1
        state.update(
            {
                "attempts": attempts,
                "last_outcome": "BLOCKED",
                "last_error": str(exc),
                "next_attempt_at": now + min(3600, COOLDOWN_SECONDS * (2 ** (attempts - 1))),
                "notification_at": now,
                "recovery_exhausted": attempts >= MAX_ATTEMPTS,
            }
        )
        _save_state(home, state)
        return f"WARNING: Hermes Calendar Guard: recovery BLOCKED: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes calendar guard")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)
    output = recover_once() if args.recover else check_once()
    if output:
        print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
