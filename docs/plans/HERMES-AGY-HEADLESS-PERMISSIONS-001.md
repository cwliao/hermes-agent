---
title: "HERMES-AGY-HEADLESS-PERMISSIONS-001 design/decision ticket"
status: ROOT_CAUSE_FOUND_NOT_A_PERMISSIONS_PROBLEM
date: 2026-08-18
type: ticket-design
ticket: HERMES-AGY-HEADLESS-PERMISSIONS-001
target_repo: hermes-agent
related_tickets:
  - HERMES-MULTI-AGENT-CLAUDE-WORKER-001 (PR #48, merged)
  - feature/lane-quorum-2-of-3-external (PR #49, merged)
---

# HERMES-AGY-HEADLESS-PERMISSIONS-001

This is a **design/decision ticket**, not an implementation ticket. It
authorizes design and decision-recording only. It does not authorize any
change to `~/.gemini/antigravity-cli/settings.json`, any change to Kanban
swarm code, or any execution of an AGY permission-bypass flag. No file
outside this new document was modified to produce this ticket.

## 1. Background and problem statement

During diagnosis of the AGY worker lane's `BLOCKED` state (recorded against
`HERMES-MULTI-AGENT-CLAUDE-WORKER-001`, PR #48), a read-only investigation on
the DGX Spark host (hostname `55-0940189-03`, user `cwliao`) established the
following, without modifying any file and without using
`--dangerously-skip-permissions`:

- The `agy` binary, its PATH resolution (both in an interactive shell and in
  the actual running `hermes-gateway.service` process environment), and its
  authentication/network path are all functioning. `agy --print` prompts that
  need no tool call complete successfully in a fully headless invocation.
- `agy`'s permission configuration lives at
  `~/.gemini/antigravity-cli/settings.json`, not at `~/.agy` or
  `~/.config/agy` as first guessed. Its `permissions.allow` list contains
  only specific, previously-approved exact command strings (for example
  `command(nvidia-smi)`, `command(jq)`, `command(grep)`); there is no
  wildcard rule. The CLI's own log shows the effective mode as
  `toolPermission=request-review`.
- When a headless invocation needs to run any tool/command call that is not
  already present in `permissions.allow`, AGY cannot prompt an interactive
  reviewer for approval (there is no TTY attached), so the request is
  **soft-denied**: the process exits `0`, prints a denial notice to stderr,
  and produces no usable result. This was reproduced directly: a prompt
  requiring an unapproved command produced no output, while an equivalent
  prompt needing no tool call, and a prompt only exercising already-approved
  commands, both completed normally.

This is the precise mechanism behind the state recorded elsewhere as "AGY
blocked by a required interactive TTY." The more accurate description is:
AGY does not have a hard TTY requirement for headless operation in general —
it has a hard TTY requirement specifically for approving any **Ask-class**
tool call whose target command is not already in the allowlist, and no
worker-scoped allowlist has ever been built for it.

## 2. Scope and non-scope

### In scope

- AGY's own permission policy (`permissions.allow`, `permissionMode`,
  sandbox-related settings) and the operational process for building and
  maintaining it.
- Deciding which of the previously enumerated remediation options (see
  Decision record) is the standing path forward, and which are excluded or
  deferred.
- Defining the observation/evidence process used to derive a minimal
  allowlist safely.
- Acceptance and rollback conditions for whatever settings change is
  eventually implemented under a follow-up ticket.

### Out of scope

- Any change to `hermes_cli/kanban_swarm.py`, `hermes_cli/kanban_db.py`,
  `hermes_cli/kanban.py`, `tools/kanban_tools.py`, or
  `optional-skills/devops/kanban-worker/SKILL.md`. The four/quorum-lane
  contract implemented in PR #48 and amended in PR #49 (lane-quorum branch
  `feature/lane-quorum-2-of-3-external`) is unaffected by and unrelated to
  this ticket; this ticket does not reopen, extend, or gate either PR.
  This ticket does not authorize implementation for PR #48 or PR #49; those
  each carry their own separately authorized scope.
