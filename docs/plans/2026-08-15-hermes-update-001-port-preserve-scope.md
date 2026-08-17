---
title: "HERMES-UPDATE-001 port and preserve scope"
status: PORT_PRESERVE_SCOPE_REVIEW_PENDING
date: 2026-08-16
type: operations/reliability
ticket: HERMES-UPDATE-001
target: Linux DGX Spark deployment
---

# HERMES-UPDATE-001 port and preserve scope

This document defines the correction set that must be reviewed before any
implementation work. It does not authorize implementation, merge, release,
service restart, or deployment.

## Source boundary

| Source | Identity | Role |
| --- | --- | --- |
| Private fork baseline | `origin/main` / `2edfacec61599317e9759d0cd2c47c0d87d6b6f2` | Feature source and current private contract |
| Upstream candidate | `upstream/main` / `45af7a71fcd420b4422d2c074b1ce58b9ce0d048` | Clean Linux-target implementation base |
| Merge base | `569b912d7d0931c7256e9f5fb326609e9deda377` | Divergence comparison anchor |
| DGX runtime | release `v2026.8.15-hermes-telegram-transport-77bcb5d0717e` | Behavior and launcher baseline only |
| DGX dirty checkout | `/home/cwliao/project/hermes-agent`, `tmp-wal-close`, `a13f4b8a6b52537fe27dadacf48442e287c6ccea` | Protected; never a port source or target |

The private and upstream histories are not a fast-forward update: 119
private-only commits, 7,420 upstream-only commits, and 7,024 changed paths.
“All need port” means all identified private product feature lanes below, not
blindly copying every divergent commit, generated file, documentation change,
or unrelated upstream feature.

## Port scope: private product features that must be adapted

Each lane must be mapped onto the upstream candidate's current architecture,
configuration, interfaces, and tests. A lane is not complete when files merely
exist; it is complete only when its behavior tests and Linux-target operational
evidence pass.

| Lane | Private source anchors | Port requirement | Minimum evidence |
| --- | --- | --- | --- |
| DGX auth and target resolution | `docs/dgx-ssh-recovery.md`, `docs/plans/2026-08-14-hermes-auth-001-dgx-ssh-recovery.md`, `gateway/config.py`, `hermes_cli/gateway_identity.py`, `hermes_cli/release_markers.py`, `scripts/release.py`, `scripts/release_snapshot.py` | Adapt SSH/config resolution, release identity, immutable snapshot selection, and fail-closed target checks to upstream APIs | Resolver tests; exact Linux host/unit/cwd/PYTHONPATH tuple; no dirty-checkout fallback |
| Immutable release and rollback | `scripts/release.py`, `scripts/release_snapshot.py`, `scripts/systemd/`, `docs/plans/2026-08-14-hermes-auth-001-dgx-ssh-recovery.md` | Preserve release markers, prior-release selection, quantitative rollback triggers, and release isolation | Snapshot/rollback tests; redacted pre/post manifest; bounded rollback drill plan |
| Calendar and gateway recovery | `hermes_cli/calendar_guard.py`, `scripts/hermes_calendar_guard.sh`, `scripts/install_calendar_guard.py`, `tests/hermes_cli/test_calendar_guard.py`, commit `d573ce0a1` | Adapt stale-code detection, restart recommendation, guard scheduling, and failure reporting without hiding service health | Guard tests; systemd timer/unit inspection; no automatic restart during review |
| Claude recovery | `hermes_cli/claude_recovery.py`, `hermes_cli/subcommands/claude_recovery.py`, `tests/hermes_cli/test_claude_recovery.py`, commits `7e78039c9`, `9f3755c42` | Adapt authenticated Claude recovery routing and fail-closed behavior to upstream provider/session interfaces | Unit/config tests; authenticated review evidence; no credential or prompt content in artifacts |
| Tool-loop and artifact truth | `docs/plans/2026-08-14-hermes-reliability-002-tool-loop-artifact-truth.md`, `run_agent.py`, `tests/run_agent/`, commit `4198f1efe` | Preserve strict message alternation, bounded continuation, truthful artifact/result reporting, and prompt-cache stability | Prompt-cache, message-sequence, runtime-recovery, and artifact-truth tests |
| Telegram transport and reconnect | `plugins/platforms/telegram/adapter.py`, `gateway/run.py`, `tests/gateway/test_telegram_network_reconnect.py`, `tests/gateway/test_telegram_plugin_callbacks.py`, commit `77bcb5d07` | Adapt polling progress, reconnect recovery, pairing/authz, inbound dispatch, outbound delivery, and controlled execution | Linux candidate tests; authorized inbound/outbound checks; recovery evidence |
| KLIB/KMDaily integration | `plugins/klib/`, `plugins/kmdaily/`, `tests/plugins/test_klib.py`, `tests/plugins/test_kmdaily.py`, related Telegram callback tests | Adapt plugin discovery, commands, pagination, Drive-link behavior, and gateway callback contracts | Plugin/import tests; no credential or message content; authorized Linux delivery checks |
| CI and baseline guardrails | `.github/workflows/ci.yml`, `docs/plans/2026-08-10-ci-baseline-001.md`, commit `d20c48132` | Port compatible CI/test gates and retain fail-closed baseline checks in the upstream workflow | CI config validation; required checks green; no weakening of existing upstream gates |
| Gateway/runtime state protections | `gateway/runtime_state.py`, `gateway/code_skew.py`, `gateway/config.py`, `tests/gateway/test_runtime_state_integration.py`, `tests/test_code_skew.py` | Adapt code-skew detection, runtime identity, session/WAL continuity, and profile/cron/plugin loading | State/WAL, discovery, cron, launcher identity, and manifest evidence |

