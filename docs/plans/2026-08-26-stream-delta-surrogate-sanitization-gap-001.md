---
title: "Gateway stream deltas bypass the codebase's only surrogate-sanitization pass, which runs solely on the final accumulated response"
status: DESIGN_REVISED
date: 2026-08-26
type: design-proposal
target_repo: hermes-agent
---

## Revision note (post cross-review, 2026-08-26)

claude and agy independently reviewed the original Scope's "mirror
`_think_buffer`'s hold-and-resolve pattern to avoid corrupting a valid
UTF-16 surrogate pair split across a chunk boundary" requirement and gave
contradictory verdicts: claude accepted the premise but said the proposed
buffering would still ship broken output unless it did real UTF-16
surrogate-pair-to-codepoint recombination math; agy said the entire
premise is a misunderstanding of Python's Unicode model and no buffering
is needed at all.

Verified directly (not just trusting either review): in CPython 3, `str`
is a sequence of Unicode *scalar values* (code points), not UTF-16 code
units. A real emoji or rare CJK extension character encoded as a single
JSON string (whether as raw UTF-8 bytes or as a `\uXXXX\uXXXX` escaped
pair within one JSON string) always decodes to **exactly one Python code
point** — `len("😀") == 1`, `ord("😀") == 0x1F600`, confirmed unchanged
across a `json.dumps`/`json.loads` round-trip. Round-2 review (codex)
correctly sharpened this: that specific test proves a pair *within one
JSON string* always recombines, but does not by itself rule out two
independent SSE/JSON delta events each carrying one lone surrogate half
(`json.loads('"\ud83d"')` does yield a genuine lone-surrogate string) —
confirmed this is possible in principle if a provider ever emits
malformed output that way. It does not change the conclusion: even in
that case, `_sanitize_surrogates` has no cross-call reconstruction logic
today (concatenating two independently-sanitized lone surrogates from two
separate `on_delta` calls just yields two independent `U+FFFD`
replacements, matching what a single call to `_sanitize_surrogates` on
already-concatenated text would do) — so a stateless, per-delta call is
still both correct and consistent with the final-response path's
existing (non-reconstructing) behavior, whether the split-lone-surrogate
case is a real provider artifact or not.

Also verified: `_sanitize_surrogates()` itself has **zero pair-awareness**
today — manually constructing two adjacent lone surrogates
(`'\ud83d' + '\ude00'`) and running it through the existing regex-replace
still produces two separate `U+FFFD` replacements, not a reconstructed
character. So even if a genuinely invalid split-surrogate artifact somehow
reached this code (e.g. from a malformed tokenizer/detokenizer output
upstream, an actual data-corruption case, not a legitimate chunking
artifact), the final-response path doesn't try to recombine it either —
it just replaces both. Making the streamed path do the same
(stateless, per-chunk, no buffer) is the version that's actually
consistent with the existing final-response behavior this doc's own
stated goal is to match, not a buffered hold-and-resolve.

**Fixed**: drop the hold-and-resolve buffer entirely. Call
`_sanitize_surrogates()` statelessly on each raw delta inside
`on_delta()`, before it's queued. This also resolves claude's two other
findings as a side effect of not needing a buffer at all: `on_delta` being
a cross-thread callback (`agent's worker thread`, per its own docstring)
is a non-issue for a pure, stateless function with no shared mutable
state, and there's no held-back tail requiring an end-of-stream flush.

Scope below is revised accordingly.

## Motivation

Investigated while chasing a live incident: a Telegram user received a
garbled message containing literal `<|endoftext|>`-style special-token
text, cut off mid-sentence. Deep code tracing (Explore agent, full read of
`agent/tool_guardrails.py`, `agent/conversation_loop.py`,
`agent/turn_finalizer.py`, `gateway/stream_consumer.py`,
`agent/message_sanitization.py`) found:

- No code anywhere in this repo strips `<|endoftext|>`-style special
  tokens from provider output, streamed or final. The only sanitization
  pass that exists at all is `_sanitize_surrogates()`
  (`agent/message_sanitization.py:32-39`), which replaces lone UTF-16
  surrogate code points (`\ud800`-`\udfff`) with U+FFFD — a narrower,
  different concern (surrogates crash `json.dumps()` inside the OpenAI
  SDK; they are not the same thing as literal special-token text).
- That one sanitization pass runs exactly once per turn, on the fully
  accumulated `final_response` string, at `agent/turn_finalizer.py:731-732`
  — after the turn has already finished streaming.
