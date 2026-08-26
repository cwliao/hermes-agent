---
title: "Kanban execution guard: the real completion-state check is gated behind a brittle prose classifier on the user's own request text, and never runs when phrasing doesn't match"
status: DESIGN_REVISED
date: 2026-08-26
type: design-proposal
target_repo: hermes-agent
---

## Revision note (post cross-review, 2026-08-26)

4 engines (claude, codex, agy, grok) independently converged on the same
finding: the original Scope's proposed entry trigger
(`_mutation_attempted`, which matches `KANBAN_MUTATION_TOOLS =
{"kanban_swarm", "kanban_create"}`) is broader than intended. Traced
exactly by all 4 reviewers: an ordinary, successful single-task
`kanban_create` turn with non-matching prose would newly enter the deep
verification logic, fail `_successful_swarm_payload` (which only accepts
`kanban_swarm` receipts), fall through to the `_mutation_attempted`
no-successful-swarm-receipt block, and get its correct reply silently
replaced with `KANBAN_EXECUTION_BLOCKED` — a real regression breaking
every ordinary Kanban task creation that doesn't happen to match the
four-lane prose classifier.

Fixed below: only the **entry trigger** needs a swarm-specific signal —
whether a `kanban_swarm` call (successful or failed) was attempted this
turn. A new `_swarm_attempted()` helper (same shape as
`_mutation_attempted` but matching only `KANBAN_SWARM_TOOL`) is added as
an alternate entry trigger alongside the existing `prose_trigger`.

## Revision note 2 (post round-2 cross-review, 2026-08-26)

Round 2 (claude, codex, grok; agy also flagged the same underlying doc
inconsistency but still said APPROVE, so did not carry) found the
Revision-note-1 draft went one step too far: it also swapped the
**no-successful-receipt fallback block** from `_mutation_attempted` to
`_swarm_attempted`. That swap is unnecessary — for the round-1 regression
(ordinary `kanban_create`, non-matching prose), `swarm_trigger` is
already `False`, so the turn never reaches the fallback block at all
regardless of which check it uses; the entry-gate fix alone is
sufficient. Worse, the swap silently changes an *existing, intentional*
behavior: today, a turn where four-lane prose matches (`prose_trigger`
true) and the model calls only a successful `kanban_create` (never
`kanban_swarm`) is correctly `blocked` via `_mutation_attempted`
(`kanban_create` is a mutation tool, no successful swarm payload exists).
Swapping to `_swarm_attempted` would turn this into a `nudge` instead
(re-prompting the model to call `kanban_swarm`, after it already wrote a
`kanban_create` mutation to the board) — a real, untested, undocumented
behavior change with a real risk of prompting a second, redundant
mutation on retry. `_mutation_attempted` has no misleading
"swarm-context-only" docstring (Revision-note-1's claim to that effect
was wrong, per round 2 — the actual explanatory comment lives on
`_failed_mutation_tools` and does not scope `_mutation_attempted` that
narrowly).

Fixed: **only the entry gate changes** (adds `swarm_trigger` as an
alternate OR condition alongside `prose_trigger`). The no-receipt
fallback block is left exactly as it is today, keyed on
`_mutation_attempted`, unchanged. This is strictly sufficient for the
round-1 regression (ordinary `kanban_create` with non-matching prose
never reaches this block either way, since `swarm_trigger` is false for
it) and preserves today's four-lane-prose-plus-create-only `blocked`
behavior exactly.

Also corrected: Revision-note-1 claimed a successful-but-non-four-lane
`kanban_swarm` (e.g. 2 workers) would "fall through to pass-through
behavior" once reachable via `swarm_trigger`. This was wrong and
contradicted the doc's own Scope code and Validation criterion 6, which
both correctly specify `"blocked"` for this case (via the *unchanged*
`_mutation_attempted` fallback, since `kanban_swarm` is itself in
`KANBAN_MUTATION_TOOLS`). Criterion 6 is authoritative; the earlier
"pass-through" wording above was a documentation error, now removed.

