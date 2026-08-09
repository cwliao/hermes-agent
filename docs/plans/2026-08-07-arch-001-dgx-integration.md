# HERMES-ARCH-001-INTEGRATION: Wire runtime state into DGX Hermes

**Status:** Rebuilt on the current DGX release target in an isolated worktree;
independent review, commit/push, and live deployment remain pending explicit
checkpoints.

Canonical roadmap: `docs/ROADMAP-HERMES-DGX.md`.

## Objective

Make the ARCH-001 runtime-state contract an active, fail-closed dependency of
the Hermes gateway running on the DGX Spark host. The integration must use the
existing Hermes profile/home resolution and must not replace or corrupt the
gateway's current `state.db`, conversation history, or other service state.

## Confirmed deployment baseline

- Host: DGX Spark `55-0940189-03`.
- Live checkout: `/home/cwliao/.hermes/hermes-agent`.
+- Observed live checkout branch: `main`, HEAD `7fa1865f7`.
+- DGX release source branch: `ticket/T0127-v2026.8.3-merged`, HEAD
+  `8ef05d5a3`; this is the rebuild baseline because production uses a baked
+  release snapshot rather than the live checkout directly.
- Service: user systemd unit `hermes-gateway.service`, currently active.
- Service entry point: `/home/cwliao/.hermes/hermes-agent/venv/bin/python
  -m hermes_cli.main gateway run`.
- The live checkout is owned by Claude's ongoing work and currently contains
  an untracked documentation file. No direct edits, reset, pull, or restart
  are allowed against that checkout during implementation.
- ARCH-001 implementation source: commit `d773f283f` on
  `agent/arch-001-implementation`.

## Scope

1. Create an isolated integration worktree based on the exact live-service
   commit, then bring in the ARCH-001 implementation without merging or
   overwriting Claude's working tree.
2. Add one Hermes-owned bootstrap/repository boundary that opens the runtime
   state database, performs read-only compatibility preflight, enables the
   required WAL/foreign-key/busy-timeout invariants, and exposes the shared
   CAS API to approved gateway lifecycle callers.
3. Resolve the database path through Hermes' active profile/home helpers. The
   implementation must not hardcode `/home/cwliao/.hermes` or use a global
   unscoped database for multiple profiles.
4. Define and test the profile name used by the gateway, and reject missing or
   mismatched profile references before state writes.
5. Map only verified lifecycle boundaries to the ARCH-001 tables: session,
   task, approval, and compression. Existing state writers must not continue
   writing the same runtime facts through an uncoordinated second path.
6. Add startup readiness evidence and a fail-closed behavior for incompatible
   schema, WAL failure, migration failure, or invalid profile configuration.
7. Provide a rollback procedure that restores the prior checkout and service
   behavior without deleting or rewriting the runtime-state database.
8. Enforce a single-writer cutover. The staging/preflight unit must not start
   the real messaging gateway or share its runtime-state database with the
   active daemon; activation must stop the old service before starting the new
   one, and rollback must use the same serialized sequence.
9. Make profile context explicit for asynchronous task, approval, and
   compression workers. A worker must receive the resolved `profile_name` and
   runtime-state handle/context rather than resolving a default home from its
   own process environment.
10. Treat `SQLITE_BUSY`, `SQLITE_LOCKED`, stale WAL/SHM files, and failed WAL
    negotiation as startup/preflight failures. Never delete lock files or
    silently fall back to another journal mode; report a bounded, actionable
    failure and let systemd's bounded restart policy decide what happens next.

## Explicit non-goals

- Do not edit `/home/cwliao/.hermes/hermes-agent` in place.
- Do not reset, force-push, merge, or rebase Claude's branch.
- Do not migrate or rewrite the existing Hermes `state.db`.
- Do not add approval policy, compression algorithms, watchdog policy, or
  ARCH-004 contention retries.
- Do not restart or deploy the systemd service until independent review,
  focused tests, a DGX preflight, and an explicit deployment checkpoint are
  complete.

## Required design decisions before coding

- The default runtime-state path under the active profile home, plus an
  explicit configuration override and its validation rules.
- The canonical gateway profile-to-`profile_name` mapping, including the
  single-profile and multiplexed-profile cases.
- The exact lifecycle call sites for each table and the ownership token used
  by each writer.
- The single-writer cutover protocol, including the isolated preflight/staging
  unit, service stop/start ordering, and systemd restart-rate behavior.
- The atomic SQLite backup mechanism and its restore verification before the
  first live activation.
- The explicit profile/context propagation contract for background workers.
- Whether gateway startup creates only the database/preflight record or also
  creates a session row; no row creation may bypass the approved repository
  boundary.
- The readiness/health signal and the systemd rollback sequence.

## Acceptance criteria

- The isolated integration branch contains ARCH-001 and the approved Hermes
  wiring with no changes to Claude's live checkout.
- Unit and integration tests prove profile-scoped writes, lifecycle mapping,
  fail-closed startup, migration idempotence, WAL/FK preflight, and rollback
  behavior using a copied database or temporary profile home.
