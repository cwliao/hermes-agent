#!/usr/bin/env bash
# REVIEW phase — runs daily, read-only with respect to `main`.
#
# Fetches upstream (never pushes to it — that remote is push-disabled at the
# git-remote level anyway), test-merges it onto a throwaway review branch,
# and runs the full safety/completeness check suite there. Never touches
# `main`, never restarts the gateway, never pushes anywhere. If the review
# passes cleanly, the result sits on branch `upstream-review-pending` until
# a human explicitly approves applying it (see hermes_upstream_apply.sh).
set -euo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO="$HERMES_HOME/hermes-agent"
LOG_DIR="$HERMES_HOME/logs"
STATE_DIR="$HERMES_HOME/cron"
REVIEW_BRANCH="upstream-review-pending"
ALERT_STATE="$STATE_DIR/.upstream_update_guard_alert.sha256"
mkdir -p "$LOG_DIR" "$STATE_DIR"
LOG="$LOG_DIR/hermes_upstream_update_guard.log"
LOCK="$STATE_DIR/.upstream_update.lock"

IN_PROGRESS_MARKER="$STATE_DIR/.upstream_apply_in_progress"
if [[ -f "$IN_PROGRESS_MARKER" ]]; then
  exit 0  # 有一個套用流程正在進行中，這次審查安靜跳過，避免互相干擾
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

cd "$REPO"
source "$HERMES_HOME/scripts/hermes_upstream_common.sh"

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
REVIEW_SCRIPT="$HERMES_HOME/scripts/hermes_upstream_review.py"
if [[ ! -x "$REVIEW_SCRIPT" ]]; then
  REVIEW_SCRIPT="$REPO/scripts/hermes_upstream_review.py"
fi
if [[ ! -x "$REVIEW_SCRIPT" ]]; then
  printf '[%s] FAIL: 找不到 upstream review script，為安全起見不執行 review。\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG"
  exit 0
fi

review_output=""
if ! review_output="$("$REVIEW_SCRIPT" \
    --repo "$REPO" \
    --state-dir "$PREFLIGHT_STATE_DIR" \
    --run-id "$(date -u '+%Y%m%d-%H%M%S')" \
    --json 2>&1)"; then
  {
    printf '[%s] upstream review blocked/failed\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf '%s\n' "$review_output"
  } >> "$LOG"
  printf '🔍 Hermes upstream review 報告 %s\n%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$review_output"
  exit 0
fi
{
  printf '[%s] upstream review completed\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '%s\n' "$review_output"
} >> "$LOG"
printf '%s\n' "$review_output"
exit 0

now() { date '+%Y-%m-%d %H:%M:%S %Z'; }
out=()
failures=()
add() { out+=("$1"); }
fail() { failures+=("$1"); }

cleanup() {
  git checkout main >/dev/null 2>&1 || true
}
trap cleanup EXIT

