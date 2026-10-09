---
Status: rebase complete, main moved locally; push NOT executed; deploy NOT executed
Priority: high
Date: 2026-10-10
Repository: hermes-agent
Related: 2026-10-09-upstream-rebase-014.md, hermes_upstream_apply.py approval gate
---

# Upstream rebase 015

Worktree `~/.hermes/worktrees/upstream-rebase-015-20261010`, branch
`upstream-rebase-015-20261010`, rebased from `main` (`360cff06a2`) onto
`upstream/main` (`46d7718a52`).

- Upstream ahead at start: 174 commits; fork-specific commits to replay: 483.
- Replayed 482; one dropped deliberately (see conflict 4). `main` reset to the
  rebased tip, tagged `backup/pre-upstream-rebase-20261010-015` for rollback.

## Conflicts

| Step | Commit | File | Resolution | Test result |
|---|---|---|---|---|
| early | google workspace | `skills/productivity/google-workspace/scripts/google_api.py` | Kept both sides; `calendar_list` validates the calendar id then uses `datetime.now(UTC)`. | 11 passed |
| ~280 | `c3f89cabfd` fail closed on kanban routing | `gateway/run_turn.py` | `_proxy_stream_consumer(..., message: Any = None)` with HEAD's unquoted `SessionSource`. | guard + transactional tests pass; see note 1 |
| 352 | `3ee8696f63` restore event_metadata/message threading | `gateway/run_turn.py` | Took the commit's `message: str = ""` with unquoted `SessionSource`; `_is_kanban_transactional_turn` accepts str. | 91 passed, 2 skipped |
| 416 | `8dcfae65b9` promote reasoning to content (vLLM step3p5) | `agent/chat_completion_helpers.py` | **Dropped (`rebase --skip`).** Its diff only deleted upstream helpers (`_bound_openai_codex_stale_timeout`, `cap_to_run_budget`, `_stream_env_stale_base`) that `run_agent.py` still imports; it contains no step3p5 logic. The step3p5 fix lives in `conversation_loop.py`, `turn_response_intake.py` and `chat_completion_helpers.py` and was already on HEAD with its test. | step3p5 test passes |

Remaining commits applied without conflicts.

## Post-rebase verification

- Conflict-marker grep: one hit, a fixture string in `tests/test_audit_old_updater_imports.py` (false positive, same as 013).
- Full-tree `ast.parse`: 0 errors.
- Scoped tests on all conflict files plus step3p5, stale-timeout, kanban, run-progress and proxy-mode suites, `ulimit -v 4194304`: 287 passed, 2 skipped, 1 failed.
  - `tests/tools/test_kanban_tools.py::test_swarm_allowed_on_different_board` fails identically on the untouched pre-rebase `main` (board `board-alpha` does not exist).
- Note 1: while at conflict 2, 17 `test_kanban_tools.py` tests failed with `kanban_db` has no attribute `connect` and assignee-unavailable errors. This is the known self-healing gap: fork commit `8178b55c39` (repoint tests to the split kanban_db modules) sits later in the sequence. All of them pass at the final tip except the one above.

## Test environment notes

- Python >= 3.13 needed: `~/.hermes/venvs/gateway-360cff06a2/bin/python` plus a pytest shim on `PYTHONPATH`.
- The shim must contain real copies of pytest, `_pytest`, pluggy, iniconfig, packaging, `py.py`, pytest_asyncio **and their `*.dist-info` directories** (the asyncio plugin loads through entry-point metadata). Symlinks into `~/.hermes/hermes-agent/.venv` make `tests/home_io_guard.py` refuse the path as the real hermes home.
- Run `tests/gateway/test_telegram_plugin_callbacks.py` first in the same invocation to avoid the first-import guard trip inside a worktree.
- `bash ~/hermes_rebase.sh verify` uses the 3.11 `.venv` and so cannot run the tests; run pytest by hand as above.

## Known residual gaps

- Pre-existing, unfixed: `test_swarm_allowed_on_different_board`; the 013 items (`test_expired_image_choice_*`, Anthropic aux profile test) were not re-run.
- `uv lock` not re-run; verify `uv.lock` before building the release.
- Commit `8dcfae65b9` is gone from the fork history. If its intent (step3p5 reasoning promotion when `enable_thinking=false`) ever regresses, check the three files named above, not the dropped commit.

## Operator hand-off

1. Push the rewritten main history, when approved:
   `git push --force-with-lease origin main`
2. Deploy only through the approval-gated pipeline. Candidate JSON
   `~/.hermes/hermes-upstream-state/candidates/20261010-rebase015.json` is prepared with the approval fields left null; the operator sets `status: APPROVED` and `approval.approved_by`.

   ```bash
   cd ~/.hermes/hermes-agent
   python3 scripts/hermes_upstream_apply.py \
     --repo ~/.hermes/hermes-agent \
     --state-dir ~/.hermes/hermes-upstream-state \
     --run-id 20261010-rebase015 \
     --previous-release ~/.hermes/releases/hermes-upstream-20261009-rebase014 \
     --previous-dropin ~/.config/systemd/user/hermes-gateway.service.d/zzzzzzzzzzzzzzzzzzzz-upstream-360cff06a2.conf
   # dry run above; append --execute to deploy
   ```

3. Cleanup (once main contains the tip): `git worktree remove --force ~/.hermes/worktrees/upstream-rebase-015-20261010`, then `git worktree prune -v`.
4. Before release/build:

   ```bash
   cd ~/.hermes/hermes-agent
   uv lock --check
   git diff --check
   ```
