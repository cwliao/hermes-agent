# Hermes / Codex 交接摘要

本文件整理目前 Hermes Agent、Codex 使用方式、Telegram 安全邊界、外部 URL gate 專案、以及後續實作規劃，目的是在新對話中快速恢復上下文並降低壓縮損失。

## 1. 目標與總方向

目前工作的主軸是：在不破壞既有 Hermes production 設定的前提下，為 Hermes 建立一個 **fail-closed 的 web gate 架構**，讓未來 browser / web / vision 相關能力在被重新啟用之前，先經過獨立的安全控制層。這個方向符合 Hermes 既有開發原則：核心工具面要保持狹窄，新增能力應優先走 service-gated tool、plugin、skill 或外部系統整合，而不是輕率擴張 core tool surface。

同時，Codex 的角色不是直接改 production，而是作為遠端 DGX Spark 上的開發助手，協助盤點 repo、補工具 skeleton、接測試、整理文件、以及在 repo 邊界內自動完成低風險工作。

## 2. 目前已完成的進度

### 2.1 URL gate 外部隔離專案

已經有一個獨立隔離專案：`~/.hermes/url-wrapper-project`。它的定位是 Hermes 外部的 gate / harness，而不是 Hermes core 的直接一部分。

已知已完成或已存在的內容：

- URL allowlist wrapper
- redirect / response / MIME 控制
- DNS skeleton
- decompression 防護 skeleton
- hash-chain audit skeleton
- integration stub
- fail-closed adapter
- shell gate
- 完整測試與結果摘要

這個專案的運作原則已明確界定：

- 不修改 Hermes production 設定
- 不動 production audit log
- 不啟 / 停 gateway
- 不直接啟用 browser / web / vision

### 2.2 Telegram policy / audit 骨架

目前已經建立以下檔案與目錄：

- `~/.hermes/policies/url_allowlist.yaml`
- `~/.hermes/policies/image_allowlist.yaml`
- `~/.hermes/audit/telegram-policy-audit.log`
- 以及工作目錄內兩份 Markdown

目前明確狀態：

- **未更動** key / token / channel / users
- **未更動** Telegram 設定或工具
- **未啟動** gateway
- 三個政策 / 審計檔為 600
- 兩個目錄為 700
- 敏感格式掃描通過

這表示 Telegram 安全治理的骨架已經落地，但仍維持保守、乾淨、未碰 production 憑證與運行狀態的階段。

### 2.3 Hermes repo 現況認知

目前已確認 Hermes repo 的重要設計前提包括：

- **Prompt caching 是神聖的**，不能隨意改變對話中的系統 prompt 或 tool surface，否則會破壞 prefix cache 並提高成本。
- Hermes 偏好把新能力放在邊界層，而不是一直往 core tools 增長。
- 新增 built-in tool 的標準流程是：在 `tools/` 新增工具檔、用 registry 註冊、再在 `toolsets.py` 中顯式掛入對應 toolset；auto-discovery 只負責載入，不代表一定會暴露給 agent。
- 路徑相關程式碼不可硬寫 `~/.hermes`，必須使用 `gethermeshome` / `displayhermeshome` 才能支援 profile-aware 行為。
- `.env` 只應放 secrets，非敏感行為設定應放在 `config.yaml`。

### 2.4 Codex 使用策略

已確認 Codex 適合拿來：

- 在 DGX Spark 遠端環境中盤點 Hermes repo
- 找出既有 web / browser 工具與 toolset 掛載點
- 新增 `web_gate` skeleton
- 加最小 demo / 測試腳本
- 撰寫和更新開發文件
- 在 repo 範圍內自動完成低風險工作

同時也已確認一個重要原則：若想讓 Codex 少問 permission，不能只靠 prompt，還要配合 repo 級指令檔與 approval mode。Codex 的 repo-specific 指令檔實務上應使用 `AGENTS.md`，而 approval mode 的日常建議值是 **Auto**，因為 Auto 允許在工作目錄內讀檔、改檔、跑命令，而不需要每一步都手動確認。[1][2]

### 2.5 Hermes repo 內的 `web_gate` skeleton

`web_gate` skeleton 已完成並提交：

- `49d3cf3ba feat(tools): add fail-closed web gate skeleton`

