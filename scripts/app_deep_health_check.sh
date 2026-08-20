#!/usr/bin/env bash
# T0213 Objective #3: DocuBot/DocHelper application-level (not just
# process-alive) health monitoring, alert-only.
#
# DocuBot and DocHelper already have real dependency-probing health tiers
# (GET /api/v1/system/health on both, plus GET /api/v1/admin/database-health
# on DocuBot) that exist but were not wired to any watcher/action -- see
# T0213.md section 3. This script polls those endpoints on a schedule and
# alerts (Telegram, same TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL .env
# convention failed_unit_watch.sh's own send_telegram() and
# mcp_health_check.sh already use) on a deep-check failure class.
#
# Deliberately alert-only, never auto-restart, matching the design's stated
# rule (T0213.md section 3, "General rule across all three"): auto-restart
# is safe only for "process not responding at all" -- already systemd's own
# Restart=on-failure plus Objective #1's failed_unit_allowlist_repair job.
# A /system/health-style deep check failure (Ollama unreachable, DB
# unreachable, disk/memory pressure) is an external-dependency problem;
# restarting docbot.service/dochelper.service cannot fix an unreachable
# Ollama or a down Postgres, and looping a restart against it would only
# burn restart budget while hiding the signal -- the same reasoning
# failed_unit_watch.sh already applies at the systemd layer.
#
# HTTP-unreachable/5xx from either endpoint is deliberately NOT alerted on
# here: that is a process-level failure, already covered by
# Restart=on-failure + Objective #1's allowlist. Alerting on it here too
# would be redundant with (and could race) that coverage.
#
# kmdaily-api is intentionally NOT polled by this script. Per T0213.md
# section 3, its /api/v1/health is genuinely static with no dependency
# probe at all; the design explicitly defers adding one to a separate,
# small follow-up ticket rather than treating it as implementation-ready
# here.
set -uo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
STATE_DIR="$HERMES_HOME"

DOCBOT_URL="${DOCBOT_HEALTH_URL:-http://127.0.0.1:8020}"
DOCHELPER_URL="${DOCHELPER_HEALTH_URL:-http://127.0.0.1:8010}"

mkdir -p "$STATE_DIR"

# --- prevent overlapping runs, same convention as mcp_health_check.sh ---
exec 200>"$STATE_DIR/app_deep_health.lock"
if ! flock -n 200; then
  echo "app-deep-health-check: another instance is already running, skipping" >&2
  exit 0
fi

