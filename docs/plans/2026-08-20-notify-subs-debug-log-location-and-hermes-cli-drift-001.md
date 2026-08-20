# NOTIFY-SUBS-DEBUG-LOG-LOCATION-AND-HERMES-CLI-DRIFT-001

Status: ticket, not yet implemented. Needs cross-review before implementation
starts.

## Context

Direct follow-up session to
[`2026-08-20-session-handover-gate8-swarm-line.md`](2026-08-20-session-handover-gate8-swarm-line.md),
which left an open mystery: a debug log added to the deployed release's
`tools/kanban_tools.py::_maybe_auto_subscribe` "never fired at all" for a
live `kanban_swarm` tool call, suggesting (per that handover) that live
Telegram calls might be going through a different, undiscovered code path.

This session re-added the same style of entry-point trace log (with a short
caller stack) to both candidate functions --
`tools/kanban_tools.py::_maybe_auto_subscribe` and
`hermes_cli/kanban.py::_maybe_auto_subscribe_swarm` -- built a new release
(`v2026.8.20-gate8-notify-subs-trace-debug-312bceba9d`, archived from
`origin/main` at `312bceba9d`), deployed it live (confirmed via
`/proc/<pid>/environ`), and asked the user to trigger one real four-lane
swarm via Telegram (tenant `fall-jokes-v3`, "請用 kanban_swarm 工具建立").

## Finding 1 -- the "mystery" was a wrong log file, not a different code path

The trace fired exactly once, for exactly the expected call:

```
~/.hermes/logs/agent.log:35757
2026-08-20 17:49:00,400 WARNING [20260820_070014_b5650f] tools.kanban_tools:
TRACE _maybe_auto_subscribe ENTRY task_id='t_8bdb7c47' pid=1999692
caller: model_tools.py:1281 handle_function_call
      -> hermes_cli/middleware.py:201 run_tool_execution_middleware
      -> model_tools.py:1273 _dispatch
      -> tools/registry.py:631 dispatch
      -> tools/kanban_tools.py:1319 _handle_swarm
      -> tools/kanban_tools.py:1105 _maybe_auto_subscribe
```

`pid=1999692` matched the gateway process's own PID at the time (verified via
`systemctl --user show -p MainPID` immediately after the debug-release
restart), confirming this ran inside the correctly-deployed debug release,
not a stale process. `_maybe_auto_subscribe_swarm` (the CLI path) never
logged anything in this run -- consistent with the turn using the
`kanban_swarm` tool directly, exactly as the user's message asked for
("請用 kanban_swarm 工具建立"), not shelling out to `hermes kanban swarm`.