- `gateway/stream_consumer.py`'s `GatewayStreamConsumer.on_delta()`
  (`:660`) receives each raw text delta from the agent's worker thread and
  live-edits it into the Telegram message via `_filter_and_accumulate()`
  (`:696+`). That function DOES already filter one category of unwanted
  content mid-stream — `<think>...</think>` reasoning blocks, via an
  explicit state machine — with a comment acknowledging exactly this
  class of gap: "The agent also strips them from the final response... but
  the stream consumer sends intermediate edits before that stripping
  happens." The same reasoning applies to surrogate sanitization, which
  received no equivalent stream-side treatment.

**Important scope note, stated plainly so this doc is not overclaimed**:
this investigation did **not** conclusively identify the mechanism that
produced the specific `<|endoftext|>` text the user saw — no code path
was found that would explain a literal special-token string appearing in
provider output at all (server-side tokenizer decode normally excludes
special tokens from `content` unless explicitly configured not to). This
ticket is scoped to the one confirmed, narrower gap: **lone surrogate code
points can reach a live-edited Telegram message unsanitized**, which is a
real defect independent of whether it explains that specific incident.
Root-causing the exact `<|endoftext|>` mechanism needs a live
reproduction with a stack/network capture, out of scope here (see the
"Explicitly not solved here" section).

## Root cause (confirmed via direct code read)

1. `_sanitize_surrogates()` (`agent/message_sanitization.py:32-39`) exists
   and is correct for what it does — replaces `[\ud800-\udfff]` with
   U+FFFD, a fast no-op when absent.
2. It is called from exactly one place in the finalize path:
   `agent/turn_finalizer.py:732`, `final_response =
   _sanitize_surrogates(final_response)` — operating on the complete,
   already-accumulated response string.
3. Streamed deltas take a different path entirely: `agent._execute...`
   (main agent loop) calls `agent.stream_delta_callback(text)` for each
   chunk as it arrives from the provider; on the gateway side this lands
   in `GatewayStreamConsumer.on_delta()` (`gateway/stream_consumer.py:660`),
   which queues the text and a drain loop live-edits it into the Telegram
   message. Nothing in this path calls `_sanitize_surrogates` or any
   equivalent.
4. Precedent for stream-side filtering already exists in the same file:
   `_filter_and_accumulate()` (`stream_consumer.py:696+`) runs a
   character-by-character state machine to strip `<think>` blocks from
   deltas *before* they're edited into the live message, specifically
   because waiting for final-response cleanup is too late for a
   live-streamed UI. The same logic gap applies to surrogates: a lone
   surrogate code point arriving mid-stream (plausible from a truncated
   multi-byte CJK sequence at a chunk boundary, or from a raw model output
   quirk) would be live-edited into the user's Telegram message with zero
   sanitization, and would only be caught (replaced with U+FFFD) in the
   final-response finalize pass — which, per `finish()`'s own docstring
   (`stream_consumer.py:672-680`), is adopted as the authoritative
   finalize payload and can differ from what was already live-edited into
   the chat. In other words: the already-sent, already-visible streamed
   text is never retroactively fixed even if the final-response
   sanitization pass would have caught it.

## Scope

Add a stateless, stream-side surrogate sanitization call in
`GatewayStreamConsumer.on_delta()` (`gateway/stream_consumer.py:660-671`),
applied to each raw text delta before it's put on the queue — no
buffering, no held-back state:

```python
from agent.message_sanitization import _sanitize_surrogates

def on_delta(self, text: str) -> None:
    if text:
        self._queue.put(_sanitize_surrogates(text))
    elif text is None:
        self.on_segment_break()
```

Also apply the identical stateless call to `on_commentary()`
(`stream_consumer.py:557-561`) — confirmed by round-2 review (all 3
responding engines) to carry the exact same gap: `gateway/run.py:5766`
and `agent/codex_runtime.py:1390` both feed raw, unsanitized
provider-derived text into `on_commentary()`, which queues it for
`_send_commentary()` to deliver directly via the platform adapter,
entirely bypassing both `on_delta()` and `turn_finalizer.py`'s
sanitization. This is firm scope, not conditional:

```python
def on_commentary(self, text: str) -> None:
    if text:
        self._queue.put((_COMMENTARY, _sanitize_surrogates(text)))
```

`_append_accumulated()` (`stream_consumer.py:453`) does **not** need
separate coverage — confirmed by round-2 review (claude, agy) that its
only callers (`_filter_and_accumulate`/`_flush_think_buffer`) exclusively
consume text that already passed through the now-sanitizing `on_delta()`
queue; it is downstream plumbing of `on_delta`, not an independent raw
entry point.

