---
Status: rebase complete, main moved locally; deploy NOT executed
Priority: high
Date: 2026-10-12
Repository: hermes-agent
Related: 2026-10-11-upstream-rebase-016.md, hermes_upstream_apply.py approval gate
---

# Upstream rebase 017

Worktree `~/.hermes/worktrees/upstream-rebase-017-20261012`, branch
`upstream-rebase-017-20261012`, rebased from `main` (`233f4a3d93`) onto `upstream/main` (`835c3dd038`).

- Upstream ahead at start: 218 commits; fork-specific commits to replay: 485.
- 484 replayed (the 016 write-up commit is the 485th and now sits on top). `main` reset to the
  rebased tip, tagged `backup/pre-upstream-rebase-20261012-017` for rollback.

## Conflicts

| Step | Commit | File | Resolution | Test result |
|---|---|---|---|---|
| 83 | `aae33c1d28` safe Claude remote recovery | `hermes_cli/main.py` | Kept upstream's `connectors` plus the fork's `claude-recovery` in `_BUILTIN_SUBCOMMANDS`; dropped `_cmd_sessions_lazy` (removed upstream, no remaining references), kept `cmd_claude_recovery`. | `test_claude_recovery.py` 14 passed |
| 140s | `09ac02aa20` harden state repair and DNS exfil guards | `tools/skills_guard.py` | Kept the fork's token-based `_dns_command_uses_variable` (regex entry removed). Upstream's new rule (a plain `dig A ${name}` is not exfil, only an interpolation inside a domain name) was ported into it: `dig`/`nslookup` now need an interpolated domain, with `$(...)` arguments joined across the tokenizer's paren separators (`_dns_argument_text`). | `test_plugin_guard`/`test_skills_guard*` 270 passed |
| later | `5c962036b2` gate Telegram startup notification | `gateway/run_notifications.py` | The fork diff also reverted upstream's #52694 stale-anchor fix and added `message_id=` to the synthetic event. Took upstream's side; only the readiness gating remains fork-specific. | 86 passed |

## Post-rebase verification

- Conflict-marker grep: one hit, a reST underline in `tests/test_audit_old_updater_imports.py`.
- Full-tree `ast.parse`: 0 errors.
- Capped (`ulimit -v 4194304`) run of telegram callbacks, image routing, claude recovery, kanban
  tools, skills/plugin guard, background and restart notifications: 470 passed, 4 failed, 6 errors.
  - 4 failures are the known baseline set (`test_expired_image_choice_replies_instead_of_falling_through[1/2/3]`,
    `test_kanban_tools.py::test_swarm_allowed_on_different_board`).
  - 6 errors are `TestInstallIntegration` fixtures asserting "the worker contract requires real uv";
    identical on untouched pre-rebase `main` in this harness (7 errors there).

## Known residual gaps

- Only the touched/related suites were run, not the full tree.
- In this harness `tests/hermes_state/test_fts_runtime_rebuild.py` and `tests/gateway/test_session.py`
  fail with "file I/O against the REAL hermes home" on `main` too; they need an isolated HERMES_HOME.
- Paddle pins in `pyproject.toml` unchanged; `uv lock` was not regenerated.

## Operator hand-off

1. Push the rewritten main history: `git push --force-with-lease origin main`
2. Deploy only through the approval-gated pipeline (operator creates the APPROVED candidate), previous
   release `~/.hermes/releases/hermes-upstream-20261011-rebase016`, previous drop-in
   `~/.config/systemd/user/hermes-gateway.service.d/zzzzzzzzzzzzzzzzzzzz-upstream-233f4a3d93.conf`.
3. Cleanup: `git worktree remove --force ~/.hermes/worktrees/upstream-rebase-017-20261012`, then `git worktree prune -v`.
