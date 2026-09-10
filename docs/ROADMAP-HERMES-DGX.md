# Hermes Architecture Roadmap

> **UPDATE 2026-09-10 (Asia/Taipei):** Everything below this notice is a
> **superseded historical snapshot** from 2026-08-17, kept for history —
> ARCH-002/003/004 and the tickets below are ancient by commit-count now
> (hundreds of commits and roughly three weeks have passed). Do not treat
> the "Current topology" table, "Ticket status" table, or "Current next
> lane" section below as live. See the new **"Current state, 2026-09-10"**
> section immediately after this notice for what's actually true today;
> everything from `## Source-of-truth rules` onward is the original
> 2026-08-17 text, unedited.
>
> ## Current state, 2026-09-10
>
> - **`main` HEAD:** `961630ab6f5f5ce522e28f73c05f4c83ec5fea5d` (2026-09-10).
>   As of the last check this same day, local `main` is 11 commits behind
>   the true upstream tip (`NousResearch/hermes-agent`, remote `upstream`)
>   — upstream merges very fast, so this number is stale by the time you
>   read it. Re-check with `git fetch upstream main && git rev-list
>   --left-right --count HEAD...upstream/main` rather than trusting any
>   number written here or in `hermes --version` (the latter compares
>   against `origin/main`, the user's own fork mirror, which can itself go
>   stale for a long time — see AgentMemory
>   `projects/LLMINFRA/design-notes/LLMINFRA-design-hermes-upstream-sync-202609-final.md`).
> - **Live gateway:** `hermes-gateway.service`, deployed via the systemd
>   drop-in + release-snapshot procedure documented in
>   `docs/operations/hermes-upstream-updater.md`'s sibling note below, and
>   in AgentMemory `LLMINFRA-design-hermes-deploy-procedure-202609-final.md`.
>   Verified live and connected to Telegram as of this update.
> - **Telegram user-visible delivery — the gate this roadmap's 2026-08-17
>   snapshot lists as `NEXT_GATE`/unverified — is now CLOSED.** Verified
>   for real on 2026-09-09: a live Telegram message triggered an instamem
>   MCP tool call from Hermes's own main agent loop, independently
>   confirmed via direct `sqlite3` query against the target database with
>   timestamps cross-referenced against the gateway's own journal log for
>   the same turn. Full record:
>   `~/project/instamem/docs/architecture/ticket-hermes-main-loop-instamem-unverified.md`.
> - **This session's upstream syncs were done manually** (`git fetch
>   upstream` + `git rebase upstream/main` + hand-resolved conflicts +
>   `git push --force-with-lease origin main` + manual
>   `scripts/release_snapshot.py` + systemd drop-in swap), **not** through
>   this repo's own formal `scripts/hermes_upstream_{preflight,review,apply}.py`
>   tool documented in `docs/operations/hermes-upstream-updater.md`. That
>   tool has a real prior track record (see
>   `~/.hermes/hermes-upstream-state/candidates/*.json`) but its own
>   preflight was `BLOCKED` on a `STALE_REVIEW_CANDIDATE` (an orphaned
>   `refs/upstream/review/20260906-050337` ref) as of its last scheduled
>   run (2026-09-09 20:30 UTC / 2026-09-10 04:30 Asia/Taipei, via the daily
>   "Hermes upstream update guard" cron job) — this predates and is
>   unrelated to today's manual syncs, but means the tool's own state may
>   now be out of sync with reality after two manual force-pushes to
>   `origin/main`. Reconcile that tool's state (or at minimum clear the
>   orphan ref per its own runbook) before trusting it for the next sync.
> - A fork-specific bug found and fixed today, unrelated to any of the
>   above tickets: external watcher leases (`hermes kanban watcher
>   register`) weren't checked against the default board when a task was
>   created on a different, explicitly-named board — see AgentMemory
>   `LLMINFRA-design-hermes-rebase-conflict-patterns-202609-final.md` for
>   the full root cause. Fixed in commit `961630ab6f`.
>
> --- original 2026-08-17 snapshot below, unedited ---
>
> Snapshot: 2026-08-17 (Asia/Taipei). `main` is the canonical Hermes
> integration line; DGX release snapshots are deployable evidence only.
> Runtime service health, Telegram inbound polling, outbound delivery, and
> rollback remain separate gates.

## Source-of-truth rules

- Every architecture or long-term integration ticket must be reviewed and merged into Hermes `main` before it is complete.
- A DGX release snapshot is deployable evidence, not mainline completion.
- Deployment requires immutable identity, effective user-unit evidence, runtime evidence, and rollback evidence.
- The review loop is review -> revise -> independent cross-review -> reconcile -> READY or BLOCKED.
- A running systemd service does not prove Telegram inbound readiness or user-visible delivery.

## Current topology

| Reference | Current evidence | Meaning |
|---|---|---|
| `main` runtime code baseline | `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea` | ARCH-004 merged via PR #33; later handover/roadmap commits are documentation-only. |
| Primary laptop checkout | `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8` | Dirty audit checkout; preserve all pre-existing changes and untracked files. |
| DGX live source checkout | `/home/cwliao/.hermes/hermes-agent`, clean HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb` | Deployment input only; do not reset or edit as active runtime source. |
| DGX active release | `/home/cwliao/.hermes/releases/v2026.8.17-hermes-arch-004-e2f94e26` | Immutable runtime snapshot selected by drop-in `36-hermes-arch-004-e2f94e26.conf`; marker matches merge `e2f94e26...`. |
| Gateway service | `active/running`, MainPID `1654068`, `NRestarts=0`, `ExecMainStatus=0` | Service/process health PASS after ARCH-004 restart and bounded post-start check; effective path matches ARCH-004 release. |
| Rollback | ARCH-003 release/drop-in and earlier Telegram/ARCH-002 releases retained | Rollback evidence remains available under `/home/cwliao/.hermes/deploy-backups/hermes-arch-004-e2f94e26`; no prior release was deleted. |

## Core engineering order

1. CI-BASELINE-001 — restore blocking Python CI while preserving behavior. **Complete.**
2. ARCH-002 — extend the runtime-state contract. **Merged, deployed, and runtime marker verified.**
3. ARCH-003 — audit/replay integration after the shared state boundary is stable. **Merged, deployed, and runtime marker verified.**
4. ARCH-004 — redaction and SQLite/WAL safeguards. **Merged, deployed, and runtime marker verified.**

## Product priority

1. Verify the private Telegram baseline: DM, one allowlisted user, /status, and restart recovery.
2. Gateway plus cron/job health monitoring and failure/recovery alerts.
3. Mobile HITL for destructive Hermes operations.
4. Verify voice and file handoff in the real deployment.
5. Safe remote coding-agent workflow on Spark, including Claude/Codex/AGY, TaskRouter, worktrees, runner supervision, and external CLI HITL.
6. Team Telegram bot with pairing, per-user sessions, groups, and isolation.
7. Scheduled briefings and knowledge-base workflows.
8. Multi-bot or bot-to-bot collaboration last.

## Ticket status

| Ticket | Status | Current evidence / next action |
|---|---|---|
| HERMES-UPDATE-001 | MERGED_DEPLOYED | PR #22 and immutable DGX release remain historical verified evidence. |
| HERMES-TELEGRAM-TRANSPORT-001 | MERGED_DEPLOYED_RUNTIME_RECOVERED | Prior startup timeout/reconnect degradation recovered through bounded retry; current service has repeated qualifying empty polling progress. User-visible delivery remains separate. |
| HERMES-TELEGRAM-INBOUND-001 | MERGED_DEPLOYED_INBOUND_POLLING_PASS | PR #26 merged as `178c9be1...`; current release records repeated `telegram_polling_progress` metadata with empty batches. Accepted-update/user-visible response remains a separate gate. |
| HERMES-AUTH-001 | MERGED_DEPLOYED | Separate auth ticket; do not conflate it with ARCH-003. |
| HERMES-AUTH-002 | MERGED_DEPLOYED | Separate target-config ticket; do not conflate it with ARCH-003. |
| HERMES-CALENDAR-GUARD-001 | MERGED_DEPLOYED | Actual DGX guard naming/effective unit remains separate documentation hygiene. |
| HERMES-MONITORING-001 | BLOCKED | No merge/deployment/readiness inference from current gateway evidence. |
| ARCH-002 | MERGED_DEPLOYED | PR #29 merge `3e9fd48d...`; 24 focused tests passed; CI `31937692260` PASS; AGY + Claude Opus implementation review PASS; immutable DGX release active. |
| ARCH-003 | MERGED_DEPLOYED | PR #30; merge `e8cdfd1e...`; implementation review consensus PASS; CI run `31981532693` PASS after retrying an unrelated pre-existing stream-consumer slice; immutable DGX release active with rollback metadata. |
| ARCH-004 | MERGED_DEPLOYED | PR #33; implementation commit `650a34808`; merge `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea`; focused tests `32 passed`, compileall PASS; required CI run `31990198398` PASS; Claude + active DGX `.hermes` AGY implementation review PASS with no corrections; immutable DGX release active with rollback metadata. |
| HERMES-TELEGRAM-DELIVERY-VERIFICATION-001 | NEXT_GATE | Repo-local verification gate because GitHub Issues are disabled. Prove an approved user-visible Telegram response/delivery path with metadata-only evidence; do not substitute service health, polling progress, `getMe`, `getWebhookInfo`, or empty `getUpdates`. |

## Current next lane

**HERMES-TELEGRAM-DELIVERY-VERIFICATION-001** is the next lane. Establish
real user-visible Telegram response/delivery evidence through an approved test
path, recording only metadata needed to correlate the attempt and outcome.
Polling progress, gateway health, token-protected API probes, and empty update
batches remain supporting evidence only and cannot close this gate. No new
runtime implementation or DGX mutation is authorized by this roadmap entry.

## Runtime and deployment boundary

The active release is selected through the ARCH-004 drop-in while the prior
ARCH-003 and Telegram releases remain intact for rollback. The service
restarted successfully and stayed active with zero restarts. The gateway has
historical evidence of API reachability and repeated empty `getUpdates`
progress after transient startup recovery, but this is inbound polling
evidence, not user-visible delivery evidence. No Telegram credentials,
allowlists, webhook state, or TLS verification were changed during the
ARCH-004 deployment.

## Ticket inventory note

GitHub Issues are disabled. The only open PR is PR #21, the HERMES-UPDATE-001
DGX upstream update plan, with required checks passing; it remains open and is
not part of the ARCH-004 runtime deployment. Repo-local plans and this roadmap
remain the authoritative ticket inventory until that planning PR is resolved.