- Any actual edit to `~/.gemini/antigravity-cli/settings.json` or any other
  AGY runtime state file. Implementation is a separate, later-authorized
  ticket.
- Any invocation of `--dangerously-skip-permissions` or any other permission
  bypass, in this ticket or its authoring.
- Claude/Grok CLI permission behavior — out of scope unless a parallel issue
  is separately confirmed; this ticket is AGY-specific.
- DGX deployment, service restart, relay/timer changes, or Telegram testing.

## 3. Threat model

An allowlist-driven policy is only as good as the threats it was designed
against. The following are the threats this design must hold in mind, in
rough order of severity:

1. **Prompt injection driving arbitrary shell execution.** A worker task body,
   a fetched web page, or file content the agent reads could contain text
   designed to make the agent request a destructive or exfiltrating command.
   An allowlist bounds *which* commands can run without human review; it does
   not by itself validate *arguments* to an allowed command unless the rule
   is written narrowly enough to constrain them.
2. **Arbitrary shell command execution in general.** Any rule shaped like
   `command(*)` or an unbounded prefix match defeats the allowlist's purpose
   and is equivalent to `--dangerously-skip-permissions` for that command
   family.
3. **Writes outside the intended workspace.** A command that is otherwise
   benign (e.g. a file write) becomes dangerous if its target path is
   outside the swarm worker's expected workspace — most obviously `$HOME`,
   but also any path outside the repo/worktree the worker was assigned.
4. **Secret/credential exfiltration.** Reading or transmitting
   `~/.gemini/antigravity-cli/antigravity-oauth-token`, `~/.ssh/*`,
   `~/.hermes/.env*`, `~/.config/gh/hosts.yml`, or any other credential path,
   whether via direct read, `curl`/`wget` upload, `git push` to an
   unintended remote, or copy into a log/comment/ticket.
5. **Uncontrolled network egress.** Any `curl`/`wget`/equivalent that is not
   scoped to a known, intended destination (e.g. a specific localhost
   service port already in the existing allowlist) is a potential
   exfiltration or SSRF vector.
6. **Destructive filesystem or system operations.** `rm -rf`, `mkfs`, `dd`,
   `shutdown`/`reboot`, and similar are catastrophic if ever approved
   unattended; they must never appear in an allowlist rule, precise or not.
7. **Privilege escalation.** `sudo`, `su`, or any command that changes
   effective UID must never be pre-approved for unattended execution.
8. **Destructive git operations.** `git push --force`, `git reset --hard`,
   `git clean -f`, branch deletion, and similar can destroy work exactly as
   this repository's own operating rules already warn against for
   interactive use; the same caution applies to anything AGY could run
   unattended.

## 4. Decision record

Four remediation paths were identified during diagnosis. This ticket records
an explicit decision on each.

### Option 1 — Minimal-scope `permissions.allow` allowlist (ADOPTED as the standing path)

Build a precise, per-command allowlist covering exactly the tool/command
calls the Kanban swarm worker actually needs, derived from observed,
reproduced necessity (see Section 6), not from guessing ahead of time.

- **Pros:** narrowest attack surface of any option that still allows
  unattended operation; every approved action is individually inspectable
  and revertible; matches this repository's existing "existing surfaces
  only, no silent scope expansion" discipline.
- **Cons:** highest ongoing maintenance cost — every new command shape the
  worker needs requires a new rule, reviewed and added deliberately; will
  under-cover on first pass and needs iteration.
- **Decision:** adopted as the standing, production path for headless AGY
  worker operation.

### Option 2 — `proceed-in-sandbox` permission mode (DEFERRED to a PoC/evaluation item, NOT adopted now)

Switch `permissionMode` to `proceed-in-sandbox`, which the operator guide
(`optional-skills/autonomous-ai-agents/antigravity-cli/SKILL.md`) documents
as an existing mode alongside `request-review`, `always-proceed`, and
`strict`, with `enableTerminalSandbox` (default `false`) as a related
setting.

