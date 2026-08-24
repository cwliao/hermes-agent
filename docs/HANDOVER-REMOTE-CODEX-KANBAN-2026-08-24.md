# Hermes / DGX Spark Remote-Control Handover

Date: 2026-08-24, Asia/Taipei

## Purpose

This handover lets a new Codex dialog, including a phone dialog, continue Hermes work on DGX Spark. The phone dialog will not automatically share this desktop conversation. It must have access to the same remote workspace and user service. This is terminal and service control, not remote desktop control.

## Host and source of truth

- Host: DGX Spark, previously verified hostname 55-0940189-03
- User: cwliao
- Hermes home: /home/cwliao/.hermes
- Source repo: /home/cwliao/.hermes/hermes-agent
- Branch: main
- Remote: git@github.com:cwliao/hermes-agent.git
- Last verified commit: 4212d1f20099467128baa54b556e982024f0ce9a
- Expected repo state: clean, main equals origin/main

Verify before changing:

~~~bash
systemctl --user show hermes-gateway.service -p ActiveState -p SubState -p MainPID -p ExecMainStatus -p NRestarts -p WorkingDirectory -p Environment
git -C /home/cwliao/.hermes/hermes-agent status --short --branch
git -C /home/cwliao/.hermes/hermes-agent rev-parse HEAD origin/main
~~~

## Live deployment

- Release: /home/cwliao/.hermes/releases/v2026.8.24-codex-dispatch-bwrap-escape-4212d1f200
- Venv: /home/cwliao/.hermes/venvs/gateway-4212d1f200
- Drop-in: /home/cwliao/.config/systemd/user/hermes-gateway.service.d/92-codex-dispatch-bwrap-escape-4212d1f200.conf
- Expected service: active/running, NRestarts=0
- Expected release SHA: 4212d1f20099467128baa54b556e982024f0ce9a
- Main PID at last check: 2955310

Previous releases, drop-ins, and rollback backups remain. Do not delete them.

## Live external Codex settings

~~~yaml
external_cli:
  enabled: true
  allowed_roots:
    - /home/cwliao/hermes-coding-cli-workspace
  codex_bin: /home/cwliao/.local/bin/codex
  codex_sandbox: danger-full-access
  profile_home: /home/cwliao/.hermes-coding-cli-home
~~~

Mode-700 profile and workspace:

- /home/cwliao/.hermes-coding-cli-home
- /home/cwliao/hermes-coding-cli-workspace

Never expose config.yaml, .env, Telegram session data, credentials, tokens, or secrets.

## Root cause and fix

The repeated failure was an environment bwrap namespace restriction, not auth or dispatcher:

~~~text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
bwrap: setting up uid map: Permission denied
~~~

Codex exec with workspace-write cannot create the needed user/network namespace in this container. The Spark external Codex pool therefore uses danger-full-access, while allowed_roots still restricts its working directory. The bridge serializes Codex turns with:

~~~text
/home/cwliao/.hermes-coding-cli-home/.codex-dispatch.lock
~~~

The general source default remains workspace-write. Claude behavior was not changed.

Important: /codex is a Telegram external-CLI command, not a Kanban lane. Kanban remains:

~~~text
native_hermes / claude / grok / agy
~~~

No Codex lane or Kanban routing change has been made.

## Verification completed

- coding-cli focused tests: 43 passed
- Direct Codex 0.149.1 smoke executed /bin/bash -lc true and returned SMOKE_OK
- Direct smoke had no bwrap error
- Gateway after deployment was active/running with NRestarts=0
- Release provenance matched the full Git SHA
- Model-refresh, Apps MCP, and analytics timeout warnings were separate non-blocking warnings around the direct smoke

## Telegram test

This tests external CLI, not Kanban:

~~~text
/codex dir /home/cwliao/hermes-coding-cli-workspace
/codex 執行 shell command true，完成後只回覆 TELEGRAM_CODEX_OK
~~~

Then inspect:

~~~bash
journalctl --user -u hermes-gateway.service --since "10 minutes ago" --no-pager
~~~

Do not claim Telegram end-to-end success until the actual Telegram reply or delivery evidence is observed.

## Safe continuation rules

1. Read this file and repo AGENTS.md first.
2. Verify host, user, service, branch, and clean status.
3. Never use git reset --hard, git clean, or broad deletion.
4. Never edit the active release directly. Build an immutable release and preserve rollback.
5. Stage only relevant files and run focused tests.
6. Never expose secrets.
7. For a Telegram test, observe and diagnose first; do not change code speculatively.

## Phone pickup prompt

Paste this into the new phone dialog:

> 你現在接管 DGX Spark 上的 Hermes remote-control 工作。先讀取 /home/cwliao/.hermes/hermes-agent/docs/HANDOVER-REMOTE-CODEX-KANBAN-2026-08-24.md 與 repo 的 AGENTS.md，再做唯讀 health check。不要猜測，不要 reset/clean dirty tree，不要暴露 secrets。
>
> 目前目標是驗證 Telegram 的 /codex external CLI bridge。先確認 gateway active、release SHA、Git clean 狀態，再讀最近 10 分鐘 gateway journal。注意：/codex 不是 Kanban lane，Kanban 仍是 native_hermes/claude/grok/agy。只有 logs 證明需要時才修 code；若要修改，建立 immutable release、測試、commit、push、deploy，並回報完整 SHA、release path、PID、rollback path。
>
> 我接下來會從手機送 Telegram 測試訊息；請持續觀察並回報成功、失敗原因與證據，不要把未觀察到的結果當成成功。

## Files

- Plugin: /home/cwliao/.hermes/hermes-agent/plugins/coding-cli/
- Ticket: /home/cwliao/.hermes/hermes-agent/docs/plans/2026-08-24-codex-dispatch-bwrap-escape-001.md
- Existing handover: /home/cwliao/.hermes/hermes-agent/docs/HANDOVER.md