{
  echo "[$(now)] start upstream review (review-only, never touches main)"

  branch=$(git branch --show-current)
  main_sha=$(git rev-parse main 2>/dev/null || echo "")

  if [[ "$branch" != "main" ]]; then
    add "目前不在 main 分支（現在是 $branch），跳過本次審查。"
  elif [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    add "工作目錄不乾淨，跳過本次審查："
    git status --short | sed 's/^/  /'
  elif ! git fetch upstream main --prune; then
    add "git fetch upstream main 失敗。"
  else
    upstream_sha=$(git rev-parse upstream/main)

    if git merge-base --is-ancestor "$upstream_sha" main; then
      add "main 已經包含目前的 upstream/main（$(short "$upstream_sha")），沒有新東西需要審查。"
      git branch -D "$REVIEW_BRANCH" >/dev/null 2>&1 || true
    else
      mapfile -t local_commits < <(git rev-list --reverse upstream/main..main)

      git branch -f "$REVIEW_BRANCH" main >/dev/null 2>&1
      git checkout "$REVIEW_BRANCH" >/dev/null 2>&1

      if ! git merge --no-edit upstream/main >/dev/null 2>&1; then
        git merge --abort >/dev/null 2>&1 || true
        git checkout main >/dev/null 2>&1
        git branch -D "$REVIEW_BRANCH" >/dev/null 2>&1 || true
        fail "試合併 upstream/main 產生衝突，需要人工處理，不會自動套用。"
      else
        merged_sha=$(git rev-parse HEAD)

        check_local_commits_preserved "${local_commits[@]}"
        check_upstream_content_complete
        check_runtime_config
        check_repo_safety_hooks

        git checkout main >/dev/null 2>&1

        if [[ "${#failures[@]}" -eq 0 ]]; then
          add "審查通過：main $(short "$main_sha") -> $(short "$merged_sha")（$(git rev-list --count "$main_sha".."$merged_sha") 個新 commit），本地 commit 全數保留、upstream 核心檔案完整併入、設定與安全防護完整，沒有衝突。"
          add "審查結果暫存在分支 $REVIEW_BRANCH，尚未套用到 main，也還沒 push 到任何地方。"
        else
          git branch -D "$REVIEW_BRANCH" >/dev/null 2>&1 || true
        fi
      fi
    fi
  fi

  if [[ "${#failures[@]}" -gt 0 ]]; then
    add "⚠️ 審查未通過，以下是原因："
    for item in "${failures[@]}"; do
      add "- $item"
    done
  fi

  if [[ "${#failures[@]}" -eq 0 ]] && git rev-parse --verify "$REVIEW_BRANCH" >/dev/null 2>&1; then
    add "想套用這批更新嗎？直接在這裡回覆「核准套用 upstream 更新」。"
    add ""
    add "[給處理這則核准回覆的 agent 的執行說明——請務必照做，不要跳過或合併步驟：]"
    add "依序執行 /home/cwliao/.hermes/scripts/hermes_upstream_apply.sh 的 5 個步驟：verify → merge → postcheck → restart → push。"
    add "每跑完一步，就把該步驟的輸出結果回報給使用者一次（用一則簡短訊息），不要等全部跑完才一次回報。"
    add "任一步驟的結果以 FAIL: 開頭、或指令回傳非 0 結束碼，立刻停止，不要自己嘗試修復或跳過，把失敗原因清楚說明後，明確詢問使用者要怎麼處理（例如：要不要重跑審查、要不要人工介入解衝突、要不要中止），等使用者授權後才能繼續。"
    add "全部 5 步驟都成功後，才告訴使用者「upstream 更新已完整套用」。"
  fi

  echo "[$(now)] done"
} >> "$LOG" 2>&1

if [[ "${#out[@]}" -gt 0 ]]; then
  # Do not send the same unchanged review result to Telegram every day. Include
  # both repository tips so a new upstream commit or local change re-opens the
  # alert even when the human-readable conflict text is identical.
  main_tip=$(git -C "$REPO" rev-parse main 2>/dev/null || true)
  upstream_tip=$(git -C "$REPO" rev-parse upstream/main 2>/dev/null || true)
  payload=$(printf '%s\n' "$main_tip" "$upstream_tip" "${out[@]}")
  digest=$(printf '%s' "$payload" | sha256sum | awk '{print $1}')
  previous=$(cat "$ALERT_STATE" 2>/dev/null || true)
  if [[ "$digest" != "$previous" ]]; then
    tmp_state="${ALERT_STATE}.tmp.$$"
    printf '%s\n' "$digest" > "$tmp_state"
    mv -f "$tmp_state" "$ALERT_STATE"
    printf '🔍 Hermes upstream 審查報告 %s\n' "$(now)"
    for item in "${out[@]}"; do
      printf -- '%s\n' "$item"
    done
  fi
else
  rm -f "$ALERT_STATE"
fi
