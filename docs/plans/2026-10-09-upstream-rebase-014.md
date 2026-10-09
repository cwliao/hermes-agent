---
Status: rebase complete, main moved locally; push NOT executed; deploy NOT executed
Priority: high
Date: 2026-10-09
Repository: hermes-agent
Related: 2026-10-09-upstream-rebase-013.md, hermes_upstream_apply.py approval gate
---

# Upstream rebase 014

The 014 worktree already existed before this session and its reflog records a
clean rebase completed at 2026-10-09 13:01:51 +0800:

- Worktree: `~/.hermes/worktrees/upstream-rebase-014-20261009`
- Branch: `upstream-rebase-014-20261009`
- Starting main: `de7b747bba89cb9a16ce2e7908d638a4de436cba`
- Upstream at rebase: `1744a19e0df568c647e4f3ff9c37f2a284a282fb`
- Upstream ahead from starting main: 4 commits
- Fork-specific commits replayed: 482
- Rebased tip: `770af3c9d3fa346e31e954cfbe963c2eeaa342ba`

This session did not re-run `start 014` because the helper correctly rejected
the already-existing branch. The existing result was verified and then finished
after recovering the start checkpoint from the verified branch-creation reflog.
No service was restarted, no deployment was performed, and no worktree was
deleted.

## Conflicts

| Commit | File | Resolution | Test result |
|---|---|---|---|
| None in this session | - | The existing 014 rebase completed cleanly; no conflict was handled or changed here. | Not applicable |

## Post-rebase verification

- `bash ~/hermes_rebase.sh verify 014`: 0 syntax errors.
- Conflict-marker grep: one fixture hit in
  `tests/test_audit_old_updater_imports.py`, intentionally containing
  `<<<<<<< HEAD` / `=======` / `>>>>>>> incoming`; not a live marker.
- Rebase count: `upstream/main...HEAD = 0 482`.
- Worktree status: clean.
- Scoped pytest: 0 files run because this session handled no conflicts.
  Python 3.13+ was not installed on the host; the available Hermes venv is
  Python 3.11.15, so no test was falsely reported as compliant.
- `uv` is not available; `uv.lock` was not re-validated.

## Known residual gaps

- Install or provide a Python 3.13+ environment before running any affected
  scoped tests. The known `tests/home_io_guard.py` worktree artifact remains
  an environment issue.
- `uv.lock` still needs an operator-side `uv lock --check` validation.
- Live gateway remains on the previous release:
  `~/.hermes/releases/hermes-upstream-20261008-rebase012`
  with drop-in
  `~/.config/systemd/user/hermes-gateway.service.d/zzzzzzzzzzzzzzzzzzzz-upstream-d168a08b3c.conf`.

## Operator hand-off

1. Push the rewritten main history, when explicitly approved:
   `git push --force-with-lease origin main`
2. Deploy only through the approval-gated pipeline, using the actual approved
   candidate run id and operator-supplied approval token:

   ```bash
   cd ~/.hermes/hermes-agent
   python3 scripts/hermes_upstream_apply.py \
     --repo ~/.hermes/hermes-agent \
     --state-dir ~/.hermes/hermes-upstream-state \
     --run-id <approved-candidate-run-id> \
     --previous-release ~/.hermes/releases/hermes-upstream-20261008-rebase012 \
     --previous-dropin ~/.config/systemd/user/hermes-gateway.service.d/zzzzzzzzzzzzzzzzzzzz-upstream-d168a08b3c.conf \
     --execute
   ```

   Do not fabricate approval fields or expose the approval token.
3. Worktree cleanup was not executed. The only currently proven
   superseded worktree is:

   ```bash
   cd ~/.hermes/hermes-agent
   git worktree remove --force ~/.hermes/worktrees/upstream-rebase-014-20261009
   git worktree prune -v
   ```

   Older rebase worktrees were not ancestors of the new main tip and remain
   untouched.
4. Before release/build, verify the lockfile:

   ```bash
   cd ~/.hermes/hermes-agent
   uv lock --check
   git diff --check
   git diff --exit-code -- uv.lock
   ```
