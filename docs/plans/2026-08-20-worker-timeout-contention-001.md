# WORKER-TIMEOUT-CONTENTION-001 — worker timeouts are inference contention, not a code fault

Status: proposed. Supersedes the *diagnosis* in
[WORKER-STARTUP-HANG-001](2026-08-20-worker-startup-hang-001.md), whose title
and central claim were wrong.

It does **not** close that ticket's open candidates. Showing that these
workers were starved of inference time does not exclude a blocking call
elsewhere: the reopened `agent/agent_init.py` change deployed at 20:00, and
the dispatcher environment (`HERMES_HOME`, the resolved `--toolsets` pin),
remain untested against a loaded host. An earlier version of this ticket
claimed to supersede "every candidate it considered", which was an overreach
a reviewer caught.

## What actually happens

A worker is given 300 seconds. Each API call it makes costs 30 to 130 seconds
depending on what else is running on the host. When enough is running, two
calls exhaust the budget and the dispatcher kills it mid-task.

Scoped to worker sessions only — this matters, see the corrections below:

| | working, 2026-08-19 17:35–17:55 | failing, 2026-08-20 07:07–07:13 |
|---|---|---|
| input tokens | 29k–37k | 25k–35k |
| latency per call | **18–39s** | **32–129s** |
| distinct sessions in window | 10 over 20 min | 14 over 6 min |
| API calls in window | 54 over 20 min | 34 over 6 min |
| call rate | 2.7/min | **5.7/min** |

Input size is the same or smaller in the failing window. **Only latency
differs.**

An earlier version of this ticket rested on the call *rate*, and a reviewer
was right that this is confounded: slower calls cause earlier timeouts and
retries, which pack more sessions into a shorter window, so the rate is partly
an effect of the thing it was being used to explain. The check that is not
confounded is summed latency against wall-clock, and it was not computed.
It has been now:

| window | calls | summed latency | wall-clock | **overlap** |
|---|---|---|---|---|
| working | 53 | 1195s | 1200s | **1.0** |
| failing | 34 | 2538s | 360s | **7.05** |

The working window shows 54 API-call lines in the first table and 53 here.
The difference is one call that recorded no latency because it did not return:
`goal judge: API call failed (Request timed out.)` at 17:39:35. It is excluded
from the latency sum, which is why 53 is the figure used.

The exclusion **favours this ticket's argument** and is flagged for that
reason. A timed-out call still consumed time, so including it would raise the
working window's overlap above the reported 1.0; leaving it out makes the
contrast with 7.05 look cleaner than the data strictly supports. The true
working overlap is ≥ 1.0 by an unknown margin. An earlier version of this
note claimed the opposite — that the exclusion worked against the comparison —
which was backwards, and a reviewer caught it.

An overlap of 1.0 means calls ran essentially back to back — no concurrency.
An overlap of 7 means seven calls were in flight on average. That is a direct
measurement of simultaneity, not an inference from rate, and it is the
evidence the contention claim rests on.

The inference server is not degraded. Probed directly while writing this: a
67k-token prompt completes in 42 seconds, a rate consistent with the working
window. `ornith:35b` is resident in 24.6 GB of VRAM and did not change.

The mechanism is that four swarm workers, a Telegram agent session and cron
jobs all issue 30k-token prompts to one GPU, which serialises them. The 300s
cap is a per-worker wall-clock budget with no notion of queueing, so a worker
that would finish in three calls when idle cannot finish two when loaded.

## What this explains that the previous diagnosis did not

The `execute_code` refusals are consistent with a worker spending its few
remaining calls trying tools rather than doing the task. That is an
interpretation, not a measurement, and it is offered as one.

**The 120-second `browser_navigate` timeout is not explained by this ticket
and should not be.** A reviewer pointed out that browser automation is not an
inference call — it is a different subsystem, with its own resources, and
absorbing it into a GPU-contention story conflates the two. Why a browser
open took 120 seconds is unexplained and stays that way here.

The previous ticket built its candidate list around both of these.

It also explains why three isolation experiments all passed: they ran one
process at a time against an idle server.

## Corrections to the record

Four wrong conclusions were reached on the way here, all mine, and they are
listed because the same errors are cheap to repeat:

1. **"The one-shot logging change caused it."** Formed from a deploy
   timestamp. Workers do not use that code path — they spawn via `chat -q`.
2. **"The session is never established."** Inferred from the worker log file
   lacking a `session_id:` line. The sessions are in `agent.log`; the log file
   is written differently. Single-source inference, wrong.
3. **"The worker is blocked at the approval layer."** The refusal *returns* an
   error; it does not block. A reviewer caught the mislabelling.
4. **"The prompt grew 50%, from 40k to 60k."** This one was measured, and
   measured wrongly: the 58k–62k figures came from the Telegram session, which
   carries 258 turns of history, mixed into a window filter that did not scope
   by session. Worker prompts did not grow.

The fourth is the instructive one. It was not a guess — it was a number read
off a log, and it was still wrong, because the population was not scoped.

## What to do about it, none of it decided

- **Raise `--worker-max-runtime`.** Simplest. Does not fix anything, buys
  headroom, and a budget that has to absorb unpredictable queueing is a
  budget that will be wrong again at a different concurrency.
- **Serialise or limit concurrent swarm workers.** Four lanes at once is the
  design; running them two at a time trades wall-clock for reliability.
- **Make the cap account for queueing** — measure time spent in the agent
  rather than wall-clock, so a worker is not punished for another process's
  load.
- **Reduce the 30k-token prompt.** The largest single lever on latency, and
  the one furthest from this ticket's evidence: nothing here establishes what
  those 30k tokens are.

## What is not established

Whether the concurrent load at 07:07 was unusual or is now normal. Two windows
are not a baseline, and no measurement of steady-state overlap exists.

What produced an overlap of 7 when only four workers were dispatched. Four
lanes plus a Telegram session plus cron accounts for the order of magnitude,
but the composition was not measured per source.

Whether raising the cap alone would have let the observed runs finish. Their
budget was consumed by two slow calls plus a 120s browser timeout; more time
might simply have bought more flailing.