Scope below is revised accordingly — entry-gate-only change.

## Motivation

Found live, 2026-08-26, testing a Telegram-triggered 4-lane kanban swarm
after the gpt-oss-120b production cutover. The model:

1. Actually called `kanban_swarm` for real (confirmed via the kanban DB:
   root card `t_7132bfcc` and its 4 workers/verifier/synthesizer children
   all genuinely exist, `hermes kanban runs` shows one real
   `swarm-orchestrator` run that created the topology).
2. Then, in the same reply, fabricated a complete success narrative:
   4 specific one-sentence color descriptions attributed to each lane, a
   verifier pass, and a synthesizer output saved to
   `/tmp/color_summary.txt` — none of which happened. Independently
   verified: all 4 workers are still `status=running` with
   `completed_at=None` and zero `result`/`comments`; verifier and
   synthesizer are still `status=todo`; `/tmp/color_summary.txt` does not
   exist on disk.

This is exactly the failure mode `agent/kanban_execution_guard.py` exists
to prevent (module docstring: "The model may describe a swarm convincingly
without ever calling the mutation tool... a lane-bound request is accepted
only after a successful `kanban_swarm` tool receipt"), and the module
*does* contain the right check for this specific case —
`_read_swarm_completion_state()` reads live verifier/synthesizer state
from the kanban DB and only allows a response to claim `"complete"` when
`synthesizer.status == "done"` and the result is non-empty. This check was
never reached.

## Root cause (confirmed via direct code read)

`try_finalization()` (`agent/kanban_execution_guard.py:258-352`) is the
sole entry point wired into the conversation loop
(`agent/conversation_loop.py:8586`). Its very first statement is the
entire gate for whether any of the module's real verification logic runs
at all:

```python
current_user = messages[current_user_idx] if 0 <= current_user_idx < len(messages) else {}
if not isinstance(current_user, Mapping) or not request_requires_four_lane_swarm(current_user.get("content")):
    agent._kanban_execution_guard_phase = ""
    return "pass"
```

`request_requires_four_lane_swarm()` (`kanban_execution_guard.py:62-69`)
is a narrow, simultaneous 4-condition natural-language classifier run
against the **user's own request text** (not the model's tool calls or
response):

```python
def request_requires_four_lane_swarm(value: Any) -> bool:
    text = _text(value).casefold()
    lane_hits = sum(1 for lane in LANE_IDS if lane in text)
    has_lane_shape = "lane" in text and ("四條" in text or "4" in text)
    has_independent_outputs = "各自獨立" in text or "獨立產出" in text or "獨立産出" in text
    has_swarm_stage = any(word in text for word in ("verifier", "synthesizer", "kanban", "swarm"))
    return lane_hits >= 3 and has_lane_shape and has_independent_outputs and has_swarm_stage
```

If the user's phrasing does not happen to contain 3+ of the literal lane
name substrings, the word "lane" plus "4"/"四條", one of the three
"獨立" phrasings, **and** a swarm-stage keyword — all four, simultaneously
— `try_finalization` returns `"pass"` on its first line. None of the
following logic runs: not `_failed_mutation_tools`, not
`_successful_swarm_payload`, not `_read_swarm_completion_state`. The
model's fabricated final response is delivered to the user completely
unguarded, even though the transcript for that exact turn contains a
100%-real, verifiable `kanban_swarm` tool receipt that the guard's own
downstream logic could have checked against the live kanban DB and
correctly found incomplete.

In the incident, the user's actual Telegram request phrasing is not
captured in the logs reviewed for this ticket, but the DB evidence proves
the swarm creation happened for real, and if `request_requires_four_lane_swarm`
had returned `True`, `_read_swarm_completion_state` would have correctly
forced `KANBAN_EXECUTION_PENDING` instead of the fabricated success
message.

