#!/usr/bin/env bash
# Fully automated daily upstream sync.
#
# Unlike hermes_upstream_update_guard.sh (review-only, kept as reference/history),
# this job actually applies a clean candidate: preflight -> review -> scoped tests
# -> self-approve -> apply --execute -> reset/push main -- all with zero human
# interaction on the happy path. Any conflict, test failure, or apply/push failure
# stops short of touching anything live and reports the problem via Telegram
# instead. See scripts/hermes_upstream_auto_update.py for the actual logic and
# docs/plans/2026-09-26-upstream-rebase-006.md for why this exists.
set -euo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO="$HERMES_HOME/hermes-agent"
LOG_DIR="$HERMES_HOME/logs"
STATE_DIR="$HERMES_HOME/hermes-upstream-state"
CRON_STATE_DIR="$HERMES_HOME/cron"
LOG="$LOG_DIR/hermes_upstream_auto_update.log"
LOCK="$CRON_STATE_DIR/.upstream_update.lock"
IN_PROGRESS_MARKER="$CRON_STATE_DIR/.upstream_apply_in_progress"

mkdir -p "$LOG_DIR" "$CRON_STATE_DIR" "$STATE_DIR"

if [[ -f "$IN_PROGRESS_MARKER" ]]; then
  printf '⏳ Hermes upstream 全自動更新暫停：目前有另一個 apply 正在執行。\n'
  exit 0
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  printf '⏳ Hermes upstream 全自動更新略過：另一個 updater 正在執行。\n'
  exit 0
fi

cd "$REPO"

DRIVER="$HERMES_HOME/scripts/hermes_upstream_auto_update.py"
[[ -f "$DRIVER" ]] || DRIVER="$REPO/scripts/hermes_upstream_auto_update.py"

now() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { printf '[%s] %s\n' "$(now)" "$*" >> "$LOG"; }

if [[ ! -f "$DRIVER" ]]; then
  printf '❌ Hermes upstream 全自動更新失敗：找不到 hermes_upstream_auto_update.py；為安全起見未執行更新。\n'
  exit 0
fi

touch "$IN_PROGRESS_MARKER"
trap 'rm -f "$IN_PROGRESS_MARKER"' EXIT

output=""
rc=0
output="$(python3 "$DRIVER" --repo "$REPO" --state-dir "$STATE_DIR" 2>&1)" || rc=$?
log "driver exit=$rc"
log "$output"
printf '%s\n' "$output"
exit 0