## Preserve scope: contracts and state that must not be lost

Preserve means the port must keep these properties even if upstream file names,
classes, or APIs differ:

1. **User state and credentials:** `~/.hermes/config.yaml`, `auth.json`,
   credential files, `state.db`/WAL/SHM, sessions, memories, skills, plugins,
   pairing, cron definitions/output, gateway state, and logs. Artifacts contain
   metadata/hash/count only, never contents.
2. **Runtime identity:** DGX host `55-0940189-03`, authenticated WSL SSH route,
   `systemctl --user` unit, immutable release path, exact cwd/PYTHONPATH,
   release marker, and rollback selection.
3. **Agent contracts:** per-conversation prompt-cache stability, strict role
   alternation, no synthetic user insertion, truthful tool/artifact results,
   bounded loops, memory/session continuity, and plugin/skill discovery.
4. **Gateway contracts:** Telegram pairing/authz, polling progress, reconnect,
   inbound dispatch, outbound delivery, controlled execution, and failure
   visibility.
5. **Safety boundaries:** no use of the Claude-owned dirty checkout as a port
   source/target, no secret/message content in packets, no blind reset/pull,
   and no runtime mutation before separate authorization.

## Explicitly out of scope for this Linux target

- Windows-native wrapper, path-mode, chmod, and symlink semantics. Linux
  candidate behavior was validated through WSL; Windows is not the deployment
  target.
- Blind cherry-picking of all private-only commits or copying unrelated
  upstream changes.
- Porting generated artifacts, caches, `.venv`, test temp directories, or
  private documentation that has no runtime contract.
- Modifying `/home/cwliao/project/hermes-agent`, `~/.hermes`, the active
  systemd unit, the deployed release, or Telegram runtime during review.
- Merge, immutable release creation, restart, deployment, rollback execution,
  and production Telegram delivery. Those are later authorization gates.

## Port order and gates

1. Review this complete port/preserve correction set independently with exactly
   one authenticated DGX Claude and one authenticated DGX AGY on the same
   bounded packet.
2. Reconcile findings into one correction set. A missing lane, missing
   preserve contract, or unsupported evidence requirement is `REVISE` or
   `BLOCKED`, not an implicit approval.
3. Implement all approved lanes on a clean upstream worktree only; preserve the
   primary dirty worktree and DGX dirty checkout.
4. Run Linux candidate tests, state/manifest checks, plugin/cron checks, and
   authorized Telegram inbound/outbound checks.
5. Repeat the same-packet independent review after implementation. Only an
   independent Claude+AGY consensus `PASS` opens the separate merge gate.
6. After separately authorized merge/deploy, capture post-change manifest,
   verify immutable release identity, restart within the bounded window, verify
   health, and verify Telegram inbound/outbound independently.

## Reviewer decision requested

Reviewers must answer whether:

- the port lanes cover all private product behavior required by the Linux DGX
  deployment;
- the preserve set protects every user-state, runtime, agent, gateway, and
  safety contract;
- any lane is incorrectly scoped, missing, duplicated, or not adaptable to the
  upstream candidate;
- the evidence gates can prove port correctness without exposing secrets or
  mutating DGX; and
- the proposed order keeps implementation, review, merge, deployment, runtime
  health, and Telegram delivery as separate gates.

Required verdict: `PASS`, `REVISE`, or `BLOCKED`, with an explicit correction
list and no implementation actions.