本次變更新增 `tools/web_gate.py` 與 `tests/test_web_gate.py`，將 `web_gate` 掛入最小的 `web` toolset，並更新 `tests/test_toolsets.py`，讓既有測試依 toolset 定義驗證，而不是凍結工具數量。

目前 handler 接收 `url`、`tool`、`actor`、`channel`、`request_source`，並透過本機 `_stub_gate_adapter` 判定。stub 固定回傳 `deny / gate_not_configured`；adapter deny、格式錯誤或拋出例外時都會拒絕，因此目前維持 fail-closed。此實作沒有連線到任何 endpoint、沒有 hardcode `~/.hermes` 路徑，也沒有修改 production config、credentials、Telegram 設定、audit log 或 gateway 狀態。

驗證結果：

- `tests/test_web_gate.py`：3 passed
- `tests/test_web_gate.py + tests/test_toolsets.py`：30 passed
- broader non-integration suite：2,578 passed、2 skipped 後，在既有且與本變更無關的 `tests/agent/test_file_safety.py::TestCacheFileReadBlocking::test_hub_index_cache_blocked` 失敗並因 `-x` 停止
- 測試程序結束時另觀察到既有 logging cleanup 對已移除暫存 log 目錄寫入所產生的錯誤訊息，與 `web_gate` 無關

## 3. AGENTS.md 與 Codex 行為治理

目前 Hermes repo 已經有一份大型 `AGENTS.md`，內容本質上是 Hermes Agent Development Guide，而不是專門給 Codex 的最小工作規則。該文件已經提供：

- Hermes 架構與 repo 結構導覽
- Contribution Rubric
- Footprint Ladder
- Adding New Tools 規則
- profile-safe path 規則
- plugin / skill / testing / gateway pitfall 指引
- config 與 .env 邊界

因此不需要重寫整份 `AGENTS.md`，而是應該在現有文件中**增補一段 Codex CLI behavioral rules**，讓 Codex 在 Hermes repo 中有明確的自治邊界。

建議增補內容的原則如下：

- 允許 Codex 自動完成 repo 內例行工作：改 `tools/`、`toolsets.py`、tests、docs、lint、pytest 等。
- 只有在下列情況才停下來問：
  - 要改 repo 外檔案
  - 要碰 `~/.hermes/config.yaml`、`~/.hermes/.env`、`~/.hermes/auth.json`
  - 要動 production audit logs
  - 要啟停 gateway
  - 要動 Telegram tokens / channel IDs / allowed user IDs
  - 要做 destructive git 操作或重大架構抉擇
- 明確要求：不准 hardcode `.hermes` 路徑、不准把非 secret 設定塞進 `.env`、不准任意擴張 core tools。

這樣的做法可以保留 Hermes 官方開發準則，同時補上一層針對 Codex 的自治規則。

## 4. 目前明確的安全邊界

以下邊界在目前規劃中被視為硬限制：

- 不修改 production credentials、API/provider keys、Telegram bot token、channel IDs、allowed user IDs
- 不修改 `~/.hermes/config.yaml`
- 不修改 `~/.hermes/.env`
- 不修改 `~/.hermes/auth.json`
- 不修改 production audit log，例如 `~/.hermes/audit/telegram-policy-audit.log`
- 不啟動、停止、重啟或重設 Hermes gateway
- 不直接在 production 啟用 browser / web / vision
- 不在 Hermes repo 中寫死外部 gate 專案的絕對路徑

這些邊界與 Hermes 既有設計風格一致，也能避免 Codex 或人工開發過早污染 production 狀態。

## 5. 已完成方案：Hermes 內的 web gate skeleton

Hermes repo 內已完成「方案 A」：新增一個 **fail-closed `web_gate` 工具 skeleton**，沒有直接修改既有 browser / web / vision 工具的執行行為。

### 5.1 `web_gate` 的定位

`web_gate` 是 Hermes 內部的前置判定層，不負責抓網頁或瀏覽。它接收 web 類請求的 metadata，組成 adapter payload，再根據 allow / deny 回傳下一步判定。

目前實作欄位：

- `url`
- `tool`
- `actor`
- `channel`
- `request_source`（限定 `cli` / `telegram` / `webui`）

### 5.2 已實作的 handler 行為