- A DGX read-only preflight proves the target path, service user, Python
  environment, branch/commit, and backup location before activation.
- A controlled service smoke test proves gateway startup, one representative
  lifecycle write through CAS, service health, and clean rollback.
- The staging unit never opens the live messaging connectors or writes the
  live runtime-state database concurrently with the active daemon.
- A copied-database backup/restore test proves that rollback does not delete,
  truncate, or silently replace runtime-state data.
- Lock/WAL failure tests prove bounded failure with no lock-file deletion or
  journal-mode fallback.
- The final record includes the implementation commit, deployed commit,
  preflight/test evidence, service status, and any unresolved blockers.

## Implementation checkpoint ??2026-08-07

Implemented in the isolated worktree
`D:\AI\project\hermes-agent-dgx-integration`, branch
`agent/arch-001-dgx-integration-impl`:

- Added `gateway/runtime_state.py` as the profile-scoped integration boundary.
- Added `GatewayConfig.runtime_state_db_path` with profile-contained override
  validation; the default is `runtime_state.db` under the active profile home.
- Added startup preflight before `GatewayRunner` adapter startup. Preflight
  fails closed on invalid schema, WAL/FK negotiation, migration, or path
  failures and never replaces the legacy `state.db`.
- Routed session/task creation through the central ARCH-001 create APIs and
  task completion through CAS ownership/release.
- Propagated explicit profile/session/task runtime context into gateway worker
  contexts; approval and compression lifecycle callbacks use that context.
- Preserved the live Claude checkout and DGX service; no service restart or
  deployment has occurred.

Validation evidence for this checkpoint:

- `116 passed` for the runtime/config/startup regression selection.
- `12 passed` for the startup-failure regression module.
- `17 passed` for focused runtime-state/integration tests.
- Ruff, compileall, and `git diff --check` passed.

Review status remains explicit: AGY's second review was `APPROVED` with zero
blockers; Claude's bounded review did not complete. The user explicitly
approved implementation despite that unresolved independent review, so this
checkpoint does not claim Claude/AGY consensus. Commit/push, DGX read-only
preflight, backup/restore rehearsal, single-writer staging, and deployment
remain pending.

## Review gate

The original draft was independently reviewed by AGY. The first AGY review
identified blockers around duplicate writers during cutover, a missing
isolated systemd staging unit, background-worker profile propagation, missing
atomic backup/rollback hooks, and underspecified lock/WAL failure handling.
These requirements were added to the ticket, and AGY's second review returned
`APPROVED` with zero blockers. Claude's review remained unresolved because its
bounded CLI review did not complete. Implementation proceeded only after the
user's explicit approval; this record does not claim consensus. Live
deployment still requires the remaining preflight, backup/restore, staging,
and deployment checkpoints.

## Rebuild checkpoint - 2026-08-09

The previous uncommitted integration worktree was preserved. Its ARCH-001
changes were re-applied to the fresh release-target worktree, with upstream
constructor compatibility retained by attaching the runtime manager after
GatewayRunner(config) construction.

Validation so far:

- 15 passed: standalone runtime-state contract tests.
- 2 passed: gateway runtime-state integration tests.
- 7 passed: startup-failure tests.
- 24 passed: combined runtime/integration/startup selection.
- python -m compileall: passed.
- git diff --check: passed.

The broader config selection had one unrelated baseline failure: Slack
ignored-channel environment bridging needs optional aiohttp, which is not
installed in the test environment. No ARCH-001 code changes that path.

## Fresh review checkpoint - 2026-08-09

- AGY reviewed the exact rebuild worktree
  `D:\AI\project\hermes-agent-dgx-release-rebuild` and returned
  `NEEDS_CHANGES`; this supersedes the earlier review of the preserved
  integration worktree for the current review gate.
- AGY found no immediate functional blocker, but required explicit coverage
  for invalid runtime-state paths, migration-failure atomicity, rollback or
  restore evidence, empty profile names, manager preflight failure, and
  startup-before-adapter integration.
- Codex independently reran the focused runtime/integration/startup selection:
  `24 passed` with one existing Windows pytest-cache ACL warning.
- Claude is unavailable for this cycle. No Claude/AGY consensus is claimed;
  the ticket remains `NEEDS_CHANGES` pending the required tests and a fresh
  AGY re-review.

## Test update checkpoint - 2026-08-09

- Added failure-path coverage for profile-path escape, empty active profile,
  migration rollback atomicity, online backup/restore preservation, and
  gateway preflight before `GatewayRunner` construction.
- Changed `_profile_name()` so an explicitly empty profile fails closed while
  preserving the historical `None` to `default` behavior.
- Focused ARCH-001 suite now passes `29 passed`; Ruff, compileall, and
  `git diff --check` also pass.
- A subsequent AGY invocation returned `READY` but cited paths that do not
  exist in this worktree (for example `.plans` and generated
  `.pytest-arch001-rebuild` tests). That output is evidence-invalid and is not
  treated as a valid re-review or consensus. Claude remains unavailable.
The rebuild is not committed, pushed, or deployed.
