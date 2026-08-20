# WORKER-STARTUP-HANG-001 — dispatcher-spawned workers hang after being refused a tool

Title corrected. An earlier version said "before establishing a session";
the session is established. See the correction below.

Status: proposed. **Cause not established.** Production is affected now: every
swarm worker started since 2026-08-19 21:39 has timed out.

## Symptom

A worker is spawned, writes one line to its log, and produces nothing further
until the dispatcher kills it at its runtime cap. It is retried once and dies
the same way, then the dispatcher gives up and the card goes `blocked`.

Every swarm run since has failed this way, including two Gate 8 attempts whose
graphs were otherwise correct — right lanes, right skills, right runtime,
valid contracts.

## The measurement that localises it

Worker logs under `<board>/kanban/logs/` split cleanly on a timestamp:

```
17:40  t_a2ed21b4   warn=1 session=1     <- worked
17:41  t_f8ecf949   warn=1 session=1
17:52  t_ec82b040   warn=1 session=1     <- last good
21:39  t_6bf5ce8c   warn=2 session=0     <- first bad
21:39  (5 more)     warn=2 session=0
06:35  (4 workers)  warn=2 session=0
07:07  (7 workers)  warn=2 session=0
```

`session=1` means the log contains a `session_id:` line. Good workers have
one; **no worker since 21:39 has produced one at all.** The agent session is
never established.

`warn=2` is not a second symptom. `t_d25b2d28` recorded two `spawned` events
(pids 1281775 and 1287638) — the original attempt and its retry — so the log
accumulates one warning per attempt. An earlier reading of this ticket treated
the doubling as evidence of a repeated initialisation. It is not.

## Correction: the session IS created

The section below and the title of this ticket say the agent session is never
established. **That is wrong**, and it was wrong because the worker log file
was treated as the only record.

The sessions are in `~/.hermes/logs/agent.log`. The 07:07 batch appears as
`20260820_070755_8ad53c`, `_3dbae3`, `_ec6438`, each with
`platform=cli` and `msg='work kanban task t_...'`. They start normally. What
they do not have is a `Turn ended` line — consistent with being killed at the
runtime cap rather than finishing.

What the log shows them doing:

```
Tool execute_code returned error: {"status": "error",
  "error": "BLOCKED: execute_code runs arbitrary local Python (including
  subprocess calls that bypass shell-string approval checks). ... runs without
  a user present to approve"}
```

**That is a refusal returned, not a call that blocks.** An earlier wording
here said the worker is "blocked at the approval layer", which mislabels the
location: `execute_code` returns an error and control goes straight back to
the agent. A reviewer caught this. What is unaccounted for is what the agent
does *after* receiving that error and before the runtime cap — a retry loop, a
stalled synthesis step, and a later call that genuinely blocks all fit the
absent `Turn ended` equally well, and nothing here distinguishes them.

So the established symptom is narrower than the earlier wording: **the worker
is refused a tool and never reaches the end of its turn.** Where it spends the
remaining time is not established.

Two checks were run on the reviewers' prompting:

* **Did working workers hit the same refusal and proceed?** No. Between
  17:35 and 17:55, when workers were completing normally, `execute_code`
  appears **zero** times in `agent.log` and `BLOCKED` appears **zero** times.
  The 07:07 batch called it twice. So the refusal is not a pre-existing path
  that healthy workers navigated — they never went down it. That kills the
  red-herring reading, and leaves open whether reaching `execute_code` at all
  is the cause or itself a symptom of the worker taking a different route.
* **Did the 20:00 deploy introduce the refusal?** No. The string
  `without a user present to approve` lives in `tools/approval.py` and appears
  **zero** times in the `910955335d..7ef40eddce` diff. It is pre-existing code.
  The suggestion that this and the reopened `agent_init.py` are the same
  thread does not hold.

One fact cuts against a simple approval explanation and is recorded rather
than buried: the same refusal text fires under `approvals.mode: manual` (the
21:39 batch) **and** under `smart` (07:07). A guard that behaves identically
under both is not reading that setting, so changing the mode cannot be the
fix.

The absent `session_id:` line in the worker log is therefore a logging
difference, not evidence about the session. Every conclusion in this ticket
that rested on "no session" needs re-reading with that correction.

## Ruled out

- **The command's argument list**, narrowly. Workers are spawned as
  `hermes -p <profile> --cli --accept-hooks [--skills …] [--toolsets …] chat -q <prompt> -Q`.
  Run by hand against the deployed release it completes in seconds, exits 0,
  prints the warning **once**, and emits `session_id`.

  This is **not** a clearance of the invocation as a whole, and an earlier
  version of this ticket overstated it. The hand run kept a terminal on stdin;
  a dispatcher-spawned worker has its stdio redirected to a log file. Anything
  that reads stdin or initialises a terminal interface would hang in one and
  not the other, which is precisely the observed shape.

  **That was tested, not deferred.** Re-run with `< /dev/null` and both
  streams to a file, it still exits 0 in seconds and emits `session_id`. Stdio
  shape is excluded by measurement.
- **The one-shot logging change.** The first hypothesis was that
  `hermes_cli/oneshot.py` had been changed at 21:32, immediately before the
  first bad batch, and that workers are one-shot runs. **They are not** —
  the spawn path is `chat -q`, which never enters that module. The hypothesis
  was formed from the timestamp alone and was wrong.
