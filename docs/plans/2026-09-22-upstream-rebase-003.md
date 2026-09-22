# Upstream rebase 003 — 1420 new upstream commits, replayed clean via /hermes-update

Status: `完成 — 2026-09-22。376/376 本地 commit（含新增的 1 個修正 commit）全數成功
rebase 到 upstream/main（aef4d7a70b, 之後又追到 5f5c9ef8f4）之上。local `main`
已在 ~/.hermes/hermes-agent 主 checkout 用 `git reset --hard` 收斂到新 tip
`bd8e0db065`（rebase 概念上的 fast-forward；因 rebase 重寫了每個 commit 的
SHA，git 物件圖上不是真正的 ancestor 關係，所以用 reset 而非
merge --ff-only，事前已打 `backup/pre-upstream-rebase-003-merge-20260922`
tag 供 rollback）。`git worktree` 全程隔離，`~/.hermes/hermes-agent` 主
checkout 與正在跑的 gateway service 直到本票最後才被觸碰（只有 reset ref，
沒有重啟任何服務）。`

**尚未做（需要 operator 本人操作）**：
- `git push origin main`（force-push；GitHub 上的 origin/main 仍是 rebase
  前的舊 SHA，一般 push 會被拒絕為 non-fast-forward——這在 auto-mode 下被
  分類為 Production Deploy / 需要 operator 明確授權的動作，本 session 未
  執行）
- `hermes_upstream_apply.py --execute`（需要 `approval.approved_by` +
  `approval.approval_token_sha256` + `HERMES_UPSTREAM_APPROVAL_TOKEN` 三者
  吻合——這是工具本身刻意設計的人工核准關卡，agent 不應該、也沒有能力
  自己核准自己）
- 三個已被 main 吸收、可安全刪除的舊 worktree 清理（見下方「清理」章節），
  因為 `git worktree remove` 被 auto-mode 分類為 Irreversible Local
  Destruction，需要 operator 自己執行

Priority: P2（up-to-date with upstream 的例行工程債，非緊急故障）
Date: 2026-09-22
Repository: `~/.hermes/hermes-agent`
Related: `2026-09-18-upstream-rebase-1356-commits-002.md`（上一輪，
369/369 完成，最終產出的分支 `upstream-rebase-002-20260920` 已在
2026-09-20 併入 main——本票是它結束後兩天內 upstream 又推進約 1400 個新
commit 造成的第三輪衝突）
Skill: 本次執行的完整程序已寫成可重用 skill
`~/.claude/skills/hermes-update`（`/hermes-update`），未來例行更新直接
呼叫該 skill，不需要重新摸索流程。

## 背景與規模

- `local_base_sha`（分岔點）在 rebase 開始時 = `3a045623bc`
- 到 rebase 開始時的 upstream/main tip（`aef4d7a70b`，rebase 途中 upstream
  又推進到 `5f5c9ef8f4`，但當時的 worktree 已經在跑，沒有重新起頭）：
  **upstream 多推了約 1420 個 commit**
- 我方在同一分岔點之後有 **376 個本地 commit**（`cwliao` author，橫跨
  ARCH-001、kanban swarm 系列、Telegram reconnect、CLI plugin toolset
  validation 等），全部貨真價實的 fork 自訂工程量
- 用 `git worktree add` 建立獨立分支 `upstream-rebase-003-20260922`，
  `git rebase upstream/main` 直接重放，**自動合併過了 59/376 個 commit
  才撞到第一個衝突**

## 衝突密度與解法摘要（13 個真實衝突，每個都停下來看 diff + 跑對應
scoped test 才繼續，符合上一輪共識訂下的「不要一路解到底」防呆）