1. 以 Pydantic 驗證必填欄位與 `request_source`。
2. 組成標準 payload。
3. 呼叫本機 `_stub_gate_adapter`；stub 不連線到任何外部 endpoint，固定回傳 `deny / gate_not_configured`。
4. 若未來 adapter 回傳 allow，結果為 `allowed: true` 與 `next_tool`。
5. 若 adapter deny、回傳異常資料或拋出例外，一律回傳 `allowed: false`，保持 **fail-closed**。

### 5.3 repo 內實際變更

- 完成 `tools/` 與 `toolsets.py` inventory
- 確認最小掛載範圍為 `web` toolset
- 新增並以 registry 模式註冊 `tools/web_gate.py`
- 在 `toolsets.py` 的 `web` toolset 顯式加入 `web_gate`
- 新增 deterministic unit tests，涵蓋固定拒絕、缺少必填欄位、adapter exception fail-closed
- 更新受影響的 toolset behavior tests

目前仍維持原安全邊界：不修改 Telegram runtime 設定、key / token / users、gateway、production browser / web / vision 狀態或 production audit log。

## 6. Codex 在新對話中的操作建議

未來若開新對話，最有效的做法是先提供這份交接摘要，然後補一句簡短操作前提：

- repo 路徑：`~/.hermes/hermes-agent`
- venv 已啟用
- 外部 gate 專案：`~/.hermes/url-wrapper-project`
- 本次目標：定義外部 gate adapter contract，不碰 production 設定或流量
- approval mode：建議 `codex --approval-mode auto`

Codex 啟動方式建議：

```bash
cd ~/.hermes/hermes-agent
source ~/.hermes/.venv/bin/activate
codex --approval-mode auto
```

Auto 模式之所以適合，是因為它允許 Codex 在工作目錄內讀檔、改檔、跑命令，而不會像更保守模式那樣頻繁中斷；但對超出 repo 範圍或高風險行為，仍保留需要確認的空間。[1][2]

## 7. 目前階段與下一步執行順序

### 已完成

1. Hermes repo inventory。
2. 選定最小的 `web` toolset。
3. 完成 fail-closed skeleton、registry 註冊與 deterministic tests。
4. 執行 focused 與 broader non-integration tests。
5. 提交 commit `49d3cf3ba`。

### 下一階段：外部 gate adapter contract

1. 定義 Hermes payload 與外部 gate response 的版本化格式。
2. 明確定義 timeout、無效 response、adapter unavailable 等情況的 deny reason。
3. 以本機 fake adapter / contract tests 驗證，不接 production endpoint。
4. 確認 audit 欄位與敏感資料遮罩策略。
5. 只有在 adapter 規格、allowlist、image policy、audit policy 都穩定且取得明確授權後，才考慮讓單一 `web` 能力進入受控整合。

browser 與 vision 不在下一階段範圍內，也不應因 skeleton 已完成而自動啟用。

## 8. 後續規劃重點

中期規劃不是一次打開所有能力，而是**分層開放**：

- Hermes repo 中的 gate skeleton 已完成；下一步是外部 adapter contract
- 再完成 Hermes 與外部 gate 的乾淨介面
- 再從單一能力開始，例如先考慮 `web`，而不是同時打開 `browser + web + vision`
- 所有開權限動作之前，都先更新 allowlist、image policy、audit 規則

這種順序符合 least-privilege 與 defense-in-depth，也與目前 Telegram policy 骨架的保守策略一致。

## 9. 可直接貼給新對話的最短摘要

可在新對話最前面貼上以下內容：

```md
Context:
- Repo: ~/.hermes/hermes-agent
- External isolated gate project: ~/.hermes/url-wrapper-project
- Completed: fail-closed `web_gate` skeleton committed as 49d3cf3ba
- Current behavior: local stub always denies with gate_not_configured; adapter exceptions also deny
- Next goal: define and test the external adapter contract without enabling production traffic
- Do not modify production config, secrets, Telegram settings, audit logs, or gateway state
- Do not enable browser/web/vision in production
- Respect Hermes AGENTS.md design rules, especially prompt caching, narrow core surface, gethermeshome/displayhermeshome, and config-vs-env boundaries
- Codex should operate in repo scope with auto approval behavior
```

這段適合作為新對話的最小上下文種子。
