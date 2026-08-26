---
title: "Kanban swarm: goal-mode judge evaluates worker completions against the whole swarm's goal, not the worker's own deliverable"
status: DESIGN_REVISED
date: 2026-08-26
type: design-proposal
target_repo: hermes-agent
---

## Revision note (post cross-review, 2026-08-26)

4 engines (claude, codex, agy, groq — grok was busy) independently
converged: root-cause diagnosis confirmed exactly as written, but **the
original Scope's fix location was too narrow** — patching only
`hermes_cli/kanban.py` leaves the bug live in two other real call paths:

1. **`tools/kanban_tools.py` has a duplicate `_goal_mode_handoff_rejection`
   implementation** (confirmed by codex and agy independently, lines
   ~370-400, called from `kanban_complete`/`kanban_request_review` tool
   handlers at ~1038/~1263/~3027-3029) — this is the path an *in-agent tool
   call* uses (as opposed to the `hermes kanban complete` CLI this
   incident's manual workaround used). Fixing only `kanban.py` would leave
   every real worker's own in-loop completion attempt subject to the exact
   same false rejection.
2. **`cli.py`'s Ralph/goal-mode driver loop also builds the judge goal from
   the full title+body** (codex: `cli.py:20785-20801` →
   `hermes_cli/goals.py:2188-2256`; agy: `cli.py:20798-20803`) — every
   intermediate turn of a goal-mode worker is judged against this same
   over-broad string, not just the final handoff.

Also refined, not contradicted:
- Root cause and both original call sites (CLI `complete`/`request-review`)
  confirmed exactly as described — no correction needed there.
- **Better mechanism than free-text parsing** (claude, agy, groq all
  independently suggested variants of this): reuse the codebase's existing
  `[swarm:contract]` marker-line convention (`CONTRACT_PREFIX`,
  `extract_contract()`, `kanban_swarm.py:30,530-541`) rather than inventing
  prose-parsing of a body that also contains the protocol block and Goal
  line. Add a structured `acceptance` field to the per-worker contract dict
  already written at `kanban_swarm.py:915-926`, populated from a genuinely
  new input (extending `--worker PROFILE:TITLE[:SKILL]` with a 4th segment,
  or a `workers[].acceptance` field on the `kanban_swarm` tool — the tool
  path already accepts a richer `workers[].body`, per codex/agy; the CLI
  parser is what currently collapses `body` down to just the title,
  `kanban_swarm.py:1383-1394`).
- **Quorum docstring needs a precision fix, not a contradiction fix**
  (codex, corroborated by agy): `worker_quorum=None` disabling
  `excuse_blocked_workers_below_quorum()` is accurate, but a *separate*,
  unconditional response-deadline excusal mechanism
  (`kanban_swarm.py:1237,1273,1315-1333`, feeding
  `_effective_expected_lane_count()` at `:552-569`) still applies
  regardless of quorum — the docstring's "completely unaffected"/"no
  graceful degradation" wording overstates it. Update the docstring's
  wording only; this doesn't change the fix's scope.
- **Keep the judge gate, don't bypass it for worker cards** — all 4
  reviewers independently rejected the bypass alternative: the verifier
  stage checks cross-lane evidence sufficiency, not per-worker goal
  compliance (`validate_completion()`, `kanban_swarm.py:582-609`, per
  codex), so removing the worker-level gate would remove the only
  per-worker semantic check that currently exists.
- **No regression risk found** in the proposed fallback (full-body judging
  preserved for non-worker/legacy cards) — nothing in the codebase relies
  on worker cards specifically being judged against the full body.

Scope below is revised accordingly: all 3 call sites, contract-field
mechanism (not prose parsing), concrete `--worker` extension, and the
docstring precision fix.

## Motivation

Found live, 2026-08-26, testing kanban swarm dispatch after a production
model cutover (unrelated to this bug's root cause). A 4-lane
(`native_hermes`/`claude`/`grok`/`agy`) swarm's `grok` worker timed out
twice (a real, separate issue — the swarm-creation prompt explicitly set
`worker-max-runtime` to 300s, overriding the lane-aware 600s default for
external CLI lanes; not a bug, out of scope here). Attempting to manually
complete that worker via `hermes kanban complete t_b8b49692 --result
"綠色代表大自然、生機與平靜。..."` was rejected:

```
kanban: goal completion of t_b8b49692 rejected by judge: Only the green
description was provided; the overall swarm task of gathering all colors
and reporting is not yet completed.
```

The rejection reasoning is accurate *about the text it was given* — but
that text was the entire swarm's goal, not this worker's own job.

## Root cause (confirmed via direct code read)

1. **`_swarm_context()`** (`hermes_cli/kanban_swarm.py:216-249`) builds every
   worker card's body by appending the swarm protocol block, which ends
   with the literal line `f"- Goal: {goal.strip()}\n"` — the *entire*,
   unmodified swarm-level goal string, not a per-worker summary. A worker
   card's body is therefore: `{worker title}\n\n{swarm protocol
   block}\n- Goal: {full swarm goal}`. In this run, the title was the
   4-character string `"green-worker"` — there is no other field carrying
   a worker-specific acceptance description anywhere in the card.

2. **`_goal_mode_handoff_rejection()`** (`hermes_cli/kanban.py:2623-2652`)
   is the judge gate applied to both `kanban complete`
   (`_cmd_complete`, `kanban.py:2712-2723`) and `kanban request-review`
   (`kanban.py:2870-2883` — confirmed both call sites share this exact
   function, so this bug affects both handoff paths identically). It calls:
   ```python
   judge_goal(
       goal=f"{task.title}\n\n{task.body or ''}".strip(),
       last_response=evidence.strip(),
   )
   ```
   i.e. it hands the judge the *entire* card body — including the full
   swarm-level goal from point 1 — as "the goal this evidence must satisfy".
   There is no extraction step that isolates a worker-specific acceptance
   criterion from the swarm-wide context that's also present in the same
   body for the worker's own reference while doing its job.

**Net effect**: any worker in a lane-bound swarm whose card body doesn't
happen to restate its own narrow deliverable in a way distinguishable from
the swarm's full goal — which, per point 1, is currently *no* worker card,
since `_swarm_context()` never writes anything more specific than the
appended full goal — will have its completion judged against "did you
single-handedly accomplish everything every worker/verifier/synthesizer in
this swarm is supposed to accomplish", not "did you do your own job". A
worker that did its actual, narrow job correctly can be rejected for not
having also verified color-uniqueness or synthesized the other three
workers' output — work explicitly assigned to *other* cards in the same
swarm.

## Scope (draft — needs cross-review before implementation)

1. Give each worker card a distinct, explicitly-labeled per-worker
   acceptance line, separate from the swarm-wide Goal line already present
   for context — e.g. in `_swarm_context()` or wherever worker cards are
   assembled, add something like `f"- Your deliverable (this worker only):
   {worker_spec.title}\n"` immediately before the existing `- Goal: ...`
   line, keeping the full swarm goal for situational context but giving
   the judge (and the worker itself) an unambiguous narrower target.
   `worker_spec.title` alone (e.g. `"green-worker"`) is likely too terse to
   be useful as a judge-facing acceptance line on its own — may need a
   richer per-worker deliverable description threaded through
   `create_swarm`'s `--worker PROFILE:TITLE[:SKILL]` spec, or derived from
   the swarm goal by whatever already decomposes the goal into per-color
   sub-tasks (if anything does — needs investigation, not assumed here).
2. Change `_goal_mode_handoff_rejection()` to build the judge's `goal`
   argument from the new narrower field when present on a worker card,
   falling back to today's full-body behavior for non-worker cards
   (verifier/synthesizer/root, and non-swarm goal-mode tasks generally,
   where judging against the full task body is presumably correct and
   should NOT change).
3. Apply symmetrically to both call sites (`_cmd_complete` and the
   `request-review` handler) since both share this function.
4. Explicitly out of scope, needs its own separate decision: whether the
   "no `worker_quorum` set → require every lane, no graceful degradation"
   behavior (`kanban_swarm.py:787-790` docstring, confirmed intentional
   design, not a bug) should also change its *default* — that's a distinct
   product-behavior question, not a fix to this bug, and was explicitly
   NOT the root cause of the incident that surfaced this ticket (the actual
   blocker was the judge rejection above, encountered while trying to
   manually route around the quorum-less deadlock).

## Explicitly not solved here

- The `grok` timeout itself (root cause: this swarm's creation explicitly
  set `worker-max-runtime=300` for all lanes, overriding the correct
  lane-aware 600s default for external CLI lanes — a caller error in how
  this particular swarm was requested, not a code defect).
- Whether swarms should gracefully degrade below full quorum by default
  (see Scope point 4).

## Required before implementation

Cross-review this design (claude/codex/agy/grok/groq per this
investigation's established practice) before any code change — the
per-worker-acceptance-field design (Scope point 1) has real product
implications (what should the judge actually hold a worker accountable
for, precisely) that deserve more than one perspective before committing
to a specific field shape.
