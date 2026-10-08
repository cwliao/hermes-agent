---
Status: rebase complete, not yet pushed/deployed
Priority: high
Date: 2026-10-08
Repository: hermes-agent
Related: upstream-rebase-011 (2026-10-07, never pushed/deployed), agentmemory provider restore
---

# Upstream rebase 012

Worktree `~/.hermes/worktrees/upstream-rebase-012-20261008`, branch
`upstream-rebase-012-20261008`, rebased from `main` (`d6884ee664`) onto
`upstream/main` (`08165d5893`).

- Upstream ahead at start: 625 commits; fork-specific commits replayed: 479
- 478 commits in the result: `fix: stop routing through run_agent.handle_function_call
  compat shim` (second copy) became empty and was dropped; its effect
  (`model_tools.handle_function_call` in `agent/tool_executor.py`) is present.
- One commit added on top: `feat(memory): add agentmemory as external memory provider`
  (cherry-pick of `eaa936d44d`; the fork plugin had only ever lived on the side branch
  `feature/agentmemory-provider-v2026.8.3`, so upstream's memory-provider migration check
  warned "configured but not installed" for `memory.provider: agentmemory`).
- `main` reset to the rebased tip (`c06da0c1f1`), tagged
  `backup/pre-upstream-rebase-20261008` for rollback.

## Conflicts

| Commit | File | Resolution | Test result |
|---|---|---|---|
| `a94ad9d071` | `tools/mcp_tool_handlers.py` | Kept HEAD's 3-tuple `_render_content_blocks` + `image_paths`; added klib formatting params. | `test_mcp_tool.py` 117 passed |
| `1f378f2fb6` | `tools/mcp_tool_handlers.py` | Same pattern; added read_page/list_index to the klib tool_name set. | 18 failed: pre-existing (commit never added `import json` to `tools/mcp_tool.py`), identical on untouched main |
| `60ac57e3f4` | `agent/agent_runtime_helpers.py` | HEAD already refactored the restore into `route_binding.reinstall_primary_runtime`; kept HEAD. Also fixed a test importing `TurnRunner` from `gateway.run` (shim removed upstream). | scoped pass |
| `90826a938d`, `7e05aa8a0b` | `plugins/kanban/dashboard/plugin_api.py`, `agent/tool_executor.py`, test | Commit reverted upstream's `_since_param`/`_latest` cursor logic and re-added the removed `run_agent.handle_function_call` alias; kept HEAD, repointed test patches to `model_tools.handle_function_call`. | 26 passed |
| `9411300da5` | `plugins/kanban/dashboard/plugin_api.py` | Only restores cursor logic HEAD already has; kept HEAD. | 35 passed, 2 failed in `test_gateway.py` (identical on main, real-home guard) |

Remaining commits applied without conflicts.

## Post-rebase verification

- Conflict-marker grep: one hit, a test fixture string in `tests/test_audit_old_updater_imports.py` (false positive).
- Full-tree `ast.parse` sweep: 0 errors; `pyproject.toml` parses.
- Scoped tests on every file touched by a conflict (`ulimit -v 4194304`): 194 passed, 22 failed, 10 skipped. The same 22 fail identically on untouched pre-rebase `main` (20 `test_mcp_tool.py` klib handlers from the missing `import json`; 2 `test_gateway.py`).
- agentmemory: `load_memory_provider("agentmemory")` instantiates, no abstract methods, `is_available()` true. Memory-provider test files: all pass except 4 in `test_pre_compress_checkpoint_contract.py` (identical on main) and 2 in `test_memory_migration_notifications.py` that fail only inside a git worktree (home I/O guard; pass on main checkout, fail with the plugin removed too).
- `uv lock` was NOT re-run (`uv` not on PATH in this shell); verify `uv.lock` before building the release.

## Known residual gaps

- `tools/mcp_tool.py` lacks `import json` (20 klib handler tests fail). Pre-existing since `1f378f2fb6`; fix as a separate commit.
- Rebase 011 (`backup/pre-upstream-rebase-20261007`) was never pushed or deployed; check what production actually runs before deploying 012.

## Operator hand-off (not executed)

1. Push: `git -C ~/.hermes/hermes-agent push --force-with-lease origin main`
2. Deploy: `scripts/hermes_upstream_apply.py` candidate with operator-supplied approval token; pass `--previous-release`/`--previous-dropin` of the live release for rollback.
3. Cleanup after merge: `git worktree remove --force ~/.hermes/worktrees/upstream-rebase-012-20261008 && git worktree prune -v`