send_telegram() {
  # Best-effort, matching failed_unit_watch.sh's own send_telegram(): a
  # delivery failure must not make this check exit non-zero.
  local text="$1" bot_token="" home_channel=""
  [[ -f "$ENV_FILE" ]] || { echo "app-deep-health-check: $ENV_FILE not found, cannot alert" >&2; return 1; }
  bot_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
  home_channel=$(grep -E '^TELEGRAM_HOME_CHANNEL=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
  if [[ -z "$bot_token" || -z "$home_channel" ]]; then
    echo "app-deep-health-check: TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL not in $ENV_FILE, cannot alert" >&2
    return 1
  fi
  curl -sf -m 10 -X POST \
    "https://api.telegram.org/bot${bot_token}/sendMessage" \
    -d "chat_id=${home_channel}" \
    --data-urlencode "text=${text}" >/dev/null 2>&1
}

# check_state <key> <ok|degraded> <detail-for-alert>
#
# Alerts only on a healthy->degraded or degraded->healthy transition (same
# debounce convention as failed_unit_watch.sh/mcp_health_check.sh) so a
# steady-state outage does not re-alert every run.
check_state() {
  local key="$1" new_state="$2" detail="$3" state_file
  state_file="$STATE_DIR/app_deep_health_state_${key}"
  local prev_state="healthy"
  [[ -f "$state_file" ]] && prev_state=$(cat "$state_file")
  if [[ "$new_state" != "$prev_state" ]]; then
    local msg
    if [[ "$new_state" == "degraded" ]]; then
      msg="App deep health check: ${key} is DEGRADED. ${detail}"
    else
      msg="App deep health check: ${key} has RECOVERED. ${detail}"
    fi
    echo "$msg" >&2
    if send_telegram "$msg"; then
      printf '%s' "$new_state" > "$state_file"
    else
      echo "app-deep-health-check: Telegram delivery failed for ${key}; state not advanced, will retry next run" >&2
    fi
  fi
}

# --- DocuBot: GET /api/v1/system/health (Ollama + resource warnings) ---
docbot_system_health=$(curl -sf -m 8 "${DOCBOT_URL}/api/v1/system/health" 2>/dev/null || true)
if [[ -n "$docbot_system_health" ]]; then
  docbot_ollama_reachable=$(echo "$docbot_system_health" | jq -r '.ollama.reachable // false' 2>/dev/null || echo false)
  docbot_warning_count=$(echo "$docbot_system_health" | jq -r '.warnings | length' 2>/dev/null || echo 0)
  if [[ "$docbot_ollama_reachable" != "true" || "${docbot_warning_count:-0}" -gt 0 ]]; then
    docbot_warning_types=$(echo "$docbot_system_health" | jq -r '[.warnings[].type] | join(", ")' 2>/dev/null || echo "")
    check_state "docbot_system" "degraded" "ollama.reachable=${docbot_ollama_reachable} warnings=[${docbot_warning_types}]"
  else
    check_state "docbot_system" "healthy" "ollama reachable, no resource warnings"
  fi
else
  echo "app-deep-health-check: docbot /api/v1/system/health unreachable -- process-level, not alerting here (see Objective #1)" >&2
fi

# --- DocuBot: GET /api/v1/admin/database-health (Postgres/SQLite reachability) ---
docbot_db_health=$(curl -sf -m 8 -H "X-ReportChecker-Role: admin" "${DOCBOT_URL}/api/v1/admin/database-health" 2>/dev/null || true)
if [[ -n "$docbot_db_health" ]]; then
  # database_status() (DocuBot/src/reportchecker/storage/database_health.py:95-112)
  # returns "healthy", "degraded", or "unavailable" -- "healthy" is the only
  # non-alerting value.
  docbot_db_status=$(echo "$docbot_db_health" | jq -r '.status // "unknown"' 2>/dev/null || echo unknown)
  docbot_db_reachable=$(echo "$docbot_db_health" | jq -r '.database.reachable // false' 2>/dev/null || echo false)
  if [[ "$docbot_db_status" != "healthy" || "$docbot_db_reachable" != "true" ]]; then
    check_state "docbot_database" "degraded" "status=${docbot_db_status} database.reachable=${docbot_db_reachable}"
  else
    check_state "docbot_database" "healthy" "status=healthy, database reachable"
  fi
else
  echo "app-deep-health-check: docbot /api/v1/admin/database-health unreachable -- process-level, not alerting here (see Objective #1)" >&2
fi

# --- DocHelper: GET /api/v1/system/health (Ollama + resource warnings) ---
# No dedicated database-health route exists for DocHelper (verified: grep
# for database-health/db_health in DocHelper/src returned nothing, per
# T0213.md section 3), so only the system/health tier is polled here.
dochelper_system_health=$(curl -sf -m 8 "${DOCHELPER_URL}/api/v1/system/health" 2>/dev/null || true)
if [[ -n "$dochelper_system_health" ]]; then
  dochelper_ollama_reachable=$(echo "$dochelper_system_health" | jq -r '.ollama.reachable // false' 2>/dev/null || echo false)
  dochelper_warning_count=$(echo "$dochelper_system_health" | jq -r '.warnings | length' 2>/dev/null || echo 0)
  if [[ "$dochelper_ollama_reachable" != "true" || "${dochelper_warning_count:-0}" -gt 0 ]]; then
    dochelper_warning_types=$(echo "$dochelper_system_health" | jq -r '[.warnings[].type] | join(", ")' 2>/dev/null || echo "")
    check_state "dochelper_system" "degraded" "ollama.reachable=${dochelper_ollama_reachable} warnings=[${dochelper_warning_types}]"
  else
    check_state "dochelper_system" "healthy" "ollama reachable, no resource warnings"
  fi
else
  echo "app-deep-health-check: dochelper /api/v1/system/health unreachable -- process-level, not alerting here (see Objective #1)" >&2
fi

exit 0
