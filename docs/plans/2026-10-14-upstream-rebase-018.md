---
Status: rebase complete, main moved locally; push and deploy NOT executed
Priority: high
Date: 2026-10-14
Repository: hermes-agent
Related: 2026-10-12-upstream-rebase-017.md, hermes_upstream_apply.py
---

# Upstream rebase 018

Worktree `~/.hermes/worktrees/upstream-rebase-018-20261013`, branch
`upstream-rebase-018-20261013`, rebased from `main` (`664f154ec9`) onto upstream `63d0b94120`
(2026-10-12). 485 fork commits replayed. `main` reset to the rebased tip (`b7301a888f`),
rollback tag `backup/pre-upstream-rebase-20261014`.

## Conflicts

| Commit | File | Resolution | Test result |
|---|---|---|---|
| KANBAN-SWARM-002 (`f76e0bee59`) | `hermes_cli/kanban_db_dispatch.py` | Kept per-attempt runtime with `_kb._to_epoch(row["active_started_at"])` (lines ~690, ~841); dropped `int(...)`. | lifecycle 12 passed; kanban dispatch/db/swarm 157 passed, 1 skipped |
| Needle 3 triage pre-filter | `pyproject.toml` | Kept `exclude-newer = "3 days"`; dropped old 14-day per-package exceptions. | tomllib parses |
| uv.lock regen | `uv.lock` | Kept P3D policy; merged `hindsight`/`needle` extras. | tomllib parses |

## Verification

- 0 syntax errors, no real conflict markers (`tests/test_audit_old_updater_imports.py:225` is a fixture string).
- An intermediate-replay report of 18-20 `kanban_db.connect` failures did not reproduce at the final HEAD;
  `kanban_db` never re-exported `connect` on main or upstream.
- Cross-review (claude, agy: no defects; codex flagged the `langfuse` extra removal, which is upstream
  moving langfuse to `plugin-catalog/langfuse.yaml`, not a fork-only dependency; grok empty, groq SSL error).

## Known residual gaps

- Branch is 477 commits behind the current local `upstream/main` (`e06df9c2ea`); the next rebase picks them up.
- Stale review candidate `20261005-203057.json` still blocks the cron preflight guard until superseded.

## Operator hand-off

Push (`git push --force-with-lease origin main`) and deploy through `scripts/hermes_upstream_apply.py`
(previous release `hermes-upstream-20261012-rebase017`, drop-in `zzzzzzzzzzzzzzzzzzzz-upstream-664f154ec9.conf`)
were not executed. Remove worktree `upstream-rebase-018-20261013` after deploy.
