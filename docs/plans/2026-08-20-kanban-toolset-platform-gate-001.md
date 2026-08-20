# KANBAN-TOOLSET-PLATFORM-GATE-001 — make the kanban toolset gate read platform_toolsets (TELEGRAM-SWARM-UNREACHABLE-001 Defect A)

Status: implemented, pending cross-review of the implementation. Original
design (below) was corrected after implementation revealed a real flaw --
see "Implementation" for what actually shipped and why it differs.

## The defect, as already recorded

[TELEGRAM-SWARM-UNREACHABLE-001](2026-08-19-telegram-swarm-unreachable-001.md)
Defect A: `tools/kanban_tools.py::_profile_has_kanban_toolset` reads the
top-level `toolsets` config key, not `platform_toolsets`:

```python
def _profile_has_kanban_toolset() -> bool:
    cfg = load_config()
    toolsets = cfg.get("toolsets", [])        # top-level, NOT platform_toolsets
    return "kanban" in toolsets
```

`_check_kanban_mode` and `_check_kanban_orchestrator_mode` both reach this
function. A config declaring `platform_toolsets.telegram: [kanban, ...]` —
which is exactly what this host's `config.yaml` declares today — has no
effect on whether the Telegram agent's tool schema actually contains any
`kanban_*` tool.

**Confirmed still live, on this host, while writing this ticket** (not
assumed from the old record): `~/.hermes/config.yaml` has
`platform_toolsets.telegram: [..., kanban, ...]` and, until a same-day
workaround, top-level `toolsets: [hermes-cli]` with no `kanban`. A
same-session workaround (adding `kanban` to the top-level list) is live now
so `GATE8-SWARM-CREATION-TOOL-001`'s new `kanban_swarm` tool could be
tested today — that is a stopgap, not a fix, and this ticket is the fix the
workaround stands in for.

Why the workaround isn't the fix: it grants every `kanban_*` tool to every
profile that resolves through the top-level list, not just Telegram — the
config schema's whole point in having a *platform-scoped* key is to avoid
exactly that. It also does not generalize: the same defect blocks any other
toolset a config author scopes to one platform via `platform_toolsets`
without also adding it to the top-level list, `kanban` is just the instance
that got noticed.

## What actually drives toolset resolution today (this was not in the original defect record)

Investigated directly against the current code, not assumed from the old
ticket:

- `hermes_cli/tools_config.py::_get_platform_tools(config, platform)` is
  the real source of truth for "which toolsets does platform X have."
  It reads `config["platform_toolsets"][platform]` (falling back to a
  per-platform default), expands composites via `toolsets.resolve_toolset`,
  and applies platform-allow rules. This is what actually computes
  `agent.enabled_toolsets` for every surface -- Telegram
  (`gateway/run.py:13860`/`18600`) **and the CLI**
  (`cli.py:16095`).
- The top-level `toolsets` key defaults to `["hermes-cli"]`
  (`hermes_cli/config.py:981`). **Correction, checked rather than assumed
  on the first pass of this ticket:** there is no config-migration step
  that folds it into `platform_toolsets.cli` -- `migrate_config()`
  (`hermes_cli/config.py:5601-6220`) only validates `platform_toolsets`,
  it never reads or writes the top-level key. CLI's actual fallback is
  `hermes_cli/platforms.py:22`'s hardcoded `default_toolset="hermes-cli"`,
  used by `_get_platform_tools` whenever `platform_toolsets.cli` is unset
  -- independent of whatever the top-level `toolsets` list says. The only
  other read site is a diagnostic dump (`hermes_cli/dump.py:422`). The
  top-level key is effectively dead for toolset resolution, not folded
  into anything; it is not the live source of truth for any platform's
  tool schema, for a different reason than first written here.
- So `_profile_has_kanban_toolset` and the real `enabled_toolsets`
  computation are **two independent mechanisms answering the same
  question, one of them stale**. `model_tools._compute_tool_definitions`
  already correctly resolves `"kanban"` into the tools a session should
  see, using `enabled_toolsets`; `registry.get_definitions` then re-derives
  "is kanban enabled" a second way via `check_fn=_check_kanban_mode`, which
  vetoes what the first mechanism already correctly allowed.

This reframes the defect: it isn't only "the wrong config key," it's
**double gating with two mechanisms that can disagree**, and today they do.

## Is a platform-aware check safe to add here?

- **Session context is available.** `get_session_env("HERMES_SESSION_PLATFORM")`
  is already used inside a `check_fn` elsewhere —
  `tools/yuanbao_tools.py::_check_yuanbao` (~line 420) does exactly this,
  as working precedent. Traced the call path: `registry.get_definitions()`
  is invoked from `model_tools.get_tool_definitions()`, itself called from
  `agent/agent_init.py` during per-request agent construction -- after the
  gateway has already bound session ContextVars for that message. Reading
  the platform inside a `check_fn` is not a new pattern here.