| # | commit / 主題 | 檔案 | 解法要點 | scoped test |
|---|---|---|---|---|
| 1 | ARCH-001 runtime-state | `gateway/run.py` | 合併我方 `force=force or replace` 與上游新增的 `runtime_state.close()` 清理 | 22 passed |
| 2 | Telegram polling reconnect bound | `plugins/platforms/telegram/adapter.py` | 純新增常數/函式，兩邊都保留 | 41 passed |
| 3 | 四車道 worker handoff contracts | `hermes_cli/kanban_db.py` | 合併空完成守門 + 新的 contract 驗證守門 | 9 passed |
| 4 | Telegram 啟動通知讀取就緒閘 | `gateway/run_notifications.py` | 合併 chat 去重 + 新的 readiness gate | 20 passed（另發現 5 個與本衝突無關的既有失敗，見下） |
| 5 | klib MCP 結果格式化 | `tools/mcp_tool_handlers.py` | 合併 helper 函式 + 保留較新版 docstring（實際行為以較新版為準） | 121 passed |
| 6 | doctor release-drift 偵測 | `hermes_cli/gateway.py` | 兩個獨立 helper 函式都保留 | 9 passed |
| 7 | kanban swarm context 邊界 | `hermes_cli/kanban_db_dispatch.py` | 合併 profile-scope context manager 重構 + toolset 選擇邏輯 | 23 passed |
| 8 | stop terminal kanban workers | 4 檔案 | 合併 evidence-error 處理，保留較新版的 attachment 回傳格式 | 通過（詳見下方已知落差） |
| 9 | kanban worker plugin discovery | `hermes_cli/kanban_db_dispatch.py` | 在新版 profile-scope context manager 內插入 `discover_plugins()` | 8 passed |
| 10 | cli plugin toolset validation | `cli.py` → `hermes_cli/cli_init_mixin.py` | **舊版整段程式碼已被搬到 mixin 檔案，HEAD 側是空的**；把小改動（`discover_plugins()` 呼叫）補到新家，砍掉 cli.py 裡 435 行的過期重複區塊 | 2 + 42 passed |
| 11 | goal-judge 只判自己的 acceptance | `cli.py` → `hermes_cli/cli_single_query.py` | 同一種模式，429 行過期重複區塊砍掉，小改動搬到 `_run_kanban_goal_loop_q` 新家 | 116 targeted tests passed |
| 12 | npm patch advisories | `package-lock.json` | 4/5 套件成功 regenerate（tar/browserslist/sanitize-html/fast-uri/xmldom）；**electron devDependency 卡在 40.10.2（目標 40.10.6）**，環境內 `npm install --package-lock-only` 拒絕重新解析（且從零 regenerate 會撞到不相關的 npm/arborist `Cannot read properties of null (reading 'edgesOut')` bug）。`electronVersion` build config key（實際打包用的版本）已正確為 40.10.6，只有本地 dev 用的 node_modules 套件版本落後 | — |
| 13 | Needle 3 kanban 前置過濾 + custom provider session_id | `uv.lock`, `plugins/model-providers/custom/__init__.py` | 純新增，兩邊 extras/method 都保留 | 12 + 57 passed |

**中途犯過一次錯，已修正**：解衝突 #12 過程中，一次 `npm install fast-uri@... @xmldom/xmldom@...` 指令沒加 `--no-save` 類參數，意外把這兩個套件新增成 ROOT `package.json` 的直接 `dependencies`（不該存在），並讓 npm 順手重排了 `devDependencies` 順序、正規化了 postinstall 字串裡的 unicode escape。commit 前已核對 diff 抓出並回復成只保留原始 commit意圖的改動。

## 已知殘留缺口

1. **electron devDependency 落後一個 patch 版本**（40.10.2 vs 目標
   40.10.6）——`electronVersion` build config 已對，只影響本地
   `dev:electron`/typings，不影響實際打包產物。需要在沒有
   `min-release-age`/`engine-strict` 限制摩擦的環境重跑
   `npm install --workspace apps/desktop --package-lock-only` 才能補齊。
2. **`_auto_post_swarm_handoff` / `DETERMINISTIC_BLOCKER_CLASSES` 暫時性
   缺口**（commit 284→353/376 之間）——與上一輪同一種模式：fork 自己的
   commit 歷史裡本來就有一個「先引用、後面才定義」的暫時性 gap，由
   commit `93a51e32ac`（fixup: restore content lost during
   upstream-rebase conflict resolution）在序列後段自動補上，rebase
   到那一步之後親自 grep 確認兩個符號都已定義，純粹自愈，非本輪造成。
3. **發現並修正一個 fork 自身既有的 regression**（與本次 rebase 衝突無
   關，是額外收穫，不是本輪造成）：commit `7c2b5b5981`
   （fix(tools): refuse unbound worker run-lifecycle mutations）加了一個
   要求 `HERMES_KANBAN_RUN_ID` 的 fail-closed 守門，該 commit 自己的
   regression test 用了會自動設兩個環境變數的 `worker_env` fixture，但
   漏查另外兩個更早就存在、手動搭建環境（只設 `HERMES_KANBAN_TASK`）的
   舊測試（`test_complete_auto_posts_to_swarm_root_named_in_body`、
   `test_complete_auto_post_is_best_effort_when_root_does_not_exist`）。
   用「對照原始未 rebase 的 main 跑同一個測試，確認在那邊也會過」的方式
   排除是本輪造成的假設後，才動手修——補上 `claim_task` 後讀
   `current_run_id` 再設 `HERMES_KANBAN_RUN_ID`，已獨立成一個 commit
   `bd8e0db065`（fix(tests): bind HERMES_KANBAN_RUN_ID in two
   pre-7c2b5b5981 kanban tests）。

