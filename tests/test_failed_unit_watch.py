"""Tests for scripts/failed_unit_watch.sh (UNIT-FAILURE-BLINDNESS-001).

The script shells out to `systemctl` and `journalctl`, so these tests put
fakes for both earlier on PATH and drive the script through them. That keeps
the behaviour under test — set comparison, debounce, ignore list, the
flapping second layer, and the exit-code contract — without depending on the
host's real unit state.

The exit-code contract is the least obvious and the most important: this
script must exit 0 even when it finds failures. A reporter that exits
non-zero on a finding becomes a failed unit itself, and the next run reports
on it — a self-sustaining alert with no underlying cause.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "failed_unit_watch.sh"


def _make_fakes(bin_dir: Path, failed_units: str, journal: str = "") -> None:
    """Install fake systemctl/journalctl/curl on PATH.

    `failed_units` is what `list-units --state=failed` prints; `journal` is
    what `journalctl` prints. curl is stubbed so no alert can leave the host
    during a test run.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)

    (bin_dir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"list-units --state=failed"*)\n'
        f"    cat <<'UNITS'\n{failed_units}\nUNITS\n"
        "    ;;\n"
        '  *"is-failed"*)\n'
        # A unit is "currently failed" iff it appears in the failed list.
        f"    unit=$(echo \"$*\" | awk '{{print $NF}}')\n"
        f"    grep -q \"$unit\" <<'UNITS'\n{failed_units}\nUNITS\n"
        "    ;;\n"
        '  *"show"*"Result"*) echo exit-code ;;\n'
        '  *"show"*"ExecMainStatus"*) echo 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "journalctl").write_text(
        "#!/usr/bin/env bash\n" f"cat <<'LOG'\n{journal}\nLOG\n"
    )
    # Controllable so delivery failure can be exercised: the state-advance
    # contract depends on it.
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ -n "${FAKE_CURL_FAIL:-}" ]]; then exit 7; fi\n'
        "# Reject a URL containing a quote character, as the real API would:\n"
        "# an unstripped .env quote lands in the path and 404s. Without this\n"
        "# the fake accepts anything and the quote-stripping test passes even\n"
        "# with the stripping removed.\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    https://api.telegram.org/*[\\\"\\']*) exit 3 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    for name in ("systemctl", "journalctl", "curl"):
        (bin_dir / name).chmod(0o755)


def _run(tmp_path: Path, failed_units: str, journal: str = "", **env_extra):
    bin_dir = tmp_path / "bin"
    _make_fakes(bin_dir, failed_units, journal)
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    # A .env with credentials so send_telegram reaches the stubbed curl
    # rather than short-circuiting on missing config.
    (home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_HOME_CHANNEL=test-channel\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HERMES_HOME": str(home),
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
    )


class TestExitCodeContract:
    def test_exits_zero_when_units_are_failing(self, tmp_path):
        """The reporter must not become a failed unit by reporting."""
        r = _run(tmp_path, "broken.service loaded failed failed Broken")
        assert r.returncode == 0
        assert "broken.service" in r.stderr

    def test_exits_zero_when_nothing_is_failing(self, tmp_path):
        assert _run(tmp_path, "").returncode == 0


class TestFailedSnapshot:
    def test_reports_failed_unit_with_result_and_exit_code(self, tmp_path):
        r = _run(tmp_path, "broken.service loaded failed failed Broken")
        assert "broken.service" in r.stderr
        assert "Result=exit-code" in r.stderr
        assert "exit=1" in r.stderr

    def test_ignore_list_suppresses_expected_failures(self, tmp_path):
        r = _run(
            tmp_path,
            "xdg-desktop-portal.service loaded failed failed Portal",
            FAILED_UNIT_WATCH_IGNORE="xdg-desktop-portal.service",
        )
        assert "xdg-desktop-portal" not in r.stderr

    def test_silent_when_nothing_failed(self, tmp_path):
        r = _run(tmp_path, "")
        assert "failed state on" not in r.stderr