**Not a bug in `_read_swarm_completion_state`, `_successful_swarm_payload`,
or `_failed_mutation_tools`** — all three were read in full and are
correct. The bug is purely that they never execute unless a fragile prose
classifier on the *user's* wording happens to match, when a far more
reliable signal — whether *the model itself actually called
`kanban_swarm`/`kanban_create` this turn* — is already computed by
`_mutation_attempted()` and `_successful_swarm_payload()` a few lines
later in the same function, just never consulted before the early return.

## Scope

Add a new swarm-specific helper, adjacent to the existing
`_mutation_attempted` (same shape, narrower tool match):

```python
def _swarm_attempted(messages: Sequence[Mapping[str, Any]], current_user_idx: int) -> bool:
    for message in messages[current_user_idx + 1 :]:
        if isinstance(message, Mapping) and any(
            _call_name(call) == KANBAN_SWARM_TOOL for call in _calls(message)
        ):
            return True
    return False
```

Change `try_finalization`'s entry gate to also trigger on this swarm-
specific signal, **in addition to** the existing prose-based trigger (not
instead of it — keep `request_requires_four_lane_swarm` as an alternate
trigger so intent expressed before any tool call, e.g. immediately after
a `nudge`, is still covered):

```python
current_user = messages[current_user_idx] if 0 <= current_user_idx < len(messages) else {}
prose_trigger = isinstance(current_user, Mapping) and request_requires_four_lane_swarm(current_user.get("content"))
swarm_trigger = isinstance(current_user, Mapping) and _swarm_attempted(messages, current_user_idx)
if not prose_trigger and not swarm_trigger:
    agent._kanban_execution_guard_phase = ""
    return "pass"
```

**Round-3 fix (codex)**: `swarm_trigger` must only be computed when
`current_user` is valid (the same `isinstance(current_user, Mapping)`
guard already used for `prose_trigger`) — `current_user_idx` can
legitimately be `-1` (per `reanchor_current_turn_user_idx()`), and an
unconditional `_swarm_attempted(messages, -1)` would scan
`messages[-1+1:]` = `messages[0:]`, the **entire** message history,
potentially misattributing a historical swarm call from a previous turn
as belonging to the current one. Gating `swarm_trigger` behind the same
validity check `prose_trigger` already uses preserves today's
invalid-index-means-pass behavior exactly.

