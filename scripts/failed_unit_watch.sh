#!/usr/bin/env bash
# Alert when any systemd --user unit is in a failed state.
#
# "Any unit" means any type — .service, .timer, .socket, .path, .mount. An
# earlier draft filtered both layers to .service while the header claimed to
# be general; a reviewer caught the mismatch. A failed .timer is exactly the
# kind of thing this must not miss, since a timer that fails to fire produces
# no failing service to notice.
#
# Why this exists (UNIT-FAILURE-BLINDNESS-001): on 2026-08-19 four scheduled
# units were found to have been failing since 08-14 with zero successes, and
# nothing had reported it. systemd had recorded every failure in a structured
# field the whole time; no code on the host read it. A grep for
# `systemctl.*is-failed|--failed` across ~/.hermes/scripts,
# ~/.hermes/hermes-agent/scripts and ~/.hermes/cron returned no matches.
#
# Deliberately general rather than per-service. The four units that failed
# had three different causes (revoked OAuth credential, expired handoff
# package, and one that was fine); a per-service check would need to
# anticipate each. This one covers a unit the moment it exists.
#
# Deliberately does NOT restart anything. A unit failing for five days is a
# signal, and auto-restarting it would suppress the signal while leaving the
# cause. Reporting is the whole job.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
STATE_FILE="$HERMES_HOME/failed_unit_watch_state"

# Units expected to be failed on this host and not worth alerting on.
# Kept explicit rather than pattern-matched so adding one is a visible
# decision. xdg-desktop-portal* time out at boot on a headless machine with
# no desktop session; they are not scheduled work.
IGNORE_UNITS="${FAILED_UNIT_WATCH_IGNORE:-xdg-desktop-portal.service xdg-desktop-portal-gtk.service}"

is_ignored() {
  local unit="$1" ignored
  for ignored in $IGNORE_UNITS; do
    [[ "$unit" == "$ignored" ]] && return 0
  done
  return 1
}

