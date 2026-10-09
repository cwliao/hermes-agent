---
Status: rebase complete, main pushed (f0e115b95f); deploy NOT executed
Priority: high
Date: 2026-10-09
Repository: hermes-agent
Related: upstream-rebase-012 (2026-10-08), hermes_upstream_apply.py approval gate
---

# Upstream rebase 013

Worktree `~/.hermes/worktrees/upstream-rebase-013-20261009`, branch
`upstream-rebase-013-20261009`, rebased from `main` (`d168a08b3c`) onto
`upstream/main` (`3b0dc776b6`).

- Upstream ahead at start: 258 commits; fork-specific commits replayed: 480 (all kept, none dropped).
- `main` reset to the rebased tip (`5a013b9f6a` before this note), tagged
  `backup/pre-upstream-rebase-20261009` for rollback.

## Conflicts

| Commit | File | Resolution | Test result |
|---|---|---|---|
| step 27 (OCR helpers) | `gateway/run_inbound.py` | Kept OCR helpers and `ocr_translate` kwarg; `_adapter_for_source` renamed to upstream's `_delivery_adapter_for` (real bug, found by `test_unknown_command.py`). | pass |
| step 55 | `hermes_cli/plugins.py`, `plugins/platforms/telegram/adapter.py` | Plugin callback branch now gated by upstream's `_callback_authorized`. `test_telegram_plugin_callbacks.py` adapted (auth stub, patch targets, gmail home/env). | 18 passed |
| approval gateway wait | `tools/approval_gateway_wait.py` | Kept both sides' `__slots__` (`runtime_lease`, `runtime_profile`). | pass |
| cron | `cron/scheduler.py` | Imports from upstream's `cron/scheduler_script.py`. | pass |
| MCP result rendering | `tools/mcp_tool_handlers.py` | `_format_klib_mcp_result` + `_render_call_tool_result(..., image_paths=)`. | pass |
| FTS | `hermes_state.py`, `tests/hermes_state/test_fts_runtime_rebuild.py` | Dropped the commit's duplicate `_is_fts_write_corruption_error` (upstream `hermes_state_fts.py` owns it); reverted 4 test hunks that contradicted upstream semantics. | pass |
| skills_guard | `tools/skills_guard.py` | Kept the tokenizer `dns_exfil` detector; added `_INTERPOLATED_DOMAIN` + literal-mode `host` rule so `host-bridge.js` prose is not flagged. Later replay (`9486da7a96`): kept HEAD's `_BACKTICK_LITERAL` form, dropped the older duplicate `_dns_command_tokens_flag`. | `test_skills_guard.py`/`test_plugin_guard.py` pass except env errors below |
| `53039338c9` | `agent/context_compressor.py` (+ kanban files merged textually) | Kept `_compression_output_budget` with lowercase generics. Kanban tests repointed to the split modules (`kanban_db_connect.connect`, `kanban_db_dispatch._resolve_worker_cli_toolsets`). | compression pass; 2 guard artifacts |
| `518d8528e5` | `gateway/run_turn.py` | Fixup restoring `event_metadata` threading: kept lowercase generics, added `event_metadata` param to the 3 helpers. `_thread_metadata_for_event_data` is defined once in `gateway/run.py`. | 27 passed |
| `1c78853c60` | `hermes_cli/goals.py` | Kept both: `gates: list[GoalGate]` plus new `status_notice_*` fields. | 67 passed |
| `c53f49bb69` | `gateway/run_inbound.py` | Imports: kept `base64` and `concurrent.futures`. | see residual gaps |
| `4f51abcc4f` | `agent/auxiliary_client.py` | `_select_pool_entry(..., *, model=)` and `_resolve_codex_credential_and_base(model=)` with lowercase `tuple`. | pass |

Remaining commits applied without conflicts.

## Post-rebase verification

- Conflict-marker grep: one hit, a fixture string in `tests/test_audit_old_updater_imports.py` (false positive).
- Full-tree `ast.parse`: 0 errors; `pyproject.toml` parses.
- Scoped tests on all conflict files, `ulimit -v 4194304`: 617 passed, 6 failed, 7 errors.
  - 3 `test_image_input_routing_runtime.py::test_expired_image_choice_*`: identical failure on untouched pre-rebase `main` (`_handle_pending_image_ocr_choice` returns None when nothing is pending; test expects `""`).
  - 1 `test_auxiliary_client.py::...test_anthropic_messages_profile_resolves_to_messages_adapter`: identical on untouched main.
  - 2 `test_kanban_worker_spawn_toolsets.py`, 7 `test_plugin_guard.py::TestInstallIntegration`: environment artifacts (see below).

## Test environment notes

- Tests need Python >= 3.13: use a 3.14 venv python (`~/.hermes/venvs/gateway-*/bin/python3`) plus a pytest shim on `PYTHONPATH`.
- `tests/home_io_guard.py` trips inside a worktree under `~/.hermes` (first import of `gateway.run`, `pm/environments.py` manifest reads, `.git/worktrees/...` paths). Running `tests/gateway/test_telegram_plugin_callbacks.py` first in the same pytest invocation works around the first-import trip. The remaining trips are artifacts; they pass or are unrelated on the main checkout.
- `TestInstallIntegration` "worker contract requires real uv": `uv` not on PATH here.

## Known residual gaps

- Pre-existing, unfixed: `_handle_pending_image_ocr_choice` expired-choice reply; Anthropic aux profile test. Fix as separate commits.
- `uv lock` not re-run; verify `uv.lock` before building the release.

## Operator hand-off

1. Push: DONE (force-with-lease from d168a08b3c).
2. Deploy: `scripts/hermes_upstream_apply.py` needs the candidate JSON to carry `status: APPROVED` and a non-empty `approved_by`; set by the operator, not by the agent. Live drop-in today: `zzzzzzzzzzzzzzzzzzzz-upstream-d168a08b3c.conf`; pass its release and drop-in as `--previous-release` / `--previous-dropin` for rollback.
3. Cleanup (superseded rebase worktrees, once main contains their tips): `git worktree remove --force` for `upstream-rebase-012-20261008`, `upstream-rebase-013-20261009`, `rebase009-20261004`, `rebase010-20261005`, `upstream-rebase-007-20261001`, `upstream-rebase-008-20261003`, then `git worktree prune -v`. `upstream-rebase-009-20261006` is not in main: leave it.
