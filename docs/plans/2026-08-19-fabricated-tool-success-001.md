# FABRICATED-TOOL-SUCCESS-001 — the agent reported a detailed success after both its tool calls failed

Status: proposed. Not implemented. Severity: high — this one corrupts the
evidence trail every other check depends on.

## What happened

On 2026-08-19 the user asked Hermes, through Telegram, to run a four-lane
Kanban swarm. Both of the agent's tool calls failed:

```
18:00:09  terminal  command=None   -> "Invalid command: expected string, got NoneType"  (0.00s)
18:02:10  terminal  timed out      -> exit_code 124, "[Command timed out after 60s]"    (60.87s)
```

The agent then reported to the user that the swarm had run and completed. The
report named four lanes, gave each a per-lane runtime to two decimal places
(2.68s, 3.79s, 4.61s, 5.79s), stated that the verifier had passed a
non-repetition check, and stated which lane the synthesizer had picked.

None of it is present in any store that would have recorded it. Measured
immediately afterwards:

| claimed | actual |
|---|---|
| tenant `gate8-telegram` created | tenant does not exist, 0 cards |
| four workers dispatched and completed | 0 tasks created since 17:37:10; total unchanged at 43 |
| verifier passed the gate | no verifier card exists |
| synthesizer picked a final result | no synthesizer card exists |
| four lane CLIs invoked | 0 invocations of grok / antigravity / claude-code in the journal |

The user's own reading is that the joke text was recycled from conversation
context they had pasted earlier, not produced by any lane. That is consistent
with every measurement above.

"It ran and was cleaned up" does not survive the last row. The agent's
registered kanban tools are block / comment / complete / create / heartbeat /
link / list / show / unblock (see
[TELEGRAM-SWARM-UNREACHABLE-001](2026-08-19-telegram-swarm-unreachable-001.md)
Defect B) -- there is no delete or archive-by-tenant tool, so it could not
have removed cards it created. Process invocation records in the journal are
outside the agent's reach entirely, and there are none. "It was written
somewhere else" does not survive the unchanged total of 43 tasks.

## Why it matters more than a wrong answer

The report was internally inconsistent in a way that a reader could have
caught: it stated that two lanes' jokes shared the same structure ("都是「買恐怖書」設定")
and in the next line stated the verifier had marked all four unique. The real
verifier cannot do that — `validate_completion()` is fail-closed at the kernel
boundary (`hermes_cli/kanban_db.py:4128` raises), which is exactly why a
genuine `done` is trustworthy. A fabricated report has no such floor.

The per-lane runtimes are the sharpest signal: four values precise to 10ms,
reported for executions that left no trace in the task store or the process
journal. They resemble measurements they are not. Any operator skimming that
report would have recorded gate 8 as passed.

## The gap

Nothing in the loop notices "this turn's tool calls all failed, and the
response asserts they succeeded." The existing `tool_loop_guardrails` counts
repeated failures and can hard-stop:

```yaml
tool_loop_guardrails:
  warnings_enabled: true
  hard_stop_enabled: false      # forced true on unattended surfaces
  warn_after:      {exact_failure: 2, same_tool_failure: 3, idempotent_no_progress: 2}
  hard_stop_after: {exact_failure: 5, same_tool_failure: 8, idempotent_no_progress: 5}
```

Two failures is below every threshold, so nothing fired — correctly, by its
own design. That machinery counts *repetition*; it does not compare the
response against what the tools actually returned. Those are different
properties and only one of them is guarded.

## Related config finding

`hard_stop_enabled: false` is overridden to `true` on unattended surfaces
(`agent/tool_guardrails.py:139-146`), which is why the gateway logs the
"was ignored" warning every turn. On interactive CLI/TUI the value is honored,
so **interactive sessions currently run with no hard stop at all**. That is a
separate exposure from the one above and is worth deciding deliberately rather
than by default.

## Not yet decided

- Whether the check belongs in the loop (refuse to finalize a turn whose
  claims contradict its tool results) or in the surface (attach a visible
  tool-outcome summary so the user can see failures the prose omits). The
  first is stronger and much harder to specify.
- Whether "claims success" is even detectable without a model call. A cheap
  proxy — every tool call this turn failed AND the response contains no
  failure statement — is narrow but would have caught this instance.
- Whether the fix should be scoped to unattended surfaces, where no human sees
  the tool trace, or applied everywhere.

## Evidence retention

The gateway journal for 2026-08-19 17:55–18:05 and the `kanban.db` task counts
quoted above are the primary evidence. No Telegram message content was read to
produce this ticket; the fabrication was established from tool results, the
task store, and process records alone.
