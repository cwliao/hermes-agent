---
Status: rebase complete, main moved locally; push NOT executed; deploy NOT executed
Priority: high
Date: 2026-10-11
Repository: hermes-agent
Related: 2026-10-10-upstream-rebase-015.md, 2026-10-10-plugin-install-history.md, hermes_upstream_apply.py approval gate
---

# Upstream rebase 016

Worktree `~/.hermes/worktrees/upstream-rebase-016-20261011`, branch
`upstream-rebase-016-20261011`, rebased from `main` (`9530111d8f`) onto
`upstream/main`.

- Upstream ahead at start: 183 commits; fork-specific commits to replay: 484.
- All 484 replayed, none dropped. `main` reset to the rebased tip `f7c07fd66b`,
  tagged `backup/pre-upstream-rebase-20261011-016` for rollback.

## Conflicts

| Step | Commit | File | Resolution | Test result |
|---|---|---|---|---|
| 381 | `b9b3bdae8b` swap Tesseract for PaddleOCR | `pyproject.toml` | Kept HEAD's `nemo-relay>=0.10,<0.11` (upstream bump) and the commit's `paddlepaddle==3.2.2` / `paddleocr>=2.10.0` entries; closed the list after them. `uv.lock` auto-merged; `uv lock --check` resolves. | `test_image_input_routing_runtime.py` 50 passed, 3 pre-existing failures |

Remaining commits applied without conflicts.

## Post-rebase verification

- Conflict-marker grep: two hits, both reST `=======` underlines in docstrings/fixtures
  (`tests/test_audit_old_updater_imports.py`, `tests/tools/test_mcp_oauth_metadata.py`).
- Full-tree `ast.parse`: 0 errors.
- Capped (`ulimit -v 4194304`) run of telegram callbacks, image routing, kanban tools,
  plugin install history and plugin update recovery tests: 183 passed, 4 failed.
  All 4 fail identically on the untouched pre-rebase `main`:
  - `test_image_input_routing_runtime.py::test_expired_image_choice_replies_instead_of_falling_through[1/2/3]`
    (`_handle_pending_image_ocr_choice` returns None, test expects `""`; same as 009/013).
  - `tests/tools/test_kanban_tools.py::test_swarm_allowed_on_different_board`.
- `git diff --check upstream/main..HEAD` only reports trailing whitespace in the fork's `docs/HANDOVER.md`.

## Known residual gaps

- The four failures above remain unfixed.
- Only the touched/related suites were run, not the full tree.
- The Paddle pins in `pyproject.toml` are unchanged by this rebase; `uv lock` was only
  checked (not regenerated), see `pm/AGENTS.md` before a release build.

## Operator hand-off

1. Push the rewritten main history, when approved:
   `git push --force-with-lease origin main`
2. Deploy only through the approval-gated pipeline; the operator creates and approves the
   candidate (`status: APPROVED`, `approval.approved_by`), then:

   ```bash
   cd ~/.hermes/hermes-agent
   python3 scripts/hermes_upstream_apply.py \
     --repo ~/.hermes/hermes-agent \
     --state-dir ~/.hermes/hermes-upstream-state \
     --run-id <run-id> \
     --previous-release ~/.hermes/releases/hermes-upstream-20261010-plugin-history \
     --previous-dropin ~/.config/systemd/user/hermes-gateway.service.d/zzzzzzzzzzzzzzzzzzzz-upstream-9530111d8f.conf
   # dry run above; append --execute to deploy
   ```

3. Cleanup (once main contains the tip):
   `git worktree remove --force ~/.hermes/worktrees/upstream-rebase-016-20261011`, then `git worktree prune -v`.
