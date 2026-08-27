status: CONSENSUS (claude and agy independently verified; groq's reply
contained fabricated code snippets -- a wrong ternary, nonexistent
`KANBAN_DEFAULT_WORKER_TIMEOUT`/schema `minimum`/`maximum` keys,
`InvalidArgumentError` -- disregarded; codex and grok timed out on
dispatch, review-only so no risk). See t_ad8046c2.

## Consensus outcome

Both reviewers independently confirmed the override-precedence code and
schema-staleness claims by reading the cited lines directly -- no
disagreement there. agy went further and found the exact mechanism this
draft's original "most plausibly" framing could only speculate about --
confirmed above in the corrected root-cause section (the literal
compacted "Constraints & Preferences" entry). claude independently
established that option 3's "only causes retries, benign" framing
undersells the real impact -- traced `enforce_max_runtime()`'s
circuit-breaker mechanics and confirmed a stale-too-short cap plausibly
escalates to genuinely `blocked` tasks needing manual intervention
(matching the 3-5-manual-unblocks-per-incident pattern observed twice
today), not a self-healing retry loop.

**Recommendation: Option 1, refined given the confirmed mechanism.**
Rather than generic "don't override without reason" guidance, target the
actual failure mode: add `KANBAN_GUIDANCE` text (near the `kanban_swarm`
orchestrator-mode section, `agent/prompt_builder.py:305-317`) telling the
model that when a compacted conversation summary's "Constraints" section
specifies a runtime/timeout value for kanban swarm workers, that value
may be stale relative to the tool schema's live, dynamically-generated
defaults -- the schema's stated default should be preferred unless the
CURRENT turn's user message explicitly restates the constraint. This is
more precise than a blanket "avoid overriding" rule (which could
discourage legitimate current-turn overrides) and directly targets the
confirmed mechanism (a stale compacted "Constraint" silently outliving
the platform value it was based on).

Option 2 (tool-side clamping/validation of low override values) was
raised by groq but its supporting code citations were fabricated and its
premise (hard-reject sub-600s values) risks rejecting genuinely
legitimate operator overrides for fast/lightweight workers -- not
adopted. Option 3 (do nothing) is not recommended given claude's
circuit-breaker-escalation finding -- this is not a low-cost nuisance.

# Model explicitly overrides worker_max_runtime_seconds to a stale 300s value on every kanban_swarm call

## Correction to this ticket's original hypothesis

The ticket that opened this investigation assumed the 300s came from a
non-lane-mode fallback path in `hermes_cli/kanban_swarm.py`/`kanban_db.py`.
That hypothesis is **wrong** -- confirmed by direct evidence, not
speculation:

1. `hermes_cli/kanban_db.py`'s `enforce_max_runtime()` query is
   `WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL ...`
   -- a task row with `max_runtime_seconds` genuinely NULL is structurally
   excluded from ever being timed out by this function. There is no
   Python-level `row["max_runtime_seconds"] or 300` fallback anywhere in
   the file (checked every downstream read).
2. `hermes kanban show <id> --json` reported `max_runtime_seconds: None`
   for the affected tasks, which is itself misleading/possibly a CLI
   display bug (see Non-goals) -- the **raw sqlite column** (queried
   directly against `/home/cwliao/.hermes/kanban.db`) shows
   `max_runtime_seconds = 300`, a real, explicit stored value, not NULL.
3. Traced the actual `kanban_swarm` tool call arguments from the live
   Telegram session's message history (`state.db`, session
   `20260827_132520_bb4ffa`, three separate calls at 13:25, 13:26, and
   15:16 today, all identical in this respect): every single call
   explicitly includes `"worker_max_runtime_seconds": 300` in its JSON
   arguments, alongside `"max_runtime_seconds": 1200` (the swarm-level
   deadline, correctly using the new value) and correctly-populated
   `lane_id` on every worker (`native_hermes`/`claude`/`grok`/`agy` --
   `lane_mode` WAS true; that part was never broken).
4. `tools/kanban_tools.py`'s `_create_swarm_uncommitted()` worker-creation
   ternary is: `spec.max_runtime_seconds if spec.max_runtime_seconds is
   not None else ((worker_max_runtime_seconds if worker_max_runtime_seconds
   is not None else _default_worker_max_runtime_seconds(worker_lane)) if
   lane_mode else None)`. Since the caller explicitly supplies
   `worker_max_runtime_seconds=300`, that value wins over
   `_default_worker_max_runtime_seconds()`'s 1200s entirely -- this is
   the ternary working exactly as designed; there's nothing wrong with
   the resolution logic itself.
5. `KANBAN_SWARM_SCHEMA`'s `worker_max_runtime_seconds` field description
   (`tools/kanban_tools.py:3158-3164`) is dynamically generated from the
   live constants and is currently correct: "Omit to use the lane-aware
   default: 1200s for native_hermes, 1200s for claude/grok/agy ..." --
   the schema text itself is NOT stale or misleading. The model is
   choosing to override this explicit "omit it" guidance on its own.

**Actual root cause, confirmed directly (not inferred)**: read message id
`15092` in `state.db` directly (the lowest/oldest row for session
`20260827_132520_bb4ffa`) -- it is a `[CONTEXT COMPACTION — REFERENCE
ONLY]` handoff summary, and its `## Constraints & Preferences` section
contains, verbatim and bolded:

> **每個 worker 的超時上限設為 300 秒。**（"每個 worker" = every worker;
> "超時上限設為 300 秒" = timeout cap set to 300 seconds）

