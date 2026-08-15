---
title: "HERMES-UPDATE-001: safely update DGX Spark from upstream"
status: MATRIX_INCOMPLETE_ENVIRONMENT_BLOCKED
date: 2026-08-15
type: operations/reliability
ticket: HERMES-UPDATE-001
target_repo: hermes-agent
---

# HERMES-UPDATE-001: safely update DGX Spark from upstream

## Ticket boundary

This is the planning, review, and source-side implementation ticket for
evaluating an upstream update. The user separately authorized source-side
implementation in the current turn. It still does not authorize merge,
deployment, service restart, or any DGX mutation. GitHub Issues are disabled
in this repository, so this repo-local plan is the ticket source of truth.

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
- The path `/home/cwliao/.hermes/hermes-agent` is the runtime virtualenv
  support path, not a Git checkout; `git -C` failed there because it has no
  repository metadata. The Claude-owned Git checkout is the separate path
  above.
- A direct bounded probe resolved the launcher: user unit
  `hermes-gateway.service`, fragment
  `/home/cwliao/.config/systemd/user/hermes-gateway.service`, is
  `active/running` with MainPID `3504674`, `NRestarts=0`,
  `ExecMainStatus=0`, and `Result=success`.
- The effective drop-in is
  `30-hermes-telegram-transport-77bcb5d0717e.conf`; its working directory
  and `PYTHONPATH` select the immutable release
  `/home/cwliao/.hermes/releases/v2026.8.15-hermes-telegram-transport-77bcb5d0717e`.
- The service process command and its MCP watchdog were directly observed.
  Runtime identity is now resolved; the dirty Claude checkout remains
  protected and is not the service's current source.

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

The source-side implementation now provides the metadata-only ref inventory in
`scripts/update_candidate.py` and the protected-behavior matrix in
`docs/plans/2026-08-15-hermes-update-001-compatibility-matrix.md`. The
inventory does not fetch, merge, reset, create a worktree, read `.hermes`, or
contact DGX.

## Concrete rollback gate (plan only)

Before any separately authorized restart, capture the effective unit/drop-ins,
prior release SHA and path, process identity, and a redacted metadata manifest
of protected user state. Roll back if any of these occurs during the bounded
post-restart window:

- the service is not `active/running` within 30 seconds, exits nonzero, or
  `NRestarts` increases;
- process cwd, `PYTHONPATH`, release marker, or effective unit does not match
  the immutable candidate;
- any required CLI/gateway smoke command exits nonzero, the service health
  tuple differs from `active/running`, `ExecMainStatus=0`, unchanged
  `NRestarts`, and the exact candidate cwd/release marker;
- the compatibility-matrix memory/session check does not return
  `PRAGMA integrity_check=ok`, the schema identity changes, or the approved
  session-continuity sentinel cannot be read back;
- any matrix-listed skill/plugin registration is missing or raises an import
  exception, or any matrix-listed cron definition fails to parse/load;
- an authorized Telegram inbound or outbound check does not return its
  documented success evidence; if either check is not authorized, that gate
  remains open and the candidate is not promoted;
- the pre/post redacted manifest detects a byte/hash/mode change in the
  protected static set (config, credentials, pairing, skills, plugins, cron
  definitions, or dirty-checkout files), or the DB/WAL integrity/schema check
  fails; or
- the 120-second health window ends without every required evidence item above
  recorded as pass. The evidence checklist is the pass/fail definition, not a
  subjective health judgment.

Restore the captured prior drop-in and immutable release selection, run a
bounded `systemctl --user daemon-reload`/restart only under the separate
rollback authorization, then verify the prior release identity, process state,
and protected-state boundary. No rollback action is executed by this ticket.

## Acceptance gates

- [x] Actual DGX launcher and runtime source identity resolved.
- [ ] Dirty checkout and `~/.hermes` state protected by metadata/backup
  evidence without exposing secrets.
- [ ] Upstream/private compatibility matrix covers all protected behaviors.
- [x] Candidate update strategy and rollback plan are explicitly selected;
  current strategy is `RETAIN_PRIVATE_RELEASE` until the matrix passes.
- [ ] Tests and CI pass for the selected strategy.
- [x] Exactly one authenticated Claude and one authenticated AGY independently
  review the same packet and reach `PASS` consensus.
- [x] Quantitative rollback triggers, prior-release restoration, and
  post-rollback verification are documented; execution remains separately
  authorized.
- [ ] Implementation, merge, deployment, restart, runtime health, and
  Telegram delivery remain separate gates.

Unchecked gates are intentionally not claimed as evidence by this plan; they
require a later implementation/deployment correction set.

For reviewer attribution, a verdict counts only when the evidence records the
host, user, absolute binary path, binary version, packet SHA256, bounded mode,
exit status, and final verdict. The final consensus pair must be DGX host
`55-0940189-03` with Claude
`/home/cwliao/.claude/remote/ccd-cli/2.1.229` and AGY
`/home/cwliao/.local/bin/agy`, both reviewing the identical packet digest.

## Review questions

- Is upstream `main` actually the intended target, given the large divergence
  from the private fork?
- Which private-fork features are required on DGX and must be ported or
  preserved before any update?
- Does the resolved `hermes-gateway.service` release identity match the
  candidate update and rollback records at deployment time?
- Can the update be rolled back without touching `~/.hermes` user state or the
  Claude-owned dirty checkout?
- Are the proposed tests sufficient to prove memory, skills, cron, plugins,
  Telegram inbound/outbound, and gateway recovery behavior separately?

## Current status