send_telegram() {
  # Best-effort. A delivery failure must not make this check exit non-zero:
  # "the alerter is broken" and "a unit is failing" are different signals and
  # conflating them is how the original blind spot would be recreated.
  local text="$1" bot_token="" home_channel=""
  [[ -f "$ENV_FILE" ]] || { echo "failed-unit watch: $ENV_FILE not found, cannot alert" >&2; return 1; }
  # Strip surrounding quotes: .env values are commonly written as
  # KEY="value", and an unstripped quote produces a malformed API URL that
  # fails silently. This host's .env happens to be unquoted today; that is
  # not a property to depend on.
  bot_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
  home_channel=$(grep -E '^TELEGRAM_HOME_CHANNEL=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
  if [[ -z "$bot_token" || -z "$home_channel" ]]; then
    echo "failed-unit watch: TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL not in $ENV_FILE, cannot alert" >&2
    return 1
  fi
  curl -sf -m 10 -X POST \
    "https://api.telegram.org/bot${bot_token}/sendMessage" \
    -d "chat_id=${home_channel}" \
    --data-urlencode "text=${text}" >/dev/null 2>&1
}

# Second detection layer: units that failed since the last run but have
# already recovered. `--failed` is a point-in-time snapshot and cannot see
# them — a oneshot that fails and is retried never stays `failed`. Verified
# on this host: six units had failed earlier the same day and none appeared
# in `--failed`, including one with 25 failures that day. A watcher polling
# only the snapshot would have reported none of them.
#
# The character class deliberately admits uppercase and underscore. systemd
# unit names legally contain both, and this host has several
# (org.freedesktop.IBus.session.GNOME.service,
# app-org.gnome.DejaDup.Monitor@autostart.service). A lowercase-only pattern
# silently skips them, which is the precise failure mode this script exists
# to prevent.
FLAP_THRESHOLD="${FAILED_UNIT_WATCH_FLAP_THRESHOLD:-5}"
FLAP_WINDOW="${FAILED_UNIT_WATCH_FLAP_WINDOW:-today}"
FLAP_STATE_FILE="$HERMES_HOME/failed_unit_watch_flap_state"

mapfile -t flapping < <(
  journalctl --user --since "$FLAP_WINDOW" --no-pager 2>/dev/null \
    | grep -oE "[A-Za-z0-9@:_.\\-]+\.[a-z]+: Failed with result" \
    | sed 's/: Failed.*//' | sort | uniq -c | sort -rn \
    | awk -v t="$FLAP_THRESHOLD" '$1 >= t {print $2 " " $1}' || true
)

primary_sent=0
flap_report=""
flap_units=""
for entry in "${flapping[@]:-}"; do
  [[ -n "$entry" ]] || continue
  unit="${entry%% *}"; count="${entry##* }"
  is_ignored "$unit" && continue
  # Skip units already covered by the --failed snapshot below; reporting a
  # unit twice in one message is noise, and the snapshot carries better
  # detail (Result= and exit code).
  systemctl --user is-failed "$unit" >/dev/null 2>&1 && continue
  flap_report+=$'\n'"  ${unit} (${count} failures in ${FLAP_WINDOW}, currently recovered)"
  # Debounce on the unit set only. Including the count would make the key
  # change every time a flapping unit fails once more, re-alerting hourly
  # for a condition already reported — the alert-fatigue failure this
  # script's design notes warn against.
  flap_units+=$'\n'"$unit"
done

# `--failed` is the authoritative list for units stuck in failure.
# No unit-type filter: a failed .timer or .socket is as reportable as a
# failed .service. `list-units --state=failed` already restricts to failures,
# so the first column is the whole answer.
mapfile -t all_failed < <(
  systemctl --user list-units --state=failed --no-legend --plain 2>/dev/null \
    | awk '{print $1}' | grep -E '\.[a-z]+$' || true
)

current=()
for unit in "${all_failed[@]:-}"; do
  [[ -n "$unit" ]] || continue
  is_ignored "$unit" || current+=("$unit")
done

# Sorted, newline-joined: the state file is compared as a set, so unit order
# from systemd must not cause a spurious transition.
current_state=""
if ((${#current[@]})); then
  current_state=$(printf '%s\n' "${current[@]}" | sort)
fi
previous_state=""
[[ -f "$STATE_FILE" ]] && previous_state=$(cat "$STATE_FILE")

# Alert only on change. Without this the timer would re-alert every run for
# as long as a unit stays broken, which trains the reader to mute it — the
# same end state as having no alert at all.
if [[ "$current_state" != "$previous_state" ]]; then
  if [[ -n "$current_state" ]]; then
    count=$(printf '%s\n' "$current_state" | wc -l)
    detail=""
    while IFS= read -r unit; do
      [[ -n "$unit" ]] || continue
      result=$(systemctl --user show "$unit" -p Result --value 2>/dev/null)
      status=$(systemctl --user show "$unit" -p ExecMainStatus --value 2>/dev/null)
      detail+=$'\n'"  ${unit} (Result=${result:-?}, exit=${status:-?})"
    done <<< "$current_state"
    msg="failed-unit watch: ${count} systemd --user unit(s) in failed state on $(hostname):${detail}"
  else
    msg="failed-unit watch: all previously failed units on $(hostname) have recovered."
  fi
  if [[ -n "$flap_report" ]]; then
    msg+=$'\n'"Also failing repeatedly but currently recovered (>=${FLAP_THRESHOLD} in ${FLAP_WINDOW}):${flap_report}"
  fi
  echo "$msg" >&2
  # Persist only on successful delivery. Recording the new state after a
  # failed send would mark this transition as reported and suppress it
  # forever — one network blip silently losing the signal is precisely the
  # blindness this script exists to remove.
  if send_telegram "$msg"; then
    primary_sent=1
    printf '%s' "$current_state" > "$STATE_FILE"
  else
    echo "failed-unit watch: Telegram delivery failed; state not advanced, will retry next run" >&2
  fi
fi

# Flapping units are reported on their own transition so a unit that only
# ever flaps still surfaces, even when the --failed set never changes.
#
# The flap message is withheld on a run where the --failed set also changed,
# to avoid double-reporting in one message. That suppression must NOT record
# the new flap_key: doing so marks the alert seen without ever sending it,
# and it is swallowed permanently once the set stabilises. An earlier version
# had exactly that bug — the same "record success without delivering"
# mistake this script fixes on the primary path, reintroduced here.
flap_key=$(printf '%s' "$flap_units" | sort)
previous_flap=""
[[ -f "$FLAP_STATE_FILE" ]] && previous_flap=$(cat "$FLAP_STATE_FILE")

if [[ -z "$flap_report" ]]; then
  # Nothing to report: recording the (empty) key is safe and lets a future
  # flap register as a change.
  printf '%s' "$flap_key" > "$FLAP_STATE_FILE"
elif [[ "$flap_key" == "$previous_flap" ]]; then
  : # Already reported this exact set; nothing to do and nothing to record.
elif [[ "$current_state" != "$previous_state" && "$primary_sent" -eq 1 ]]; then
  # The primary message already carried this flap_report as an appended
  # section and was delivered, so it HAS been reported — record it. An
  # earlier version assumed this branch meant "withheld" and left the state
  # unadvanced, which made the next stable run send the same flap content a
  # second time. The distinguishing fact is whether the primary send
  # succeeded, not whether the failed-set changed.
  printf '%s' "$flap_key" > "$FLAP_STATE_FILE"
elif [[ "$current_state" != "$previous_state" ]]; then
  # Failed-set changed but its delivery failed, so the appended flap section
  # never arrived either. Leave unadvanced; the retry carries both.
  echo "failed-unit watch: flap report not delivered (primary send failed); will retry next run" >&2
else
  msg="failed-unit watch: unit(s) on $(hostname) failing repeatedly but recovering between runs (>=${FLAP_THRESHOLD} in ${FLAP_WINDOW}):${flap_report}"
  echo "$msg" >&2
  if send_telegram "$msg"; then
    printf '%s' "$flap_key" > "$FLAP_STATE_FILE"
  else
    echo "failed-unit watch: Telegram delivery failed; flap state not advanced, will retry next run" >&2
  fi
fi

# Always exit 0. This unit reports on others; if it exits non-zero when it
# finds a failure it becomes a failed unit itself, and the next run reports
# on it — a self-sustaining alert with no underlying cause.
exit 0