- **Pros:** could reduce the per-command maintenance burden of Option 1 by
  auto-approving execution while still constraining it to some sandbox
  boundary.
- **Cons:** the exact sandbox boundary (filesystem, network egress, and
  workspace scope under `proceed-in-sandbox`) is not yet confirmed for the
  AGY CLI version installed on this host (`1.1.14`). Adopting it without
  first confirming those boundaries would mean approving unattended
  execution against an unverified security boundary — unacceptable given the
  threat model in Section 3.
- **Decision:** not adopted now. Recorded as a follow-up PoC/evaluation item.
  Before any adoption decision, a separate investigation must confirm: which
  settings keys this CLI version actually honors for `proceed-in-sandbox`;
  what filesystem paths and network destinations the sandbox boundary
  permits or blocks; and whether the boundary is enforced by the CLI itself
  or assumes an external sandbox (container, namespace, etc.) that does not
  currently exist on this host.

### Option 3 — `--dangerously-skip-permissions` (EXPLICITLY PROHIBITED for standing production use)

Launch AGY with the flag that auto-approves every tool permission request.
This repository's own `antigravity-cli/SKILL.md` documents it as the
existing pattern for long/bounded real-work runs.

- **Pros:** none, for standing production worker use — it collapses the
  entire threat model in Section 3 into "trust every request unconditionally
  with no human or allowlist in the loop."
- **Cons:** removes all per-command review; a single prompt-injected or
  buggy request can run arbitrary destructive commands with the same effect
  as an attacker having a shell.
- **Decision:** **prohibited** from any standing/production worker path,
  including the Kanban swarm lane. It is permitted **only** for isolated,
  short-lived, rebuildable-sandbox, human-operated diagnostic sessions —
  never inside a service, a scheduled job, or an unattended worker — and
  only with a separate, explicit approval each time it is used, matching how
  this session's own environment already blocks the flag by default and
  requires the human to authorize it individually. It must never be wired
  into `hermes_cli/kanban_swarm.py`, any worker skill, or any systemd unit.

### Option 4 — PTY/tmux interactive path (ADOPTED as the human-exception/debug fallback, NOT a worker dependency)

Launch AGY under a real PTY (with tmux for capture/monitoring), the pattern
already documented for interactive multi-turn sessions in
`antigravity-cli/SKILL.md`, so a human can approve permission prompts as they
appear.

- **Pros:** preserves full per-request human judgment with no allowlist
  maintenance; useful for one-off diagnosis, first-time command discovery
  (see Section 6), and any task whose command shape is not yet allowlisted.
- **Cons:** cannot be a dependency for unattended/automatic worker execution
  by definition — it requires a human present.
- **Decision:** adopted as the standing **manual exception and debugging
  fallback**. It is not, and must never become, a dependency of the ordinary
  Kanban swarm worker execution path; a lane-bound swarm that needs a human
  at a PTY every run has silently regressed into a manual process wearing an
  automated worker's contract.

## 5. Minimum-privilege design principles

Any future implementation of Option 1 must follow these principles:

1. **Allow only actions that are observed, reproducible, and necessary.**
   Every `permissions.allow` entry must trace back to a specific,
   reproduced worker need (Section 6), not to speculation about what a
   worker "might" need.
2. **Rules are exact command patterns; `command(*)` and unbounded prefix
   wildcards are prohibited.** A rule like `command(git status)` is
   acceptable; a rule like `command(git *)` or `command(*)` is not, because
   it defeats the allowlist's purpose per the threat model in Section 3.
3. **Deny must take priority over, and explicitly cover, at minimum:**
   `sudo`, `su`, `rm -rf` (and equivalents), `mkfs`, `dd`, `shutdown`,
   `reboot`, and any `curl`/`wget`/equivalent whose destination is not an
   already-known, intended target (the existing allowlist's local
   `127.0.0.1` service calls are the model for what "known, intended" looks
   like — an open-ended outbound URL is not). Destructive git operations
   (`push --force`, `reset --hard`, `clean -f`, branch deletion) fall under
   the same deny-first principle.