- **Risk is bounded to additive, at the two check_fn call sites.** Both
  `_check_kanban_mode` and `_check_kanban_orchestrator_mode` are already
  gated behind the `HERMES_KANBAN_TASK` short-circuit first
  (dispatcher-spawned workers), so this change only affects non-worker
  sessions there -- exactly the orchestrator/chat surfaces the toolset is
  meant for. No test in the repo asserts on `_profile_has_kanban_toolset` /
  `_check_kanban_mode` / `_check_kanban_orchestrator_mode` directly
  (checked); the fixture-driven tests in `tests/tools/test_kanban_tools.py`
  all set `HERMES_KANBAN_TASK` or the top-level `toolsets` list explicitly
  and are unaffected either way. `tests/hermes_cli/
  test_kanban_worker_spawn_toolsets.py` exercises the dispatcher-spawn
  `--toolsets` pin via `platform_toolsets.cli`, a different path, also
  unaffected.

- **A third call site exists and needs its own accounting: `agent/
  skill_utils.py::_detect_environment`** (~line 236), which gates whether
  kanban-tagged skills are *offered* to the user. Its own comment states
  the intent explicitly: "Mirror the same signals the kanban tools
  themselves gate on ... so the offer filter agrees with tool
  availability" -- so routing it through the corrected,
  platform-aware `_profile_has_kanban_toolset` is not an incidental side
  effect, it is what this call site already asked for and was getting
  wrong in the same way the tools were. **But its result is cached in
  `_ENV_DETECT_CACHE`, keyed only by the string `"kanban"`, for the
  process lifetime** (`agent/skill_utils.py:220-268`) -- not per session,
  not per platform. Today that is safe, because the answer never varied
  within a process regardless of which platform asked. Once
  `_profile_has_kanban_toolset` becomes platform-aware, a single long-lived
  gateway process serving more than one platform (e.g. Telegram and CLI in
  the same process) would cache the *first* platform's answer and serve it
  to every other platform afterward -- silently wrong in whichever
  direction the first caller happened to land. **This must be addressed
  as part of implementation, not deferred**: either key
  `_ENV_DETECT_CACHE` by `(env, platform)` instead of `env` alone, or stop
  routing this call site through the cache for the `"kanban"` key
  specifically. Left as an implementation decision, not a design one --
  both are small, mechanical changes to `agent/skill_utils.py`.
- **No evidence of an existing profile relying on today's behavior to
  *deny* kanban tools** despite an explicit `platform_toolsets.<platform>:
  [kanban]` entry -- the defect can only ever have suppressed access no
  config author asked for, never granted access one didn't.

## Proposal

Rewrite `_profile_has_kanban_toolset` to resolve the active platform (via
`get_session_env("HERMES_SESSION_PLATFORM", "") or "cli"`, matching the
existing fallback idiom in `plugins/coding-cli/__init__.py`) and reuse
`hermes_cli.tools_config._get_platform_tools(cfg, platform)` -- the same
function that already computes `enabled_toolsets` for every real surface --
rather than re-deriving toolset membership a second, independent way:

```python
def _profile_has_kanban_toolset() -> bool:
    try:
        from gateway.session_context import get_session_env
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or "cli"
        cfg = load_config()
        return "kanban" in _get_platform_tools(cfg, platform)
    except Exception:
        return False
```

This closes the double-gating problem at its root: after this change there
is exactly one mechanism deciding "does this session have the kanban
toolset," not two that can disagree.

Once this lands, the same-session top-level-`toolsets` workaround should be
reverted (remove `kanban` from the top-level list, leave it declared only
under `platform_toolsets.telegram`) so the scoping the config already
promises actually holds.

## Not yet decided

- Whether `_get_platform_tools` needs a small compatibility check for
  callers outside a request context (pure CLI script invocation with no
  session bound at all) -- `get_session_env` already falls back cleanly to
  `"cli"` in that case per its own documented resolution order, but this
  should be exercised by a test, not assumed from reading the function.
- Whether this same fix should also be applied anywhere else in the
  codebase using the identical top-level-`toolsets`-only pattern for a
  *different* toolset name -- this ticket found the pattern is specific to
  `_profile_has_kanban_toolset` (grepped for other functions reading
  `cfg.get("toolsets", [])` directly; none found), so is not itself
  evidence of a wider problem, but that grep was not exhaustive against
  every plugin/overlay in the tree.
- The original ticket's alternative option (keep the top-level list
  authoritative, reject a `platform_toolsets` entry the gate can't see via
  `toolset_validation.py`) is not pursued here -- it would leave the
  scoping promise broken, just loudly instead of silently, and the
  investigation above found a working, low-risk path to actually deliver
  what the config schema promises instead.

## Review round 1

