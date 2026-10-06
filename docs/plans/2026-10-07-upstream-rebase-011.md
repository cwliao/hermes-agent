---
Status: rebase complete, not yet pushed/deployed
Priority: high (fork is 1137 commits behind upstream before this round)
Date: 2026-10-07
Repository: hermes-agent
Related: upstream-rebase-009 (2026-10-06), t_e770abce-preflight
---

# Upstream rebase 011

Worktree `/home/cwliao/.hermes/worktrees/upstream-rebase-011-20261007`, branch
`upstream-rebase-011-20261007`, rebased from `main` (`fe32318d42`) onto
`upstream/main` (`59a3866ea5`).

- Upstream ahead at start: 1137 commits (from `2c542f7948`)
- Fork-specific commits replayed: 478
- Result: fully caught up to upstream/main; `main` reset to the rebased tip
  (`6727d25f8c`), tagged `backup/pre-upstream-rebase-20261007` for rollback.

Note: several leftover worktrees from earlier, abandoned rebase attempts were
found (`upstream-rebase-008-20261003`, a stray `rebase009-20261004`, a stray
`rebase010-20261005`) — none were ancestors of `main`, so none were touched or
reused; a fresh branch number (011) was picked instead. Not cleaned up this
round.

## Conflicts

| # | Commit | File | Resolution | Test result |
|---|---|---|---|---|
| 51/478 | `ddfeb2ab32` feat: mandatory web_gate interception + codex/claude CLI Telegram bridge | `hermes_cli/config_defaults.py`, `hermes_cli/plugins.py` | Independent additions at the same `DEFAULT_CONFIG` insertion point: kept HEAD's `"security"` comment variant that the incoming side itself updates (anticipating later tirith-scanning content), plus the incoming side's new `external_cli` and `web_gate` sections. | `tests/hermes_cli/test_plugins.py`, `tests/test_web_gate.py`, `tests/plugins/test_coding_cli.py`: 192/192 passed |
| 55/478 | `b0a9bec8d5` feat(T0085): add Telegram-only plugin callback-keyboard extension | `hermes_cli/plugins_ledger.py` | Independent containers landing in the same `clear()` tuple (HEAD's `_automation_blueprints`, incoming's `_plugin_callback_handlers`) — kept both. Also fixed a pre-existing bug surfaced in `tests/gateway/test_unknown_command.py` (a test referencing `gateway.run.InlineKeyboardMarkup`, which fails even at the commit's original pre-rebase checkout — `gateway.run_inbound.InlineKeyboardMarkup` is the correct current import). | 192/192 passed incl. the fix; 12 pre-existing failures in `tests/gateway/test_telegram_plugin_callbacks.py` confirmed identical at the commit's original checkout (not a rebase regression) |
| 241/478, 266/478 | `8f6355382d`, `6c1cc5abe8` (both "suppress Tirith false-positive...") | `tools/tirith_security.py`, `tests/tools/test_tirith_security.py` | modify/delete: a later, already-replayed upstream commit (`68dd992769`, Teknium, "flag credential uploads and invisible Unicode...") retired the whole tirith scanner, folding its detection into `tools/approval_detection.py`. Took the deletion both times; the unrelated kanban_swarm.py hunks in the first commit applied cleanly and their own new test passed. | `test_swarm_context_names_kanban_comment_and_rules_out_observed_failure_modes`: 1/1 passed |
| 275/478 | `e4a120097f` fix: bound kanban swarm context and validate outputs | `agent/context_compressor.py` | HEAD had already refactored the route-pinning call site into `self._apply_summary_route(...)` (a later fork commit, already replayed, that also fixes a merge-vs-replace bug in the pinned-route logic). Kept that call, and additionally applied this commit's own distinct contribution — wiring `call_kwargs["max_tokens"]` through the newly-added `_compression_output_budget()` (the function itself, and the file-header comment update removing the old "NEVER add max_tokens" note, had already auto-merged cleanly). | 18/18 passed (verified from a copy outside `~/.hermes` — see note below) |
| 384/478 | `dfbf43965e` feat(gateway): add vision-based verification pass for namecard fields | `gateway/run_inbound.py` | Pure import-list conflict (`base64` + `concurrent.futures`); kept both, preserving HEAD's `# noqa: F401` comment on the still-technically-unused `concurrent.futures` import. | 38/41 passed; 3 failures (`test_expired_image_choice_replies_instead_of_falling_through`) confirmed identical at the commit's original checkout |
| 417/478 | `84bdc38ab1` feat(doctor): warn when hermes CLI and gateway daemon run different code (#91) | `hermes_cli/gateway.py` | HEAD's `_spawn_gateway_restart_watcher` signature (`host`/`home` kwargs, `GATEWAY_RESTART_WATCHER_TIMEOUT_S`, `_restart_argv_is_host_gateway`, `_host_gateway_watcher_env`) is a strict superset of the incoming commit's old simple signature — kept HEAD's side entirely. | 37/62 failed in `tests/hermes_cli/test_doctor.py`, confirmed identical (same 37, same names) at the commit's original checkout — unrelated to this hunk |
| 437/478 | `fa93526cfd` fix: restore content lost during upstream-rebase-006 conflict resolution | `hermes_cli/gateway.py` | Same pattern again: HEAD had already gone further (added the `home` kwarg on top of what this fixup commit was restoring). Kept HEAD's side. | 38/38 passed (`test_gateway_restart_watcher_bare_python.py`, `test_kanban_dashboard_plugin.py`) |
| 470/478 | `385ee226d8` build(uv): make uv.lock resolvable again | `uv.lock` | Lockfile conflict — took HEAD's `uv.lock` as-is for the commit (pyproject.toml's own hunk, flipping the paddlepaddle/paddleocr `python_version` marker and excluding android from `environments`, auto-merged cleanly), then regenerated the lock once at the very end of the whole rebase via `uv lock` against the final merged `pyproject.toml`, committed separately (`6727d25f8c`). Resolved in 1.77s; diff matches the commit's own stated intent (paddleocr/opencv stack dropped, markitdown/hindsight/needle extras added). | n/a (lockfile) |

Commits not listed above (469 of 478) applied with **zero conflicts**.

## Post-rebase verification

- `grep` for leftover `<<<<<<<`/`=======`/`>>>>>>>` markers: 2 hits, both confirmed
  false positives (a docstring section-underline in `tests/tools/test_mcp_oauth_metadata.py`,
  a test fixture literally testing conflict-marker detection in
  `tests/test_audit_old_updater_imports.py`).
- Full-tree `ast.parse` syntax sweep: 0 errors.
- `uv lock`: resolves cleanly (1.77s, 337 packages).
- Scoped test run (every file touched by a conflicting commit, run together, capped
  `ulimit -v 4194304`): 424 passed, 25 failed, 8 skipped. Every failure traced to one
  of:
  1. A pre-existing bug/gap confirmed identical at the commit's own original
     (pre-rebase) checkout — not introduced by this rebase.
  2. A host-specific test-isolation artifact: this host's dev checkouts and
     worktrees live under `~/.hermes/` (`~/.hermes/hermes-agent`,
     `~/.hermes/worktrees/...`), so `pm/environments.py::_payload_manifest`'s
     parent-directory probe for a sealed-payload `manifest.json` resolves to a
     real path under the real hermes home, which `tests/home_io_guard.py`
     unconditionally refuses regardless of whether the file exists. Verified by
     running the same failing tests from an `rsync` copy outside `~/.hermes`,
     where they pass cleanly. Not a hermes-agent bug — specific to this
     machine's directory layout.

## Operator hand-off

`main` has been reset to the rebased tip locally (reflog-recoverable;
`backup/pre-upstream-rebase-20261007` tags the pre-rebase state). **Not pushed,
not deployed** — both need the operator:

```bash
cd ~/.hermes/hermes-agent
git push origin main --force-with-lease
```

Deploy goes through `scripts/hermes_upstream_apply.py` (three-stage
preflight → review → apply pipeline); the `--execute` approval-token gate is a
deliberate human-in-the-loop step this session will not fabricate. Point
`--previous-release`/`--previous-dropin` at whatever is currently live so
rollback stays possible.

Leftover worktree cleanup (not done this round, left for the operator to
confirm no one references them first): `upstream-rebase-008-20261003`,
`rebase009-20261004`, `rebase010-20261005` — none are ancestors of the new
`main`.
