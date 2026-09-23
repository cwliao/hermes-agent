# Upstream rebase 004 — 388 local commits replayed onto upstream/main via /hermes-update

Status: `完成 — 2026-09-23。388/388 本地 commit 全數成功 rebase 到 upstream/main
（d3b25b52ad）之上。local `main` 已在 ~/.hermes/hermes-agent 主 checkout 用
`git reset --hard` 收斂到新 tip `93fb12971e`（rebase 重寫了每個 commit 的
SHA，非真正 ancestor 關係，所以用 reset 而非 merge --ff-only；rollback tag
`backup/pre-upstream-rebase-004-20260923` 已打在舊 tip
`506492e889` 上）。`git worktree` 全程隔離，`~/.hermes/hermes-agent` 主
checkout 與正在跑的 gateway service 直到最後才被觸碰（只有 reset ref，沒有
重啟任何服務）。`

**尚未做（需要 operator 本人操作）**：
- `git push origin main`（force-push；origin/main 仍是 rebase前的舊 SHA，
  一般 push 會被拒絕為 non-fast-forward——auto-mode 下這是 Production
  Deploy，需要 operator 明確授權，本 session 未執行）
- `hermes_upstream_apply.py --execute`（需要 `approval.approved_by` +
  `approval.approval_token_sha256` + `HERMES_UPSTREAM_APPROVAL_TOKEN` 三者
  吻合——人工核准關卡，agent 不應該也沒有能力自己核准）
- 三個已被 main 吸收、可安全刪除的舊 worktree 清理（見下方「清理」章節）
- **目前正式 live release 的來源判定不明確**：`systemctl --user show -p
  DropInPaths hermes-gateway.service` 目前有效的 ExecStart 指向
  `~/.hermes/venvs/gateway-78259f40a7`（drop-in
  `...goal-reply-resume-78259f40a7.conf`），`WorkingDirectory` 直接是
  `~/.hermes` 而非某個 `releases/<id>` 目錄、也沒有設 `PYTHONPATH` ——這跟
  rebase-003 那次 apply 流程假設的「releases/<id> + drop-in」形狀不一致，
  可能是另一個正在測試中的 feature-branch 部署，本 session 沒有把握判斷
  正確的 `--previous-release`/`--previous-dropin` rollback 指標，**故意
  不猜**。Operator 執行 apply 前請先自行確認目前真正 live 的 release/
  drop-in（例如跑 `hermes doctor` 或直接核對 `DropInPaths` 清單），再決定
  rollback 指標。

Priority: P2（up-to-date with upstream 的例行工程債，非緊急故障）
Date: 2026-09-23
Repository: `~/.hermes/hermes-agent`
Related: `2026-09-22-upstream-rebase-003.md`（上一輪，376/376 完成，兩天內
main 上又新增 12 個本地 commit，upstream 又推進 394 個，造成本輪衝突）
Skill: `~/.claude/skills/hermes-update`（`/hermes-update`）

## 背景與規模

- rebase-003 的成果（`bd8e0db065`）已是本次起點 `main`（506492e889）的祖先
- 起點時 upstream/main 領先 **394 個 commit**
- 本地在同一分岔點之後有 **388 個 commit**（含 rebase-003 完成後兩天內新增
  的 namecard OCR / PaddleOCR / TypeSafe AI 整合等工作）
- worktree 分支 `upstream-rebase-004-20260923`，`git rebase upstream/main`
  自動合併 55/388 個 commit 才撞到第一個衝突

## 衝突密度與解法摘要（3 個真實衝突，每個都停下來看 diff + 跑對應 scoped
test 才繼續）

| # | commit / 主題 | 檔案 | 解法要點 | scoped test |
|---|---|---|---|---|
| 1 | T0085 Telegram plugin callback-keyboard | `gateway/run_inbound.py`, `hermes_cli/plugins_ledger.py` | HEAD 側新增 session-env 綁定＋sync handler 走 executor pool；被重放的 commit 在拿到 `result` 後新增 `(text, keyboard)` 二元組判斷；兩者不衝突，用 HEAD 的呼叫方式取得 `result` 後接上 tuple 判斷邏輯。`plugins_ledger.py` 是純粹「兩邊各自新增一個要清空的 container」，全部保留 | 134 passed（另有 3 個既有失敗，見下） |
| 2 | pytest 誤寫進真實 agent.log 的 test isolation fix | `tests/conftest.py` | HEAD 側該位置是 `pytest_unconfigure`，被重放的 commit 要插入新函式 `_sandbox_hermes_home_and_logging`；`pytest_configure` 呼叫點已在 HEAD 乾淨套用（不在衝突區），只有函式定義位置撞在一起，兩個函式都保留、前後排列即可 | 3 passed |
| 3 | kanban gc 死圖 archive（#76） | `hermes_cli/kanban_db.py` | HEAD 新增 `_retention_seconds` 共用 helper，被重放的 commit 新增一整組獨立的 `find_dead_graphs`/`_children_before_parents`/`archive_graph`；互不重疊，全部保留。**驗證過**：這個位置的舊版 `find_dead_graphs` 後來（commit 225-388 之間，非本輪衝突）被同一 fork 歷史裡一個更完整的版本乾淨取代/搬移到檔案後段（含 tenant 驗證、`include_untenanted`），沒有殘留重複定義 | 9 passed |

