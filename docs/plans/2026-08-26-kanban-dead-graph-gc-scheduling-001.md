---
title: "KANBAN-DEAD-GRAPH-GC-001 — schedule dead-graph GC and cover untenanted boards"
status: DESIGN_REVISED
date: 2026-08-26
type: design-proposal
ticket: KANBAN-DEAD-GRAPH-GC-001
target_repo: hermes-agent
---

## Revision note (post cross-review, 2026-08-26)

Cross-review (claude + agy, independently, both with exact code citations
verified against the live source) found the original Scope section 2
underspecified in two ways that would have reintroduced real bugs/risk if
implemented as first drafted. Both are addressed in the revised Scope
below:

1. **`find_dead_graphs`'s existing `tenant` parameter is a truthiness
   check, not a three-way switch** (`hermes_cli/kanban_db.py` ~line 12936):
   `if tenant: ... else: SELECT id, status FROM tasks` with no filter at
   all. `tenant=None`/`""` does not mean "match `tenant IS NULL` rows" —
   today it is simply unreachable (the CLI refuses to call this without
   `--tenant`), but the *obvious* first-draft implementation of an
   "untenanted" flag (translate it to `tenant=None` and pass through)
   would silently widen a sweep from "untenanted graphs only" to "the
   whole board, every tenant included" — exactly the multi-tenant leak the
   original design existed to prevent. Confirmed by reading the function
   directly.
2. **The tenant filter is applied before the connected-component walk, not
   after** — `status_of` (and therefore which `task_links` edges are
   followed) is already tenant-scoped before components are computed. A
   graph that spans a tenant boundary isn't detected and excluded, it is
   silently truncated into a smaller sub-component. Low-risk historically
   (only run manually against small, deliberately-isolated test tenants);
   becomes a live concern the moment this runs unattended against a real,
   messy board with no tenant-purity invariant enforced anywhere else.
