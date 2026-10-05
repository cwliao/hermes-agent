---
Status: rebase complete, not yet pushed/deployed
Priority: high (fork is 341 commits behind upstream before this round)
Date: 2026-10-06
Repository: hermes-agent
Related: upstream-rebase-007 (2026-10-01), upstream-rebase-008 (2026-10-03), t_b060bd33
---

# Upstream rebase 009

Worktree `/home/cwliao/.hermes/worktrees/upstream-rebase-009-20261006`, branch
`upstream-rebase-009-20261006`, rebased from `main` (`cf2ce09b80`) onto
`upstream/main` (`e36a818033`).

- Upstream ahead at start: 341 commits
- Fork-specific commits replayed: 477
- Result: `HEAD..upstream/main` = 0 (fully caught up), `upstream/main..HEAD` = 476
  (one commit dropped as a confirmed duplicate, see below)

Note: worktrees `upstream-rebase-007-20261001` and `upstream-rebase-008-20261003`
were found stale/superseded (diverged from current `main`, not an ancestor either
direction — consistent with the 2026-09-26 771-commit full rebase that replaced
`main`'s history after those worktrees were branched). Left in place, not cleaned
up this round; worth pruning once confirmed no one still references them.

## Conflicts

| # | Commit | File | Resolution | Test result |
|---|---|---|---|---|
| 1/477 | `54c2448c7a` docs(agents): add Codex CLI behavioral rules | `AGENTS.md` | Independent additions, kept both (HEAD's more detailed "Long-form background" paragraph + the incoming new Codex CLI Behavioral Rules section). **Self-caught bug**: my first edit left a stray `>>>>>>> 54c2448c7a (...)` marker as literal dangling text at the end of the inserted section — this caused a confusing-looking conflict two commits later; found and cleaned up when resolving conflict #2. | docs-only, no test |
| 2/5 (this file) | `5547b558a0` Document Codex CLI behavior and web_gate safety boundaries | `AGENTS.md` | Both commits are the fork's own sequential edits to the same section. Commit 1 (picked first, chronologically earlier in replay order) already added a detailed, superset version (Autonomy / Stop-and-Ask / Hard Constraints / Startup Context). This commit's content is a shorter, overlapping draft of the same guidance — dropped as redundant rather than appended (appending would have produced duplicate/conflicting Codex guidance in one doc). Also fixed the stray marker from conflict #1 while here. | docs-only, no test |
| 31/477 | `3f693d68b0` fix: use tesseract for telegram image-only ocr | `gateway/run_inbound.py` | Pure insert-conflict on the import block (`shutil`/`subprocess`); HEAD's side was empty, kept the incoming two import lines. Function bodies below applied cleanly. | `tests/gateway/test_image_input_routing_runtime.py`: 5/5 passed |
| 258/477 | `89518f42f8` docs(agents): add Codex CLI behavioral rules section | `AGENTS.md` | **Confirmed exact duplicate** of commit #1's content (same title, verbatim same 69-line section, byte-identical). `git rebase --skip`'d entirely — applying would have duplicated the whole section a second time in the file. | docs-only, no test |

Commits 259–477 (220 commits) applied with **zero further conflicts**.

## Post-rebase verification

- `grep` for leftover `<<<<<<<`/`=======`/`>>>>>>>` markers: 2 hits, both confirmed
  false positives per the skill's own documented pattern — `tests/test_audit_old_updater_imports.py`
  (a test that deliberately writes conflict-marker text as fixture data to test the
  audit tool's own conflict detection) and `tests/tools/test_mcp_oauth_metadata.py`
  (a `=======` reST section-underline inside a docstring). No real leftovers.
- Full-tree `ast.parse` syntax check: 0 syntax errors (a few pre-existing harmless
  regex-escape `SyntaxWarning`s, unrelated to this rebase).
- Re-ran `tests/gateway/test_image_input_routing_runtime.py` (the file touched by
  conflict #3) at the end of the full rebase: **3 new failures** appeared
  (`test_expired_image_choice_replies_instead_of_falling_through[1/2/3]`) that were
  NOT failing right after conflict #3 was resolved (31/477) — i.e., something later
  in the 477-commit chain interacts with this file.

## Known residual gap — confirmed pre-existing, NOT a rebase regression

Root-caused per the skill's "known self-healing gap" procedure: `git log -S "圖片選單已過期"`
found exactly one commit, `9c9a7e186c` ("fix(gateway): reply with expiry notice instead
of silently falling through on expired image-OCR menu choice", explicitly labeled
"Stage 1 of t_b060bd33" in its own message) — and that commit **only adds the test
file**, 71 insertions, zero changes to `gateway/run_inbound.py`. The corresponding
implementation (the actual expiry-notice short-circuit in
`_handle_pending_image_ocr_choice`) was never committed anywhere in this fork's
history.

Confirmed this predates the rebase entirely: ran the same test directly against
the **original, unrebased, currently-live** `main` (`cf2ce09b80`) in
`/home/cwliao/.hermes/hermes-agent` — identical 3 failures reproduce there too.

**User-visible impact (live in production right now, independent of this rebase):**
if a gateway restart happens between showing the image-OCR menu (numbered choices)
and the user's numeric reply, the in-memory pending-choice dict is empty, the
handler returns `None`, and the message falls through to normal LLM turn
processing — which then answers based on unrelated stale conversation history
instead of telling the user the menu expired.

**Not fixed in this rebase** — out of scope (this rebase's job is to catch the fork
up to upstream, not to implement a different, already-tracked ticket's missing
Stage 1 code). Flagging here per ticket `t_b060bd33` for whoever picks that up next;
the fix is scoped precisely in `9c9a7e186c`'s own commit message (reply with the
expiry notice and short-circuit, except falling through when the same session key
has a live `_pending_last30days_choices()` entry via peek-not-pop).

## Operator hand-off — not executed by this session

1. **Push**: `git push origin main` after the reset below needs
   `--force-with-lease` (history was rewritten). Flagged as production-adjacent;
   hand the exact command to the operator rather than running it.
2. **Conclude the rebase** (move `main`):
   ```bash
   cd ~/.hermes/hermes-agent
   git tag backup/pre-upstream-rebase-20261006 main
   git rev-parse main   # verify still cf2ce09b80 before reset -- hasn't moved since this worktree was created
   git reset --hard upstream-rebase-009-20261006
   ```
3. **Deploy**: via `scripts/hermes_upstream_apply.py`'s three-stage pipeline
   (preflight → review → apply). `--execute` requires a human-supplied
   `HERMES_UPSTREAM_APPROVAL_TOKEN` matching the candidate's approval record —
   not fabricated by this session. Prepare the candidate and hand the exact
   `--execute` command (with `--previous-release`/`--previous-dropin` pointing at
   the currently-live release) to the operator.
4. **Cleanup**: once `main` includes this branch's tip, `upstream-rebase-009-20261006`'s
   worktree is safe to remove (`git worktree remove --force` + `git worktree prune -v`,
   hand to operator per the worktree-boundary doc's destructive-action note). Also
   worth a separate decision: whether to remove the now-confirmed-stale
   `upstream-rebase-007-20261001` / `upstream-rebase-008-20261003` worktrees.
