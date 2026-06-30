# Hermes web_gate 交接摘要

## 工作範圍與狀態

- Repo：`~/.hermes/hermes-agent`；branch：`main`
- `web_gate.v1` contract、local fake adapter、repo-local wiring 與 deterministic tests 已完成。
- 真實 external gate adapter、外部 endpoint/transport、production configuration、audit integration，以及 production browser/web/vision enablement 均刻意 deferred。
- 不得讀取、呼叫、修改或依賴 repo 外的 gate 專案。

## 已實作行為

`tools/web_gate.py` 是 fail-closed 判定層，不執行網路存取或 target tool。

- Strict request/response contract：`web_gate.v1`
- Request：`url`、`tool`、`actor`、`channel`、`request_source`
- `request_source`：`cli`、`telegram` 或 `webui`
- Response decision：`allow` 或 `deny`
- Request/response 禁止額外欄位
- Allow 只能回傳 request 原本的 `tool`，adapter 不能改寫 target
- `WebGateAdapter.evaluate()` 保持 transport-independent，測試可直接注入 adapter

`LocalFakeWebGateAdapter` 是 deterministic、deny-only 的預設 adapter。它不做 network request、不讀 external/runtime state，固定回傳 `deny / gate_not_configured`。

Repo-local wiring：

- Wiring version：`web_gate.wiring.v1`
- Default mode：`local_fake`
- `resolve_web_gate_adapter()` 驗證 wiring，並由 repo-local factory selection 建立 adapter
- 沒有 external dependency，也沒有 hardcode external project path
- `web_gate` 已透過 registry 註冊，只掛入既有 `web` toolset

## Fail-closed taxonomy

| 狀況 | 結果 |
| --- | --- |
| Wiring 缺漏、格式錯誤或額外欄位 | `gate_invalid_config` |
| Unsupported wiring/response version | `gate_version_mismatch` |
| Unknown adapter mode | `gate_unknown_adapter_mode` |
| Factory exception 或 adapter 缺少 callable `evaluate` | `gate_wiring_error` |
| Adapter exception | `gate_adapter_error` |
| Response 非 mapping、欄位/decision 無效或額外欄位 | `gate_invalid_response` |
| Default local fake | `gate_not_configured` |

輸入 payload 缺少必要欄位時由 Pydantic 拒絕。任何 wiring、factory、adapter 或 response 錯誤都不會產生 allow。

## 測試狀態

- `venv/bin/pytest tests/test_web_gate.py`：15 passed
- `venv/bin/pytest tests/test_web_gate.py tests/test_toolsets.py`：42 passed
- `git diff --check`：passed

Coverage 包含 default selection、versioned request、allow/deny、原 target preservation、invalid config、unknown mode/version、factory errors、invalid responses 與 adapter exceptions。

## Production 安全邊界

不得：

- 修改 repo 外檔案
- 修改 `~/.hermes/config.yaml`、`~/.hermes/.env` 或 `~/.hermes/auth.json`
- 修改 credentials、Telegram token/channel/users
- 讀取或修改 production audit logs
- 啟停、重啟或重設 gateway
- 在 production 啟用 browser/web/vision
- 讀取、呼叫、修改或依賴 external gate project
- hardcode external project path

後續仍須遵守 `AGENTS.md`：narrow core surface、fail-closed、非 secret 設定不得放入 `.env`，且不得新增 counts/snapshots 類 change-detector tests。
