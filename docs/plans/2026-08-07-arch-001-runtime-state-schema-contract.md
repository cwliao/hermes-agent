# HERMES-ARCH-001: Versioned shared runtime state schema

Status: review consensus reached; implementation not included
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
- Use the following canonical DDL names, types, constraints, relationships, and
  lookup indexes; implementation may add indexes but must not change this
  contract without a new ticket revision:

  ```sql
  CREATE TABLE schema_version (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      schema_version INTEGER NOT NULL,
      min_compatible_schema_version INTEGER NOT NULL,
      updated_at TEXT NOT NULL
  );

  CREATE TABLE runtime_state_migrations (
      version INTEGER PRIMARY KEY,
      description TEXT NOT NULL,
      checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
      applied_at TEXT NOT NULL
  );

  CREATE TABLE session_state (
      profile_name TEXT NOT NULL,
      session_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      workspace TEXT NOT NULL,
      target_host TEXT,
      deployment_target TEXT,
      status TEXT NOT NULL CHECK (status IN
          ('active', 'completed', 'failed', 'degraded', 'cancelled')),
      owner TEXT,
      owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
      schema_version INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (profile_name, session_id)
  );

  CREATE TABLE task_state (
      profile_name TEXT NOT NULL,
      task_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      branch TEXT,
      worktree TEXT,
      target_host TEXT,
      deployment_target TEXT,
      status TEXT NOT NULL CHECK (status IN
          ('pending', 'running', 'succeeded', 'failed', 'blocked', 'cancelled', 'degraded')),
      owner TEXT,
      owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
      schema_version INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (profile_name, task_id),
      UNIQUE (profile_name, task_id, session_id),
      FOREIGN KEY (profile_name, session_id)
          REFERENCES session_state(profile_name, session_id)
          ON DELETE RESTRICT
  );

  CREATE TABLE approval_state (
      profile_name TEXT NOT NULL,
      approval_id TEXT NOT NULL,
      session_id TEXT,
      task_id TEXT,
      approval_status TEXT NOT NULL CHECK (approval_status IN
          ('pending', 'approved', 'denied', 'expired', 'reset_pending')),
      breaker_status TEXT NOT NULL CHECK (breaker_status IN
          ('closed', 'open', 'reset_pending')),
      owner TEXT,
      owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
      schema_version INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (profile_name, approval_id),
      CHECK (session_id IS NOT NULL OR task_id IS NOT NULL),
      FOREIGN KEY (profile_name, session_id)
          REFERENCES session_state(profile_name, session_id)
          ON DELETE RESTRICT,
      FOREIGN KEY (profile_name, task_id)
          REFERENCES task_state(profile_name, task_id)
          ON DELETE RESTRICT,
      FOREIGN KEY (profile_name, task_id, session_id)
          REFERENCES task_state(profile_name, task_id, session_id)
          ON DELETE RESTRICT
  );

  CREATE TABLE compression_state (
      profile_name TEXT NOT NULL,
      session_id TEXT NOT NULL,
      task_id TEXT,
      compression_status TEXT NOT NULL CHECK (compression_status IN
          ('idle', 'running', 'succeeded', 'failed', 'degraded', 'disabled')),
      owner TEXT,
      owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
      schema_version INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (profile_name, session_id),
      FOREIGN KEY (profile_name, session_id)
          REFERENCES session_state(profile_name, session_id)
          ON DELETE RESTRICT,
      FOREIGN KEY (profile_name, task_id)
          REFERENCES task_state(profile_name, task_id)
          ON DELETE RESTRICT,
      FOREIGN KEY (profile_name, task_id, session_id)
          REFERENCES task_state(profile_name, task_id, session_id)
          ON DELETE RESTRICT
  );

  CREATE INDEX task_state_session_idx
      ON task_state(profile_name, session_id);
  CREATE INDEX approval_state_session_idx
      ON approval_state(profile_name, session_id);
  CREATE INDEX approval_state_task_idx
      ON approval_state(profile_name, task_id);
  CREATE INDEX compression_state_task_idx
      ON compression_state(profile_name, task_id);
  CREATE INDEX session_state_schema_version_idx
      ON session_state(schema_version);
  CREATE INDEX task_state_schema_version_idx
      ON task_state(schema_version);
  CREATE INDEX approval_state_schema_version_idx
      ON approval_state(schema_version);
  CREATE INDEX compression_state_schema_version_idx
      ON compression_state(schema_version);
  ```
