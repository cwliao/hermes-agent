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

## 5. 目標方案：Hermes 內的 web gate skeleton

目前傾向的主方案，是在 Hermes repo 內走「方案 A」：新增一個 **fail-closed `web_gate` 工具 skeleton**，而不是直接改壞既有 browser tool 行為。

### 5.1 `web_gate` 的定位

`web_gate` 的角色不是自己完成抓網頁或瀏覽，而是作為 Hermes 內部的前置判定層。它接收即將執行的 web / browser 類請求，將請求內容轉成 gate adapter 可理解的 payload，再根據 allow / deny 決定是否允許下一步工具被呼叫。

設計上的最低欄位建議：

- `url`
- `tool`
- `actor`
- `channel`
- `request_source`（例如 `cli` / `telegram` / `webui`）

### 5.2 handler 行為

`web_gate` handler 的理想 skeleton 行為：

1. 接收上述欄位。
2. 組成標準 JSON payload。
3. 送到 gate adapter（目前先用 stub，不綁死命令或 endpoint）。
4. 若 gate 回傳 allow：
   - 回傳「可繼續」的 JSON 結果，例如 `allowed: true`、`next_tool: ...`。
5. 若 gate deny 或 gate 自身出錯：
   - 一律回傳拒絕結果。
   - 保持 **fail-closed**。

### 5.3 repo 內預計變更

若進入 Hermes repo 實作 skeleton，預計只做這幾件事：

- 找出現有 web / browser 類 tools
- 找出最適合插入 `web_gate` 的 toolset
- 新增 `tools/web_gate.py`
- 在 `toolsets.py` 掛入 `web_gate`
- 補最小 demo / 測試腳本
- 撰寫最小開發文件

此階段**不應**：

- 修改 Telegram runtime 設定
- 變更 key / token / users
- 啟停 gateway
- 直接打開 production browser / web / vision
- 實際接 production audit log

## 6. Codex 在新對話中的操作建議

未來若開新對話，最有效的做法是先提供這份交接摘要，然後補一句簡短操作前提：

- repo 路徑：`~/.hermes/hermes-agent`
- venv 已啟用
- 外部 gate 專案：`~/.hermes/url-wrapper-project`
- 本次目標：只做 Hermes repo 內的 `web_gate` skeleton，不碰 production 設定
- approval mode：建議 `codex --approval-mode auto`

Codex 啟動方式建議：

```bash
cd ~/.hermes/hermes-agent
source ~/.hermes/.venv/bin/activate
codex --approval-mode auto
```

Auto 模式之所以適合，是因為它允許 Codex 在工作目錄內讀檔、改檔、跑命令，而不會像更保守模式那樣頻繁中斷；但對超出 repo 範圍或高風險行為，仍保留需要確認的空間。[1][2]

## 7. 建議的下一步執行順序

### 階段 1：整理治理層

1. 把這份文件存入工作目錄或 Notion。
2. 將 Codex behavioral rules 小節補進現有 `AGENTS.md`。
3. 確認未來所有自動化都以 `AGENTS.md + Auto approval mode` 為基礎。

### 階段 2：Hermes repo 盤點

1. 讓 Codex 掃描 `tools/` 與 `toolsets.py`。
2. 找出現有 web / browser 工具。
3. 決定 `web_gate` 掛載的最小 toolset 範圍。
4. 先出 diff 計畫，再落地實作。

### 階段 3：Skeleton 實作

1. 新增 `tools/web_gate.py`。
2. 以現有 Hermes tool registry 模式註冊。
3. 掛到 `toolsets.py`。
4. 使用 stub adapter，不綁死外部專案絕對路徑。
5. 補 deterministic test 或最小 demo。

### 階段 4：驗證與文件

1. 跑最小相關測試。
2. 跑必要的 broader tests，確認沒有破壞既有 tool discovery / toolset behavior。
3. 產出簡短文件：
   - 改了哪些檔
   - 哪些是 skeleton
   - 哪些是真正留待後續接外部 gate 的部分

### 階段 5：未來再考慮功能打通

只有在以下條件都滿足後，才考慮真正讓 Hermes 的 web 行為經過 gate：

- `web_gate` skeleton 已穩定
- 測試覆蓋足夠
- 外部 gate adapter 規格穩定
- allowlist / image policy / audit policy 清楚
- 你明確決定要開哪一個工具（web、browser、vision）

## 8. 後續規劃重點

中期規劃不是一次打開所有能力，而是**分層開放**：

- 先完成 Hermes repo 中的 gate skeleton
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
- Goal: add a fail-closed `web_gate` skeleton inside Hermes repo only
- Do not modify production config, secrets, Telegram settings, audit logs, or gateway state
- Do not enable browser/web/vision in production
- Use existing Hermes tool patterns and toolsets
- Respect Hermes AGENTS.md design rules, especially prompt caching, narrow core surface, gethermeshome/displayhermeshome, and config-vs-env boundaries
- Codex should operate in repo scope with auto approval behavior
```

這段適合作為新對話的最小上下文種子。
