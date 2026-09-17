# Upstream rebase 002 — 1356 new upstream commits, first conflict at 60/365

Status: `共識通過（v2，3/4 全數同意）— 可以開始按修正版計畫動手`
Priority: P3（不影響 hermes-agent 目前運作——這是「要不要跟上 upstream
最新進度」的問題，不是「現在壞了」；daily guard 已經正確擋下、沒有誤
apply 任何東西）
Date: 2026-09-18
Repository: `~/.hermes/hermes-agent`
Related: `2026-09-15-upstream-sync-arch001-rebase-conflict-001.md`
（今天稍早才剛完成的 759-commit 同步，本票是它結束後不到 48 小時內
upstream 又推進 1356 個新 commit 造成的第二輪衝突）；
`2026-09-18-upstream-auto-update-recurring-conflicts-root-cause-001.md`
（同時開的姊妹票，查「為什麼每次都撞牆」的根因，本票只處理「這一次
實際要怎麼解」）

## 背景

daily guard（`hermes_upstream_update_guard.sh`，`30 4 * * *`）今天早上
照例送出「rebase 卡住」的精簡提醒（這正是本 session 稍早修好的「不要
洗版原始 JSON」機制在正常運作）。原始提醒文字說「卡在第 0/365 個」，
但這是 guard 的簡化描述，不是真相——實際查證發現不是卡在最開頭。

## 查證結果（唯讀，`git worktree add` + `git rebase --abort` 後
`git worktree remove`，repo 本身完全沒被動到）

- `local_base_sha`（我方與 upstream 的分岔點）= `af4a3eba0a`
  （2026-09-15 08:07，正是稍早那次 759-commit 同步完成的時間點，不是
  更早——排除了「upstream 重寫了自己的 main 歷史」這個一開始懷疑的
  可能，`git merge-base --is-ancestor` 確認舊 upstream tip
  （`a1b1c0e328`）確實是新 tip（`77fb7f0a70`）的祖先，乾淨的
  fast-forward，沒有 force-push 過 `main`）
- 從 `af4a3eba0a` 到目前 upstream/main 最新（`77fb7f0a70`），
  **upstream 多推了 1356 個 commit**（2026-09-15 08:07 ～
  2026-09-17 16:36 這 2.5 天內）
- 我方在同一個分岔點之後有 **365 個本地 commit**，全部確認是
  `cwliao`／`Claude`／`rebase probe`（今天早上那次 rebase 重放時的
  committer 身份）author，不是不小心夾帶進來的舊 upstream commit——
  這 365 筆是這個 fork 貨真價實的自訂工程量（web_gate、ARCH-001、
  kanban swarm 一系列強化票、DNS-exfil 偵測等，橫跨 06-30 到今天，
  commit 內的 author date 保留原始撰寫時間，不是今天造出來的假象）
- 用 `git worktree add` + `git rebase --onto` 實際重現：
  **自動合併過了 60/365 個 commit 才撞到衝突**，不是一開始就死
- 衝突檔案：**`tools/approval_gateway_wait.py`**——跟稍早
  759-commit 同步時撞到的**同一個檔案**；衝突的 commit 是
  `3975570a7e feat(arch-001): reconcile runtime state onto Hermes main`
  （我方 ARCH-001 lease 邏輯）
- 這個檔案在同一個時間窗內，**upstream 端自己也改了 5 次**，我方只改
  1 次——代表這是雙方都在活躍開發的「熱點檔案」，不是我方單方面
  硬改造成的碰撞

## 共識結果（2026-09-18，真實 instamem dispatch，`reviewer_dispatch.py`，
ticket-id `hermes-upstream-rebase-1356-commits-002-plan`，quorum=2）

```
status: consensus（quorum 數字上達標，但不是全數通過，見下方）
approved_by: [claude, agy]
rejected_by: [grok]          ← 真實反對票，不是派工失敗
failed_or_timeout: [native_hermes]
approved_count: 2 / quorum 2
```

**誠實說明**：這次 dispatch 的 `status` 欄位回報 `consensus`，是因為
核准數（2）達到 quorum（2）——但這不代表「三個都同意」，`grok`
明確投了 **REJECT**。`dispatch_records` 的隱私設計只留 verdict token
跟 digest／字數，看不到 `grok` 具體反對的理由文字。

