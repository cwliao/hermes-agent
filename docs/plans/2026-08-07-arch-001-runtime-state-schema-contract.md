# HERMES-ARCH-001: Versioned shared runtime state schema

Status: ticket detail revised for implementation review; implementation not included
Parent: `HERMES-ARCH-000`
Priority: P0
Dependencies: none
Owner: Runtime/Data
Schema version: `1`
Minimum compatible schema version: `1`

## Objective

Create the authoritative SQLite/WAL models consumed by approvals, recovery,
compression, and watchdog features.

## Scope and contract

- Add versioned migrations for `session_state`, `task_state`, `approval_state`,
  and `compression_state`.
- Keep this database separate from the existing `hermes_state.py`
  conversation-history database. ARCH-001 does not wire the new database into
  `cli.py`, `gateway/`, or live service entry points.
- Store `SCHEMA_VERSION` and `MIN_COMPATIBLE_SCHEMA_VERSION` in a single-row
  `schema_version` table. Store one append-only row per applied migration in
  `runtime_state_migrations`, including version, description, immutable
  migration checksum, and applied time.
- Use these composite business keys:
  - `session_state(profile_name, session_id)`
  - `task_state(profile_name, task_id)`
  - `approval_state(profile_name, approval_id)`
  - `compression_state(profile_name, session_id)`
- Every CAS predicate must include `profile_name`; an unqualified id is not
  globally unique and must never update another profile's row.
- Cross-table task/session references must carry and validate the same
  `profile_name`. A reference with a matching id but a different profile must
  be rejected.
- Required state identity and operational fields include profile, session,
  user, workspace, task, branch, worktree, target host, deployment target,
  current status, breaker status, compression status, row schema version, and
  created/updated timestamps as applicable to each table.

## Ownership and CAS

Every state row has `owner TEXT` and `owner_version INTEGER` starting at zero.
The shared CAS API provides claim, update, and release operations.

- A successful write atomically checks the composite business key plus the
  expected owner/version, increments `owner_version`, and returns success.
- A lost race returns the current owner/version without retrying. Contention
  retry behavior belongs to `HERMES-ARCH-004`.
- Writable columns are explicit and exclude ids, owner/version, schema
  version, and creation time.
- A claim against another owner's row fails closed by default. Silent takeover
  is forbidden; any future takeover requires a separately audited contract.

## Migration and WAL safety

- Check schema compatibility before any persistent mutation, including a
  journal-mode change. A newer-than-supported or older-than-minimum database
  fails closed with an actionable error and unchanged file contents.
- Fresh install and every registered migration must apply DDL and version
  metadata atomically. Do not rely on `executescript` behavior that implicitly
  commits an open transaction.
- Migrations are ordered, additive functions. Migration versions are unique;
  changing a previously applied migration checksum or editing migration
  history fails closed.
- Request SQLite WAL mode and apply the busy-timeout hook during preflight. If
  WAL cannot be enabled, return an explicit degraded/preflight failure rather
  than claiming WAL guarantees under another journal mode.
- Expose schema constants, database open/migration, schema errors, CAS
  results, and retry-configuration hooks to downstream consumers. Retry loops
  remain outside ARCH-001.

## Non-goals

- Approval policy logic.
- Compression algorithms.
- Watchdog behavior.
- Automatic database repair.
- Wiring into live entry points or deployment.

## Acceptance criteria

- Fresh creation is deterministic and idempotent.
- Upgrade preserves session, task, approval, and breaker state.
- Incompatible schema fails before writes, with database bytes unchanged.
- Migration failure does not leave partial DDL or falsely advanced metadata.
- Concurrent CAS tests cover two profiles reusing the same ids and show no
  cross-profile writes, duplicate owners, or lost updates without implementing
  ARCH-004 retry behavior.
- Cross-table profile mismatch is rejected, and non-owner claims cannot steal
  rows.
- Migration ordering, duplicate versions, checksum tampering, and release
  manifest consistency fail closed.
- WAL preflight either confirms WAL or returns an explicit degraded/error
  result.
- Downstream tickets can declare and verify the required schema version.

## Required tests and evidence

- Fresh install, upgrade, and idempotent re-run tests.
- Migration atomicity and rollback-on-copy test.
- Same-id-across-profile CAS and concurrent WAL writer tests.
- Incompatible newer/older schema test comparing database bytes before/after.
- WAL preflight success/failure and retry-hook-only boundary tests.
- Cross-profile foreign-reference and non-owner claim-denial tests.
- Migration ordering, duplicate-version, checksum-tamper, and release-manifest
  tests.
- Rollback procedure exercised against a copied state database only.

## Rollout and rollback

Run migration in preflight before gateway activation and keep a timestamped
database backup. If migration fails, do not activate the release; restore the
previous release without deleting the database. Tests must never overwrite or
delete the original DGX database.

## Definition of done

- Migration files, schema contract, and tests are committed.
- Schema constants appear in the release manifest and downstream documentation.
- Composite profile-scoped CAS behavior is documented and tested.
- Rollback is tested against a copied DGX state database.