## 驗證方式（記取教訓：只能 scoped + memory cap，不能整包 full suite）

本輪第一次嘗試「補充信心」時，起了一個跨六個目錄
（`tests/gateway/ tests/hermes_cli/ tests/tools/ tests/agent/
tests/plugins/ tests/providers/`）、沒有記憶體上限的背景 pytest，
在被 operator 抓到之前已經吃到 8GB+ RSS 且持續攀升（host 當時已用掉
80/121GB）——這是同一個錯誤的重複發生，已寫成 feedback memory
（`feedback_pytest_memory_cap.md`）和本次新增的 `/hermes-update` skill
的固定步驟，明文禁止不設 cap 的大範圍 sweep。

改正後的正確流程：`ulimit -v 4194304`（4GB）、`-p no:cacheprovider`、
**只**列出這 13 個衝突各自對應的 scoped test 檔案（不是整個目錄），
sequential 執行（不開 xdist 平行 worker）：

```
479 passed, 2 failed（上述第 3 點的既有 regression）, 1 skipped in 93.42s
```

修正第 3 點的 regression 後，同樣 capped 方式重跑
`tests/tools/test_kanban_tools.py` 整個檔案：`92 passed in 25.41s`。

- 全 repo `ast.parse` 語法檢查：7122 個 `.py` 檔案，0 錯誤
- 全 repo grep 衝突標記殘留：0 個真衝突（1 個假陽性，
  `tests/tools/test_mcp_oauth_metadata.py` 裡一個 docstring 章節的
  reST 底線 `=======`，人工確認非衝突殘留）
- `git rev-list --left-right --count upstream/main...HEAD` → `0 376`
  （乾淨的線性 rebase，無殘留分歧）

## 部署狀態

`~/.hermes/hermes-agent` 主 checkout 的 `main` 已用 `git reset --hard`
收斂到新 tip（`bd8e0db065`），rollback tag
`backup/pre-upstream-rebase-003-merge-20260922` 已打在舊 tip 上。
**尚未** push 到 `origin/main`（GitHub 上仍是舊 SHA，一般 push 會被拒絕
為 non-fast-forward，需要 force-push；auto-mode 把這個動作分類為
Production Deploy，需要 operator 自己執行）。
**尚未**跑 `hermes_upstream_apply.py --execute`（需要 operator 本人核准，
見下方指令）。正在跑的 gateway service 全程沒有被重啟或觸碰。

### Operator 待辦（依序）

```bash
# 1. force-push（rebase 後的標準動作；已有 rollback tag）
cd ~/.hermes/hermes-agent
git push origin main --force-with-lease

# 2. 產生/核准一個反映目前狀態的 candidate 紀錄（若既有的
#    hermes_upstream_review.py candidate 流程在你的環境裡可以直接吻合，
#    優先用它；否則需要人工確認 rebase_ok/tests_ok 後手動寫
#    candidate JSON 並設 approval.approved_by /
#    approval.approval_token_sha256）
export HERMES_UPSTREAM_APPROVAL_TOKEN="<你的 token>"
python3 scripts/hermes_upstream_apply.py \
  --repo ~/.hermes/hermes-agent \
  --state-dir ~/.hermes/hermes-upstream-state \
  --run-id <candidate run-id> \
  --previous-release ~/.hermes/releases/upstream-rebase-002-339bb89d41 \
  --previous-dropin ~/.config/systemd/user/hermes-gateway.service.d/zzzz-upstream-apply.conf.bak \
  --execute

# 3. 清理已被 main 吸收、可安全刪除的舊 worktree
git worktree remove --force /home/cwliao/.hermes/worktrees/upstream-rebase-003-20260922
git worktree remove --force /home/cwliao/.hermes/worktrees/upstream-rebase-002-20260920
git worktree remove --force /home/cwliao/.hermes/worktrees/upstream-rebase-1356-002
git worktree prune -v
```

## Blast radius

本票全程在獨立 `git worktree`（`upstream-rebase-003-20260922`）裡解衝突，
`~/.hermes/hermes-agent` 主 checkout 直到最後才被 `git reset --hard`
收斂（純 ref 移動，rollback tag 已備妥）；正在跑的 gateway service
全程未被重啟。force-push 與 `hermes_upstream_apply.py --execute`（含
systemd drop-in 覆寫、service restart）兩個真正會影響 shared/production
狀態的動作，本 session 刻意不越權執行，留給 operator 決定時機。