Also check `finish(final_text=...)`'s `_FINAL_TEXT` adoption path
(`stream_consumer.py:672-686`, `~924`) during implementation — codex's
round-2 review flagged that this early-return path (used by
`codex_app_server`, per its own docstring) may bypass the normal
`turn_finalizer.py:731-732` sanitization that the standard gateway path
relies on. If confirmed, sanitize `final_text` at adoption too (reusing
`_sanitize_surrogates`, same as everywhere else in this doc) so the
"gateway never live-delivers a lone surrogate" property holds regardless
of which finalize path a given turn takes.

**Constraints:**
- Do not change `_sanitize_surrogates` itself, `_filter_and_accumulate`'s
  existing think-block logic, or the final-response finalize path
  (`turn_finalizer.py:731-732`) — that pass stays as the authoritative
  final safety net regardless of what the stream-side fix does.
- No hold-and-resolve buffering across chunk boundaries — confirmed
  unnecessary (see Revision note above): a valid non-BMP character
  (emoji, rare CJK extension) is always a single Python code point from
  decode time onward and can never legitimately arrive as a split
  surrogate pair across two deltas, and `_sanitize_surrogates` itself has
  no pair-reconstruction logic to begin with, so per-chunk stateless
  sanitization is both correct and consistent with the final-response
  path's existing behavior.
- This only touches the gateway's live-editing display path
  (`gateway/stream_consumer.py`). Does not touch the CLI's own
  `_stream_delta` (mentioned in the existing think-block comment as a
  separate implementation) unless investigation during implementation
  finds it has the identical gap — if so, flag it as a candidate for a
  follow-up ticket rather than silently expanding this one's scope.

## Validation criteria

1. Unit test: a delta containing a lone surrogate code point → the text
   put on the queue has it replaced with U+FFFD, matching what
   `_sanitize_surrogates` produces on the same input directly.
2. Unit test: a delta containing a real non-BMP character (emoji like
   `😀`, a rare CJK extension character, a math symbol like `𝕏`) →
   passed through unchanged, whether it arrives whole within one delta or
   its surrounding text is split across multiple deltas at arbitrary
   points (since the character itself is always a single code point, no
   special boundary case exists for it — this test exists to confirm
   that fact empirically, not because a buffer is needed to protect it).
3. Unit test: two lone surrogate code points arriving in consecutive
   deltas → each is independently replaced with U+FFFD on its own delta,
   matching `_sanitize_surrogates`'s existing no-reconstruction behavior
   on the final-response path (no attempt to "combine" them across the
   two `on_delta` calls, since `_sanitize_surrogates` doesn't do that
   even within a single string).
4. Regression: existing `<think>`-block filtering tests (find and confirm
   whatever test file already covers `_filter_and_accumulate`) must still
   pass unchanged — this fix must not interfere with that state machine.
5. Regression: normal text with no surrogates at all — the fast no-op
   path in `_sanitize_surrogates` — must not add meaningful per-delta
   overhead (this runs on every streamed chunk of every live Telegram
   response, so the common case must stay cheap, matching
   `_sanitize_surrogates`'s own documented fast-path design).
6. If `on_commentary()`/`_append_accumulated()` are found during
   implementation to carry the same gap and get covered per the Scope's
   note, add equivalent tests for those call sites too.

## Explicitly not solved here

- Does not identify or fix the mechanism behind the specific
  `<|endoftext|>` incident that motivated this investigation — no code
  path was found that would produce literal special-token text in
  provider output at all. If this recurs, capture a live stack dump
  (`py-spy dump --pid <gateway-pid>`) or the turn's raw network request/
  response during the incident, not after — those are quoted anywhere.
- Does not add general special-token (`<|endoftext|>`, `<|im_end|>`,
  etc.) stripping anywhere in the codebase — that would be a different,
  separate ticket if a concrete reproduction shows it's needed, and
  should not be inferred from this investigation's inconclusive evidence
  on that point.
- Does not investigate the separate multi-minute-silence "wedge" symptom
  observed in the same testing session — traced to a different, known
  historical incident class (`gateway/run.py`'s `_dump_wedged_turn_stacks`
  reaper, built for an Aug-2026 WhatsApp wedge) that needs a live capture
  to confirm, not speculative fixing.
- Does not touch the CLI's separate `_stream_delta` implementation unless
  implementation-time investigation finds the identical gap there (see
  Scope).

## Required before implementation

Round 1 cross-review (claude, agy — codex/grok returned empty) gave
contradictory verdicts on the chunk-boundary handling question; resolved
by direct empirical verification in agy's favor — no buffering needed.
Round 2 cross-review (claude, agy, codex — grok returned empty): claude
and agy APPROVE outright; codex CHANGES-NEEDED on wording precision plus
two scope additions (`on_commentary()` firm inclusion, `finish(final_text=...)`
early-return check), both incorporated above. No engine disputed the core
stateless, no-buffer approach across either round. Ready for
implementation.
