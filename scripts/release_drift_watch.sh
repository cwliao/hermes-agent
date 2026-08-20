#!/usr/bin/env bash
# T0214 Objective #2: alert-only companion for klib's baked-release
# staleness detector (klib's scripts/check_release_drift.sh, a separate
# repo this script shells out to — read-only, no auto-rebake anywhere in
# this path).
#
# 2026-08-19: klib's 7 baked-release services silently ran a release
# frozen for a full day because nothing compared the running release's
# RELEASE_SHA against the dev checkout's git HEAD. This script schedules
# that comparison and alerts (same TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL
# .env convention failed_unit_watch.sh/mcp_health_check.sh/
# app_deep_health_check.sh already use) when the checked-out repo has
# genuinely relevant, unbaked drift older than the alert threshold.
#
# Deliberately NOT reused verbatim from any existing watcher's dedup
# logic: failed_unit_watch.sh and app_deep_health_check.sh both only
# alert on a state *transition* (healthy<->degraded), never re-alert
# while a bad state persists. That is wrong for this check specifically
# -- a release left stale for days must not go silent after one ping, or
# this recreates exactly the "nobody noticed for a full day" failure this
# ticket exists to prevent. So STALE/ERROR get their own explicit
# re-remind behavior: alert once on transition into a non-OK status, then
# again every RELEASE_DRIFT_REMIND_SECONDS while any non-OK status
# persists (STALE and ERROR share one dedup state -- flipping between the
# two is not treated as a fresh transition out of OK, so the reminder
# clock does not reset on a STALE<->ERROR flip).
set -uo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
STATE_DIR="$HERMES_HOME"
STATE_FILE="$STATE_DIR/release_drift_watch_state"

KLIB_ROOT="${KLIB_CHECK_ROOT:-/home/cwliao/project/klib}"
CHECK_SCRIPT="${KLIB_ROOT}/scripts/check_release_drift.sh"
REMIND_SECONDS="${RELEASE_DRIFT_REMIND_SECONDS:-21600}"

mkdir -p "$STATE_DIR"

# --- prevent overlapping runs, same convention as mcp_health_check.sh /
# app_deep_health_check.sh ---
exec 200>"$STATE_DIR/release_drift_watch.lock"
if ! flock -n 200; then
  echo "release-drift watch: another instance is already running, skipping" >&2
  exit 0
fi

send_telegram() {
  # Best-effort, matching failed_unit_watch.sh's send_telegram(): a
  # delivery failure must not make this check exit non-zero.
  local text="$1" bot_token="" home_channel=""
  [[ -f "$ENV_FILE" ]] || { echo "release-drift watch: $ENV_FILE not found, cannot alert" >&2; return 1; }
  bot_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
  home_channel=$(grep -E '^TELEGRAM_HOME_CHANNEL=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
  if [[ -z "$bot_token" || -z "$home_channel" ]]; then
    echo "release-drift watch: TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL not in $ENV_FILE, cannot alert" >&2
    return 1
  fi
  curl -sf -m 10 -X POST \
    "https://api.telegram.org/bot${bot_token}/sendMessage" \
    -d "chat_id=${home_channel}" \
    --data-urlencode "text=${text}" >/dev/null 2>&1
}

if [[ ! -x "$CHECK_SCRIPT" ]]; then
  echo "release-drift watch: $CHECK_SCRIPT not found or not executable -- cannot check" >&2
  exit 0
fi

check_output="$("$CHECK_SCRIPT" 2>/tmp/release_drift_watch_stderr.$$)"
check_exit=$?
check_stderr="$(cat /tmp/release_drift_watch_stderr.$$ 2>/dev/null)"
rm -f /tmp/release_drift_watch_stderr.$$

status="unknown"
case "$check_exit" in
  0) status="ok" ;;
  49) status="stale" ;;
  50) status="error" ;;
  *) status="error"; check_stderr="unexpected exit code ${check_exit}. ${check_stderr}" ;;
esac

prev_status="ok"
prev_alerted_at=0
if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  prev_status="${LAST_STATUS:-ok}"
  prev_alerted_at="${LAST_ALERTED_AT:-0}"
fi

now_ts=$(date +%s)

if [[ "$status" == "ok" ]]; then
  if [[ "$prev_status" != "ok" ]]; then
    msg="Release-drift watch: klib release is back in sync. ${check_output}"
    echo "$msg" >&2
    send_telegram "$msg" || true
  fi
  {
    echo "LAST_STATUS=ok"
    echo "LAST_ALERTED_AT=0"
  } > "$STATE_FILE"
  exit 0
fi

# status is stale or error -- both share one non-OK alert/dedup class, so
# a STALE<->ERROR flip does not reset the reminder clock (see header
# comment). Alert now if: (a) this is a fresh transition out of OK, or
# (b) the persistent non-OK condition has gone unremembered for longer
# than REMIND_SECONDS since the last alert.
should_alert=0
if [[ "$prev_status" == "ok" ]]; then
  should_alert=1
elif (( now_ts - prev_alerted_at > REMIND_SECONDS )); then
  should_alert=1
fi

if [[ "$should_alert" -eq 1 ]]; then
  if [[ "$status" == "stale" ]]; then
    msg="Release-drift watch: klib's baked release is STALE (unbaked relevant commits older than its alert threshold). ${check_output}"
  else
    msg="Release-drift watch: could not determine klib release drift state. ${check_output} ${check_stderr}"
  fi
  echo "$msg" >&2
  if send_telegram "$msg"; then
    {
      echo "LAST_STATUS=${status}"
      echo "LAST_ALERTED_AT=${now_ts}"
    } > "$STATE_FILE"
  else
    echo "release-drift watch: Telegram delivery failed; LAST_ALERTED_AT not advanced, will retry next run" >&2
    {
      echo "LAST_STATUS=${status}"
      echo "LAST_ALERTED_AT=${prev_alerted_at}"
    } > "$STATE_FILE"
  fi
else
  {
    echo "LAST_STATUS=${status}"
    echo "LAST_ALERTED_AT=${prev_alerted_at}"
  } > "$STATE_FILE"
fi

exit 0
