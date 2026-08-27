status: CONSENSUS (claude/codex/agy/grok independently read kanban_swarm.py
and kanban_db.py, unanimous on option (a); groq's reply was fabricated --
cited files/line numbers that do not exist in this repo -- and is
disregarded). See t_9c102aff.

## Consensus outcome

Implement **option (a)** only: delete `skills=["requesting-code-review"]`
at `hermes_cli/kanban_swarm.py:1020` (inside `_create_swarm_uncommitted()`,
not `create_swarm()` -- corrected per grok's read). Reject (b): no reliable
code-vs-non-code classifier exists today (`workspace_kind` has no `"git"`
value; neither public entry point to `create_swarm()` even passes
`workspace_kind`), and it wouldn't matter anyway because
`hermes_cli/kanban_db.py`'s `_resolve_worker_cli_toolsets()` already pins
`role in {"verifier", "synthesizer"}` down to `requested = ["kanban"]` by
design -- so ANY swarm's verifier that follows this skill's
git/terminal/file instructions is a guaranteed failure, not just non-code
swarms. Reject (c): hardening the general-purpose skill fixes the wrong
layer. Item 2 (invalid-tool-call exhaustion should call `kanban_block`
instead of silently exiting rc=0) is **out of scope** for this ticket --
track separately; it's a shared `agent/conversation_loop.py` code path
affecting all kanban workers, not swarm-specific.

Watch item (not in scope): the same function hardcodes
`skills=["humanizer"]` on the synthesizer -- same pattern, lower risk
(humanizer's primary path is inline rewriting, not tool calls), flagged in
the earlier `docs/plans/2026-08-19-swarm-e2e-defects-001.md` dump too.
Leave alone for this ticket.

## Implementation scope (exact)

1. Delete the `skills=["requesting-code-review"]` line (verifier's
   `skills` falls back to `None`/default).
2. Add a test asserting a freshly-created swarm's verifier task does NOT
   carry `requesting-code-review` in its `skills` -- cover both lane-mode
   and non-lane-mode `create_swarm()` calls.
3. Existing `tests/hermes_cli/test_kanban_swarm.py` and related swarm test
   files must stay green.

Do not touch: `humanizer` on the synthesizer, `requesting-code-review`
skill file itself, `workspace_kind`, invalid-tool-call/`kanban_stop`
handling in `agent/conversation_loop.py`.

# Kanban swarm verifier: hardcoded `requesting-code-review` skill breaks non-code swarms

## Problem

`hermes_cli/kanban_swarm.py:1020`, inside `create_swarm()` (or equivalent
swarm-planning function), unconditionally attaches
`skills=["requesting-code-review"]` to every swarm's verifier task:

```python
verifier = kb.create_task(
    conn,
    title=verifier_title,
    ...
    skills=["requesting-code-review"],
    max_runtime_seconds=DEFAULT_WORKER_MAX_RUNTIME_SECONDS,
)
```

`skills/software-development/requesting-code-review/SKILL.md` is written for
verifying a real git diff before commit: `git diff --cached`, running
`pytest`/`eslint`, grepping added lines for hardcoded secrets, etc. Its
instructions assume tools such as `search_files`, `terminal`, `read_file` are
available to the calling agent.

A kanban swarm worker's actual toolset does not include those tool names.
When a swarm's goal has nothing to do with a git repo (e.g. "compare 4
sorting algorithms' time/space complexity with example output," any
text-only or cross-lane-comparison goal), the verifier worker still gets
this skill attached, follows its instructions, calls nonexistent tools,
gets 3 consecutive "Unknown tool" corrections, and gives up -- exiting
`rc=0` without ever calling `kanban_complete` or `kanban_block`. The
dispatcher counts this as a protocol violation; after
`protocol_violation_limit` (3) such violations it gives up and the task
lands in `blocked`, permanently stalling that swarm's verify -> synthesize
chain.

## Reproduction (this session, 2026-08-27)

Swarm root `t_9e9e56fd`, goal: "Compare time/space complexity of 4 sorting
algorithms with concrete Python execution examples." All 4 workers
(Quicksort/Merge/Heap/Timsort) completed successfully. The verifier
(`t_b6aa50ac`) failed 6 consecutive runs:

- Runs 1-4 (08:44-08:50): unrelated `vllm-production` connectivity issue
  (separate, already-known infra problem) plus an OpenRouter fallback
  credit exhaustion (`HTTP 402`) -- model produced only empty/thinking-only
  responses, never got to call any tool.
- Runs 5-6 (08:58-09:00), **after `vllm-production` was confirmed stable**:
  model got a real response, called `search_files` / `terminal` /
  `read_file` (none exist), got 3x "Unknown tool" corrections, gave up.
  Log: `~/.hermes/kanban/logs/t_b6aa50ac.log`.

This is deterministic given the skill's content and the kanban worker's
actual toolset -- not a fluke, not related to the vLLM issue. Any future
non-code-repo `kanban_swarm` goal will hit this every time its verifier
runs.

## Proposed fix

Do not hardcode `skills=["requesting-code-review"]` for every verifier.
Two sub-questions for reviewers:

1. **Attachment condition.** Options, ranked by how targeted they are:
   - (a) Drop the hardcoded skill entirely. The verifier's `verifier_body`
     already contains generic instructions ("Review every worker handoff
     and blackboard update. Gate the swarm: pass only when the evidence is
     sufficient; otherwise block with the exact missing work.") which do
     not depend on git/code tooling. Simplest, smallest change.
   - (b) Attach the skill conditionally, only when the swarm's goal/workers
     indicate an actual code-repo diff-review task (e.g.
     `workspace_kind == "git"` or a keyword/goal classifier). Requires a
     reliable signal for "this is a code review swarm," which may not
     exist today -- needs to be verified against the actual `create_swarm`
     call sites and worker spec shape before assuming it's easy.
   - (c) Keep the skill attached but harden `requesting-code-review`
     itself to no-op gracefully when its assumed tools aren't present
     (e.g. check tool availability before calling, or check for a git repo
     before running Step 1). More invasive, fixes a symptom in a
     general-purpose skill rather than the swarm-specific misuse.

   Recommend (a) unless reviewers know of swarms that specifically rely on
   this skill being auto-attached for code-review verifiers -- check
   `kanban_swarm.py` callers/tests for that assumption before committing to
   (a).

2. **Defense in depth, orthogonal to (1):** should a kanban worker's
   "unknown tool called" retry-exhaustion path do anything better than
   silently exiting `rc=0`? Today it prints "Stopping as partial" to the
   log but apparently still exits 0 without a terminal kanban call, which
   is what triggers the protocol-violation/give-up spiral. Worth asking
   reviewers whether this should itself call `kanban_block` with a clear
   reason (e.g. "worker hit invalid-tool-call limit") instead of exiting
   silently -- that would make ANY future skill/tool mismatch fail loudly
   and recoverably (retry with a corrected skill) instead of silently
   burning through the protocol-violation budget. This is a more general
   robustness fix and may be out of scope for this ticket; flag only.

## Non-goals

- Not fixing the `vllm-production` KV-cache/max-model-len fragility here
  (separate, already known, tracked verbally this session -- not yet
  ticketed).
- Not fixing the OpenRouter fallback credit exhaustion (account/billing
  issue, not code).

## Verification plan (post-fix)

- Existing kanban_swarm tests must still pass.
- Add/adjust a test asserting the verifier task created by `create_swarm()`
  does NOT carry `skills=["requesting-code-review"]` for a swarm whose
  goal has no code-review signal (or, if a conditional classifier is
  chosen, tests for both branches).
- Manually retry a fresh sorting-algorithm-style swarm end-to-end (worker
  -> verify -> synthesize all producing non-empty results) after deploying,
  using the same manual release-snapshot procedure this session's other
  fixes used (`hermes update --yes` has a known fetch-delta blind spot --
  see `docs/operations/dgx-spark-hermes-directory-boundaries.md`).