`MATRIX_INCOMPLETE_ENVIRONMENT_BLOCKED`: upstream and private fork are not a
fast-forward update. Source-side inventory and matrix implementation review
passed, but prompt-cache, gateway, state, browser, and upstream-candidate rows
remain blocked or not run because the local fallback environment lacks required
dependencies/Windows symlink privilege. The final Claude/AGY reviews remain
`PASS`; merge, deployment, restart, runtime health, and Telegram delivery
remain separate gates.

## Review evidence

- Initial review packet SHA256:
  `282f74977c475857b4bb1bc564e79393e85c3910b00edba50d8f5f3387e73499`;
  AGY correctly blocked that packet on the then-unresolved runtime identity.
- DGX AGY preflight: host `55-0940189-03`, user `cwliao`, absolute binary
  `/home/cwliao/.local/bin/agy`, version `1.1.13`, packet-only mode,
  exit `0`; final verdict `BLOCKED`.
- AGY confirmed the dirty-checkout/state protections and upstream isolation
  strategy, but blocked on unresolved runtime supervisor/launcher identity.
- Historical DGX/WSL/Windows Claude probes without a final verdict remain
  recorded as failed attempts; they were not used as approval.
- Corrected re-review packet: the current plan contents after this correction;
  the packet SHA256 is recorded in the bounded review command/evidence for
  that run.
- Corrected re-review packet SHA256:
  `bb08bc805b8ca6e1ed4c3f391817ba830b0ab3c073496d2b94e6308326c0c6cd`.
- DGX AGY reviewed that corrected packet with host `55-0940189-03`, user
  `cwliao`, absolute binary `/home/cwliao/.local/bin/agy` version
  `1.1.13`, packet-only mode, exit `0`; final verdict `PASS`.
- DGX Claude `2.1.229` then reviewed packet
  `3f8ccbe9c3eb79a7e897a4f774223d45e693bf145b493084892f9b5d82c8c410`
  directly on host `55-0940189-03`, with packet-only tool restrictions and
  exit `0`; final verdict `PASS`.
- Claude flagged the initial hash as one character short. Independent local
  verification counted the recorded initial digest as 64 hexadecimal
  characters, so no hash correction is required; this is provenance
  clarification only.
- Same-packet run `77ae411c95290c4b4d216c4831c434734746772bdb496f97684d618d6b5262a8`:
  AGY returned `PASS`; DGX Claude returned `BLOCKED` because rollback
  triggers were not quantitative and the evidence trail still described
  different packet snapshots. Those findings are the current correction set.
- The correction set now defines measurable smoke, state-manifest,
  DB/WAL-integrity, compatibility-matrix, Telegram-evidence, and 120-second
  health-window pass/fail criteria, plus the required reviewer identity fields.
- Final same-packet review packet SHA256:
  `84431197fd6e09c10164c95ca7b124a0f8798cbd5b09df1d7cbedc17fcb499`.
- Final DGX AGY review: host `55-0940189-03`, user `cwliao`, absolute binary
  `/home/cwliao/.local/bin/agy`, version `1.1.13`, packet-only mode, exit `0`,
  final verdict `PASS`.
- Final DGX Claude review: host `55-0940189-03`, user `cwliao`, absolute
  binary `/home/cwliao/.claude/remote/ccd-cli/2.1.229`, version `2.1.229`,
  packet-only mode with disallowed tools, exit `0`, final verdict `PASS`.
- Both final reviewers inspected the identical packet SHA256 above;
  independent review consensus is `PASS`. No merge, deployment, restart, or
  DGX mutation was performed by this ticket.
- Source-side implementation started after separate user authorization:
  `scripts/update_candidate.py` reports the two refs, merge base, symmetric
  commit counts, changed-file count, and path categories without touching
  runtime state. The current inventory is 119 private-only commits, 7,420
  upstream-only commits, and 7,024 changed paths.
- The compatibility matrix is recorded in
  `docs/plans/2026-08-15-hermes-update-001-compatibility-matrix.md`; all
  behavior rows remain `NOT_RUN`, so the candidate decision remains
  `RETAIN_PRIVATE_RELEASE`.
- Focused fallback validation: `py -m pytest -q
  tests/scripts/test_update_candidate.py` -> `2 passed`. The canonical
  `scripts/run_tests.sh` could not run because no repo `.venv`/`venv` or shared
  Hermes test venv is present; this is not claimed as CI-parity evidence.
- Implementation packet SHA256:
  `601e222bff2aae4cc3f3c139b10d2e2ee4b52585e225a318611cc3ea64b11997`.
- Post-implementation DGX AGY review: host `55-0940189-03`, user `cwliao`,
  absolute binary `/home/cwliao/.local/bin/agy`, version `1.1.13`, packet-only
  mode, exit `0`, final verdict `PASS`.
- Post-implementation DGX Claude review: host `55-0940189-03`, user `cwliao`,
  absolute binary `/home/cwliao/.claude/remote/ccd-cli/2.1.229`, version
  `2.1.229`, packet-only mode with tools disabled/disallowed, exit `0`, final
  verdict `PASS`.
- Both implementation reviewers inspected the identical packet digest and
  reached independent consensus `PASS`. Claude noted only minor optional test
  gaps for invalid refs/non-repo input and a non-mutating-subcommand property;
  neither is a correction request. The matrix has only partial baseline
  evidence; blocked/unrun rows keep `RETAIN_PRIVATE_RELEASE` and all
  merge/deploy/runtime gates in force.
- Matrix execution evidence: cron profile isolation `4 passed`, general plugin
  discovery `4 passed`, and candidate inventory `2 passed`. Prompt-cache and
  gateway checks were blocked by missing `requests`/`httpx`; browser/web plugin
  checks were partially blocked by the same missing dependencies; Hermes state
  path checks also hit Windows symlink/HERMES_HOME environment limits. No
  dependencies were installed and no DGX state, Telegram transport, or dirty
  checkout was touched.
