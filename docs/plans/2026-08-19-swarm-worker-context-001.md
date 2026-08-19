# SWARM-WORKER-CONTEXT-001 — worker cards omit their harness, loop budget, and graph position

Status: proposed. Not implemented. Sibling of `SWARM-E2E-DEFECTS-001` Defect 2.

## Origin

Raised by the user while Defect 2 was being fixed: swarm workers should carry
harness, loop, and graph engineering concepts. Defect 2 turned out to be one
instance of that gap, not a separate problem — the completion contract is the
*loop termination condition*, and it was missing for the same reason the rest
is missing: `create_swarm()` sets these facts on the card and never tells the
agent about them.

## The general shape

`create_swarm()` writes a task body from three parts: the caller's task text,
`_swarm_context()`, and (after Defect 2) the completion contract. Several
facts that the runtime enforces are set as card *columns* and never appear in
the body the agent reads. An agent cannot act on a constraint it is not told.

This is the failure class already recorded twice in this repo: an invariant
everything depends on is never stated on the surface where it would be acted
on.

## Gap 1 — harness

Set on the card, absent from the body:

| card field | set at | in body? |
|---|---|---|
| `skills` | `create_swarm` per worker spec | no |
| `workspace_kind` | `kanban_swarm.py:210`, default `"scratch"` | no |
| `workspace_path` | caller | no |

An external-lane worker is an external CLI running with a specific skill
loaded. Nothing in its task text says so. It does not know which tools it
has, which workspace it may write to, or what it must not touch.

## Gap 2 — loop

| card field | value in lane mode | in body? |
|---|---|---|
| `goal_max_turns` | `DEFAULT_GOAL_MAX_TURNS = 5` (`:33`) | no |
| `max_runtime_seconds` | `DEFAULT_WORKER_MAX_RUNTIME_SECONDS = 120` (`:32`) | no |
| completion contract | added by Defect 2 fix | **yes** |

The agent is given five turns and 120 seconds and is told neither. It cannot
budget, and it has no stated behaviour for exhaustion — block with partial
evidence, or fail. Defect 2 supplied the "when am I done" half of the loop;
the "how much room do I have" half is still missing.

## Gap 3 — graph

`_swarm_context()` (`:75-83`) names the root id and the goal. It does not say:

- which verifier will consume this output;
- that the verifier checks `lane_id`, `preflight_skill_id`, and
  `verified_clean` against the parent card's expected lane bindings;
- that a synthesizer runs only after the verifier persists `gate=pass`;
- that the worker's completion metadata and blackboard entries are the only
  things downstream sees.

This is the same defect as Defect 2 one level out. A worker that is not told
what its consumer checks cannot know what evidence to leave. Defect 2 fixed
the agent-to-kernel contract; this is the worker-to-verifier contract.

## Constraint on any fix

The Defect 2 fix is guarded by an anti-drift test that parses the generated
body and asserts the kernel accepts it. Any text added here must not break
that parser, and the same principle should apply: state facts that are read
from the card, so that changing the card changes the text.

## Not yet decided

- Whether the harness section is generated from the card fields or supplied
  by the caller. Generating it keeps it honest; supplying it is more flexible.
- Whether turn/time budget should be stated as absolutes or as guidance, given
  that an agent told "you have 5 turns" may spend turns reasoning about turns.
- Whether the graph section belongs on every card or only on workers.

## Not in scope

No change to `validate_completion()`, the lane quorum, or the dispatcher.
