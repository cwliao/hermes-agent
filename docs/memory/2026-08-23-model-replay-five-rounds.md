# 五輪工程心得與目前交接記錄

日期：2026-08-23（Asia/Taipei）
範圍：`MODEL-REPLAY-RELIABILITY-001`、`SESSION-TRANSCRIPT-REPLAY-001`、DGX
gateway 部署與真實 Telegram 驗收。
本文件是 repo 內的長期記憶；它記錄可重用的判斷規則，不保存訊息全文、prompt、token、credential 或私密內容。

## 先看結論

這次的「Hermes 重複上一輪回答」不是單一 bug，而是幾個不同層次的問題疊加：

1. `_row_id` 修正解決了 database duplicate-row identity，不能證明模型重新執行任務。
2. replay guard 原先沒有接到 production conversation loop；只測 unit test 不足以證明實際流程會執行。
3. guard 接通後，受污染的 compression lineage 沒有任何 clean tool-backed baseline；`no_exact_tool_backed_candidate` 是合理但過度寬鬆的放行結果。
4. 新的 baseline-less policy 若只限制「候選搜尋」而沒有同時限制「receipt gate」，會產生第二層安全漏洞：候選被判為安全，但真正執行的 terminal tool 可能仍是 mutating。
5. repo HEAD、release snapshot、release-pinned venv、systemd effective unit 和 live state 是五個不同真相來源；更新 repo 或重啟服務本身都不等於部署。

## 第一輪：先把症狀拆成資料層與模型層

### 觀察

最初的 acceptance test 看到同一個 Webboard 回覆再次出現，包含舊時間戳 `08:03:29` 與不應外洩的 `AGENTS.md` 內容。先前的 `_row_id` 修正已經通過 19 個 dedup/compaction 測試、57 個 Telegram/replay 測試，並確認修正上線後 state database 沒有新的重複非空 assistant rows；但真實 Telegram 重測仍原樣複讀。

### 判斷

「資料庫裡有兩列相同訊息」與「模型選擇複製上一輪答案而不重新執行工具」是兩個獨立成因。前者可由 row identity、dedup、compaction 修正；後者必須檢查 conversation loop、tool receipt、session lineage、model/runtime 行為。不能因 database 測試全過，就把原始模型症狀標成完全解決。

### 可重用規則

- 每個 acceptance criterion 必須明確標記它驗證的是 database、application control-flow、model behavior 還是 production delivery。
- 「沒有 duplicate rows」不是「有 fresh tool execution」的替代證據。
- 對即時報告類工作，驗收至少要同時看 inbound、tool call、tool result、fresh final、delivery 與 runtime log。

## 第二輪：確認 guard 真正進入 production call-site

### 觀察

第一版 `model_replay_guard` 的 unit tests 通過，但真實 `agent.log` 完全沒有
`model_replay_guard`、`replay_guard`、`nudge` 或 `action_identity` 紀錄。這表示「程式存在」不等於「實際對話路徑呼叫」。`60c293154b` 將 guard 接入
`conversation_loop.py` 的 finalization 路徑後，production log 才出現
`decision=invoked`。

### 判斷

對 safety/reliability guard，call-site 是 acceptance 的一部分。需要用真實 gateway process、真實 release、真實 inbound 執行一次，並確認 log 中有 guard decision；只 import 模組或跑 mock loop 不足以排除 wiring、session rotation、feature flag、release mismatch。

### 可重用規則

- 每個 guard 都要有可搜尋、metadata-only 的 decision log：`invoked`、`pass`、`nudge`、`blocked` 或 `fallback`。
- unit test 驗證判斷函式；integration test 驗證 conversation loop；production E2E 驗證 effective release 的 call-site。
- 若 log 沒有 decision，優先判定為未接線、未部署或查錯 log，不要先解釋成「guard 判斷後放行」。

## 第三輪：處理沒有 clean baseline 的 compression lineage

### 觀察

受影響 session 的 lineage 為
`20260821_163406_895ea0 -> 20260823_192418_3bd23f -> 20260823_195406_62658f`。
Guard 已被呼叫，但以 `reason=no_exact_tool_backed_candidate` 放行，因為整條 lineage 沒有可驗證的 clean tool-backed answer 可作比較基準。這不是 guard 靜默失效，而是 policy 對「全 lineage 污染」沒有 fail-safe 分支。

### 修正與限制

`a780bd4772d36ba1971907d9407efb0e785d3abf` 增加只針對精確 Telegram `Webboard` action 的 baseline-less handling：以 terminal tool surface、精確 command identity 與有效 execution evidence 判斷；若本輪沒有 tool turns 且找不到 baseline，要求 bounded fresh nudge，沒有新 receipt 就阻擋，不用 timestamp 或一般 prose heuristic 猜測。這保留了其他 action 的正常行為，也避免把普通文字誤判成報告。

### 可重用規則

- session rotation 後必須沿 lineage 查找 execution evidence，不能只看最新 session ID。
- 沒有 baseline 時，對需要即時資料的精確 action 應採 fail-closed／bounded retry；不要用「看起來像日期或統計數字」作為唯一判斷。
- baseline-less policy 必須是 action registry 的窄規則，不可擴大成所有無工具回覆都重試。
- 刪除已污染 session 是 recovery，不是根因修正；必須同時改善 policy，否則新 session 仍可能再污染。

## 第四輪：交叉 review 找到 receipt gate 的第二層錯誤

