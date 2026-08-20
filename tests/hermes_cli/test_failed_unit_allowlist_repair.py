"""T0213 Objective #1: hermes_cli.failed_unit_allowlist_repair.

Mirrors the injectable-collaborator test convention already used in
test_calendar_guard.py's recovery tests (fake runner/restart/sleep) so
these tests never touch a real systemd --user instance or the network.
"""
import json

from hermes_cli import calendar_guard
from hermes_cli import failed_unit_allowlist_repair as repair


def test_allowlist_excludes_credential_and_double_owned_units():
    # T0213 design section 1: these must never appear on this allowlist,
    # regardless of Restart= setting -- either they touch Gmail/OAuth/
    # credential mutation, or they already have their own dedicated,
    # claim/lock-based repair path that a second mechanism would race.
    excluded = {
        "trend-mail-auth-watch.timer",
        "kmdaily-gmail-sync.service",
        "trend-mail-remote-auth-handoff.service",
        "kmdaily.service",
        "kmdaily-digest.service",
        "kmdaily-daily-report.service",
        "hermes-gateway.service",
    }
    assert excluded.isdisjoint(repair.ALLOWLIST)
    assert set(repair.ALLOWLIST) == {
        "klib-query.service",
        "klib-brain-query.service",
        "docbot.service",
        "dochelper.service",
        "kmdaily-api.service",
    }


def test_constants_match_calendar_guard_exactly():
    # The design requires these to match calendar_guard.py's existing
    # constants exactly -- importing them directly (rather than
    # redefining) is what actually guarantees that, so this test also
    # guards against a future refactor accidentally breaking the import.
    assert repair.MAX_ATTEMPTS == calendar_guard.MAX_ATTEMPTS == 3
    assert repair.COOLDOWN_SECONDS == calendar_guard.COOLDOWN_SECONDS == 300
    assert (
        repair.RECOVERY_WINDOW_SECONDS
        == calendar_guard.RECOVERY_WINDOW_SECONDS
        == 3600
    )


def test_healthy_unit_is_never_touched(tmp_path):
    restarted = []
    notified = []
    messages = repair.repair_once(
        home=tmp_path,
        now=100.0,
        units=("klib-query.service",),
        is_failed=lambda unit: False,
        restart=lambda unit: restarted.append(unit),
        notify=lambda text: notified.append(text),
    )
    assert messages == []
    assert restarted == []
    assert notified == []
    assert not (tmp_path / "gateway" / "failed_unit_allowlist_state").exists()


def test_first_failure_claims_and_restarts(tmp_path):
    restarted = []
    notified = []
    messages = repair.repair_once(
        home=tmp_path,
        now=100.0,
        units=("docbot.service",),
        is_failed=lambda unit: True,
        restart=lambda unit: restarted.append(unit),
        notify=lambda text: notified.append(text),
    )
    assert restarted == ["docbot.service"]
    assert len(notified) == 1
    assert "restarted docbot.service" in notified[0]
    assert "attempt 1/3" in notified[0]
    state = json.loads(
        (tmp_path / "gateway" / "failed_unit_allowlist_state" / "docbot.service.json").read_text()
    )
    assert state["recovery_attempts"] == [100.0]
    assert state["next_attempt_at"] == 100.0 + calendar_guard.COOLDOWN_SECONDS


def test_second_attempt_within_cooldown_is_skipped_silently(tmp_path):
    is_failed = lambda unit: True
    restarted = []
    notified = []
    common = dict(
        home=tmp_path,
        units=("docbot.service",),
        is_failed=is_failed,
        restart=lambda unit: restarted.append(unit),
        notify=lambda text: notified.append(text),
    )
    repair.repair_once(now=100.0, **common)
    restarted.clear()
    notified.clear()
    # Still inside the 300s backoff window seeded by COOLDOWN_SECONDS.
    messages = repair.repair_once(now=150.0, **common)
    assert messages == []
    assert restarted == []
    assert notified == []


def test_exhaustion_after_max_attempts_stops_restarting_and_notifies(tmp_path):
    is_failed = lambda unit: True
    restarted = []
    notified = []
    common = dict(
        home=tmp_path,
        units=("kmdaily-api.service",),
        is_failed=is_failed,
        restart=lambda unit: restarted.append(unit),
        notify=lambda text: notified.append(text),
    )
    # Three attempts, each spaced past its own backoff, all inside the
    # 3600s sliding recovery window.
    repair.repair_once(now=0.0, **common)
    repair.repair_once(now=400.0, **common)
    repair.repair_once(now=1200.0, **common)
    assert restarted == ["kmdaily-api.service"] * 3
    notified.clear()
    restarted.clear()

    # A fourth failed check inside the same window must not restart again.
    messages = repair.repair_once(now=1300.0, **common)
    assert restarted == []
    assert len(notified) == 1
    assert "exhausted" in notified[0]
    assert messages == notified


def test_restart_failure_is_notified_but_does_not_raise(tmp_path):
    def failing_restart(unit: str) -> None:
        raise RuntimeError("systemctl restart failed: unit not found")

    notified = []
    messages = repair.repair_once(
        home=tmp_path,
        now=100.0,
        units=("dochelper.service",),
        is_failed=lambda unit: True,
        restart=failing_restart,
        notify=lambda text: notified.append(text),
    )
    assert len(notified) == 1
    assert "restart of dochelper.service failed" in notified[0]
    assert messages == notified


def test_units_are_independent_state(tmp_path):
    # Exhausting one allowlisted unit's retry budget must not affect
    # another unit's independent state.
    restarted = []
    common_notify = lambda text: None
    common = dict(
        home=tmp_path,
        units=("klib-query.service", "klib-brain-query.service"),
        is_failed=lambda unit: unit == "klib-query.service",
        restart=lambda unit: restarted.append(unit),
        notify=common_notify,
    )
    for now in (0.0, 400.0, 1200.0):
        repair.repair_once(now=now, **common)
    assert restarted == ["klib-query.service"] * 3

    # klib-brain-query.service was never reported failed, so it must have
    # no state file at all -- exhaustion of klib-query.service must not
    # leak into it.
    assert not (
        tmp_path / "gateway" / "failed_unit_allowlist_state" / "klib-brain-query.service.json"
    ).exists()
