# KANBAN-CARD-GC-001 — dead swarm graphs accumulate on the board forever

Status: proposed. Design only, no implementation.

## Measured, not estimated

Two days of testing on this host produced **47 cards**. Of the 34 still on the
active board when this was written, **every one is test residue** — no user
work. Nine will never move again.

| tenant | cards | non-terminal | last activity |
|---|---|---|---|
| `telegram-spike` | 7 | **3** | 2026-08-18 |
| `e2e-fourlane-v2` | 7 | 0 | 2026-08-19 |
| `e2e-fourlane-v3` | 7 | **6** | 2026-08-19 |
| `e2e-fourlane-v4` | 7 | 0 | 2026-08-19 |
| (no tenant) | 6 | 0 | 2026-08-18 |

"Non-terminal" is the measured property; **"permanently stuck" would overstate
it**, since `kanban unblock` can restart a blocked card. What the data does say
is that none of the nine has moved in one to two days — nobody has resumed any
of them, 0 for 9.

Six of the 34 are **not swarm graphs at all**: untenanted single-card smoke
tests, no root and no contract. Any graph-scoped rule below misses them
entirely, which matters for scoping a first implementation.

The shape is consistent for the rest: **one swarm is seven cards, and a failed
swarm leaves its whole graph behind** — workers `blocked` after the dispatcher gave up,
verifier and synthesizer `todo` waiting on parents that will never complete.

This is not cosmetic: both the operator and the agent read `kanban list` to
decide what is happening. A 2026-08-20 misreport by the agent is **not**
evidence for this — that one is traced to the operator archiving the cards
while the agent was still polling them, which is a different fault and is
recorded here so it is not miscounted as clutter damage.

## The machinery exists and starts one step too late

`hermes kanban gc` already cleans:

- scratch workspaces of tasks **already** in `archived`
- `task_events` rows older than N days for tasks **already** terminal
- worker log files older than N days

Every one of those is post-archival housekeeping. **Nothing ever decides a
card is dead and archives it.** That single missing step is why the board
grows without bound.

## The trap any design must avoid

`recompute_ready` (`hermes_cli/kanban_db.py:3282`) promotes `todo` to `ready`
when all parents are **`done` or `archived`**, and considers `blocked` tasks
for promotion too. **Archiving satisfies a dependency.**

The link is not inferred: `archive_task` (`:5533`) calls `recompute_ready`
immediately after its own transaction, under the comment *"Promote
newly-unblocked dependents immediately instead of waiting for a later
dispatcher tick."* It archives one card per call, recomputing after each.

**This already happened, in production, during the 2026-08-20 cleanup.** The
verifier of the failed run carries exactly these events:

```
created    06:29:51
promoted   06:44:53
archived   06:44:53
```

Archiving the last worker promoted it to `ready`. It was archived a fraction
of a second later, and the dispatcher ticks about every 60 seconds, so nothing
claimed it. Timing, not design.

Two separable requirements follow, and an earlier draft of this ticket
collapsed them into one:

- **Forced by the code:** archival of a graph must be atomic with respect to
  `recompute_ready` — no partially-archived state may become visible, or a
  dependent is promoted against a graph that produced nothing. This is a
  transaction-boundary requirement.
- **A design choice, not a necessity:** that the *decision* to archive be
  graph-scoped. It is the obvious pairing, but it does not follow from the
  code, and an implementer should not read "whole graph" as forbidding any
  decomposition.

### The constraint may itself be the defect

An earlier draft fenced `recompute_ready` out of scope. A reviewer pushed
back, and the objection is strong enough to record rather than defer:
**archiving is cancellation, not completion**, and treating the two alike is
why a cancelled graph can wake its own verifier. Designing GC around that
behaviour means building graph traversal to work around a scheduler semantic
that is arguably wrong.

The alternative is to distinguish them — an archived parent satisfies nothing,
and dependents of a cancelled graph are cancelled with it. That is a smaller
change than any GC option below and would remove the atomicity requirement
entirely.

It is not free: `archived` currently unblocks children deliberately (`:5530`
says so), so some caller depends on the present meaning, and changing it
without finding that caller would strand children that today proceed
correctly. **This is now the first thing to evaluate, ahead of choosing among
A, B, and C** — if the semantic changes, the options are re-scoped.

## What is structurally certain versus what is policy

Worth separating, because an earlier draft of this ticket conflated them:

- **Certain:** a card whose graph has been archived cannot run. Nothing.
- **Not certain:** a `blocked` card is not permanently dead. The dispatcher
  gave up, but `kanban unblock` exists and a human can restart it. Archiving
  blocked cards is a **policy choice about recoverability**, not a structural
  deduction. Whether anyone would in practice is a separate question the data
  answers weakly in the other direction: nine cards, zero resumed, over two
  days. That is a small sample from a testing period, not a usage pattern.

  The stakes are also lower than "discard" implies, and an earlier draft
  overstated them: **archiving is reversible and preserves the row**. The
  2026-08-19 diagnostics were reconstructed from archived cards afterwards.
  Treating a blocked card as dead risks an inconvenience — someone must
  un-archive — not a loss. That materially weakens the case for conservatism
  here, and the argument should not lean on it.

Any age threshold is likewise policy. Nothing in the data says what N should
be; it says only that 9 cards have sat unchanged for one to two days.

## Options

### A — mark disposable at creation
A swarm created for a test carries a flag, and GC only ever touches flagged
graphs. Real work is untouchable by construction.

- Strongest safety property: GC cannot delete what was never marked.
- Requires the caller to mark, and the failure mode is silent — an unmarked
  test graph accumulates exactly as today.

### B — graph-level age sweep
A graph with no event on any of its cards for N days is archived whole.

- Catches everything, including graphs nobody remembered to mark.
- Needs the whole-graph constraint above, and N is a guess.
- Risk: a long-running or deliberately parked graph looks identical to a dead
  one from the outside.

### C — retention by count
Keep the most recent N swarm graphs per tenant, archive older ones whole.

- Bounded board size regardless of failure rate.
- Same whole-graph constraint. Says nothing about a single tenant that runs
  once and fails.

## The ticket's own lean, stated rather than hidden

A reviewer noted that describing A and B as complementary *is* a
recommendation wearing neutral clothing. It is: **A plus B is the shape this
ticket leans towards**, A to make real work untouchable and B to catch graphs
nobody marked. That is a lean, not a decision — nothing here fixes the
threshold B needs, and C remains a legitimate bounding backstop if a cap is
preferred to an age.

## Not yet decided

- Which option, or which combination.
- Whether GC archives or deletes. Archiving preserves evidence and is
  reversible; the 2026-08-19 diagnostics were reconstructed from archived
  cards afterwards, which would have been impossible had they been deleted.
- Whether GC runs on a timer or only when invoked. A timer that archives
  graphs without anyone watching is a larger commitment than a manual command.
- How a graph is identified. `root_id` appears in the swarm contract, but
  cards created outside `create_swarm` have no root.
- What happens to a graph that is partially archived already.
- The six untenanted smoke-test cards, which no graph-scoped rule reaches.
- What happens when a GC sweep races a concurrent `kanban unblock` — the two
  reach opposite conclusions about the same card and nothing orders them.
- Who owns the threshold once it exists: a config key the operator sets, a
  fixed default, or a per-tenant value. An unowned threshold becomes whatever
  the first implementer guessed.

## Not in scope

No change to the dispatcher, or to the existing `gc` subcommand's
workspace/event/log behaviour. `recompute_ready` was previously listed here
and has been moved into the design space above, on review.