### 觀察

實作 review 發現，candidate detection 已把精確的
`bash ~/.hermes/scripts/hermes_webboard_report.sh` 視為允許的報告 action，然而 receipt gate 只允許 registry 中標記為 idempotent 的工具。若 terminal tool 在 registry 中沒有該標記，nudge 後即使真的執行了精確報告，也可能被 receipt gate 拒絕。

### 修正與 review 邊界

共用 `is_read_only_webboard_report_call`／`replay_tool_call_is_safe` predicate，讓 candidate detection 與 receipt validation 使用完全相同的窄安全定義：只接受精確固定報告 command，不把任意 terminal call 當成安全。模型 replay focused tests 最終 10/10，合併 replay／vLLM／compression／session 測試最終 35/35，並通過 `py_compile` 與 `git diff --check`。

本輪曾嘗試送 private repo source 給外部 Claude review，但被安全／effects policy 阻擋；因此不能宣稱外部 Claude 已完成 review。可採信的結果是本地 structured review 找到並修正 receipt predicate 問題，並由測試與 production E2E 交叉驗證。

### 可重用規則

- 同一個 safety invariant 若在兩個 gate 使用，必須共用 predicate；禁止 candidate gate 與 receipt gate 各自複製一份近似邏輯。
- 外部 reviewer 不可用時要記錄 `adapter_unavailable`／blocked，改用其他獨立證據；不能把未發生的 review 寫成 PASS。
- review 結果要分清：code review、unit/integration test、production E2E、user-visible delivery 是不同 gate。

## 第五輪：把部署與真實 Telegram 驗收接起來

### 部署事實

`hermes update --gateway --yes --backup` 顯示 fork 已在
`a780bd4772...`，並跳過 upstream sync；這只證明 source update 狀態，沒有自動切換 gateway release。依 directory-boundary runbook 另建 immutable release、matching SHA venv、systemd drop-in，再 drain/restart。最後 production identity 為：

- source fix：`a780bd4772d36ba1971907d9407efb0e785d3abf`
- release：`/home/cwliao/.hermes/releases/v2026.8.23-model-replay-a780bd4772`
- gateway venv：`/home/cwliao/.hermes/venvs/gateway-a780bd4772`
- systemd gateway：active/running，MainPID `1595835`

必須從 effective systemd properties、process cwd、interpreter 與 release marker 共同驗證，不能只看 repo HEAD 或 base unit file。

### 真實驗收

2026-08-23 20:18:37 CST 的 Telegram inbound 在 fresh transcript 中成功執行精確 Webboard terminal command；tool result timestamp 為 20:18:47 CST，final response 422 chars，沒有舊 `08:03:29`、沒有 `AGENTS.md` leak，runtime `tool_turns=1`。guard log 有 `decision=invoked`，並以 `reason=no_exact_tool_backed_candidate` pass；這次 pass 是因為本輪已存在真實 tool receipt，不代表 baseline-less nudge 永遠不需要。

同一個 session ID 字串被 rotation 後重用，不可只以 ID 名稱判斷污染；應讀 state rows、lineage、created time 與 tool receipt。原先受污染的三個 exact IDs 已按官方 CLI 刪除，重新 inbound 後 state database 只留下 fresh rows。

### 備份規則

部署產生的舊 `deploy-backups` 已依使用者要求清理；目前 rollback zip
`pre-update-2026-08-23-200640.zip` 約 5.9 GiB，在 E2E 驗收前保留。以後只能刪除已明確不需要 rollback 的精確舊 backup，先確認沒有 active unit、timer、cron、release 或未完成驗收依賴；不可用廣泛 glob 或「看起來舊」作為刪除依據。

## 現行 runbook（摘要）

1. 先讀 `AGENTS.md`、本文件、`docs/HANDOVER.md` 與
   `docs/operations/dgx-spark-hermes-directory-boundaries.md`。
2. 確認 repo branch/HEAD/origin、worktree；upstream 只下載，不 push。
   需要 upstream 新功能時，以乾淨 merge／reconciliation 為基礎，逐檔與
   patch-level diff 驗證，不以 merge graph 或 commit message 推論內容已存在。
3. 修改後先跑 focused tests，再跑相關 integration tests、`py_compile`、
   `git diff --check`；若有外部 review，記錄實際 adapter 與結果。
4. 部署時建立新 immutable release + 同 SHA venv，更新 effective drop-in，
   drain/restart，驗證 process/release identity 與 health。
5. 對即時 Telegram action 做真實 E2E：確認 inbound、tool call、tool result、
   guard decision、fresh final、user-visible delivery；完成後才清理 rollback backup。
6. 結束時 `git status --short` 應只反映本次有意變更；若有 untracked runtime
   檔案，先查 systemd/timer/cron/process reference，再決定是否清理。

## 目前狀態

- `MODEL-REPLAY-RELIABILITY-001`：code、部署與 Telegram E2E 已完成；baseline-less
  policy 的未來行為仍以 regression tests 與 production decision logs 監測。
- `SESSION-TRANSCRIPT-REPLAY-001`：database duplicate-row 修正有效；模型自行複讀是獨立限制，不能回填成同一票已解決。
- `origin/main` 在本文件寫入前的 verified tip 是 `b7f42f1b4a`；寫入本記錄後需以 git 的新 commit 為準。
- 任何 runtime claim 都必須重新讀 effective systemd state；不要沿用本文件中的 PID 作為永久事實。