This is not vague model "stickiness" -- it is a **literal, structured
constraint baked into this specific session's own compacted memory**,
most likely captured from an earlier turn (before today's
`DEFAULT_WORKER_MAX_RUNTIME_SECONDS` raise, commit `3f16898d3a`, when
300s genuinely was the platform default and something in that earlier
turn stated or implied it as a requirement). The compaction summarizer
correctly preserved it as a "Constraint," and the model has since been
faithfully honoring its own remembered instructions on every subsequent
`kanban_swarm` call in this same session (13:25, 13:26, 15:16 today) --
this is arguably *correct* model behavior given what it believes its
constraints are, not a hallucination or context-window pattern-matching
error. The tool schema's live "omit to use 1200s" guidance
(`tools/kanban_tools.py:3158-3164`, confirmed accurate) is simply losing
out to a more specific, session-local "constraint" the model considers
higher-priority.

This produced a real, repeated user-facing cost: 2 separate live swarm
runs today each needed 3-5 manual `hermes kanban unblock` interventions
over 15-20 minutes to push through workers that kept hitting the 300s
cap. One reviewer (claude) traced through `enforce_max_runtime()`'s
circuit-breaker mechanics and confirmed this is not a benign
auto-retry-and-forget pattern -- lane workers structurally need more than
300s (per this codebase's own historical comments), so a retry under the
same stale 300s cap is likely to fail again and eventually trip the
consecutive-failure breaker into a genuinely `blocked` state requiring
manual intervention, exactly as observed.

This is fundamentally a **session-scoped memory artifact** (it will
resolve on its own once this specific session's context ages past this
compaction boundary or gets compacted again without re-preserving the
stale constraint) rather than a generally-reproducible defect -- but
since compacted "Constraints" tend to persist across multiple subsequent
compactions once anchored, it's not safe to assume this self-resolves
quickly, and the same failure mode (an earlier turn's now-stale
observation getting compacted into a persistent "Constraint" that
silently overrides a later-changed platform default) could recur for any
tunable default this codebase adjusts in the future, not just this one
value.

## Proposed fix

This is not a resolver-logic bug -- the ternary correctly honors an
explicit caller override, which is the right general design (an operator
who deliberately wants a shorter/longer per-worker cap should be able to
set one). The fix needs to discourage a model from gratuitously
overriding it with a stale value, without removing the legitimate
override capability. Options, for reviewers to weigh:

1. **Add explicit `KANBAN_GUIDANCE` text** (`agent/prompt_builder.py`,
   near the `kanban_swarm` orchestrator-mode section,
   ~line 305-317) telling the model: do not set
   `worker_max_runtime_seconds` unless you have a specific, current reason
   to override the platform default -- omit it and let the tool apply its
   own lane-aware default, which already reflects the correct current
   value. This is the most direct fix for the actual observed behavior
   (a model habitually re-supplying an old value) but is soft guidance,
   not a hard guarantee -- consistent with this session's general
   principle that this class of fix is still better than nothing given
   the low blast radius of a slightly-too-short worker timeout (it just
   causes retries, not silent failures or data loss).
2. **Tool-side staleness guard** (more invasive, needs care): if
   `worker_max_runtime_seconds` is explicitly set to a value at or below
   some suspiciously-low threshold (e.g. equal to a known historical
   default that's since been raised), log a warning or nudge rather than
   silently honoring it verbatim. Risk: this could be seen as
   second-guessing legitimate operator overrides, and picking the right
   threshold/heuristic is fragile. Reviewers should weigh whether this is
   worth the complexity given option 1 addresses the actual observed
   cause more directly.
3. **Do nothing / reject**: is a model occasionally overriding a runtime
   cap with a suboptimal-but-not-crazy value (300s is not zero or
   negative, just tight) actually worth guidance for, given it only
   causes retries (handled automatically by the dispatcher, at the cost
   of wall-clock time and, in a live interactive test, manual operator
   patience)? Reviewers should sanity check whether the repeated
   real-world cost observed today (2 separate live incidents, 3-5 manual
   unblocks each) justifies a fix, or whether this is a one-off artifact
   of this specific very-long, multiply-compacted session that would not
   recur in a fresh session.

Recommend option 1 as the minimal, low-risk fix given the evidence, with
option 3's question genuinely open for reviewers -- this session's
own precedent (adding explicit guidance to `KANBAN_GUIDANCE` for the
swarm-substitution and swarm-duplication fixes earlier today) suggests
prompt-level guidance is this codebase's preferred first response to a
model behavioral gap before reaching for a heavier tool-level guard.

## Non-goals

- Not fixing `hermes kanban show --json`'s apparent display discrepancy
  (`max_runtime_seconds: None` shown for a task whose raw DB column is
  `300`) -- this is a separate, lower-priority CLI/display bug, not
  load-bearing for this fix, and wasn't the actual cause of the timeout
  (the raw DB value is what `enforce_max_runtime()` actually reads).
  Worth a follow-up ticket, not blocking here.
- Not touching the worker-creation ternary's override-precedence logic
  itself (`spec.max_runtime_seconds` / `worker_max_runtime_seconds` /
  `_default_worker_max_runtime_seconds()`) -- it's working as designed.
- Not adding a hard block on any explicit `worker_max_runtime_seconds`
  value -- legitimate operator overrides must remain possible.

## Verification plan (post-fix)

- If option 1: no code-level test possible (prompt-only change) --
  manual verification: start a fresh (non-compacted) session, request a
  lane-bound swarm, confirm the model omits `worker_max_runtime_seconds`
  or supplies a reasonable value, not a stale 300.
- If option 2: unit test the staleness-detection heuristic in isolation,
  plus confirm a legitimate low-but-intentional override (e.g. 60s for a
  known-fast worker) isn't wrongly flagged.
- Follow-up ticket for the `hermes kanban show --json` display
  discrepancy, separate from this fix.