**本票的判斷**：quorum 制度上通過，不代表可以無視這張真實反對票直接
動手。稍早（KMDaily 那兩張票）的兩次 dispatch 都是 3/4 全數同意，
這次是本 session 第一次出現真正的分歧，性質不同，需要使用者看過這個
結果再決定要不要繼續，而不是我自己解讀「反正 quorum 過了就做」。

## 共識結果 v2（2026-09-18，`hermes-upstream-rebase-1356-commits-002-plan-v2`，
quorum=2）

```
status: consensus
approved_by: [claude, grok, agy]   ← grok 這次轉為同意
rejected_by: []
failed_or_timeout: [native_hermes]
approved_count: 3 / quorum 2
```

v1 遭 `grok` 反對，本票判斷最可能的原因是「一路解到底才做一次最終
共識，中途沒有檢查點」，修正加入「每解一個衝突就停下來檢查」+
「衝突數超過 7 個（稍早那次的紀錄）就停下回報，不自己判斷要不要
繼續」兩項防呆後，v2 這次是**真正的 3/4 全數同意**，不是勉強壓線。
可以按下面「修正版計畫」動手。

## 修正版計畫（2026-09-18，針對 v1 共識反對票調整）

v1 計畫最大的洞是：「一路解到 305/365 全部做完，才做一次最終共識」
——這代表在第一次真正的人工/共識檢查點之前，會有大量低能見度的
單方面工作，如果解法方向中途錯了（例如誤解了 ARCH-001 邏輯、或
`approval_gateway_wait.py` 這個熱點檔案後面還衝突好幾次、方向跑偏），
要等到最後才會被發現，成本很高。**這很可能就是 `grok` 反對的實際
理由**（雖然看不到文字，但這是最合理、最常見的一類反對——「範圍
不設限的長時間單方面工作」）。修正如下：

1. **設檢查點，不是一路做到底**：每解掉 **1 個衝突**（不是一段、
   不是全部）就停下來，跑對應測試 + 用 `git diff` 讓使用者／下一輪
   共識看過這個衝突的具體解法，再繼續下一個。不會累積到「已經解了
   十幾個才發現方向錯了」這種情況。
2. **設硬性上限**：如果衝突數量超過稍早 759-commit 那次的紀錄
   （7 個），**先停下來回報，不要自己判斷「反正差不多，繼續解」**
   ——衝突次數異常多本身就是訊號，可能代表 `approval_gateway_wait.py`
   這個熱點檔案的碰撞比想像中嚴重，或代表
   `2026-09-18-upstream-auto-update-recurring-conflicts-root-cause-001.md`
   提到的策略問題（vendor/patch-queue 模式）比「繼續硬解」更值得
   優先處理。
3. 每解掉一個衝突：讀雙方實際 diff（不只看 `<<<<<<<`/`>>>>>>>`
   標籤字面）、跑對應測試（不是整包 full suite，除非使用者要求）、
   留下清楚的修復記錄。
4. 全部解完（或撞到上限停下）後，**再過一次三審共識**審查完整變更
   （correctness／completeness／risk），才能進到部署——不能因為稍早
   那次已經走過一次流程就跳過。
5. 部署走既有的 `hermes_upstream_apply.py`／release snapshot／
   systemd drop-in 流程，不是直接 push。

## 驗收標準

- [ ] 三審共識通過（含 correctness／completeness／risk 三個角度）
- [ ] 365 個本地 commit 全部成功重放到新 upstream 之上，沒有殘留
      衝突標記
- [ ] 針對衝突段落新增/確認測試覆蓋，測試綠燈（範圍由當時衝突數量
      決定，不預設全套）
- [ ] 部署驗證 gateway 正常啟動、Telegram 連線正常，比照稍早那次的
      驗收方式
- [ ] 部署後回頭確認 daily guard 隔天不再回報 BLOCKED（或至少不是
      同樣的衝突）

## Blast radius

本票查證階段全程唯讀：`git fetch`、`git worktree add`（獨立目錄）、
`git rebase --onto` 重現衝突後立刻 `git rebase --abort` +
`git worktree remove --force`。原本的 `~/.hermes/hermes-agent` repo
主目錄從頭到尾沒有被修改、沒有任何 commit、沒有觸碰任何正在跑的
gateway 服務。實際解衝突與部署是本票「待做」列出的獨立動作，需要先
過共識才執行。