4. **Prefer workspace-relative read/write over `$HOME`- or system-wide
   paths.** Rules should be scoped to the worker's assigned workspace
   (matching this repository's existing `trustedWorkspaces` concept) rather
   than granting broad `$HOME` or filesystem-wide access.
5. **No token, API key, cookie, or SSH private key ever appears in a log,
   ticket, or allowlist entry.** This applies to this ticket itself (no
   secret material is included above) and to any future implementation
   ticket, its review packets, and its evidence records.

## 6. Observation and evidence-gathering process

Before any allowlist entry is written, the actual commands a Kanban swarm
worker needs must be observed directly, not guessed:

1. Run the swarm worker's real prompt/skill path (the same one PR #48 wires
   the `agy` lane through) using AGY's non-interactive, observable mode —
   `--print` with `--output-format stream-json` (or an equivalent
   structured/observable mode the installed CLI version supports) — so that
   every requested tool/command call is visible in the output stream, rather
   than opaquely denied.
2. Run this under a real TTY/PTY (Option 4) or with a temporary, narrowly
   time-boxed elevated review mode, so denied requests are actually visible
   for capture instead of being silently soft-denied, and so no destructive
   action is auto-approved during observation itself.
3. Collect the resulting command-request list. A human reviews each request
   for necessity and safety against Section 5's principles before it is
   proposed as an allowlist entry — the goal is to add each rule
   individually and deliberately, not to bulk-import an observed trace.
4. Repeat across the actual range of worker prompts the swarm design uses
   (not just the disposable joke-brainstorm test), since different task
   content can request different commands.
5. Discard, rather than allowlist, any observed request that is broader than
   necessary (e.g. an unbounded `curl` a narrower one could replace) or that
   touches a path/command called out in the Section 5 deny list.

This process itself does not require `--dangerously-skip-permissions` and
does not require editing `settings.json` in advance — it only requires
visibility into what is being requested and denied.

## 7. Acceptance criteria (for a future implementation ticket)

1. A headless AGY worker invocation completes a defined, non-destructive
   Kanban swarm worker task (e.g. the disposable joke-brainstorm prompt)
   without any human present to approve a permission prompt.
2. A command that is *not* present in the allowlist is still refused when
   attempted headlessly, and the refusal is diagnosable — visible in AGY's
   own log or an equivalent observable record — rather than a silent,
   unexplained empty result.
3. `--dangerously-skip-permissions` is not used anywhere in the
   implementation, the worker invocation path, or any systemd unit/service
   configuration that results from this work.
4. Any settings change ships with: a diff of the exact `permissions.allow`
   (and any other changed key) before/after; a record of who/what reviewed
   each new rule against Section 5; and test/verification evidence that the
   intended worker task now succeeds headlessly.
5. A documented rollback path (Section 8) is verified to actually restore
   the prior state before the change is considered complete.

## 8. Rollback

If an implemented allowlist change causes unexpected behavior (over-broad
approval, a worker task failing in a new way, or any denial that should not
have occurred):

1. Restore the immediately prior `~/.gemini/antigravity-cli/settings.json`
   from its pre-change backup (this repository's existing convention, visible
   on this host today, is a sibling file such as
   `settings.json.<ticket>-backup-<timestamp>`; the implementation ticket
   must create an equivalent dated backup before editing).
2. Confirm the restored file's `permissions.allow` and `permissionMode`
   (or equivalent) match the pre-change values before considering rollback
   complete.
3. Fall back to the Option 4 PTY/tmux interactive path for any worker task
   that depended on the rolled-back rule, until a corrected allowlist entry
   is re-implemented and re-verified.
4. Record the rollback and its reason in the implementation ticket; do not
   silently re-attempt the same rule without addressing why it caused the
   problem.