- Migration 1 seeds the singleton/version history in the same transaction as
  the DDL; a fresh database must not pass preflight with empty metadata:

  ```sql
  INSERT INTO schema_version
      (id, schema_version, min_compatible_schema_version, updated_at)
  VALUES (1, 1, 1, 'YYYY-MM-DDTHH:MM:SS.sssZ');

  INSERT INTO runtime_state_migrations
      (version, description, checksum_sha256, applied_at)
  VALUES (1, 'Initial runtime state schema', '<sha256_hex>',
          'YYYY-MM-DDTHH:MM:SS.sssZ');
  ```

  The timestamp and checksum placeholders above are bound values in the
  migration implementation. The checksum is computed over the canonical
  Migration 1 body (DDL plus seed SQL), encoded as UTF-8 after LF line-ending
  and placeholder normalization, before the metadata row is inserted.
- Enable `PRAGMA foreign_keys = ON` for every connection before any state
  mutation. Composite foreign keys and API validation together enforce the
  same-profile reference rule.
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
- Every row `schema_version` is stamped to the global schema version at insert
  and CAS update. Opening the database fails closed if any row has a version
  outside `[MIN_COMPATIBLE_SCHEMA_VERSION, SCHEMA_VERSION]`; consumers may
  treat an older but compatible row as requiring migration normalization before
  update.
- When an approval or compression row supplies both `session_id` and `task_id`,
  the API must verify that the referenced task belongs to that same session;
  inconsistent pairs are rejected.
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
  version, creation time, and `updated_at`; the CAS API stamps `updated_at`.
- A claim against another owner's row fails closed by default. Silent takeover
  is forbidden; any future takeover requires a separately audited contract.
- Define typed CAS outcomes and errors: `Success`, `StaleVersion`,
  `OwnerMismatch`, `NotFound`, and `InvalidProfileReference`. The public API
  must return the current owner/version for `StaleVersion` without retrying.
- CAS predicates use SQLite's NULL-safe `IS` operator for owner comparison:
  `owner IS :expected_owner`, together with the profile/id and expected
  `owner_version` predicates. This permits an explicit claim of an unowned
  row without treating `NULL = NULL` as false.
- `release` requires the current owner and expected version, atomically sets
  `owner = NULL`, increments `owner_version`, stamps `updated_at`, stamps the
  current global `schema_version`, and returns the new owner version. It does
  not change status or silently release another owner's row.
- All runtime writers must use the shared CAS API. Direct DML outside the
  migration/preflight implementation is forbidden, so row-version, ownership,
  timestamp, and cross-reference invariants have one enforcement path.

## Migration and WAL safety

- Check schema compatibility before any persistent mutation, including a
  journal-mode change. A newer-than-supported or older-than-minimum database
  fails closed with an actionable error and unchanged file contents.
- For an existing database, perform the initial compatibility inspection using
  a SQLite URI opened with `mode=ro` before opening a read-write connection.
  Fresh-install detection may use the absence of the database file, but must
  still run the normal preflight before DDL.
- Fresh install and every registered migration must apply DDL and version
  metadata atomically. Do not rely on `executescript` behavior that implicitly
  commits an open transaction.
- Run each migration inside an explicit `BEGIN IMMEDIATE` or `BEGIN EXCLUSIVE`
  transaction, with explicit statement execution and rollback on failure.
- Migrations are ordered, additive functions. Migration versions are unique;
  changing a previously applied migration checksum or editing migration
  history fails closed. A checksum is the lowercase SHA-256 hexadecimal digest
  of the migration's canonical body (DDL plus its metadata/seed SQL), encoded
  as UTF-8 after normalizing line endings to LF (`\\n`) and replacing declared
  runtime placeholders with their canonical tokens. This is the same
  normalization used for Migration 1 and all later migrations.
- Request SQLite WAL mode and apply the busy-timeout hook during preflight. If
  WAL cannot be enabled, return an explicit degraded/preflight failure rather
  than claiming WAL guarantees under another journal mode.
- The busy-timeout hook defaults to `5000` milliseconds and accepts only a
  configured value in the inclusive range `1..10000` milliseconds. ARCH-001
  sets the connection timeout but does not implement contention retries;
  retry policy remains ARCH-004 scope.
- Encode every timestamp as an ISO-8601 UTC string with millisecond precision:
  `YYYY-MM-DDTHH:MM:SS.sssZ`.
- Foreign-key deletes use `ON DELETE RESTRICT`; runtime code does not delete
  state rows. Completed or retired state is represented by an allowed status,
  and any future archival/deletion contract requires a separate ticket.
- `compression_state` is intentionally session-scoped: its primary key permits
  one row per `(profile_name, session_id)`, while `task_id` is optional context
  for the active compression operation, not a second identity. Per-task
  compression history requires a future schema ticket.
- The simple task foreign key is intentionally retained alongside the stronger
  `(profile_name, task_id, session_id)` foreign key: it validates task-only
  references when no session id is supplied, while the composite key validates
  both-reference consistency.
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
- Canonical DDL, `PRAGMA foreign_keys`, read-only preflight, typed CAS error,
  timestamp-format, and explicit migration-transaction tests.
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
