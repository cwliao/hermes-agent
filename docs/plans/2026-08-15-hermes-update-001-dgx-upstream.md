---
title: "HERMES-UPDATE-001: safely update DGX Spark from upstream"
status: PLAN_ONLY_BLOCKED_ON_RUNTIME_IDENTITY
date: 2026-08-15
type: operations/reliability
ticket: HERMES-UPDATE-001
target_repo: hermes-agent
---

# HERMES-UPDATE-001: safely update DGX Spark from upstream

## Ticket boundary

This is a planning and review ticket for evaluating an upstream update. It does
not authorize implementation, merge, deployment, service restart, or any DGX
mutation. GitHub Issues are disabled in this repository, so this repo-local
plan is the ticket source of truth.

## Verified source state

- Upstream remote: `https://github.com/NousResearch/hermes-agent.git`.
- Upstream `main`: `45af7a71fcd420b4422d2c074b1ce58b9ce0d048`.
- Private fork `origin/main`: `2edfacec61599317e9759d0cd2c47c0d87d6b6f2`.
- The two histories are not fast-forward compatible. The local comparison was
  119 commits on the private side and 7,420 on the upstream side.
- A blind `pull`, reset, or full-tree replacement is therefore prohibited.

## Verified DGX boundary

- Host: `55-0940189-03`; user: `cwliao`; access: authenticated WSL SSH.
- Actual source checkout found: `/home/cwliao/project/hermes-agent`.
- Checkout branch/HEAD: `tmp-wal-close` /
  `a13f4b8a6b52537fe27dadacf48442e287c6ccea`.
- The checkout is Claude-owned and dirty. Observed changes:
  - modified `plugins/platforms/telegram/adapter.py`;
  - untracked `plugins/kmdaily_gateway.py`;
  - untracked `tests/gateway/test_video_cache.py`.
- The previously recorded path `/home/cwliao/.hermes/hermes-agent` was not
  present during this verification.
- `systemctl --user show hermes-gateway.service` reported `inactive/dead`, and
  `systemctl --user cat hermes-gateway.service` reported no unit file. A
  transient `hermes` process was observed separately and then was no longer
  present; the real launcher/process owner must be resolved before updating.

## State and behavior that must be protected

1. **User state and credentials:**
   `~/.hermes/config.yaml`, `auth.json`, credential files, `state.db` and WAL
   files, sessions, memories, skills, plugins, pairing, cron jobs/output,
   gateway state, and logs. Do not copy secrets or message content into a
   packet, commit, or log.
2. **Private-fork behavior:** DGX SSH target/config resolution, immutable
   release markers and rollback selection, calendar guard/recovery, Claude
   remote recovery, tool-loop/artifact truth, CI baseline, KLIB/KMDaily
   integration, and Telegram transport/reconnect changes.
3. **Dirty DGX work:** preserve the Telegram adapter modification and the
   KMDaily/video-cache files byte-for-byte. Do not reset, clean, pull, rebase,
   or overwrite the checkout.
4. **Gateway contracts:** prompt caching, message-role alternation,
   tool/skill discovery, memory continuity, cron scheduling, plugin loading,
   terminal/browser tools, Telegram authorization/pairing, inbound polling,
   outbound send, and failure/recovery semantics.
5. **Operational identity:** resolve the real launcher, process cwd/PYTHONPATH,
   effective unit/supervisor, source identity, config inputs, and release
   selection before any change.

## Proposed safe update strategy

1. Inventory the actual DGX launcher, process identity, effective unit or
   supervisor, and release/config inputs using metadata-only checks.
2. Capture a redacted manifest and preserve the dirty checkout; never use the
   dirty checkout as the update target.
3. Build an isolated candidate from upstream `main` and produce a compatibility
   matrix against private `origin/main` and the DGX dirty changes.
4. Choose explicitly between upstream adoption, private-fork merge/cherry-pick,
   or retaining the current private release. Do not assume upstream is a
   drop-in replacement.
5. Run source-level, focused, and relevant integration tests for every
   protected behavior. Review the same bounded packet independently with
   exactly one authenticated DGX Claude session and one authenticated DGX AGY
   session, then reconcile findings.
6. Only after separate authorization, build an immutable DGX release, verify
   the effective launcher identity, restart within a bounded window, verify
   CLI/gateway/cron/memory/skills/plugins/Telegram inbound and outbound
   separately, and retain a tested rollback path.

## Acceptance gates

- [ ] Actual DGX launcher and runtime source identity resolved.
- [ ] Dirty checkout and `~/.hermes` state protected by metadata/backup
  evidence without exposing secrets.
- [ ] Upstream/private compatibility matrix covers all protected behaviors.
- [ ] Candidate update strategy and rollback plan are explicitly selected.
- [ ] Tests and CI pass for the selected strategy.
- [ ] Exactly one authenticated Claude and one authenticated AGY independently
  review the same packet and reach `PASS` consensus.
- [ ] Implementation, merge, deployment, restart, runtime health, and
  Telegram delivery remain separate gates.

## Review questions

- Is upstream `main` actually the intended target, given the large divergence
  from the private fork?
- Which private-fork features are required on DGX and must be ported or
  preserved before any update?
- What launcher currently owns the observed Hermes process, since the recorded
  user service is absent/inactive?
- Can the update be rolled back without touching `~/.hermes` user state or the
  Claude-owned dirty checkout?
- Are the proposed tests sufficient to prove memory, skills, cron, plugins,
  Telegram inbound/outbound, and gateway recovery behavior separately?

## Current status

`PLAN_ONLY_BLOCKED_ON_RUNTIME_IDENTITY`: upstream and private fork are not a
fast-forward update; DGX source/runtime identity has drifted from the previous
handover; no implementation or deployment is authorized.

## Review evidence

- Review packet SHA256:
  `282f74977c475857b4bb1bc564e79393e85c3910b00edba50d8f5f3387e73499`.
- DGX AGY preflight: host `55-0940189-03`, user `cwliao`, absolute binary
  `/home/cwliao/.local/bin/agy`, version `1.1.13`, packet-only mode,
  exit `0`; final verdict `BLOCKED`.
- AGY confirmed the dirty-checkout/state protections and upstream isolation
  strategy, but blocked on unresolved runtime supervisor/launcher identity.
- Recorded DGX Claude path
  `/home/cwliao/.claude/remote/ccd-cli/2.1.229` was absent. The current
  DGX `claude` command was also not available. WSL Claude `2.1.233`
  preflight produced no final marker and ended with a cancelled session-end
  hook; no Claude verdict is accepted.
- Independent reviewer consensus: `BLOCKED`; do not implement, merge,
  deploy, restart, or overwrite the DGX checkout until runtime identity is
  resolved and one authenticated Claude plus one authenticated AGY review the
  same corrected packet.
