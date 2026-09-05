#!/usr/bin/env bash
# Daily review-only upstream monitor.
#
# This job fetches and reviews upstream, then writes a human-readable report to
# stdout for Hermes cron to deliver to Telegram. It never applies, restarts, or
# pushes. A real update requires an explicit user approval for the run_id.
set -euo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO="$HERMES_HOME/hermes-agent"
LOG_DIR="$HERMES_HOME/logs"
STATE_DIR="$HERMES_HOME/hermes-upstream-state"
CRON_STATE_DIR="$HERMES_HOME/cron"
LOG="$LOG_DIR/hermes_upstream_update_guard.log"
LOCK="$CRON_STATE_DIR/.upstream_update.lock"
IN_PROGRESS_MARKER="$CRON_STATE_DIR/.upstream_apply_in_progress"

mkdir -p "$LOG_DIR" "$CRON_STATE_DIR" "$STATE_DIR"

if [[ -f "$IN_PROGRESS_MARKER" ]]; then
  printf '⏳ Hermes upstream 每日檢查暫停：目前有 update apply 正在執行。\n'
  exit 0
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  printf '⏳ Hermes upstream 每日檢查略過：另一個 updater 正在執行。\n'
  exit 0
fi

cd "$REPO"

PREFLIGHT_SCRIPT="$HERMES_HOME/scripts/hermes_upstream_preflight.py"
[[ -x "$PREFLIGHT_SCRIPT" ]] || PREFLIGHT_SCRIPT="$REPO/scripts/hermes_upstream_preflight.py"
REVIEW_SCRIPT="$HERMES_HOME/scripts/hermes_upstream_review.py"
[[ -x "$REVIEW_SCRIPT" ]] || REVIEW_SCRIPT="$REPO/scripts/hermes_upstream_review.py"
REPORT_SCRIPT="$HERMES_HOME/scripts/hermes_upstream_report.py"
[[ -x "$REPORT_SCRIPT" ]] || REPORT_SCRIPT="$REPO/scripts/hermes_upstream_report.py"

# Shared read-only gate. Prefer the installed runtime copy, but fall back to
# the checkout copy while the local cron-script installer catches up.
PREFLIGHT_SCRIPT="$HERMES_HOME/scripts/hermes_upstream_preflight.py"
if [[ ! -x "$PREFLIGHT_SCRIPT" ]]; then
  PREFLIGHT_SCRIPT="$REPO/scripts/hermes_upstream_preflight.py"
fi
if [[ ! -x "$PREFLIGHT_SCRIPT" ]]; then
  printf '[%s] FAIL: 找不到 upstream preflight script，為安全起見不執行 review。\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG"
  exit 0
fi

PREFLIGHT_STATE_DIR="$HERMES_HOME/hermes-upstream-state"
preflight_output=""
if ! preflight_output="$("$PREFLIGHT_SCRIPT" \
    --repo "$REPO" \
    --state-dir "$PREFLIGHT_STATE_DIR" \
    --mode review \
    --json 2>&1)"; then
  {
    printf '[%s] upstream preflight blocked/failed; review body skipped\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf '%s\n' "$preflight_output"
  } >> "$LOG"
  printf '🔍 Hermes upstream preflight 報告 %s\n%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$preflight_output"
  exit 0
fi

now() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { printf '[%s] %s\n' "$(now)" "$*" >> "$LOG"; }

if [[ ! -x "$PREFLIGHT_SCRIPT" || ! -x "$REVIEW_SCRIPT" || ! -x "$REPORT_SCRIPT" ]]; then
  message="❌ Hermes upstream 每日檢查失敗：找不到 preflight、review 或 report script；為安全起見未執行更新。"
  printf '%s\n' "$message"
  log "$message"
  exit 0
fi

preflight_output=""
if ! preflight_output="$("$PREFLIGHT_SCRIPT" \
    --repo "$REPO" \
    --state-dir "$STATE_DIR" \
    --mode review \
    --json 2>&1)"; then
  log "preflight blocked/failed"
  log "$preflight_output"
  # Concise summary (code + message per issue), not the full JSON -- same
  # "don't flood Telegram with raw candidate/preflight JSON" fix as the
  # review-blocked path below. Falls back to a short fixed message if the
  # output isn't parseable JSON (e.g. a Python traceback from a crash).
  summary="$(printf '%s' "$preflight_output" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
issues = data.get("issues") or []
lines = []
for item in issues:
    if isinstance(item, dict):
        lines.append("- " + str(item.get("code", "?")) + ": " + str(item.get("message", "")))
print("\n".join(lines) if lines else "status=" + str(data.get("status", "?")))
' 2>/dev/null || true)"
  if [[ -n "$summary" ]]; then
    printf '⚠️ Hermes upstream 每日檢查被 gate 阻擋（未 deploy、未 restart、未 push）。\n%s\n完整輸出在 log。\n' "$summary"
  else
    printf '⚠️ Hermes upstream 每日檢查被 gate 阻擋（未 deploy、未 restart、未 push）。詳見 log。\n'
  fi
  exit 0
fi

run_id="$(date -u '+%Y%m%d-%H%M%S')"
review_output=""
review_rc=0
review_output="$("$REVIEW_SCRIPT" \
    --repo "$REPO" \
    --state-dir "$STATE_DIR" \
    --run-id "$run_id" \
    --json 2>&1)" || review_rc=$?

candidate_path="$STATE_DIR/candidates/$run_id.json"
log "review exit=$review_rc for run_id=$run_id"
log "$review_output"

if [[ ! -f "$candidate_path" ]]; then
  # No candidate metadata at all (e.g. review.py crashed before writing it) --
  # this is the one case with nothing structured to render concisely from, so
  # it's the only path that still surfaces raw output. Full detail stays in
  # the log either way.
  printf '❌ Hermes upstream review 沒有產生 candidate metadata（run_id=%s）；未 deploy。詳見 log。\n' "$run_id"
  exit 0
fi
# A non-zero exit with a candidate file present means review.py hit a known,
# structured outcome (e.g. BLOCKED/REBASE_CONFLICT) -- fall through to the
# report script below, which renders a concise message for that case too
# (see _render_blocked in hermes_upstream_report.py). Only dumping raw JSON
# on a truly unstructured failure was the actual cause of past flooding: a
# rebase conflict is the expected daily steady-state for a long-lived fork,
# not something that needs its full candidate JSON (commit-id arrays and
# all) reposted to Telegram every morning.

# The report helper reads only candidate metadata and Git history/diff. Its
# stdout is intentionally the Telegram payload for this no-agent cron job.
if ! "$REPORT_SCRIPT" --repo "$REPO" --candidate "$candidate_path"; then
  printf '⚠️ Hermes upstream report 產生失敗（run_id=%s）；未 deploy。\n' "$run_id"
  log "report failed for run_id=$run_id"
  exit 0
fi
