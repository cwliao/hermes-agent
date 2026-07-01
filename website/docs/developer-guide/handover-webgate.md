# Hermes web_gate 交接摘要

## Baseline 狀態

- Hermes repo：`~/.hermes/hermes-agent`，branch `main`
- External gate：`/home/cwliao/work/hermes-audit/url-wrapper-project`
- `web_gate.v1`、repo-local wiring、`subprocess_json` 與 active config 已完成
- Allow、deny、adapter-failure smoke tests 已通過
- Actual `web`、`browser`、`vision` 已在 CLI 與 Telegram platform config 啟用
- Mandatory interception 尚未啟用；toolset 變更應以 fresh session 驗證

## Hermes contract 與 wiring

`tools/web_gate.py` 是 fail-closed 判定層，不執行 target tool。

- Request：`web_gate.v1`，含 `url`、`tool`、`actor`、`channel`、`request_source`
- `request_source` 限 `cli`、`telegram`、`webui`
- Allow 只回傳原 request 的 `tool`，adapter 不能改寫 target
- `local_fake` 預設固定 deny：`gate_not_configured`
- `subprocess_json` 以 argv list 執行 command，JSON 經 stdin/stdout 傳遞，不使用 shell

Active non-secret config：

```yaml
web_gate:
  wiring_version: web_gate.wiring.v1
  adapter_mode: subprocess_json
  command:
    - /usr/bin/python3
    - /home/cwliao/work/hermes-audit/url-wrapper-project/bin/hermes_web_gate_json.py
  timeout_seconds: 5
```

## Capability rollout

`web` 是現有 configurable toolset，包含 `web_search`、`web_extract` 與
`web_gate`。這三個工具現在也共同存在於 platform core web surface，讓 default
platform bundle 的 subset resolution 能正確恢復 `web`。在
`platform_toolsets.<platform>` 明確列出 `web` 也會只暴露這組 web tools。

Web-only selection 不會隱含啟用 `browser` 或 `vision`；behavior test 已覆蓋
`web + terminal`，證明 unrelated `terminal` 保留，而
`browser_navigate`、`vision_analyze` 不會出現。Current active
`platform_toolsets.cli` 與 `platform_toolsets.telegram` 則依使用者決策明確列出
`web`、`browser`、`vision`，三項 capability 均進入 runtime rollout。

## External CLI

Entrypoint：

```text
/home/cwliao/work/hermes-audit/url-wrapper-project/bin/hermes_web_gate_json.py
```

由 `/usr/bin/python3` 執行。CLI 嚴格驗證 `web_gate.v1` envelope，重用 external project 的 URL policy/DNS evaluator，只在 stdout 輸出 `{"allowed": true}` 或 `{"allowed": false, "reason": "..."}`。

Policy deny 與 malformed request 都輸出有效 deny decision 並 exit 0；Hermes 因此能保留具體 reason。CLI 不呼叫 Hermes target tool，也不啟用 browser/web/vision。

## Fail-closed taxonomy

| 狀況 | 結果 |
| --- | --- |
| Policy deny | `allowed: false`，保留 reason，例如 `https_required` |
| External malformed JSON input | `invalid_json` |
| External request 欄位錯誤或多餘 | `invalid_request_fields` |
| External unsupported contract version | `unsupported_contract_version` |
| Wiring 缺漏、格式錯誤或額外欄位 | `gate_invalid_config` |
| Unsupported wiring/response version | `gate_version_mismatch` |
| Unknown adapter mode | `gate_unknown_adapter_mode` |
| Factory error 或 adapter 無 callable `evaluate` | `gate_wiring_error` |
| Timeout、non-zero exit、啟動失敗、invalid subprocess JSON/schema/version | `gate_adapter_error` |
| Injected adapter malformed response | `gate_invalid_response` |
| Default local fake | `gate_not_configured` |

任何 policy、wiring、process、schema 或 version failure 都不會產生 allow。

## Smoke validation

以下 one-shot tests 已通過；直接呼叫 `web_gate_tool()`，未啟動 gateway 或 target web tool：

| Case | Input | Output |
| --- | --- | --- |
| Allow | `https://docs.nvidia.com/`，`tool=web` | `{'allowed': True, 'next_tool': 'web'}` |
| Deny | `http://localhost/`，`tool=web` | `{'allowed': False, 'reason': 'https_required'}` |
| Adapter failure | command override `['/usr/bin/false']` | `{'allowed': False, 'reason': 'gate_adapter_error'}` |

Repo tests：

- `venv/bin/pytest tests/test_web_gate.py`：23 passed
- `venv/bin/pytest tests/test_web_gate.py tests/test_toolsets.py`：50 passed
- `tests/hermes_cli/test_tools_config.py` 覆蓋 explicit web-only platform exposure
- Combined platform/toolset/web_gate validation：159 passed

## Runtime 與 service 部署狀態

- Rollout commit：`3240cdf33 Enable web_gate in the core web platform surface`
- Commit 已 push 至 `origin/main`；working tree clean
- Active profile：`/home/cwliao/.hermes/config.yaml`
- Hermes secret file：`/home/cwliao/.hermes/.env`（mode 600）；`.hermes.env` 不存在
- Telegram token 位於 `.env`，channel/routing config 保持不變
- Local Ollama endpoint：`http://127.0.0.1:11434/v1`；API 可達且模型已偵測
- User systemd unit `~/.config/systemd/user/hermes-gateway.service` 尚未建立、載入或啟動

Canonical 下一步：

```bash
hermes gateway install --no-start-now --start-on-login
systemctl --user cat hermes-gateway.service
systemctl --user start hermes-gateway.service
systemctl --user status hermes-gateway.service
```

Hermes canonical unit 會設 `HERMES_HOME=/home/cwliao/.hermes`、使用 repo venv，
並由 Hermes loader 自行載入 `.env`；不需要在 unit hardcode secret 或
`EnvironmentFile`。啟動 service 前須確認沒有 manual gateway process。

Config health audit：YAML 可解析，`terminal.cwd` 已在 `config.yaml`；local Ollama
使用 `model.provider=custom` 與 localhost base URL。`.env` 仍混有 behavior/debug
設定，例如 web-disable、browser/terminal timeout 與 tool debug flags；這些應逐項移至
既有 `config.yaml` keys。Provider keys 與 `TELEGRAM_BOT_TOKEN` 留在 `.env` 是正確的。

## Audit 與安全邊界

- Hermes production audit logs：未修改
- External project-local test audit log：有效 allow/deny evaluation 使用 `/home/cwliao/work/hermes-audit/url-wrapper-project/logs/test-telegram-policy-audit.log`
- 未修改 credentials、`.env`、`auth.json`、Telegram settings 或 gateway state
- Web、browser、vision rollout 已依 active platform config 開始
- External path 只在 non-secret `config.yaml`，未 hardcode 進 Hermes source

Baseline ready 表示 Hermes 可由 active config 選擇 `subprocess_json`、呼叫 local CLI、保留原 target，並對 allow、policy deny 與 adapter failure 做 fail-closed 判定。Web、browser、vision 已在 CLI/Telegram rollout；強制所有 web-capable calls 經 gate 仍刻意 deferred。Systemd-managed gateway 是下一個尚未完成的 deployment step。