## 9. Open questions to confirm before implementation

- **Exact `settings.json` path and format stability.** Confirmed for this
  host and this AGY version (`1.1.14`) as
  `~/.gemini/antigravity-cli/settings.json`; not yet confirmed to be stable
  across AGY CLI upgrades or across other hosts this swarm might run on.
- **Whether `permissionMode` and `proceed-in-sandbox` are valid, documented
  configuration keys/values for the installed CLI version**, and if so,
  exactly what boundary `proceed-in-sandbox` enforces (filesystem, network,
  workspace) — required before Option 2 can even be responsibly evaluated,
  independent of the decision not to adopt it now.
- **The actual, complete list of commands the Kanban swarm `agy` lane needs**,
  which does not yet exist — Section 6 defines the process to derive it, but
  it has not been run.
- **Sandbox network policy**, if Option 2 is later evaluated: which egress
  destinations (if any) a sandboxed AGY process could reach, and whether
  that set overlaps with the existing allowlist's local `127.0.0.1` service
  calls or extends further.
- **Execution account and workspace boundary — CONFIRMED (2026-08-18).**
  `$HOME` and `$USER` were checked directly via `/proc/<pid>/environ` for the
  running `hermes-gateway.service` process and compared to an interactive
  session: both are `/home/cwliao` / `cwliao`, identical. AGY's permission
  state (`settings.json`, OAuth token, `trustedWorkspaces`) is therefore
  **shared, not siloed**, between an interactive session and the gateway
  service's invocation of `agy`. This removes one axis of uncertainty for
  Option 1: an allowlist entry approved interactively will also apply to a
  gateway-dispatched worker, and vice versa — there is only one
  `settings.json` in play, not two independent copies to keep in sync.
- **Rule matching semantics for multi-word/argument-bearing commands.**
  Section 11's `whoami` trace confirms that for a **bare, no-argument**
  command, matching is a simple presence check: `command(pwd)` exists in
  `permissions.allow` so `pwd` is allowed; no `command(whoami)` entry exists
  so `whoami` is denied. This does **not** yet confirm whether an
  argument-bearing command must match the **entire** command line exactly
  (e.g. does `command(python3 -c "X")` match only that exact `X`, or does it
  match any `python3 -c <anything>`?) — the 5 flagged `python3 -c "..."`
  rules in Section 4/5's risk assessment remain unresolved on this specific
  point. A follow-up trace with an argument-bearing, not-allowlisted command
  (e.g. `echo test123`, since bare `echo` is allowlisted but that exact
  argument is not) would resolve this before any Option 1 implementation
  ticket writes rules for argument-bearing commands.

## 10. Next step

Before any settings change: run the observation/evidence process in
Section 6 to obtain a real command trace from an actual Kanban swarm `agy`
worker invocation. Only after that trace is reviewed by a human against
Section 5's minimum-privilege principles should a follow-up **implementation**
ticket be opened to propose and land the concrete `permissions.allow` diff.
Section 11 below is a step toward that — controlled, minimal-prompt traces
run outside any real swarm worker invocation, used to characterize AGY's
permission mechanism itself rather than to enumerate a real worker's
command needs. It does not substitute for tracing an actual swarm worker.

This design ticket does not itself change `settings.json`, `permissionMode`,
or any other AGY runtime state, and does not authorize doing so.

## 11. Trace evidence log

All traces below: single-shot (never rerun), `agy --print ... --output-format
stream-json`, output captured to a `mktemp -d` directory (`chmod 700`, then
`chmod 600` on the output files immediately after), no
`--dangerously-skip-permissions`, no settings changes made before or after.

### Trace 1 (2026-08-18) — no tool call needed

Prompt asked for a fixed reply with no tool use. Result:
`result.status = SUCCESS`, `result.response` exactly `"ping"`. Confirms
headless operation works cleanly when no permission decision is required at
all. Does not exercise the permission path.