- ~~**The guardrail change in `agent/agent_init.py`.**~~ **Reopened.** An
  earlier version of this ticket excluded it because the modified block is
  wrapped in `try/except` that logs and continues. That is a category error,
  and a reviewer caught it: `try/except` catches a raised exception. **The
  symptom is a hang**, and a blocking call that never returns never reaches
  the `except` clause at all. The exclusion tested a failure mode the incident
  does not exhibit. This change is deployed at 20:00, inside the window, and
  is **not** ruled out.
- **Task attributes.** A working worker (`t_a2ed21b4`) and a hanging one
  (`t_d25b2d28`) carry identical `skills` (`["claude-code"]`), assignee
  (`default`), `max_runtime_seconds` (300), `goal_max_turns` (5) and
  `goal_mode`.

## Not established

Why the session is never created. Two kinds of candidate remain, and an
earlier version of this ticket listed only the first.

**Deployed code inside the window.** `agent/agent_init.py` changed at 20:00
and is reopened above, its exclusion having rested on a category error. It is
not established that it hangs — only that the reason given for dismissing it
was invalid. Whether the guardrail block can block at all, and whether that
depends on profile or kanban configuration, is unexamined. Isolating it is
step 4 below.

**Environment the dispatcher sets**, which a hand run does not have — `HERMES_KANBAN_TASK`, `HERMES_KANBAN_GOAL_MODE`,
`HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`, a profile-scoped
`HERMES_HOME` from `resolve_profile_env`, and a `--toolsets` pin resolved at
dispatch time. None of these has been isolated.

Three config changes were made around this period. An earlier version of this
ticket dismissed them as "after the first bad batch" without timestamps —
exactly the timestamp-free reasoning that produced the two discarded
hypotheses. Checked rather than asserted, from the mtimes of `config.yaml` and
its backups:

```
2026-08-19 17:38   last modification before the window
2026-08-19 17:52   last working worker
2026-08-19 21:39   first hanging worker
2026-08-20 05:51   terminal.timeout   60 -> 300
2026-08-20 06:18   approvals.timeout  60 -> 600
2026-08-20 06:23   approvals.mode     manual -> smart
```

**`config.yaml` was not modified between 17:38 and 05:51.** No configuration
change falls inside the window, so none of the three can explain the first bad
batch. The conclusion is unchanged; the evidence for it now exists.

Worth noting for anyone reading later: `approvals.mode: smart` is a
structurally plausible cause of "writes one line, then blocks forever" on a
non-interactive `-Q` spawn, since it introduces an auxiliary decision before a
tool runs. It is excluded here only by the timestamps above — it postdates the
incident by eight hours — not by argument.

## What the environment experiments showed

Steps 1 to 3 were run. **None reproduces the hang**, which is why the approval
finding above matters more than any of them:

| step | result |
|---|---|
| `HERMES_HOME` alone | not a variable at all — `resolve_profile_env("default")` returns the root home, the same one a hand run uses |
| plus `HERMES_KANBAN_TASK` and goal-mode vars | exit 0, session present, 42s |
| plus the dispatcher's resolved `--toolsets` pin | exit 0, session present, 9s |

The hand reproduction differs from a real worker in one further way the
experiments could not cover: it was given a task that needs no tool. A real
worker must call one, and that is where the refusal lands.

## Suggested next steps, cheapest discriminator first

An earlier version proposed setting the dispatcher's whole environment at
once. A reviewer pointed out that is six variables in a bundle: it would show
that *something* in the set matters without saying which. Ordered by
information per unit of effort:

1. **Vary `HERMES_HOME` alone.** The dispatcher gives workers a
   profile-scoped home via `resolve_profile_env`. That single variable decides
   **which `config.yaml` is read**, so it can silently change every setting at
   once — a working hand-run under the root home proves nothing about a worker
   under a pinned profile home. This is one environment variable and one
   command.
2. **Add `HERMES_KANBAN_TASK` and the goal-mode variables**, against a
   disposable card. If the hang appears only here, the goal loop
   (`_run_kanban_goal_loop_q`, reached only under `-Q` with a kanban task set)
   is the place to look.
3. **Add the resolved `--toolsets` pin**, which is computed at dispatch time
   and differs from what a hand-run resolves.

4. **Trace one hanging worker turn by turn.** The gap between "refused a
   tool" and "killed at the cap" is the whole remaining question, and no log
   read so far covers it. Everything below is secondary until that gap is
   filled.
5. **Isolate the reopened `agent_init.py` change.** Run a worker against the
   19:04 release, which predates it, with everything else held constant. That
   is a one-line drop-in change and it either clears the deploy or convicts
   it, which no amount of reading the diff can do.

If none of these reproduces it, the remaining difference is that a worker is a
child of the long-lived gateway process rather than of a shell, which would
point at inherited state rather than at anything in the command.

## Why this is filed rather than fixed

Two hypotheses were formed from timing alone and discarded on inspection, and
a third exclusion turned out to rest on a category error. Filing what is
measured — with the discarded hypotheses and the faulty exclusion both
recorded, so neither is re-formed — is worth more than another guess.

## On the tempting fix

Auto-approving `execute_code` for unattended or goal-mode workers would remove
the refusal. It should not be reached for: that guard exists because
`execute_code` runs arbitrary local Python including subprocess calls that
bypass shell-string approval checks, and the swarm path is exactly the
unattended case it was written for. Loosening it there reopens the risk it
closes, and — per the correction above — it is not even established that the
refusal is where the time goes.

## Impact

Swarm is unusable. Gate 8 of `HERMES-MULTI-AGENT-CLAUDE-WORKER-001` cannot
pass while this stands, regardless of how correct the dispatch is — and two
attempts have already been spent proving the dispatch correct.
