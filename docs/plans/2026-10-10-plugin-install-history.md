---
Status: implemented in this worktree; not committed
Priority: medium
Date: 2026-10-10
Repository: hermes-agent
Related: hermes_cli/plugins_cmd_git.py (_write_install_metadata), hermes_cli/plugins_provenance.py
---

# Plugin install history (auto-recorded)

## Goal
Every time a plugin is installed, updated or removed, append one dated entry to a
human-readable history so the operator can answer "when did I install what, and which
catalog pin" without guessing. `.install-metadata.json` has no timestamps today.

## Design
- Hook points: direct CLI metadata changes use `_write_install_metadata()` in
  `hermes_cli/plugins_cmd_git.py`, while normal install/update publication uses
  `pm/publication.py::StagedPlugin.publish`. The latter renders the history rows once, with one
  fixed timestamp, and stores them in the publication journal as `history_rows` alongside the
  metadata transition. `hermes_cli/runtime_state.py::finish_publication()` sets `committed=True`
  and then appends those rows. Committed-journal recovery appends them again, idempotently, so a
  crash between commit and append is repaired; both paths use exact-line dedupe under
  `INSTALL-HISTORY.md.lock` via `pm.filesystem.lock_fd`. An append failure only logs a `WARNING`;
  it never retains or undoes the committed publication. Uncommitted recovery still restores the
  metadata without appending a row. The worker-facing logic lives in `pm/install_history.py`,
  which is stdlib-only and derives `INSTALL-HISTORY.md` from the supplied metadata sidecar path.
  Inside the metadata lock, diff the old on-disk dict against the new dict:
  - key added            -> `install`
  - key removed          -> `remove`
  - `revision` changed   -> `update` (record old and new 7-char SHA)
  - anything else        -> no entry (no noise on unrelated rewrites)
- Sink: `<metadata sidecar parent>/INSTALL-HISTORY.md`, NOT a file in the git checkout. Deriving
  from the sidecar keeps profile homes correct in both the CLI and worker callers.
  Append-only markdown table, one row per event:
  `| 2026-10-10 07:26:12 +08:00 | install | hermes-dreaming | community | e2e4da3 | https://... |`
  Header written once when the file is created. Timestamps are local time with UTC offset.
- Failure isolation: history write is best-effort. Any exception is caught and logged at
  WARNING; it must never fail or roll back the metadata write or the install.
- Profile-local: follows the sidecar path supplied by the caller.
- No secrets: history has its own scrubber. Query data is dropped while a `#fragment` (including a
  monorepo subdirectory) is kept. Userinfo is dropped through the last `@`, even when that
  userinfo contains `/`, `:`, `#` or `?`; scp-style `git@host:path` becomes `host:path`. The
  existing `_scrub_git_url` is intentionally unchanged.
- Concurrency: `_write_install_metadata()` acquires `_install_metadata_lock()` itself. `cmd_adopt`
  and `cmd_trust_update_url` use `_update_install_record()` so they re-read and mutate only their
  own key under the same lock. Adopt uses event `adopt`, not `install`.
- Markdown safety: every cell escapes `|` and converts control characters, including CR/LF, to
  spaces. Missing revisions render as an empty install/update/remove cell; one-sided update
  revisions render `->abcdefg` or `abcdefg->`, and two missing revisions produce no update row.
- Existing runtime `INSTALL-HISTORY.md` rows, if present, predate this feature and are left
  untouched. The worktree contains no manually seeded rows; the schema has no `notes` column.

## Accepted trade-offs
- Dedupe is by exact rendered line, including its timestamp.
- An `@` in a URL path is over-scrubbed because scrubbing uses the last `@` as the userinfo
  boundary.
- Profile cloning does not copy `INSTALL-HISTORY.md`.
- Direct, non-journal writers append history best-effort after the metadata write.

## Tests (tests/hermes_cli/)
1. install of a new key appends one `install` row with name, tier, short SHA.
2. revision change appends one `update` row showing old->new.
3. removal appends one `remove` row.
4. rewriting identical metadata appends nothing.
5. history file unwritable (read-only dir) -> metadata write still succeeds, WARNING logged.
6. URL with embedded credentials, including `ssh://` and `git+ssh://`, is scrubbed in the row.
7. concurrent direct writers keep both records and produce one row each.
8. staged publication records exactly one install/update row after commit; committed-journal
   recovery repairs a crash between commit and append, while rollback records none.
9. cell escaping, absent revision rendering, and `adopt` event semantics.

## Out of scope
Deploying it. The change lands on main; going live needs a new release through
`scripts/hermes_upstream_apply.py` (operator approval).
