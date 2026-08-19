# TELEGRAM-SWARM-UNREACHABLE-001 — a swarm request through Telegram produced nothing

Status: proposed. Not implemented. Blocks acceptance gate 8 of
`HERMES-MULTI-AGENT-CLAUDE-WORKER-001`.

## What happened

On 2026-08-19 the user sent a four-lane swarm request through Telegram, the
last unexecuted gate of the multi-agent programme. The same swarm had
completed unassisted from the CLI 20 minutes earlier (tenant
`e2e-fourlane-v4`, seven cards, zero blocked events). Through Telegram it
produced nothing.

Measured after the attempt:

```
tenant 'gate8-telegram'                       0 cards
tasks created after 17:37:10                  0
total tasks in kanban.db                      43 (unchanged)
grok/antigravity/claude-code invocations      0
```

## What is established

Both of the agent's tool calls failed:

```
18:00:09  terminal  command=None   -> "Invalid command: expected string, got NoneType"  (0.00s)
18:02:10  terminal  timed out      -> exit_code 124, "[Command timed out after 60s]"    (60.87s)
```

Nothing was created. That is the whole of what the evidence establishes.

**Why the calls failed is NOT established.** An earlier draft of this ticket
presented three "causes"; two of them are real defects but neither is shown to
be the reason this attempt failed, and one of them cannot be, as noted below.
They are recorded here as defects found while investigating, not as causes.

The second call is the informative one: it ran for 60s and created zero cards.
So whatever that command was, it had not reached card creation when it was
killed. The command text is not in the logs.

For scale, from the CLI run 20 minutes earlier: its seven cards all carry
`created_at = 1787132230`, a single second, so graph creation itself is fast;
the run then took 872s (17:37:10 to 17:51:42) to finish executing.

## Defect A — the kanban toolset gate reads a different key than the config sets

Real, and worth fixing on its own. **It is not a cause of this failure**:
Defect B means the kanban toolset contains no swarm tool, so enabling it would
not have let the agent build a swarm.

`config.yaml` declares the toolset per platform:

```yaml
platform_toolsets:
  telegram:
    - kanban        # <-- declared
    - terminal
toolsets:
  - hermes-cli      # <-- no kanban
```

`tools/kanban_tools.py` gates every kanban tool on the top-level key, and the
logged check functions reach it directly:

```python
def _check_kanban_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()      # <-- the logged check calls this

def _profile_has_kanban_toolset() -> bool:
    cfg = load_config()
    toolsets = cfg.get("toolsets", [])        # top-level, NOT platform_toolsets
    return "kanban" in toolsets
```

`_check_kanban_orchestrator_mode()` reaches the same function by the same
route. Observed at 17:55:51:

```
check_fn _check_kanban_mode returned False; dependent tools will be unavailable this turn
check_fn _check_kanban_orchestrator_mode returned False; dependent tools will be unavailable this turn
```

So `platform_toolsets.telegram: [kanban, ...]` has no effect on the gate, and
the gate decides. A reader of `config.yaml` reasonably concludes Telegram has
kanban tools; it does not, on any surface, unless the top-level `toolsets`
list contains `kanban`.

**Not yet decided:** whether to make the gate consult `platform_toolsets` for
the active platform, or keep the top-level list authoritative and have
`toolset_validation.py` reject a `platform_toolsets` entry naming a toolset
the gate cannot see. The second is smaller; the first is what the config
appears to promise.

## Defect B — no swarm-creation tool is registered

The kanban tools registered for the model are:

```
kanban_block  kanban_comment  kanban_complete  kanban_create  kanban_heartbeat
kanban_link   kanban_list     kanban_show      kanban_unblock
```

No swarm tool, and `kanban_create` cannot set the lane contract fields
(`expected_lane_id`, `preflight_skill_id`, `expected_lane_count`) that
`create_swarm()` writes and `validate_completion()` enforces. An agent can
create and link tasks, but the result is not a lane-bound swarm and the
verifier has nothing to enforce.

So the only path from a Telegram message to a swarm **visible in the current
tool registration** is shelling out to `hermes kanban swarm` through
`terminal`. Whether some other path exists was not exhaustively verified.

## Defect C — `terminal.timeout: 60` is shorter than some of the work asked of it

```yaml
terminal:
  timeout: 60
```

Real as a configuration mismatch — the request itself specified a 300s
per-worker cap, and the equivalent CLI run took 872s end to end, so any path
that waits for swarm completion cannot fit in 60s.

**But raising it would not have fixed this attempt.** Zero cards were created
during those 60 seconds, so the command had not reached swarm creation; a
longer timeout would have let a stalled command stall for longer. This is a
defect to fix on its own terms, not the reason gate 8 failed.

## Also observed, cause not established

At 18:00:09 the model called `terminal` with a null command. Whether that is a
malformed tool call from the model or something in this surface's
streaming/parsing dropping the argument is not established. No truncation or
stop-reason record separates the two.

Determining both this and the 60s stall needs the same request run against the
same agent runtime off Telegram — the variable-isolation method that separated
"the swarm works" from "Telegram delivers it" earlier the same day.

## Not in scope

No change to `create_swarm`, `validate_completion`, the lane quorum, or the
dispatcher. Those were exercised end to end and passed.