The subscribe succeeded: `kanban_notify_subs` has a correct row for
synthesizer `t_8bdb7c47` (`platform=telegram`,
`chat_id=-1004391006048`, `user_id=386879279`, `created_at` matching the
swarm's creation timestamp), confirmed by querying `hermes_cli.kanban_db`
directly against the live DB, not by trusting narrated Telegram output.

**Root cause of the original "log never fired" observation:** the prior
session's debug log addition was correct and did execute on prior runs too,
in all likelihood -- it was written to `~/.hermes/logs/agent.log` (the
`tools.kanban_tools` logger, i.e. tool-execution-level logging), while that
session was checking `~/.hermes/logs/gateway.log` (the `gateway.run`
logger, i.e. message-routing-level logging). These are two separate log
files fed by two separate loggers/subsystems. There is no evidence, from
this session's test, of a third/undiscovered code path for the `kanban_swarm`
tool's auto-subscribe.

**Not established:** this single successful run does not explain the
*original* intermittent symptom (sometimes no row gets written, no error
anywhere). It only resolves the meta-question of why the previous session's
diagnostic instrumentation appeared silent. Reproducing the actual
intermittent failure -- now that the correct log file
(`~/.hermes/logs/agent.log`) is known -- is unstarted follow-up work, not
covered by this ticket. `WORKER-SUBPROCESS-SESSION-ENV-001` (companion
ticket, filed alongside this one) covers the one *confirmed-by-reading-code*
gap that could produce the symptom; there may be others not yet found.

## Finding 2 -- `hermes` CLI wrapper unsets `PYTHONPATH`, so CLI invocations bypass whatever is deployed

While tracing the debug-release rollout, `/home/cwliao/.local/bin/hermes` (the
`hermes` binary resolved by `terminal`-tool shell invocations, e.g. any agent
turn that runs `hermes kanban swarm ...` instead of calling the
`kanban_swarm` tool) was read directly:

```bash
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "/home/cwliao/.hermes/hermes-agent/venv/bin/hermes" "$@"
```

It unconditionally strips `PYTHONPATH` and `PYTHONHOME` before exec'ing the
venv's own `hermes` console-script entry point. The venv
(`/home/cwliao/.hermes/hermes-agent/venv`) has `hermes-agent` installed
**editable**, pointing at `/home/cwliao/.hermes/hermes-agent` itself
(confirmed via `pip show hermes-agent` -> `Editable project location:
/home/cwliao/.hermes/hermes-agent`, and by reading the generated
`__editable___hermes_agent_0_18_2_finder.py`, whose `MAPPING` hardcodes
`hermes_cli`, `tools`, `gateway`, `agent`, `plugins`, etc. to that exact
directory). With `PYTHONPATH` unset, any `hermes <subcommand>` invocation
resolves those top-level packages via the editable-install mapping, i.e.
**from `/home/cwliao/.hermes/hermes-agent`'s own on-disk state**, not from
whatever release directory `hermes-gateway.service`'s systemd drop-in
currently points `PYTHONPATH` at.

This matters because `/home/cwliao/.hermes/hermes-agent` -- the same
directory this whole multi-day effort has been treating as "the repo," and
where `git status --short` was run at the start of this session -- currently
has **HEAD far behind `origin/main`** (multiple PRs behind, including some of
this effort's own merged/deployed fixes) **and a large uncommitted diff on
top of that old HEAD** (dozens of modified/deleted files spanning
`gateway/run.py`, `hermes_cli/config.py`, `hermes_cli/kanban_swarm.py`,
`plugins/coding-cli/*`, `tools/mcp_tool.py`, several `tests/` files, deleted
plugins like `mermaid_renderer`, deleted docs, etc.). Nobody touched this in
this session; it was already in that state at session start.

**Consequence, not yet fully scoped:** any agent turn that runs
`hermes kanban ...` (or any other `hermes <subcommand>`) via the `terminal`
tool -- as opposed to calling a typed tool like `kanban_swarm` directly --
executes against this drifted main checkout's code, which is neither
`origin/main` nor whatever release is currently deployed. A fix shipped via
"new release directory + systemd drop-in restart" (this effort's whole
deploy pattern) has **no effect** on CLI invocations made this way. Whether
this has actually caused an observed-but-unexplained discrepancy anywhere in
this effort's history is not established here -- it is a structural risk
surfaced by reading the wrapper script and the editable-install mapping, not
a specific incident with its own evidence trail (unlike Finding 1).

## Questions for cross-review, not yet resolved

1. Is `/home/cwliao/.hermes/hermes-agent`'s drift from `origin/main`
   intentional (e.g. a deliberate long-running local branch never meant to
   sync) or accidental leftover state that should be reconciled? This ticket
   does not touch that directory -- resolving the drift is the user's call,
   not something to decide unilaterally.
2. Should the `hermes` CLI wrapper's `unset PYTHONPATH` be changed at all? It
   may be intentional (isolating the operator's own shell env from whatever
   `PYTHONPATH` a parent shell happens to have), in which case the real fix
   is making sure nothing that matters is invoked via `hermes <subcommand>`
   through `terminal` in agent turns -- prefer typed tools
   (`kanban_swarm`, etc.) that run in-process, which this effort's own
   `GATE8-SWARM-CREATION-TOOL-001` (PR #80) already moved towards for swarm
   creation specifically.
3. If the wrapper does need to change, the safest fix is almost certainly
   **not** re-enabling inherited `PYTHONPATH`, but instead making the
   editable-install mapping (or a config value) resolvable to the *current*
   release directory, so `hermes` CLI invocations track deploys the same way
   the gateway service does. This needs design, not a one-line patch.

## Suggested next steps

- Do not implement anything here without the user weighing in on question 1
  above first -- touching `/home/cwliao/.hermes/hermes-agent`'s working tree
  is out of scope for a quiet fix.
- For question 2/3, cross-review should include an explicit recommendation
  (change the wrapper vs. audit+reduce `terminal`-shelled `hermes` usage in
  agent-facing tool guidance) before any code is written.
- No regression test is proposed here yet -- this is a deploy/process gap,
  not a code-level bug with a clear unit-test boundary. A cross-reviewer
  should weigh in on whether a smoke test (e.g. "does `hermes --version`
  under `terminal` match `HERMES_RELEASE_SHA` of the currently deployed
  release") is worth adding as a deploy-time sanity check instead.

## Process notes

- This ticket must go through cross-review before implementation starts, and
  the implementation itself must be cross-reviewed again before merge -- per
  this effort's established working rule.
- The debug release and its systemd drop-in used to capture Finding 1 have
  already been torn down this session: drop-in removed, gateway restarted
  back onto the stable release (`v2026.8.20-kanban-toolset-gate-92051b5450`,
  confirmed via `/proc/<pid>/environ`), debug release directory deleted, and
  the two throwaway trace-log edits reverted from the working tree
  (`git checkout -- hermes_cli/kanban.py tools/kanban_tools.py`). Nothing
  from this session's debug patch was committed.
