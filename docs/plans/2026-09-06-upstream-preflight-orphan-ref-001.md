---
title: "UPSTREAM-PREFLIGHT-ORPHAN-REF-001: scoped apply must not misclassify historical review refs"
status: "CLOSED — fixed 2026-09-08, see 本次處置結果 update below"
date: 2026-09-06
type: follow-up-ticket
ticket: UPSTREAM-PREFLIGHT-ORPHAN-REF-001
target_repo: hermes-agent
---

# 背景

2026-09-06 的 upstream apply 以指定 `run_id` 執行時，preflight 只檢查
該 run 的 candidate metadata，卻把其他仍有 metadata 的歷史
`refs/upstream/review/*` refs 判定為 orphan，因而阻擋合法的 apply。

本次以 operator 核對後清理 4 個已被新 candidate 取代的 refs，並保留
cleanup manifest：

`~/.hermes/hermes-upstream-state/cleanup/20260906-preflight-review-ref-cleanup.json`

# 觀察到的影響

- apply dry-run 回傳 `APPLY_PREFLIGHT / STALE_REVIEW_CANDIDATE`。
- deployment 在 gate 階段停止，未造成錯誤 restart 或 partial release。
- 需人工清理歷史 refs 後才能套用新 candidate。

# 根因假設

`_check_candidates()` 在指定 `run_id` 時只載入單一 metadata，但後續
orphan-ref scan 仍掃描整個 `refs/upstream/review` namespace；因此合法的
其他 candidate refs 不在 `known_refs` 集合中。

# 修正範圍

1. 讓 apply preflight 的 orphan-ref 檢查以完整 candidate metadata 集合
   建立 `known_refs`，或明確區分「指定 candidate 驗證」與「全域 orphan
   scan」兩個階段。
2. 新增測試：指定 `run_id` 時，其他具備 metadata 的 review refs 不得
   阻擋 apply；真正沒有 metadata 的 ref 仍必須 fail closed。
3. 維持現有安全條件：不自動刪除 refs、不自動 deploy，並保留 operator
   可審查的 cleanup/rollback 記錄。

# 驗收條件

- apply 對指定 candidate 的 dry-run 在存在歷史、metadata 完整的 review
  refs 時通過。
- 真正 orphan 的 review ref 仍回報 `STALE_REVIEW_CANDIDATE`。
- 相關 unit tests 通過，並保留本次 incident 的 regression test。

# 本次處置結果

- 新 candidate `20260906-050337` 已通過 updater 測試 `16 passed`。
- release `hermes-upstream-20260906-050337` 已成功套用並通過 service
  identity/health check。
- 本 ticket 僅記錄 follow-up 修正，未混入本次 deployment。

# 2026-09-08 修正實作

實際卡住的根因與本 ticket 假設稍有不同（但屬於同一類問題，修正範圍涵蓋
兩者）：daily review-only cron（無 `--run-id`）也持續回報
`refs/upstream/review/20260906-050337` 為 orphan——因為該 candidate 的
`status` 已是 `DONE`（成功套用後的終態），不在 `ACTIVE_CANDIDATE_STATES
= {"PENDING", "APPROVED"}` 內，導致 `known_refs` 沒有包含它，即使
metadata 本身完整存在。

修正（`scripts/hermes_upstream_preflight.py` `_check_candidates()`）：
orphan-ref 掃描現在一律讀取「完整」candidate 目錄（不受 `--run-id` 限縮，
對應本 ticket 原始修正範圍第 1 點的兩階段區分），並且只要 ref 有任何
metadata 檔案對應（不論 status 是 PENDING/APPROVED/DONE/SUPERSEDED），
一律視為已知、不算 orphan；只有完全沒有 metadata 檔案的 ref 才會回報
`STALE_REVIEW_CANDIDATE` 並 fail closed。

新增 regression test（`tests/test_upstream_recovery.py`）：
`test_done_candidate_ref_is_not_flagged_as_orphan`（DONE candidate 的 ref
不得被標為 orphan）與
`test_ref_with_no_metadata_at_all_is_still_flagged_as_orphan`（真正沒有
metadata 的 ref 仍必須 fail closed）。`./venv/bin/python -m pytest
tests/test_upstream_recovery.py -q` → 4 passed。

驗證：修正後對真實 repo/state-dir 重跑
`hermes_upstream_preflight.py --mode review --json`，
`STALE_REVIEW_CANDIDATE`（orphan-ref 訊息）已消失；剩餘的
`DIRTY_WORKTREE` 是本次修正本身尚未 commit 造成，commit 後應可通過
review-only gate。
