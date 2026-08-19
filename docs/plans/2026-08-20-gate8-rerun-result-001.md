# GATE8-RERUN-RESULT-001 — the isolation diagnostic, rerun with logging restored

Status: result record. **Supersedes the central conclusion of**
[GATE8-ISOLATION-RESULT-001](2026-08-19-gate8-isolation-result-001.md).

## Why it was rerun

That record could not obtain a turn-end reason for its own run and named it as
the missing datum. [GATE8-OBSERVABILITY-001](2026-08-19-gate8-observability-001.md)
established why — one-shot runs wrote nothing to `agent.log` — and the fix
landed. This is the rerun.

Same request text, same CLI surface, tenant `gate8-iso2`. The deployed release
differs. Checked rather than asserted: between the two runs, three files
outside `docs/` and `tests/` changed — `hermes_cli/oneshot.py` (the logging
fix), `scripts/failed_unit_watch.sh`, and `systemd/failed-unit-watch.service`.
The latter two are the failed-unit watcher and are not on the agent execution
path. No prompt, model, tool-schema, or dispatcher code differs.

## Neither run produced output, and the previous conclusion is not supported

**Net result of both runs is the same: nothing delivered.** Run 1 created no
cards; this rerun created ten and every one went `blocked` within minutes. A
reader should not take "0 vs 10" as progress toward gate 8 — it is progress in
what could be *observed*, not in what was produced.

What does not survive is the earlier reading of *why*.

`GATE8-ISOLATION-RESULT-001` recorded that the Telegram failure reproduced off
Telegram: the agent announced the decisive command and stopped, creating
nothing. That specific behaviour did not recur here.

| | first isolation run | this rerun |
|---|---|---|
| `terminal` calls | exploratory, then stopped | **21 succeeded, 2 failed** |
| cards created | 0 | **10** |
| how the run ended | recorded as "stopped at the announcement" | **killed by the harness at 400s, mid-turn** |

There is no `Turn ended` line for this session because the turn never ended —
the 400-second cap in the test harness cut it off while it was still working.

An earlier draft of this record then over-corrected, claiming the first run
might equally have been killed by the same 400-second cap. **That is refuted
by evidence already in hand:** run 1 exited **0**, this rerun exited **124**.
A harness kill produces 124. Run 1 ended on its own; only the rerun was cut
off. The doubt was manufactured while writing, from the same impulse this
ticket exists to guard against, and the disproving datum had been recorded at
the time.

So the two runs differ genuinely, not as an artifact:

- run 1 — the agent ended its own turn after announcing the command, having
  created nothing;
- rerun — the agent worked for the full 400 seconds, built two graphs, and was
  still working when cut off.

**The behaviour is intermittent.** What remains true about the cap is narrower:
it is below the ~15 minutes a completed run needs, so neither run could have
observed a success even had one been forthcoming.

## What the agent got right

- Composed a syntactically valid `hermes kanban swarm` invocation.
- Passed `--worker-lane`; the resulting contract carries
  `expected_lane_id: native_hermes`, so the graph is genuinely lane-bound.
- Built a complete graph: four workers, verifier, synthesizer.
- The dispatcher picked the cards up and ran them.

Twenty-one `terminal` calls returned normally. That is **not** a clearance of
the terminal path: those were exploratory reads, and one of the two failures
was a shell quoting error — `unexpected EOF while looking for matching "` —
which is a composition failure at the terminal layer, on the way to the
decisive command. Exploration succeeding says nothing about the decisive
invocation, and this run contains a terminal-layer failure of exactly the kind
option B exists to address.

## What the agent got wrong

| | expected | actual |
|---|---|---|
| per-lane skill | `claude-code` / `grok` / `antigravity-cli` / none | **`"HUMANIZER"` for all four** |
| worker runtime | 300s, stated explicitly in the request | **not passed; defaulted to 120** |
| first invocation | one complete graph | **partial graph left behind, and dispatched** |
| command assembly | — | one `unexpected EOF while looking for matching "` |

Every worker exited 1 immediately — the skill does not name a lane CLI — and
the dispatcher gave up after the retry. All six went `blocked`.

## Consequence for GATE8-PATH-001

That ticket left A (register a swarm tool) and B (repair the terminal path)
both unsupported, because the decisive command had never been reached. It has
now been reached, twice, and the evidence is asymmetric:

Three distinct failures occurred, and they do not all belong to one option:

| failure | would typed tool arguments prevent it? |
|---|---|
| skill `"HUMANIZER"` for every lane | **partly** — an enum rejects a value that is not a known skill, so this specific call fails loudly instead of building an unrunnable graph. It does **not** stop an agent choosing a valid-but-wrong skill, e.g. `grok` on the claude lane |
| shell quoting `unexpected EOF` | **yes** — there is no shell string to mis-quote |
| `--worker-max-runtime` omitted | **no** — an optional field left unset is a completeness failure, and an agent can omit a typed field just as easily |

So A addresses two of the three, and one of those two (quoting) is also
squarely in B's territory. B is not cleared by this run; it is implicated by
the quoting error and untested on the decisive invocation, because the
decisive invocation succeeded syntactically and failed semantically.

The honest asymmetry is narrower than an earlier draft of this ticket claimed:
**the failure that actually blocked the swarm would have failed loudly at a
typed boundary instead of silently producing a graph that could not run.** The
graph was built, dispatched, and consumed real compute before anything noticed
the skill was meaningless. That is a real point in A's favour. It is not a
clearance of B, and it is not a claim that a typed interface makes the agent
choose correctly.

This is the reading a reviewer proposed during that ticket's review — that the
agent stalls at the exploration-to-composition transition — which was recorded
with equal standing to the alternative because nothing separated them. This
run separates them, in that direction.

**It remains one observation.** The first run produced the opposite outcome and
was also one observation. What this changes is which reading has evidence, not
whether the question is settled.

## Cost of an unimplemented ticket, observed

The first, partial invocation left root `t_6109f004` and two workers, and
**the dispatcher ran them** — real compute spent on a graph with no verifier to
consume it. That is `SWARM-E2E-DEFECTS-001` Defect 1, filed and not
implemented, reproducing in production rather than in a test.

## Harness defect, not a finding about the system

The 400-second cap is too short for a task whose workers alone take minutes.
Both isolation runs used it. Any rerun must exceed the full completion time or
it cannot answer the question it is being run to answer.

## What is still unobserved

Whether the agent carries a swarm through to reporting a result to the user —
gate 8's actual criterion. Neither run reached it. One was cut off; the other
produced a graph that could not run.
