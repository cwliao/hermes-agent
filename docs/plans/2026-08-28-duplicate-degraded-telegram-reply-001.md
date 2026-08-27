status: INVESTIGATION ONLY -- not yet designed/fixed. Filed at end of a long
2026-08-27 session; deliberately NOT attempting a live fix at this hour on
a production message-delivery path without the usual design -> cross-review
-> dispatch -> cross-review-diff -> test -> deploy cycle. This doc exists so
the next session can start from real findings instead of re-discovering them.

# A second, degraded/raw-code Telegram reply was sent ~3 minutes after the real completion, bypassing delivery_obligations entirely

## Problem (observed live, 2026-08-27 23:57 - 2026-08-28 00:00)

User sent one Telegram message requesting a 4-lane kanban swarm (sorting
algorithm comparison). Two replies arrived in the same Telegram thread:

1. **23:57:49** -- a clean, properly formatted completion message ("已完成。
   我用一個 4-lane 分工進行...") with correct content, correct terminal
   traces, correct complexity analysis.
2. **~00:00** -- a second message, ~3 minutes later, containing **raw,
   unformatted, syntactically broken Python** (e.g. `l=2i+1` -- missing a
   multiplication operator, invalid Python) for the same four sorting
   algorithms. No markdown formatting, no explanatory text, just dumped
   code blocks back-to-back.

The user did not send a second request. This is Hermes sending a second,
lower-quality, spontaneous reply to a single user turn.

## What's confirmed so far

- **Root user message was logged into TWO session ids simultaneously**,
  with identical timestamps and even identical tool-call ids for the first
  several turns: `20260827_194622_dbfcef1a` (the original session, alive
  since 19:46 that evening -- the same session id used for every swarm test
  earlier in the night) and `20260827_235727_198f4c` (apparently a
  continuation/compaction session). Confirmed via direct `state.db`
  `messages` table inspection -- both session rows have byte-identical
  `tool_calls` JSON (same `call_id`) for the first ~7 turns.
- **The two sessions diverge in content quality at the exact point of a
  compaction artifact**: for the identical `skill_view(kanban-orchestrator)`
  call, `_dbfcef1a` got the full skill content (`"success": true, ...`)
  while `_198f4c` got a pruned stub
  (`"[skill_view] name=kanban-orchestrator (17,239 chars) [SKILL_PRUNED:
  content lost in compression; reload with skill_view(...)]"`). This is
  strong evidence `_198f4c` is a compacted continuation of `_dbfcef1a`, not
  an unrelated duplicate delivery.
- **`_198f4c` produced the good reply** (23:57:49) and has a corresponding
  row in `delivery_obligations` (`state=delivered`, one row only, matching
  the good message's exact text).
- **`_dbfcef1a`'s own message history in `state.db` goes silent at
  23:57:17** (last logged row: `kanban_show` tool result, id 15897) --
  there is NO further `messages` table activity for `_dbfcef1a` anywhere in
  the following 10+ minutes, and it never produced a logged `assistant`
  text row.
- **There is no `delivery_obligations` row at all for the second (raw
  code) message.** Only one delivery obligation exists in the entire
  window, and it matches the first (good) message exactly.
- **`journalctl` shows nothing relevant in this window** -- the gateway
  process logs at WARNING level only (confirmed separately tonight: zero
  INFO-level lines exist anywhere in the log, including from the
  long-established dispatcher/notifier watchers), so a normal successful
  send produces no trace there either.

**Conclusion so far:** the second message was sent through a code path that
bypasses both the normal `messages` table logging for its final text *and*
the `delivery_obligations` bookkeeping used by the tracked send path. It did
not originate from a fresh `kanban_swarm`/session-turn call (no matching
tool-call evidence anywhere), and its content (terse, unformatted, actually
syntactically invalid Python) looks like a raw/partial LLM completion
dumped directly to the platform adapter -- consistent with some kind of
timeout/fallback/cleanup handler in the STALE session `_dbfcef1a` finally
firing ~3 minutes after it went quiet, and sending whatever partial text it
had captured without going through the normal formatting/delivery pipeline.

## Not yet done / where to start next session

1. **Find the actual send call site.** `plugins/platforms/telegram/adapter.py`
   has ~15 different `send*` methods (`send`, `send_or_update_status`,
   `send_draft`, `_send_message_with_thread_fallback`, etc. -- see
   `grep -n "async def send" plugins/platforms/telegram/adapter.py`). Need
   to find which of these (if any) is called from a turn-timeout, exception
   handler, or session-cleanup path in `gateway/run.py` WITHOUT going
   through the `delivery_obligations` insert -- that's the actual bug
   surface. `grep -rn` for direct `adapter.send(` call sites outside the
   normal delivery-obligation-tracked flow is the fastest way in.
2. **Explain why `_dbfcef1a` and `_198f4c` both exist and both process the
   same turn.** This looks related to (but is a DIFFERENT shape from) the
   `model_replay_guard`/`unverifiable_recovery` incident and the
   context-compaction stale-constraint incident investigated earlier the
   same night (both tied to this exact long-running, multiply-compacted
   Telegram session). Worth checking whether compaction is supposed to
   *replace* the old session going forward, or whether it's supposed to run
   alongside it briefly -- if the latter is intentional, the bug is only in
   the stale session's cleanup path still being allowed to send; if the
   former, the bug is that the stale session kept running turns at all
   after compaction should have retired it.
3. **Check for a watchdog/timeout that fires ~180s after last activity.**
   The gap between `_dbfcef1a`'s last logged activity (23:57:17) and the
   second message (~00:00) is close to 3 minutes -- look for any
   `asyncio.wait_for`/timeout constant in that range in `gateway/run.py`'s
   turn-execution or session-idle-cleanup code.
4. **Do not guess-fix.** This touches the live Telegram send path for every
   user of this gateway -- needs the full design -> cross-review ->
   dispatch -> cross-review-diff -> test -> deploy cycle used for every
   other fix tonight, not a rushed patch.

## Severity assessment

Low-to-medium: the *good* reply was still delivered correctly and first;
the bug is an extra, lower-quality, unsolicited second message, not a
missed or corrupted primary delivery. Annoying and confusing to the user
(exactly the reaction this session's own test produced), but not silently
losing or corrupting real completions. No urgency to hotfix overnight, real
urgency to fix properly next session.