其餘 385 個 commit（56-178、180-223、225-388）全部自動合併，無需人工介入。

## 已知殘留缺口

1. **既有失敗，非本輪造成**：
   - `tests/gateway/test_telegram_plugin_callbacks.py::test_gmail_triage_approve_reject_actions_remain_intact`（2 個 parametrize case）
   - `tests/gateway/test_unknown_command.py::test_plugin_keyboard_result_attaches_markup_to_telegram_send`

   三者在**未 rebase 的原始 main**上跑同樣的測試也一樣失敗（`AttributeError:
   module 'gateway.run' has no attribute 'InlineKeyboardMarkup'`，一個 lazy
   compat shim 缺了這個符號），與本輪衝突無關，未動手修。

2. **一個既有的 tenant 繼承設計缺口**（與本輪衝突無關，是額外發現）：
   `tests/hermes_cli/test_kanban_gc_dead_graphs.py::test_cross_tenant_boundary_graph_is_excluded_from_both_scopes`
   期望「parent 有 tenant=X，child 沒指定 tenant」會形成一個混合
   tenant 的圖、應該被兩種 scope 都排除；但 `initial_task_state`
   （`hermes_cli/kanban_db_graph.py`）在 `tenant is None` 時會自動從
   parent 繼承 tenant，導致 child 實際上也變成 tenant=X，圖並非混合，
   `find_dead_graphs(tenant="X")` 因此正確找到它、回傳非空，與測試預期的
   `[]` 矛盾。這是後面某個本地 commit（同時擴充 `find_dead_graphs` 簽章
   與這批測試）自帶的既有缺陷，不是本輪任何一次衝突解法動到的程式碼。
   是否要幫 `create_task` 加一個明確跳過繼承的方式（例如比照
   `origin_platform=""` 的模式），還是改測試本身的建構方式，需要 operator
   決定設計方向，本輪未動手修。

## 驗證方式（capped + scoped，未跑 full suite）

```
grep 全 repo 衝突標記殘留：1 個假陽性（tests/tools/test_mcp_oauth_metadata.py
  的 docstring reST 底線 =======，非真衝突，與 rebase-003 那次同一個假陽性）
全 repo ast.parse 語法檢查：7209 個 .py 檔案，0 錯誤
git rev-list --left-right --count upstream/main...HEAD → 0 388（乾淨線性
  rebase，無殘留分歧）
```

三個衝突各自的 scoped test 全部再一起跑一次（`ulimit -v 4194304`）：
`159 passed, 4 deselected`（deselect 的正是上面兩點列出的 4 個既有/獨立
問題，全部逐一確認過原因，非本輪 regression）。

## 部署狀態

`~/.hermes/hermes-agent` 主 checkout 的 `main` 已用 `git reset --hard`
收斂到新 tip（`93fb12971e`），rollback tag
`backup/pre-upstream-rebase-004-20260923` 已打在舊 tip 上。**尚未** push
到 `origin/main`。**尚未**跑 `hermes_upstream_apply.py --execute`。正在跑
的 gateway service 全程沒有被重啟或觸碰。

### Operator 待辦（依序）

```bash
# 1. force-push（rebase 後的標準動作；已有 rollback tag）
cd ~/.hermes/hermes-agent
git push origin main --force-with-lease

# 2. 確認目前真正 live 的 release/drop-in 後，產生/核准 candidate 紀錄，
#    再執行 apply（見上方「尚未做」章節關於 DropInPaths 判定不明確的說明，
#    --previous-release/--previous-dropin 請自行核對後填入，本文件故意不
#    猜測填值）
export HERMES_UPSTREAM_APPROVAL_TOKEN="<你的 token>"
python3 scripts/hermes_upstream_apply.py \
  --repo ~/.hermes/hermes-agent \
  --state-dir ~/.hermes/hermes-upstream-state \
  --run-id <candidate run-id> \
  --previous-release <確認後的路徑> \
  --previous-dropin <確認後的路徑> \
  --execute

# 3. 清理已被 main 吸收、可安全刪除的舊 worktree
git worktree remove --force /home/cwliao/.hermes/worktrees/upstream-rebase-004-20260923
git worktree prune -v
```

## Blast radius

本票全程在獨立 `git worktree`（`upstream-rebase-004-20260923`）裡解衝突，
`~/.hermes/hermes-agent` 主 checkout 直到最後才被 `git reset --hard`
收斂（純 ref 移動，rollback tag 已備妥）；正在跑的 gateway service 全程
未被重啟。force-push 與 `hermes_upstream_apply.py --execute`（含 systemd
drop-in 覆寫、service restart）兩個真正會影響 shared/production 狀態的
動作，本 session 刻意不越權執行，留給 operator 決定時機。
