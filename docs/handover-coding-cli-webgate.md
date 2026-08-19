# Handover: coding-cli plugin + mandatory web_gate

**Date:** 2026-07-15
**Operator at handoff:** cwliao (paused deliberately — "decide tomorrow")
**Pickup by:** the next operator (human or Claude Code session) reading this file

## TL;DR

Two features were designed, implemented, tested, and shipped to
`origin/main` (`4c2b433bf6`) in this session. Both are **live in code but
off by default** — nothing changed in live agent behavior tonight. Two
real decisions are open before either can be turned on; see "Next steps."

## What shipped

| Feature | State | Commit |
|---|---|---|
| `web_gate.mandatory` config flag | Merged, tested, deployed. Off (`false`). | `4c2b433bf6` |
| `plugins/coding-cli/` (`/codex`, `/claude` Telegram bridge) | Merged, tested, deployed. Plugin not enabled; `external_cli.enabled: false`. | `4c2b433bf6` |
| `hermes-gateway.service` | Restarted onto the new code; `hermes doctor --fix` migrated `~/.hermes/config.yaml` to `_config_version: 35`. | n/a |

### 1. `web_gate.mandatory`

Forces `web_extract` / `browser_navigate` / `vision_analyze` through
`web_gate` for every `http(s)` URL, fail-closed, before the tool call
executes. `web_search` is exempt (no target URL). Implementation:
`tools/web_gate.py::mandatory_web_gate_block_message()`, wired into the
existing fail-closed chokepoint
`hermes_cli.plugins._get_pre_tool_call_directive_details()` — zero changes
needed at any of the 4 real dispatch call sites. Full design rationale,
policy (multi-URL all-or-nothing on `web_extract`, http(s)-only scope on
`vision_analyze`), and known limitations are documented in
`docs/handover-webgate.md` under "Mandatory interception".

**Why it's still off:** the external adapter this repo shells out to
(`~/work/hermes-audit/url-wrapper-project/integration/hermes_fail_closed_adapter.py`)
is explicitly a **"fail-closed adapter skeleton for a *future* Hermes
integration"** per its own docstring — not production-ready:

- Hardcodes `if payload["tool"] != "web": return False, "unsupported_tool"`
  — denies `browser_navigate`/`web_extract`/`vision_analyze` unconditionally,
  regardless of URL.
- Hardcodes `if payload["request_source"] != "telegram": return False,
  "unsupported_request_source"` — denies all CLI-originated calls too.
- Defaults to `policies/url_allowlist.test.yaml` and
  `logs/test-telegram-policy-audit.log` — test fixtures, not a real policy.

Flipping `web_gate.mandatory: true` today would not gate web access — it
would kill it outright for every real tool. This needs a fix on the
*external* project (accept real tool names + request sources, wire a real
non-test policy file), not a patch inside a "skeleton" file.

### 2. `plugins/coding-cli/`

New bundled plugin. `/codex <prompt>` / `/claude <prompt>` shell out to the
real `codex`/`claude` CLIs non-interactively (`codex exec [resume <id>]` /
`claude -p [--resume <id>]`), one Telegram message = one CLI turn, resumed
via each CLI's own session/thread id persisted per chat at
`$HERMES_HOME/coding-cli-sessions.json`. `/codex dir <path>` /
`/codex reset` manage per-chat working directory and session reset.
Zero changes to `gateway/run.py` (the ~10k-line core dispatch file) —
implemented entirely via the existing `ctx.register_command()` plugin API,
which the gateway awaits inline with no enclosing timeout.

Fail-closed behind two config gates in `external_cli:`
(`hermes_cli/config.py::DEFAULT_CONFIG`):
- `enabled: false` (master switch)
- `allowed_roots: []` (empty = `/codex dir` / `/claude dir` reject
  everything; must list at least one directory)

Full usage, config table, and known limitations (no streaming, per-chat
not per-user, `timeout_seconds` default 180s) are in
`plugins/coding-cli/README.md` and
`website/docs/user-guide/features/built-in-plugins.md` under "coding-cli".

**Why it's still off:** enabling it arms a Telegram-triggered
code-execution pathway (`codex_sandbox: workspace-write` and
`claude_permission_mode: acceptEdits` by default — both can write files
and run shell commands within `allowed_roots`) that persists across
gateway restarts. The Claude Code permission system itself blocked an
attempt to silently default `allowed_roots` to this repo mid-session,
correctly flagging that "enable it!!" didn't specify the intended scope.

## Next steps (pick up here)

| # | Item | What's needed |
|---|---|---|
| B1 | Decide `coding-cli` scope | Pick `allowed_roots` (this repo? elsewhere? multiple?) and sandbox level (keep `workspace-write`/`acceptEdits`, or start `read-only`/`plan` and loosen later). Then: `hermes plugins enable coding-cli`, `hermes config set external_cli.enabled true`, `hermes config set external_cli.allowed_roots '["<path>"]'`. Restart gateway. |
| B2 | Fix the external web_gate adapter | In `~/work/hermes-audit/url-wrapper-project`: accept the real tool names (`web_extract`/`browser_navigate`/`vision_analyze`, not just `"web"`) and `request_source in {cli, telegram, webui}` (not just `"telegram"`), and point at a real (non-test) policy file with an actual allowlist. Separate project, separate session. |
| B3 | Only after B2 | Set `web_gate.mandatory: true` in `~/.hermes/config.yaml`, restart gateway, verify with an allow + deny case through each of `browser_navigate`/`web_extract`/`vision_analyze` (see the manual-verification steps in `docs/handover-webgate.md`). |

## Verification already done this session

- `venv/bin/pytest tests/test_web_gate.py tests/hermes_cli/test_plugins.py tests/test_toolsets.py tests/plugins/ tests/hermes_cli/test_config.py tests/hermes_cli/test_config_validation.py -q` — all passed (2000+ tests, zero regressions).
- `hermes plugins list` confirmed `coding-cli` discovered as `bundled`, `not enabled`.
- Manual dry-run of `mandatory_web_gate_block_message` against the live `subprocess_json` wiring on this box surfaced the `unsupported_tool` finding above.
- `hermes-gateway.service` restarted cleanly onto the new code (`journalctl` showed normal startup, Telegram reconnect, no crash).

## Audit / what was NOT changed

- `~/.hermes/config.yaml` — only `_config_version` migrated (33→35, via
  `hermes doctor --fix`, adds new default keys); `web_gate.mandatory` and
  `external_cli.enabled` both landed as `false`. No plugin enabled.
- `~/.hermes/.env` — not touched.
- `~/work/hermes-audit/url-wrapper-project` — not touched (read-only
  investigation only).
- `upstream` remote — push disabled, not touched. Pushed to `origin/main`
  only.