Independent agent, instructed to verify every checkable claim against live
code rather than trust the draft. Confirmed accurate: the current
`_profile_has_kanban_toolset` code, `_get_platform_tools`'s return type
(a set of toolset names, so the proposed one-line membership check is
correctly typed and would work), the `_check_yuanbao` precedent for
reading platform inside a `check_fn`, and the `enabled_toolsets` call
sites in `gateway/run.py`/`cli.py`. Found two real issues, both corrected
above: a false claim that config migration folds the top-level `toolsets`
key into `platform_toolsets.cli` (no such migration exists; corrected to
describe the actual, different reason the top-level key is inert), and a
missing third call site (`agent/skill_utils.py::_detect_environment`)
whose process-lifetime cache would serve stale, platform-wrong results
once the underlying check becomes platform-aware -- now recorded as a
required part of implementation, not an afterthought.

## Implementation

**The design above, as written, was wrong -- caught by this repo's own test
suite before it shipped, not after.**

The proposed `"kanban" in _get_platform_tools(cfg, platform)` was tested
against the live Telegram config during design and returned `True` as
expected. It was not tested against an *empty, default* config -- doing so
during implementation surfaced that `_get_platform_tools` has a "recover
non-configurable platform toolsets" pass (`hermes_cli/tools_config.py`,
after the `has_explicit_config` branch) that adds any toolset whose static
tool names are a subset of the platform's default composite,
**unconditionally** -- not gated on any user config at all. Since every
`kanban_*` tool name is statically listed inside the `hermes-cli` composite
(itself required so the tools are reachable at all, per
`GATE8-SWARM-CREATION-TOOL-001`), `_get_platform_tools(cfg, "cli")` returns
`"kanban"` for a config with **zero** toolset configuration of any kind.
Routing `_profile_has_kanban_toolset` through it would have made every
kanban tool visible in *every* session on *every* platform, unconditionally
-- the opposite of a platform-scoped gate, and a straightforward
over-authorization bug. `tests/tools/test_kanban_tools.py::
test_kanban_tools_hidden_without_env_var` failed immediately once the
change was made, which is what caught it.

**What actually shipped instead:** `_profile_has_kanban_toolset` reads
`platform_toolsets` directly rather than going through toolset resolution
at all. If the active platform (`get_session_env("HERMES_SESSION_PLATFORM")`)
has an explicit, saved list in `platform_toolsets`, that list alone decides
-- `"kanban" in explicit_list`, nothing else consulted. Only when there is
no explicit entry for the platform (or no platform context at all, e.g. a
bare CLI script) does it fall back to the legacy top-level `toolsets` list,
preserving every surface that already depended on that behavior. This
never touches `_get_platform_tools` or its recovery logic, so the
over-authorization failure mode does not apply -- confirmed by a dedicated
regression test (`test_kanban_toolset_check_does_not_leak_via_platform_
composite_recovery`) that pins the empty-config case to `False`.

`agent/skill_utils.py::_detect_environment`'s cache fix (keying by
`f"kanban:{platform}"` instead of the bare `"kanban"` string, per this
ticket's original risk analysis) shipped as designed -- that part of the
original plan was correct.

**A second, unrelated bug surfaced writing the tests, not the implementation
itself:** the first draft of the caching regression test used
`gateway.session_context.set_session_vars`/`clear_session_vars` to simulate
different platforms. Their own docstring says they are "not
nestable/stack-safe" and `set_session_vars` flips a process-global
`_session_context_engaged` flag with no reset path back to "never
engaged." Using them inside one test made `get_session_env` stop falling
back to `os.environ` for the rest of the pytest process -- silently
breaking an unrelated, already-passing test
(`test_cmd_swarm_subscribes_synthesizer_when_session_context_present` in
`tests/hermes_cli/test_kanban_cli.py`, which sets platform via
`monkeypatch.setenv`) purely by import/collection order, with no code
relationship between the two tests. Fixed by rewriting the test to
monkeypatch `gateway.session_context.get_session_env` directly instead of
touching real session state. Left as a note for future test-writing in
this codebase: `set_session_vars`/`clear_session_vars` are unsafe to use
inside a test unless the whole test file is willing to accept
process-global side effects for every test that runs after it in the same
process.

Test coverage: 5 new tests in `tests/tools/test_kanban_tools.py` (platform-
scoped grant, platform-scoped explicit denial does not fall through to the
top-level list, no-platform-context fallback to the legacy list, and the
empty-config regression guard for the rejected design) and 1 new test in
`tests/agent/test_skill_utils.py` (per-platform cache isolation). Full run
across every kanban/toolset-adjacent test file: 287 tests pass, in two
different file orderings.

## Not in scope

No change to `enabled_toolsets` computation itself, `_get_platform_tools`,
`toolsets.resolve_toolset`, the dispatcher, or any toolset other than
`kanban`'s gating mechanism.