The no-successful-receipt fallback block (`_mutation_attempted`-keyed,
around today's line 315-319) is **left completely unchanged** — do not
swap it to `_swarm_attempted`. It is unreachable for the round-1
regression case (ordinary `kanban_create`, non-matching prose: both
`prose_trigger` and `swarm_trigger` are false, so the entry gate already
returns `"pass"` before this block), and keeping it as
`_mutation_attempted` preserves today's correct `blocked` outcome for a
four-lane-prose turn where the model calls only `kanban_create` and never
`kanban_swarm`.

`_successful_swarm_payload` and `_failed_mutation_tools` are unchanged —
both already correctly scoped (swarm-only, and swarm-and-create-aware
respectively).

**Explicitly not in scope:**
- `request_requires_transactional_delivery()` (`kanban_execution_guard.py:71-83`)
  is a separate, wider classifier used only by `gateway/run.py:132-134`
  for a different purpose (whether to buffer/delay streaming of ordinary
  Kanban-mutation prose, not just four-lane swarms) — confirmed via
  grep as the only other caller of either classifier in the repo. Do not
  change its behavior or the `_KANBAN_MUTATION_WORDS` list it uses.
- `request_requires_four_lane_swarm` itself is not being loosened or
  changed — it keeps its existing (narrow, prose-based) behavior
  unchanged as one of two OR'd triggers, not replaced.
- No change to `_failed_mutation_tools`, `_successful_swarm_payload`,
  `_read_swarm_completion_state`, or any of the three terminal message
  constants (`KANBAN_EXECUTION_BLOCKED`/`_PENDING`/`_NUDGE`).

## Validation criteria

1. **Incident reproduction**: a turn where the user's request text does
   NOT match `request_requires_four_lane_swarm` (e.g. plain "跑一個顏色
   swarm 測試" with no "各自獨立"/lane-count phrasing) but the model DOES
   call `kanban_swarm` successfully this turn, with verifier/synthesizer
   still `todo` in the DB → `try_finalization` must return `"pass"` with
   `final_msg["content"]` rewritten to `KANBAN_EXECUTION_PENDING`, not
   silently pass through the model's fabricated completion text.
2. **Ordinary-create regression guard (the gap cross-review found)**: a
   turn where the user's text does NOT match the four-lane prose
   classifier and the model calls plain `kanban_create` successfully (no
   `kanban_swarm` call at all) → `try_finalization` must return `"pass"`
   with `final_msg["content"]` **unchanged** (not rewritten to
   `KANBAN_EXECUTION_BLOCKED`), and `agent._kanban_execution_guard_phase`
   left at `""`. Same for a *failed* plain `kanban_create` with
   non-matching prose — must also stay untouched by this guard (that
   failure surfaces through whatever normal error-handling path already
   existed for it, not this guard).
3. Regression: existing four-lane-phrasing-triggered test cases in
   `tests/run_agent/test_kanban_execution_guard.py` (7 tests, confirmed
   present) must still pass unchanged — they all use the four-lane
   `PROMPT` fixture and enter via `prose_trigger`, unaffected by the
   `swarm_trigger` addition.
4. Regression: a turn with NEITHER prose match NOR any `kanban_swarm`
   call this turn (ordinary conversation, or an ordinary `kanban_create`
   turn per #2) → still returns `"pass"` immediately, unchanged behavior.
5. Regression: a turn where `swarm_trigger` is true but the `kanban_swarm`
   call failed (`_failed_mutation_tools` non-empty) → returns `"blocked"`
   with `KANBAN_EXECUTION_BLOCKED`, now reachable via `swarm_trigger` even
   without matching prose (this is new coverage the original bug also
   missed — a failed swarm attempt with non-matching prose previously
   passed through unguarded too).
6. Secondary case (grok): a successful but non-four-lane `kanban_swarm`
   receipt (e.g. 2 workers, `_successful_swarm_payload` returns `None`
   because `len(worker_ids) < 4`) with non-matching prose → `swarm_trigger`
   is true, enters deep logic, falls through to the (unchanged)
   `_mutation_attempted`-keyed no-receipt block, returns `"blocked"`.
   This matches today's behavior for prose-matching turns with the same
   shape exactly — not a pass-through, `"blocked"` is the correct and
   intended outcome here.
7. **Required regression test (round-2 finding)**: a turn where
   four-lane prose DOES match `request_requires_four_lane_swarm`
   (`prose_trigger=True`) and the model calls only a successful
   `kanban_create` (no `kanban_swarm` call at all) → must still return
   `"blocked"` with `KANBAN_EXECUTION_BLOCKED`, exactly as today. Must
   NOT become `"nudge"`. This is the case round 2 found would silently
   change if the no-receipt block were also swapped to
   `_swarm_attempted` — confirming the block is correctly left on
   `_mutation_attempted` is the primary purpose of this test.

## Required before implementation

Round 1 cross-review (claude/codex/agy/grok) found and precisely traced
the ordinary-`kanban_create`-over-block regression in the original
Scope's `_mutation_attempted`-based entry trigger. Round 2 cross-review
(claude, codex, grok — agy said APPROVE but on a doc reading later
corrected by the others) found the round-1 fix over-corrected by also
swapping the no-receipt fallback block, which was unnecessary and
introduced its own undocumented behavior change. Revision note 2 above
reverts the no-receipt block to unchanged `_mutation_attempted`, keeping
only the entry-gate `swarm_trigger` addition. Re-review this narrower,
entry-gate-only Scope before implementation.
