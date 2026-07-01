# Hermes web_gate 交接摘要

## Baseline 狀態

- Hermes repo：`~/.hermes/hermes-agent`，branch `main`
- Current main includes latest fetched upstream merge `9c69b43a6b`
- External gate：`/home/cwliao/work/hermes-audit/url-wrapper-project`
- `web_gate.v1`、repo-local wiring、`subprocess_json` 與 active config 已完成
- Allow、deny、adapter-failure smoke tests 已通過
- Actual `web`、`browser`、`vision` 已在 CLI 與 Telegram platform config 啟用
- Mandatory interception 尚未啟用；toolset 變更應以 fresh session 驗證
- User-level `hermes-gateway.service` 已建立並啟用；gateway 目前由 systemd user
  service 管理

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
- Upstream integration focused set after merge：159 passed
- Full `scripts/run_tests.sh` after upstream merge：36,852 passed，10 unrelated
  host/config-dependent failures（vision/provider/model-switch surfaces；非 web_gate）

## Runtime 與 service 部署狀態

- Current main：`9c69b43a6b`，已 push 至 `origin/main`；working tree clean
- Active profile：`/home/cwliao/.hermes/config.yaml`
- Hermes secret file：`/home/cwliao/.hermes/.env`（mode 600）；`.hermes.env` 不存在
- Config file：`/home/cwliao/.hermes/config.yaml`（mode 600），已 migrate 至
  `_config_version: 32`
- Local Ollama endpoint：`http://127.0.0.1:11434/v1`；API 可達，最近驗證 9 models
- User systemd unit：`~/.config/systemd/user/hermes-gateway.service`
  - `ExecStart=/home/cwliao/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run`
  - `WorkingDirectory=/home/cwliao/.hermes`
  - `HERMES_HOME=/home/cwliao/.hermes`
  - service 已 enable 且 restart 後 active
- 管理 gateway 時優先使用：

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service
journalctl --user-unit hermes-gateway.service -f
```

Prompt/toolset changes still require fresh CLI/Telegram sessions because
conversation prompt/tool schema prefixes may be cached.

Config/security health baseline：

- `approvals.destructive_slash_confirm: true` 是 intended hardened setting；
  使用者在 Telegram 按「Always Approve」會再次把它改回 `false`。
- `skills.guard_agent_created: true`
- Tirith installed at `/home/cwliao/.hermes/bin/tirith`，version `0.3.1`
- `security.tirith_fail_open: false`（Tirith unavailable 時 fail closed）
- `.env` cleanup 已移除 stale/non-secret overrides：
  `TERMINAL_ENV`、`TERMINAL_TIMEOUT`、`TERMINAL_LIFETIME_SECONDS`、
  `TERMINAL_MODAL_IMAGE`、tool debug flags、`BROWSER_SESSION_TIMEOUT`、
  `HERMES_DISABLE_WEB_TOOLS`
- `terminal.timeout: 60` 與 `terminal.lifetime_seconds: 300` 已保留在
  `config.yaml`
- `.env` typo corrected：`TAVILI_API_KEY` → `TAVILY_API_KEY`
- Browserbase compatibility envs (`BROWSERBASE_PROXIES`,
  `BROWSERBASE_ADVANCED_STEALTH`) remain in `.env` because current provider code
  still reads them directly and no YAML bridge exists yet.
- Provider keys and `TELEGRAM_BOT_TOKEN` remain in `.env` as secrets.

Telegram channel note：

- Hermes has successfully used Telegram chat id `-1003954447810`.
- Previous startup notification failed because `TELEGRAM_HOME_CHANNEL` was
  `-3954447810`, missing the `-100` supergroup/channel prefix.
- Intended home-channel value should be:

```env
TELEGRAM_HOME_CHANNEL=-1003954447810
```

Do not change Telegram token/user allowlist values unless explicitly requested.

## Audit 與安全邊界

- Hermes production audit logs：未修改
- External project-local test audit log：有效 allow/deny evaluation 使用 `/home/cwliao/work/hermes-audit/url-wrapper-project/logs/test-telegram-policy-audit.log`
- 未修改 credentials、`.env`、`auth.json`、Telegram settings 或 gateway state
- Web、browser、vision rollout 已依 active platform config 開始
- External path 只在 non-secret `config.yaml`，未 hardcode 進 Hermes source

Baseline ready 表示 Hermes 可由 active config 選擇 `subprocess_json`、呼叫 local CLI、保留原 target，並對 allow、policy deny 與 adapter failure 做 fail-closed 判定。Web、browser、vision 已在 CLI/Telegram rollout；強制所有 web-capable calls 經 gate 仍刻意 deferred。Systemd-managed gateway 已建立並由 user service 管理。
