# Handover prompt — paste this into a new session

Copy everything below the line into the new session's first message. It is
written to be tool-agnostic (works for Claude Code, Codex CLI, or any other
coding agent) and self-contained — it does not depend on any local
scratch/session file, only on the `hermes-agent` git repo, which is the
durable source of truth.

---

You're picking up mid-effort on the `hermes-agent` repo
(`github.com/cwliao/hermes-agent` — **not** the `NousResearch` upstream;
`gh` commands need `-R cwliao/hermes-agent` or they hit the wrong repo by
default). Work happens on host `55-0940189-03` (a DGX Spark), in an
isolated git worktree — check `git worktree list` from the repo root, or
create a fresh one under `~/.hermes/worktrees/` if none is free. Never work
directly in the deployed release directories under `~/.hermes/releases/`.

First step: read the full handover doc already committed to the repo at
`docs/plans/2026-08-20-session-handover-gate8-swarm-line.md` on `main`
(`git show main:docs/plans/2026-08-20-session-handover-gate8-swarm-line.md`
if you don't have a checkout yet). It has the complete detail; this prompt
is a compressed pointer to it, not a replacement.

**Compressed summary, in case you need it before reading that file:**

A multi-session effort got Gate 8 (a four-lane Telegram-initiated kanban
swarm: native_hermes + claude + grok + agy workers → verifier →
synthesizer) working end-to-end for the first time, via four merged and
deployed PRs (#78 concurrency cap, #79 worker handoff + notify-subscribe
on the CLI path, #80 a new typed `kanban_swarm` tool, #81 a kanban
toolset-visibility gate fix). All four are on `main` and deployed to
`55-0940189-03`'s live `hermes-gateway.service` (confirm with
`systemctl --user status hermes-gateway.service` and check
`/proc/<pid>/environ` for `HERMES_RELEASE_SHA`, then `git log --oneline`
to confirm main is ahead of nothing you'd need to redo).

**The one open task**: `kanban_notify_subs` (the table that drives
synthesizer-result delivery back to Telegram) gets a row written
*intermittently* — sometimes a swarm's result reaches Telegram, sometimes
it silently doesn't, with no error logged either way. Confirmed causes so
far:
- Dispatcher-spawned worker subprocesses (`hermes_cli/kanban_db.py::
  _default_spawn`) never forward `HERMES_SESSION_PLATFORM`/
  `_CHAT_ID`/`_KEY` into the child env, so any `kanban_create`/
  `kanban_swarm` call made *from inside* a worker always silently fails to
  subscribe. This part is real, reproducible by reading the code, and not
  yet fixed.
- That does **not** explain everything — a live, same-turn, non-subprocess
  `kanban_swarm` tool call also failed once, and a temporary debug log
  added to the live deployed release's `_maybe_auto_subscribe` function
  never fired at all for that failing call, nor for a later call that
  *did* succeed. This means live Telegram calls may be going through a
  different code path than expected (most likely
  `hermes_cli/kanban.py::_cmd_swarm` → its own separate
  `_maybe_auto_subscribe_swarm`, reached via a `terminal`-tool-shelled
  `hermes kanban swarm` command rather than the `kanban_swarm` tool
  directly) — but no such terminal command was found in
  `~/.hermes/logs/gateway.log` for the relevant time window either. This
  is the actual mystery: confirm which function is really executing
  before attempting any fix.

**Suggested first move**: add debug logging to *both* candidate functions
at once (`tools/kanban_tools.py::_maybe_auto_subscribe` and
`hermes_cli/kanban.py::_maybe_auto_subscribe_swarm`), deploy, ask the user
to trigger one live four-lane swarm via Telegram, and read
`~/.hermes/logs/gateway.log` to see which one (if either) actually fires.
Full detail, prior probes already ruled out, and the deploy procedure used
all session are in the committed handover doc above — read it before
re-deriving anything.

**Working rules this whole effort has followed, keep following them**:
cross-review every ticket before implementing and every implementation
before merging (a second independent read, not self-review); verify any
delegated/sub-agent claim against the live system yourself before acting
on it; confirm with the user before any git push, PR merge, or gateway
restart (this is a live, single, shared production service — no staging
environment); commit tickets to `docs/plans/YYYY-MM-DD-slug.md` as you go.