class TestDebounce:
    def test_second_run_with_unchanged_state_is_silent(self, tmp_path):
        units = "broken.service loaded failed failed Broken"
        first = _run(tmp_path, units)
        second = _run(tmp_path, units)
        assert "failed state on" in first.stderr
        assert "failed state on" not in second.stderr

    def test_recovery_is_announced_once_then_silent(self, tmp_path):
        units = "broken.service loaded failed failed Broken"
        _run(tmp_path, units)
        recovered = _run(tmp_path, "")
        after = _run(tmp_path, "")
        assert "have recovered" in recovered.stderr
        assert "have recovered" not in after.stderr

    def test_new_failure_alongside_existing_one_re_alerts(self, tmp_path):
        """A changed set is a new signal even if it was already non-empty."""
        _run(tmp_path, "a.service loaded failed failed A")
        r = _run(tmp_path, "a.service loaded failed failed A\nb.service loaded failed failed B")
        assert "failed state on" in r.stderr
        assert "b.service" in r.stderr


class TestUnitTypeCoverage:
    """The script claims to cover any unit type; an earlier draft filtered
    both layers to `.service` while the header said otherwise. A failed
    `.timer` is the case that matters most — a timer that fails to fire
    produces no failing service to notice."""

    def test_failed_timer_is_reported(self, tmp_path):
        r = _run(tmp_path, "backup.timer loaded failed failed Backup timer")
        assert "backup.timer" in r.stderr

    def test_failed_socket_is_reported(self, tmp_path):
        r = _run(tmp_path, "listener.socket loaded failed failed Listener")
        assert "listener.socket" in r.stderr

    def test_flapping_layer_covers_non_service_units(self, tmp_path):
        journal = "\n".join(["backup.timer: Failed with result 'exit-code'"] * 7)
        r = _run(tmp_path, "", journal=journal)
        assert "backup.timer" in r.stderr


class TestUnitNameCharacters:
    """systemd unit names legally contain uppercase and underscores, and this
    host has several (org.freedesktop.IBus.session.GNOME.service,
    app-org.gnome.DejaDup.Monitor@autostart.service). A lowercase-only
    pattern skips them silently."""

    def test_uppercase_unit_name_in_flapping_layer(self, tmp_path):
        journal = "\n".join(
            ["org.freedesktop.IBus.session.GNOME.service: Failed with result 'exit-code'"] * 7
        )
        r = _run(tmp_path, "", journal=journal)
        assert "IBus.session.GNOME" in r.stderr

    def test_underscore_unit_name_in_flapping_layer(self, tmp_path):
        journal = "\n".join(["Backup_Job.service: Failed with result 'exit-code'"] * 7)
        r = _run(tmp_path, "", journal=journal)
        assert "Backup_Job.service" in r.stderr

    def test_templated_unit_name_in_flapping_layer(self, tmp_path):
        journal = "\n".join(
            ["app-org.gnome.DejaDup.Monitor@autostart.service: Failed with result 'exit-code'"] * 7
        )
        r = _run(tmp_path, "", journal=journal)
        assert "DejaDup.Monitor@autostart" in r.stderr


class TestDeliveryFailureDoesNotAdvanceState:
    """A failed send must not mark the transition as reported.

    Recording the new state after a failed delivery suppresses that
    transition forever — one network blip silently losing the signal is the
    exact blindness this script exists to remove.
    """

    def test_failed_delivery_retries_on_next_run(self, tmp_path):
        units = "broken.service loaded failed failed Broken"
        first = _run(tmp_path, units, FAKE_CURL_FAIL="1")
        assert "state not advanced" in first.stderr
        second = _run(tmp_path, units)
        assert "failed state on" in second.stderr, "alert was lost after a delivery failure"

    def test_successful_delivery_advances_state(self, tmp_path):
        units = "broken.service loaded failed failed Broken"
        _run(tmp_path, units)
        second = _run(tmp_path, units)
        assert "failed state on" not in second.stderr


class TestFlapDebounceIgnoresCount:
    """The flap key must be the unit set, not the formatted counter.

    Including the count re-alerts every hour that a flapping unit fails once
    more, which is the alert-fatigue failure the design notes warn against.
    """

    def test_rising_failure_count_does_not_re_alert(self, tmp_path):
        seven = "\n".join(["flappy.service: Failed with result 'exit-code'"] * 7)
        eight = "\n".join(["flappy.service: Failed with result 'exit-code'"] * 8)
        first = _run(tmp_path, "", journal=seven)
        assert "flappy.service" in first.stderr
        second = _run(tmp_path, "", journal=eight)
        assert "flappy.service" not in second.stderr, "re-alerted on a count change alone"

    def test_new_flapping_unit_does_re_alert(self, tmp_path):
        one = "\n".join(["a.service: Failed with result 'exit-code'"] * 7)
        two = one + "\n" + "\n".join(["b.service: Failed with result 'exit-code'"] * 7)
        _run(tmp_path, "", journal=one)
        r = _run(tmp_path, "", journal=two)
        assert "b.service" in r.stderr


