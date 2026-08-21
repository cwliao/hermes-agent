# SWARM-LANE-TIMEOUT-RETEST-002

Status: one fix implemented and shipped here (the synthesizer completion
loop); two findings documented as known, out-of-scope issues (quorum
not actually requested by the bot; content drift across LLM handoffs).
Needs cross-review before merge, per this effort's established
practice.

## Context

A live Telegram-triggered 4-lane swarm test (tenant `autumn-jokes-v6`,
root `t_0b661c55`), run specifically to validate PR #97's
`worker_quorum` feature end-to-end with a real user asking the bot for
it by name, surfaced three separate, previously-undocumented problems.
None of them are bugs in the `worker_quorum`/`excuse_blocked_workers_
below_quorum` mechanism itself -- that mechanism's own core behavior
was independently re-confirmed working correctly during this same test
(see "What worked" below). All three are new findings.

## Finding 1 (fixed here): synthesizer completion-loop + false self-diagnosis

All four worker lanes genuinely completed and posted real jokes to the
shared blackboard (verified directly against `task_comments` -- not
taking the bot's word for it). The verifier genuinely ran and posted a
"Gate: PASS" verdict. The synthesizer was correctly promoted to
`ready`, started running -- and then failed `kanban_complete` **19
times over roughly 10 minutes**, each attempt missing a different
required field (`role`, `result_present`, `outcome`, `root_id` -- never
all simultaneously alongside a non-empty `result`). It ultimately gave
up and self-blocked with this stated reason:

> "Kanban kernel validator bug: swarm synthesizer completion rejects
> every attempt because role="synthesizer" metadata field requirement
> is unsatisfiable alongside result_present=true flag within available
> tool parameters. Tried 8 combinations — all rejected... Developer
> needs to fix validator logic..."

**Independently disproven, not just doubted.** Ran the exact same
task's contract through `validate_completion` directly with a
correctly-shaped payload:

```python
metadata = {"role": "synthesizer", "root_id": "t_0b661c55",
            "outcome": "completed", "result_present": True}
validate_completion(task, metadata=metadata, result="some non-empty result text")
# -> None (accepted)
```

The kernel validator is not buggy. The synthesizer's self-diagnosis was
fabricated -- the same class of finding as the earlier agy
investigation (`2026-08-20-swarm-agy-headless-oauth-block-001.md`) and
the fabricated `min_3_workers_success` gate claim from earlier the same
day (`2026-08-21-swarm-partial-quorum-001.md`): a small local model
(`ornith:35b`) confidently inventing a plausible-sounding external
cause rather than reporting genuine confusion.

**Root cause of the actual confusion (not the fabricated one):** the
task body's `_completion_requirements` text listed required fields as
flat `field = value` lines (e.g. `role = "synthesizer"`,
`result_present = true`) without ever showing which of those are
top-level `kanban_complete` tool-call parameters (`task_id`, `result`,
`summary`) versus which belong nested inside the `metadata` object
parameter. That mapping ambiguity is exactly the kind of thing a weak
model reliably mis-resolves under repeated pressure -- and unlike
`_completion_requirements`'s existing field list (which states
*what's* required), nothing showed *how* to shape the actual call.

**Fix**: `_completion_requirements` now appends one concrete,
copy-pasteable example of the actual `kanban_complete` call for the
task's role, built programmatically from the same contract dict
`validate_completion` checks against (not a separately hand-typed
string that could drift) -- e.g. for a synthesizer:

```
kanban_complete({"task_id": "<this task's id>", "result": "<your final, non-empty deliverable text>", "metadata": {"role": "synthesizer", "root_id": "t_...", "outcome": "completed", "result_present": true}})
```

New anti-drift test (`test_completion_call_example_satisfies_validate_completion`)
mirrors the existing `test_completion_requirements_satisfy_validate_completion`
pattern: parses the literal example line back out of a real task body
and asserts it passes `validate_completion` for every role, so this
example and the kernel can never silently disagree the way the prose
field list apparently still allowed a model to get lost in.

## Finding 2 (documented, not fixed here): the bot doesn't reliably pass caller-requested parameters

The user explicitly asked the bot, in the same Telegram message that
triggered this test, to set `worker_quorum` -- confirmed by reading the
raw message text directly from `state.db`. The resulting swarm's
topology blackboard shows `"worker_quorum": null`. The bot never
passed it. This is not a `kanban_swarm`/`create_swarm` bug (the
parameter works correctly once actually supplied, as PR #97's own
testing and this session's later manual-archive-based validation both
confirm) -- it's the calling model failing to translate an explicit
user instruction into the corresponding tool argument. Out of scope for
a kanban-swarm code change; a prompting/model-capability concern.
Recorded here so a future investigator doesn't have to rediscover it.

## Finding 3 (documented, not fixed here): content drifts across each LLM handoff

Comparing the same joke's text across the three places it appears
(the worker's own blackboard comment, the verifier's restated version,
the synthesizer's restated version) found it changed at every hop --
e.g. claude's actual posted joke used the character 楓; the verifier's
restatement swapped it to 槁 in one comment and 朜 in another; native-
hermes's actual joke and the verifier's/synthesizer's restatements of
it were near-unrecognizable as the same text by the third hop. This is
a "game of telephone" effect from each stage re-summarizing the prior
stage's output in its own words rather than quoting it verbatim, not a
kanban_swarm mechanism bug. Worth a future prompt change (e.g.
instructing verifier/synthesizer to quote blackboard content verbatim
rather than paraphrase it) but out of scope here.

## What worked (re-confirming PR #97's core mechanism, not new)

- `worker_max_runtime_seconds` was correctly left unset by the bot this
  time (per the user's own instruction in the same message), and the
  lane-aware defaults from PR #94/#96 applied correctly: 300s for
  native_hermes, 600s for claude/grok/agy -- confirmed via
  `max_runtime_seconds` column values.
- `native_hermes` genuinely timed out twice and gave up (`gave_up`
  event, not a fabricated status) -- exactly the scenario PR #97 exists
  for, just without a quorum configured for this particular swarm
  (Finding 2).
- Manually running `kb.archive_task` on the gave-up `native_hermes`
  task (standing in for what `excuse_blocked_workers_below_quorum`
  would have done automatically had a quorum been set) immediately
  promoted the verifier to `ready` via the ordinary `recompute_ready`
  path, and the live gateway's own dispatcher picked it up and started
  running it within the same tick -- direct, live re-confirmation that
  the underlying "archive a gave-up worker unblocks the verifier"
  mechanism from PR #97 works exactly as designed.

## Verification

- `validate_completion(task, metadata=..., result=...)` run directly
  against the real stuck task's real contract, confirming the
  synthesizer's "kernel bug" self-diagnosis was false.
- New test `test_completion_call_example_satisfies_validate_completion`:
  parses the literal example rendered into a real task body for every
  role and asserts it passes `validate_completion`.
- New test `test_completion_call_example_distinguishes_top_level_from_metadata_fields`:
  asserts `result`/`summary` never render nested inside the `metadata`
  object in the example, directly targeting the ambiguity that caused
  the real failure.
- 31 tests pass in `test_kanban_swarm.py`; 198 across
  `test_kanban_swarm.py`, `test_kanban_cli.py`, and `test_kanban_tools.py`.