### Trace 2 (2026-08-18) — `pwd`, already allowlisted

Prompt asked for exactly one `pwd` tool call. Result: tool step went
`ACTIVE` → `DONE` (not `ERROR`), `result.status = SUCCESS`, no denial
markers in the tool output. Traced to allow-rule `command(pwd)` (a
pre-existing exact entry) — this is an **allowed** case, not a denial. At
the time, this left open whether headless AGY has some other, non-allowlist
route to auto-approving "safe-looking" commands.

### Trace 3 (2026-08-18) — `whoami`, NOT allowlisted — clean soft-deny reproduced

Prompt asked for exactly one `whoami` tool call (bare, no arguments;
confirmed absent from `permissions.allow`'s 76 entries). Result:

```json
{
  "event": "step_update",
  "step_update": {
    "step_index": 3,
    "state": "ERROR",
    "step_type": "tool",
    "tool_name": "run_command",
    "duration_seconds": 0.168919739,
    "tool_info": {
      "name": "run_command",
      "parameters": { "CommandLine": "whoami" },
      "error": {
        "type": "TOOL_ERROR",
        "message": "User denied permission to run command:\nwhoami"
      }
    }
  }
}
```

`stderr`:
```
jetski: no output produced — a tool required the "command" permission that
headless mode cannot prompt for, so it was auto-denied. Add an allow-rule
under permissions.allow in settings.json (e.g. command(<target>)).
Alternatively, re-run with --dangerously-skip-permissions to auto-approve
all tools.
```

`result.status` was still `SUCCESS` at the turn level (the *conversation*
completed normally), but `result.response` was empty (`""`) — the model did
not produce the requested `"done"` reply after its one permitted tool call
was denied.

**Conclusion**: this closes the missing piece flagged throughout Sections 1
and 9 — a real, attributable, schema-visible soft-deny event, not merely an
absence of success. Combined with Trace 2, this confirms AGY's permission
mechanism for bare commands is a **simple presence check** against
`permissions.allow`, not a risk-based classifier: present → proceed; absent
→ auto-deny in headless mode (no TTY to prompt a human). See the updated
Section 9 bullet on rule matching for what remains unconfirmed
(argument-bearing commands).

## 12. Root cause found (2026-08-19): a global skill, not permissions

Running the ticket's **actual** agy-lane worker prompt — `Return one short
clean joke.` — under `--print --output-format stream-json` produced the
decisive trace. It invalidates this ticket's central premise.

### What the trace shows

1. AGY first calls `view_file` on
   `~/.gemini/config/skills/slave-mode/SKILL.md` — it auto-loads a globally
   installed skill.
2. It then calls `run_command` with a **composed, multi-line shell blob**:
   `pwd && git rev-parse --show-toplevel ... || echo ...` / `git branch
   --show-current ...` / `git rev-parse --short HEAD ...` / `hostname`.
3. That command is auto-denied headless (`duration_seconds` 0.0067, no
   output), and the whole run ends `status=CANCELED` with an empty
   response.

### Why it happens

`slave-mode`'s SKILL.md requires every reply to be rendered as a
fixed-width ASCII dashboard card containing, among other fields, the
current time **and the repository/branch**. Populating that card is what
forces the `git`/`hostname` reconnaissance on every single invocation —
including one that only asks for a joke.

The skill is installed globally at `~/.gemini/config/skills/slave-mode/`,
so it applies to **every** AGY call on this host, not just interactive
ones. (It is also self-aware about this: its own text tells AGY not to rely
on unattended headless CLI subprocesses, which is precisely the mode the
Kanban swarm dispatches it in.)

### Why an allowlist is the WRONG fix

Sections 4–6 of this ticket assumed the fix was a minimal
`permissions.allow` allowlist built from observed commands. That assumption
does not survive this trace:

- The denied command is **composed on the fly** and is a multi-line
  compound statement with `&&`, `||`, and redirects. There is no stable
  exact string to allowlist, and matching is exact (Section 11).
- Allowlisting it would grant unattended `git` + shell execution to satisfy
  a **cosmetic dashboard**, not a task requirement. That trades real
  privilege for decoration — the opposite of Section 5's principles.
- Even when the command is permitted, the reply is wrapped in the dashboard,
  so the swarm worker's `result` field would carry an ASCII console instead
  of a joke.

### What actually works — no settings change at all

Adding an explicit no-tool instruction to the worker prompt suppresses both
the recon and the dashboard:

> `Return one short clean joke. Do not use tools, commands, files, network
> access, agents, or subagents. Reply with the joke text only, no dashboard,
> no formatting, no preamble.`

Three consecutive runs: `status=SUCCESS`, **zero tool calls**, 66–77 byte
replies, no dashboard markup. Compare the bare prompt, which reliably ends
`CANCELED` with an empty response.

`--disable-slash-commands` was also tested and is **not** sufficient: the
run succeeded, but the slave-mode persona still applied and the joke came
back wrapped in the full ASCII dashboard, which is unusable as a worker
result.

### Revised recommendation

1. **Adopt the bounded no-tool worker instruction for the `agy` lane.** It
   needs no `settings.json` change, grants no new privilege, and is
   consistent with the ticket's existing rule that bounded instructions
   live in the task body.
2. **Do not implement the Section 4 Option-1 allowlist for this lane.** It
   was designed against a misdiagnosed cause. Keep Option 3
   (`--dangerously-skip-permissions`) prohibited as before.
3. The pre-existing hygiene findings in Section 3 still stand on their own
   merits and are unrelated to this lane: `permissions.allow` has 3 `sudo`
   rules and 5 `python3 -c` rules, and there is no `deny`/`ask` array. These
   remain the operator's call; nothing here changes them.
4. **Open question for the operator:** whether `slave-mode` should be
   globally installed for AGY at all, given it makes every headless
   invocation require shell access and emit dashboard-wrapped output. This
   ticket does not change it.

## 13. Reliability data: scoping the skill is not sufficient on its own

After Section 12, the operator's `slave-mode` skill was scoped — its
frontmatter description and a new "Activation Scope" section now state it
applies only to interactive TTY sessions, and explicitly not to
`agy --print` / `-p` or to workers dispatched by the Kanban dispatcher, CI,
or cron. The skill file lives outside this repository at
`~/.gemini/config/skills/slave-mode/SKILL.md`; a timestamped backup was
taken before editing.

That change was then measured against the bare, unmodified lane prompt
(`Return one short clean joke.`) and against the bounded no-tool prompt.
A run counts as hijacked if it made any tool call or returned dashboard
markup.

| Configuration | clean | hijacked |
|---|---|---|
| before scoping, bare prompt | 0 | all `CANCELED` |
| after scoping, bare prompt | 5 / 9 | 4 / 9 |
| bounded no-tool prompt (before + after scoping) | **9 / 9** | 0 |

**Conclusion: scoping the skill helps but is not a control.** A SKILL.md
description is advisory to the model, not enforced; it cut the hijack rate
from ~100% to roughly 45%, which is a coin flip and cannot be relied on by
an unattended worker.

The deterministic control is the **bounded no-tool instruction in the
worker's task body**, which was 9/9 across both skill configurations. That
instruction is required by the parent ticket anyway — bounded worker
instructions belong in the task body, never in the swarm parser's skill
field.

Therefore:

- The `agy` lane contract MUST carry the bounded no-tool instruction. It is
  the control, not an optimization.
- The skill scoping is retained as defense-in-depth and documentation: it
  costs nothing, halves the failure rate for any caller that forgets the
  instruction, and records why the boundary exists.
- Removing the skill was offered by the operator and is **not** necessary:
  the measured control works with the skill installed. Removal would also
  lose its intended interactive value.
- Still unchanged: no `settings.json` edit, no allowlist rule, and no
  `--dangerously-skip-permissions` anywhere in this path.