class TestEnvQuoting:
    def test_quoted_env_values_are_stripped(self, tmp_path):
        """.env values are commonly written KEY="value"; an unstripped quote
        produces a malformed API URL that fails silently."""
        bin_dir = tmp_path / "bin"
        _make_fakes(bin_dir, "broken.service loaded failed failed Broken")
        home = tmp_path / "hermes"
        home.mkdir(exist_ok=True)
        (home / ".env").write_text(
            'TELEGRAM_BOT_TOKEN="quoted-token"\nTELEGRAM_HOME_CHANNEL="quoted-channel"\n'
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HERMES_HOME": str(home),
        }
        r = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
        )
        assert "cannot alert" not in r.stderr
        assert "delivery failed" not in r.stderr


class TestFlapDeliveryAccounting:
    """Whether a flap report counts as delivered depends on whether the
    message carrying it was sent — not on which branch produced it.

    When the --failed set also changes, the flap report is appended to the
    primary message rather than sent separately. An earlier version treated
    that branch as "withheld" and left the state unadvanced, so the next
    stable run sent the same flap content again. An earlier version of THIS
    test asserted that duplicate as if it were the requirement.
    """

    JOURNAL = "\n".join(["flappy.service: Failed with result 'exit-code'"] * 7)

    def test_flap_appended_to_primary_is_not_resent(self, tmp_path):
        units = "broken.service loaded failed failed Broken"
        first = _run(tmp_path, units, journal=self.JOURNAL)
        assert "failed state on" in first.stderr
        assert "flappy.service" in first.stderr, "flap content should ride the primary message"
        second = _run(tmp_path, units, journal=self.JOURNAL)
        assert "flappy.service" not in second.stderr, "flap content was sent twice"

    def test_flap_survives_when_primary_delivery_failed(self, tmp_path):
        """If the primary send failed, its appended flap section never
        arrived either — both must retry."""
        units = "broken.service loaded failed failed Broken"
        first = _run(tmp_path, units, journal=self.JOURNAL, FAKE_CURL_FAIL="1")
        assert "not delivered" in first.stderr or "state not advanced" in first.stderr
        second = _run(tmp_path, units, journal=self.JOURNAL)
        assert "flappy.service" in second.stderr, "flap content lost after a failed primary send"


class TestFlappingLayer:
    """The gap that `--failed` alone cannot see.

    Verified on the real host: six units had failed earlier the same day and
    none appeared in `--failed`, because a oneshot that fails and is retried
    does not stay failed.
    """

    JOURNAL = "\n".join(["flappy.service: Failed with result 'exit-code'"] * 7)

    def test_reports_unit_that_failed_repeatedly_but_recovered(self, tmp_path):
        r = _run(tmp_path, "", journal=self.JOURNAL)
        assert "flappy.service" in r.stderr
        assert "7 failures" in r.stderr

    def test_below_threshold_is_not_reported(self, tmp_path):
        journal = "\n".join(["rare.service: Failed with result 'exit-code'"] * 2)
        r = _run(tmp_path, "", journal=journal)
        assert "rare.service" not in r.stderr

    def test_currently_failed_unit_is_not_double_reported(self, tmp_path):
        """A unit in `--failed` is reported there, not twice."""
        journal = "\n".join(["broken.service: Failed with result 'exit-code'"] * 9)
        r = _run(tmp_path, "broken.service loaded failed failed Broken", journal=journal)
        assert "currently recovered" not in r.stderr

    def test_ignore_list_applies_to_flapping_layer_too(self, tmp_path):
        r = _run(
            tmp_path,
            "",
            journal=self.JOURNAL,
            FAILED_UNIT_WATCH_IGNORE="flappy.service",
        )
        assert "flappy.service" not in r.stderr