3. **The single-card-component case is the sharper problem for this
   operator specifically.** The function's own comment: "a standalone card
   forms a component of one and is skipped below unless it is itself stale
   and non-terminal" — i.e. IS swept if stale+non-terminal. For a
   multi-tenant deployment where untenanted-vs-named-tenant distinguishes
   disposable test data from real work, that's fine. For this operator,
   `tenant = NULL` is the *default* for every real card, including any
   backlog item deliberately left `todo`/`blocked` for more than 7 days —
   an "untenanted dead-graph sweep" as originally scoped would silently
   archive a human's parked backlog, not just abandoned swarm wreckage.
   The motivating problem (this session's manual cleanup) was entirely
   multi-card swarm graphs (root + workers + verifier + synthesizer,
   linked via `task_links`) — the fix should be scoped to that shape
   specifically, not to "any old untenanted card."

# KANBAN-DEAD-GRAPH-GC-001

## Motivation

Diagnosed live on 2026-08-26 while manually cleaning up ~18 stuck swarm
tasks (created 2026-08-24, 2 days old, from repeated Telegram jokes-swarm
testing): the board had accumulated real, dead cruft that nothing on the
system was ever going to clean up automatically. Three independent, stacked
reasons, confirmed by direct inspection:

1. **No scheduled job ever invokes `hermes kanban gc` at all.** Checked
   every `systemctl --user list-timers` entry on `55-0940189-03`; the only
   kanban-related timer is `hermes-kanban-summary.timer` (a periodic report,
   unrelated to cleanup). `hermes kanban gc` is 100% manual today.
2. **Even run manually, the part that actually archives dead swarm graphs
   (`--dead-graphs`, KANBAN-CARD-GC-001) is off by default** — plain
   `hermes kanban gc` only prunes derived data (old `task_events`, worker
   logs), never touches board state.
3. **`--dead-graphs` structurally cannot ever reach this operator's real
   boards.** It requires `--tenant <value>` and, by explicit design
   (`find_dead_graphs`'s own docstring: "the caller must name what is
   disposable, because nothing on a card distinguishes a test graph from a
   backlog somebody parked on purpose"), only considers a graph dead when
   *every* card in it shares that exact tenant string. A live count on this
   database: 228 of ~235 tasks have `tenant = NULL`; only a handful of
   deliberately-tenant-scoped E2E test batches (`autumn-jokes-v4`,
   `e2e-fourlane`, etc.) carry a tenant at all. Every swarm this operator
   actually runs through Telegram in normal use is untenanted, so
   `--dead-graphs` can never select it, at any age, under any schedule.

That KANBAN-CARD-GC-001's own design intentionally treats "no tenant" as
"do not touch" is a deliberate, previously-reviewed safety choice (per its
docstring, guarding against silently sweeping a backlog someone parked on
purpose in a multi-tenant deployment) and this ticket does not relax that
general principle. It adds an explicit, separately opt-in path for the
single-operator case this system is actually running as.

## Scope

1. **Add a scheduled systemd `--user` timer** that runs `hermes kanban gc`
   on a regular cadence (proposed: daily; confirm with cross-review whether
   a longer interval fits given the 7-day dead-graph age default below).
   Follow the existing timer/service pattern already used on this host
   (e.g. `codex-remote-control-healthcheck.timer`,
   `hermes-kanban-summary.timer`) — a `.service` unit invoking the CLI,
   paired with a `.timer` unit, both under
   `~/.config/systemd/user/`. Log output the same way the existing
   healthcheck timer does, so a failure is visible via `systemctl --user
   --failed` the same way this session's earlier OOM investigation found
   the codex healthcheck's failure.

2. **Fix `find_dead_graphs` itself before building anything on top of it**
   (prerequisite, not optional):
   - Give "sweep untenanted graphs" a genuinely distinct third state that
     cannot collapse into the existing `if tenant:` truthiness branch —
     e.g. split the parameter into `tenant: Optional[str] = None` (must be
     a real, non-empty string when given) plus a separate
     `include_untenanted: bool = False`, and make the "no scope requested
     at all" case (`tenant=None, include_untenanted=False`) return `[]`
     immediately rather than falling through to the unfiltered
     "every task" query. The unfiltered branch already has no CLI caller
     today; removing its reachability entirely (rather than leaving it as
     a landmine for the next caller) is preferred over just adding a new
     branch alongside it.
   - Compute connected components over the **full, unfiltered** task/link
     graph first, then filter each discovered component by tenant
     uniformity (all cards `tenant = X` for the requested `X`, or all
     cards `tenant IS NULL` for the untenanted case) — not the current
     order of tenant-filter-then-component-walk, which can silently
     truncate a graph that spans a tenant boundary instead of correctly
     excluding it.

3. **Scope the untenanted sweep to actual swarm topology, not "any stale
   untenanted card."** Per the revision note, a lone parked backlog card
   is a legitimate, common shape for this operator and must not be treated
   as disposable the same way an abandoned swarm graph is. Restrict the
   automated untenanted path to components where `len(component) > 1`
   (i.e. it has at least one `task_links` edge — the shape every swarm
   root+workers+verifier+synthesizer graph has, and no standalone card
   has). A standalone stale untenanted card is left alone by the automated
   sweep entirely; an operator can still `--dead-graphs --tenant <name>`
   sweep named-tenant single cards manually as today, unaffected by this
   change.

4. **Add a per-run archive cap.** If a run's candidate list (after the
   size-filter in point 3) exceeds a configurable threshold (default
   proposed: 20), archive nothing and alert instead of proceeding — this
   is the actual backstop against a scoping bug or a clock/data anomaly
   wiping out a large slice of the board in one unattended run, not
   something a post-hoc notification alone can prevent.

5. **Add a mandatory one-time manual promotion gate**: the first time this
   ships, the operator runs `hermes kanban gc --dead-graphs
   --include-untenanted --dry-run` by hand and reviews the candidate list
   before the scheduled timer is ever enabled to run it live
   (`--include-untenanted` without `--dry-run`). The timer unit itself
   should not be enabled by this ticket's implementation as a side effect
   of merging code — enabling it is a separate, explicit operator action
   after that manual review.

6. **`archive_task` must re-validate a card's current status immediately
   before archiving it**, not trust the `find_dead_graphs` snapshot from
   moments earlier — this is the first time `--dead-graphs` runs
   unattended and repeatedly against a board where live swarms may be
   concurrently active (previously: manual, chosen moments, isolated test
   tenants only). Confirm whether `archive_graph`'s per-id `archive_task`
   calls already pass `_allowed_statuses` (the parameter KANBAN-SWARM-002
   added to `archive_task`/`_archive_task_in_txn` for exactly this kind of
   guard) — if not, add it, scoped to the statuses observed at
   `find_dead_graphs` time for that specific card.

7. **CLI shape: a dedicated boolean flag, not a sentinel string.**
   `--include-untenanted`, mutually exclusive with `--tenant <name>` (both
   supplied is a hard argparse error, not silently-defined behavior). A
   typo on a flag name fails loud via argparse; a typo inside a sentinel
   string value (`--tenant __untenated__`) would silently match zero rows
   and look like "nothing was dead" — a failure mode that's easy to
   mistake for success.

8. **The scheduled timer's default invocation must explicitly pass
   `--include-untenanted`** (after the one-time manual promotion in point
   5) for this to actually solve the motivating problem — a timer running
   bare `hermes kanban gc --dead-graphs` would still never archive
   anything untenanted, unchanged from today. Named-tenant sweeps
   (`autumn-jokes-v4` etc.) stay a manual/existing workflow, out of this
   ticket's scope, to keep blast radius small.

9. **Visibility**: emit a Telegram alert (best-effort, matching the
   pattern other healthchecks on this host already use — failure to send
   must not affect the gc run's own exit code) whenever a run archives
   N>0 graphs: a count, the archived root task titles/ids (bounded, e.g.
   first 10), and a pointer to `journalctl --user -u <the gc service
   unit>` for the full list. Also alert (separately) if the per-run cap
   from point 4 is hit, since that path deliberately archives nothing and
   needs a human to look.

## Confirmed safe, no design changes needed

Cross-review (agy, with exact time-constant citations) checked whether an
unattended, repeated `--dead-graphs --include-untenanted` run could race
with SWARM-PARTIAL-QUORUM-001, SWARM-WORKER-DEADLINE-001's
`excuse_overdue_workers`, or KANBAN-SWARM-002's synthesizer retry/backoff
logic. All three operate on time scales of minutes
(`_WORKER_RESPONSE_DEADLINE_SECONDS` = 660s; synthesizer retry backoff =
tens of seconds to a few minutes) against this ticket's 7-day dead-graph
age window — three orders of magnitude apart. Any swarm actively subject
to those mechanisms converges (excused, retried, or completed) within
minutes, long before it could ever become a `--dead-graphs` candidate.
`find_dead_graphs` also already excludes any graph with a `running` or
`ready` card at any age. No changes needed here; point 6 above (status
re-validation at archive time) is the residual, much narrower gap worth
closing, not a race with these specific mechanisms.

## Explicitly out of scope for this ticket

- Does not change `find_dead_graphs`'s core dead-graph criteria (no
  running/ready card; no event within the age window) — that logic is
  already correct, already used successfully (manually) today.
- Does not change the default `--dead-graph-days` age threshold (7 days) —
  no evidence yet that it's wrong; today's manual cleanup targeted
  ~2-day-old boards specifically because they were known-dead by direct
  inspection, not because 7 days is too conservative in general.
- Does not touch `--event-retention-days` / `--log-retention-days` (the
  non-`--dead-graphs` part of `gc`) — no evidence those defaults are
  wrong, and they were never gated behind tenant scoping to begin with.
- Does not touch SWARM-WORKER-DEADLINE-001 or KANBAN-SWARM-002's logic —
  fully independent code paths (this is about board hygiene for graphs that
  are ALREADY stuck, not about preventing swarms from getting stuck).

## Required before implementation

Design cross-reviewed once already (claude/codex/agy/grok/groq, 2026-08-26)
— findings incorporated above as the Revision note and Scope points 2-9.
Per established practice on this repo, the resulting code diff will go
through its own separate cross-review before commit, same as
SWARM-WORKER-DEADLINE-001 and the notifier LLM-formatting feature.
