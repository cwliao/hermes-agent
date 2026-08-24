"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed


IDEMPOTENT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "web_search",
        "web_extract",
        "session_search",
        "browser_snapshot",
        "browser_console",
        "browser_get_images",
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_read_multiple_files",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_list_directory_with_sizes",
        "mcp_filesystem_directory_tree",
        "mcp_filesystem_get_file_info",
        "mcp_filesystem_search_files",
    }
)

MUTATING_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "todo",
        "memory",
        "skill_manage",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_navigate",
        "send_message",
        "cronjob",
        "delegate_task",
        "process",
    }
)

# Tools that are legitimately re-invoked with identical arguments and may
# legitimately return an unchanged result while waiting on external progress —
# background-process management and job pollers. The identical-call loop
# notice (agent.stall_guards) never fires for these, so polling patterns like
# ``process(action="poll")`` or repeatedly checking a generation job stay
# unannotated.
STALL_GUARD_REPEATABLE_TOOLS = frozenset(
    {
        "process",
        "bfl_flux3_get_result",
    }
)

# Poller naming conventions (e.g. ``<vendor>_get_result``) used by generated /
# MCP tool surfaces. Matched as suffixes so vendor-prefixed pollers are exempt
# without enumerating every vendor.
_STALL_GUARD_REPEATABLE_SUFFIXES = (
    "_get_result",
    "_poll",
)

# The notice fires on the Nth consecutive identical call (same tool, same
# canonical args, same result). 3 tolerates one legitimate double-check while
# catching the observed re-issue loops (3x/4x identical calls in eval traces).
STALL_GUARD_IDENTICAL_CALL_THRESHOLD = 3

# Result-reference stubbing (agent.stall_guards): from the 2nd consecutive
# identical call whose FRESH result is byte-identical to the previous one,
# the duplicate payload is replaced in context by a short reference stub.
# Results under this size aren't worth stubbing (the stub itself plus the
# lost locality outweigh the savings), and error results are never stubbed
# (the model must see every fresh error verbatim).
IDENTICAL_RESULT_STUB_MIN_CHARS = 512

# How much of the canonical args JSON the stub carries so the model still
# knows WHAT the referenced call was even if context compression later
# evicts the referenced result (cheap dangling-reference mitigation).
_RESULT_STUB_ARGS_PREVIEW_CHARS = 120


def is_stall_guard_repeatable(tool_name: str) -> bool:
    """Whether a tool is exempt from the identical-call loop notice."""
    if tool_name in STALL_GUARD_REPEATABLE_TOOLS:
        return True
    return tool_name.endswith(_STALL_GUARD_REPEATABLE_SUFFIXES)

DETERMINISTIC_BLOCKER_CLASSES = frozenset(
    {"missing_target", "permission", "invalid_workdir", "malformed_input"}
)
_TARGET_KEYS = frozenset(
    {
        "path", "file", "file_path", "filename", "target", "destination",
        "dest", "source", "src", "workdir", "cwd", "directory",
        # Tool-specific target identifiers.  These values remain private and
        # are only hashed for the in-memory ledger; content/prompt fields are
        # intentionally excluded so changing payload does not evade a blocker
        # for the same target.
        "url", "key", "name", "job_id", "message_id", "chat_id",
        "channel", "ref", "element", "goal", "script",
    }
)


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Hard stops
    are explicit opt-in so interactive CLI/TUI sessions get a gentle nudge unless
    the user enables circuit-breaker behavior in config.yaml.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    cross_turn_failure_halt_after: int = 3
    cross_turn_ledger_max_entries: int = 128
    cross_turn_ttl_seconds: int = 1800
    unattended_soft_mode: bool = False
    # True on surfaces where nobody watches the tool trace scroll by -- the
    # tool-outcome footer is only useful there. Set by the caller, not from
    # config.yaml, so there is one definition of "unattended".
    unattended: bool = False
    configuration_warning: str = ""
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    loop_caps: "LoopCapConfig" = field(default_factory=lambda: LoopCapConfig())

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
        *,
        default_hard_stop_enabled: bool = False,
    ) -> "ToolCallGuardrailConfig":
        """Build config from the ``tool_loop_guardrails`` config section.

        ``default_hard_stop_enabled`` lets unattended runtimes choose a safe
        default without changing the interactive CLI/TUI behavior. The
        ``auto`` value follows that runtime default. On unattended surfaces,
        explicitly disabling the stop requires ``unattended_soft_mode`` so a
        softened safety policy is observable.
        """
        if not isinstance(data, Mapping):
            return cls(hard_stop_enabled=default_hard_stop_enabled)

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls(hard_stop_enabled=default_hard_stop_enabled)
        hard_stop_value = data.get("hard_stop_enabled")
        soft_mode = _as_bool(data.get("unattended_soft_mode"), False)
        configuration_warning = ""
        if isinstance(hard_stop_value, str) and hard_stop_value.strip().lower() == "auto":
            hard_stop_enabled = default_hard_stop_enabled
        else:
            hard_stop_enabled = _as_bool(hard_stop_value, defaults.hard_stop_enabled)
            if default_hard_stop_enabled and not hard_stop_enabled and not soft_mode:
                hard_stop_enabled = True
                configuration_warning = (
                    "tool_loop_guardrails.hard_stop_enabled=false was ignored for "
                    "an unattended surface; set unattended_soft_mode=true to opt "
                    "into visibly degraded soft mode"
                )
        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=hard_stop_enabled,
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
            loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")),
            cross_turn_failure_halt_after=_positive_int(
                data.get("cross_turn_failure_halt_after"),
                defaults.cross_turn_failure_halt_after,
            ),
            cross_turn_ledger_max_entries=_positive_int(
                data.get("cross_turn_ledger_max_entries"),
                defaults.cross_turn_ledger_max_entries,
            ),
            cross_turn_ttl_seconds=_positive_int(
                data.get("cross_turn_ttl_seconds"),
                defaults.cross_turn_ttl_seconds,
            ),
            unattended_soft_mode=soft_mode,
            configuration_warning=configuration_warning,
        )


# Default session-wide caps, matching Claude Code's v2.1.212 runaway-loop
# Per-turn (per-agent-loop) caps on runaway-prone tool calls. Counts reset at
# the start of every agent loop (reset_for_turn), so the limit is "within a
# single turn" rather than cumulative over the whole session. A single loop
# issuing dozens of web searches or spawning dozens of subagents is already
# pathological, so the defaults are deliberately low.
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50


@dataclass(frozen=True)
class LoopCapConfig:
    """Per-turn caps on runaway-prone tool calls.

    Inspired by Claude Code v2.1.212 (Week 29, July 2026), which added caps on
    WebSearch calls and subagent spawns to stop runaway search / delegation
    loops. Here the caps count *within a single agent loop* (one turn): the
    counters reset in ``reset_for_turn`` at the start of every
    ``run_conversation``, so a legitimate multi-turn session is never starved,
    but a single turn that spirals into an unbounded search / delegation loop
    is stopped.

    Semantics differ from the per-turn loop *detector* above (which keys on
    repeated identical/failing calls): these caps are a hard ceiling on the
    total count of a tool within the turn and fire regardless of
    ``hard_stop_enabled``. A value of ``0`` disables the cap (unlimited).
    """

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        """Build config from the ``tool_loop_guardrails.loop_caps`` section."""
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        return cls(
            max_web_searches=_non_negative_int(
                data.get("max_web_searches"), defaults.max_web_searches
            ),
            max_subagents=_non_negative_int(
                data.get("max_subagents"), defaults.max_subagents
            ),
        )


@dataclass(frozen=True)
class IdenticalCallObservation:
    """Outcome of observing one completed tool call for the stall guards.

    ``notice`` is the identical-call loop-breaker notice (appended after the
    result). ``stub`` is the result-reference replacement for a byte-identical
    duplicate result (replaces the result content). Both may be set on the
    same call (3rd+ identical call): the stub replaces the payload and the
    notice is appended after it.
    """

    notice: str | None = None
    stub: str | None = None


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        # Argument digests and target identities are private controller state;
        # never put them into durable tool results or transcripts.
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Safety-fallback classifier used only when callers don't pass ``failed``.

    Mirrors ``agent.display._detect_tool_failure`` exactly so the guardrail
    never disagrees with the CLI's user-visible ``[error]`` tag. Production
    callers in ``run_agent.py`` always pass an explicit ``failed=`` derived
    from ``_detect_tool_failure``; this function exists so standalone callers
    (tests, tooling) still get consistent behavior.
    """
    if result is None:
        return False, ""
    if file_mutation_result_landed(tool_name, result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        return False, ""

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                return True, " [full]"

    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


def is_terminal_kanban_failure(tool_name: str, result: str | None) -> bool:
    """Return True for a lifecycle mutation rejected because it is terminal.

    A completed Kanban worker must not spend the rest of its turn retrying the
    same ``kanban_complete``/``kanban_block`` call.  The Kanban tool surface
    marks this deterministic state transition with a private structured flag;
    keeping the classifier here lets the runtime guard stop it even when the
    normal configurable hard-stop policy is disabled.
    """
    if tool_name not in {"kanban_complete", "kanban_block"}:
        return False
    data = safe_json_loads(result) if result else None
    return isinstance(data, Mapping) and data.get("kanban_terminal_error") is True


def classify_failure_class(tool_name: str, result: str | None) -> str:
    """Classify deterministic blockers without retaining their raw text."""
    if not result:
        return "unknown"
    parsed = safe_json_loads(result)
    fragments: list[str] = []
    if isinstance(parsed, Mapping):
        for key in ("error", "stderr", "stdout", "message", "reason"):
            value = parsed.get(key)
            if value:
                fragments.append(str(value))
        exit_code = parsed.get("exit_code")
        if exit_code not in (None, 0) and not fragments:
            fragments.append(f"exit code {exit_code}")
    fragments.append(str(result)[:1200])
    text = " ".join(fragments).lower()
    if any(token in text for token in ("permission denied", "access denied", "approval denied", "not approved")):
        return "permission"
    if any(token in text for token in ("working directory", "workdir", "cwd", "directory does not exist")):
        return "invalid_workdir"
    if any(token in text for token in ("no such file", "file not found", "path not found", "does not exist", "missing target")):
        return "missing_target"
    if any(token in text for token in ("syntaxerror", "parse error", "invalid json", "malformed", "schema validation", "invalid syntax")):
        return "malformed_input"
    return "unknown"


def _target_identity(tool_name: str, args: Mapping[str, Any]) -> str:
    """Return a private target identity that never leaves the controller."""
    targets: list[str] = []
    for key, value in args.items():
        key_lower = str(key).lower()
        if key_lower in _TARGET_KEYS:
            targets.append(f"{key_lower}={value}")
    if not targets and tool_name == "terminal":
        command = str(args.get("command", ""))
        candidates = re.findall(r"(?:/[^\s'\"]+|[A-Za-z]:[\\/][^\s'\"]+)", command)
        targets.extend(candidates[:4])
    if not targets:
        targets.append(tool_name)
    canonical = json.dumps(sorted(targets), ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{tool_name}:{_sha256(canonical)}"


class ToolCallGuardrailController:
    """Bounded per-turn and cross-turn controller for tool-call loops."""

    def __init__(self, config: ToolCallGuardrailConfig | None = None):
        self.config = config or ToolCallGuardrailConfig()
        self._cross_turn_failures: dict[str, tuple[int, float, str]] = {}
        self._cross_turn_lock = threading.RLock()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # Identical-call loop-breaker state (agent.stall_guards): tracks the
        # CONSECUTIVE streak of identical (tool, canonical args) calls whose
        # results were also identical. Any different call — or a different
        # result — resets the streak, so legitimate re-reads after edits and
        # varied polling are never flagged. Per-turn, like everything else here.
        # NOTE: open PR #85352 (patrykkopycinski) tracks no-progress loops
        # ACROSS turns via a detection window — a different mechanism from
        # this per-turn consecutive streak. Coordinate future work there.
        self._identical_streak_sig: ToolCallSignature | None = None
        self._identical_streak_result_hash: str = ""
        self._identical_streak_count: int = 0
        # tool_call_id of the FIRST call in the current streak, so a
        # result-reference stub can point at the message that carries the
        # full payload.
        self._identical_streak_first_call_id: str = ""
        # tool_call_id -> spillover file path for results that were persisted
        # out of context (persisted-output preview). Lets a reference stub
        # carry the file path so the reference can't dangle when the first
        # occurrence entered context as a preview.
        self._persisted_result_paths: dict[str, str] = {}
        # Per-turn runaway-loop cap counters. Reset every turn (this method
        # runs at the start of each run_conversation), so the caps bound a
        # single agent loop rather than accumulating across the session.
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0
        # Per-turn tally for FABRICATION-REMEDY-001. The existing counters
        # measure repetition; these measure this turn's outcome, which is a
        # different property and the one that went unguarded on 2026-08-19.
        self._turn_calls_attempted = 0
        self._turn_calls_failed = 0
        self._turn_failed_tools: list[str] = []

    def reset_for_session(self) -> None:
        """Clear all state when the agent session identity changes."""
        with self._cross_turn_lock:
            self._cross_turn_failures.clear()
        self.reset_for_turn()

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def turn_tool_outcome(self) -> "TurnToolOutcome":
        """This turn's tool-call tally, for the outcome footer."""

        return TurnToolOutcome(
            attempted=self._turn_calls_attempted,
            failed=self._turn_calls_failed,
            failed_tools=tuple(self._turn_failed_tools),
        )

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))

        # A terminal Kanban mutation failure is an unconditional stop.  This
        # is deliberately checked before the configurable hard-stop policy:
        # allowing a stale worker to retry a completed card is a lifecycle
        # correctness bug, not an optional loop-warning preference.
        if (
            self._halt_decision is not None
            and self._halt_decision.code == "kanban_terminal_task_halt"
        ):
            return self._halt_decision

        # ── Per-turn runaway-loop caps ──────────────────────────────────
        # These are hard ceilings on how many times a runaway-prone tool may
        # be called within a single agent loop (turn). They apply regardless
        # of hard_stop_enabled (which only governs the per-turn loop detector).
        # We block BEFORE the call runs once the count is already at the cap,
        # then increment for an allowed call so the (cap+1)-th is refused.
        cap_block = self._check_loop_cap(tool_name, _coerce_args(args), signature)
        if cap_block is not None:
            return cap_block

        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        target_key = _target_identity(tool_name, _coerce_args(args))
        with self._cross_turn_lock:
            self._prune_cross_turn_failures()
            cross_record = self._cross_turn_failures.get(target_key)
        if cross_record is not None:
            cross_count, _last_seen, _failure_class = cross_record
            if cross_count >= self.config.cross_turn_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="cross_turn_deterministic_blocker",
                    message=(
                        f"Blocked {tool_name}: the same deterministic blocker persisted "
                        f"for this target across {cross_count} failed attempts. Stop "
                        "retrying this path; change the target or report the blocker."
                    ),
                    tool_name=tool_name,
                    count=cross_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same tool call failed {exact_count} "
                    "times with identical arguments. Stop retrying it unchanged; "
                    "change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"Blocked {tool_name}: this read-only call returned the same "
                            f"result {repeat_count} times. Stop repeating it unchanged; "
                            "use the result already provided or try a different query."
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
        kanban_worker: bool = False,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)

        self._turn_calls_attempted += 1
        if failed:
            self._turn_calls_failed += 1
            if tool_name not in self._turn_failed_tools:
                self._turn_failed_tools.append(tool_name)

        if failed:
            failure_class = classify_failure_class(tool_name, result)
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            if kanban_worker and is_terminal_kanban_failure(tool_name, result):
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="kanban_terminal_task_halt",
                    message=(
                        f"Stopped {tool_name}: the Kanban task is already terminal. "
                        "Do not retry the lifecycle mutation; end this worker turn."
                    ),
                    tool_name=tool_name,
                    count=1,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            if failure_class in DETERMINISTIC_BLOCKER_CLASSES:
                self._record_cross_turn_failure(
                    _target_identity(tool_name, args), failure_class
                )

            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this turn. "
                        "Stop retrying the same failing tool path and choose a different approach."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with identical arguments. "
                        "This looks like a loop; inspect the error and change strategy "
                        "instead of retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        with self._cross_turn_lock:
            self._cross_turn_failures.pop(_target_identity(tool_name, args), None)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query instead of "
                    "repeating it unchanged."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def observe_identical_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
    ) -> str | None:
        """Track consecutive identical calls; return a loop-breaker notice or None.

        Back-compat wrapper around :meth:`observe_call` for callers that only
        care about the loop-breaker notice.
        """
        return self.observe_call(tool_name, args, result).notice

    def observe_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        tool_call_id: str = "",
        failed: bool = False,
    ) -> "IdenticalCallObservation":
        """Track consecutive identical calls; return notice + dedupe stub info.

        Two independent outputs from the same consecutive-streak tracker:

        - ``notice``: the compact loop-breaker notice, fired when the SAME
          tool is called with identical canonical arguments AND returns an
          identical result for the ``STALL_GUARD_IDENTICAL_CALL_THRESHOLD``-th
          (and every subsequent) consecutive time within the turn. Purely
          observational — never blocks the call. Allowlisted pollers
          (``is_stall_guard_repeatable``) are exempt from the NOTICE.
        - ``stub``: a short reference replacement for the CURRENT result,
          produced from the 2nd consecutive identical call whose fresh result
          is byte-identical to the previous one. The tool still executed —
          only the context representation is deduplicated, so polling
          semantics are preserved (a changed result flows through whole and
          resets the streak). Pollers are NOT exempt from stubbing: for a
          poller, an identical result means nothing changed, which is exactly
          when the stub saves the most context and loses nothing. Results
          under ``IDENTICAL_RESULT_STUB_MIN_CHARS`` and failed/error results
          are never stubbed, and only plain-string results are considered.

        Any intervening different call or changed result resets the streak.
        Callers substitute/append at tool RESULT construction time, which is
        cache-safe: tool results are append-only and never mutate
        already-sent context.
        """
        is_plain_str = isinstance(result, str)
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        result_hash = _result_hash(result) if is_plain_str else ""

        if (
            is_plain_str
            and self._identical_streak_sig == signature
            and self._identical_streak_result_hash == result_hash
        ):
            self._identical_streak_count += 1
        else:
            # New streak (or non-string result, which never forms a streak —
            # multimodal content lists pass through untouched).
            self._identical_streak_sig = signature if is_plain_str else None
            self._identical_streak_result_hash = result_hash
            self._identical_streak_count = 1 if is_plain_str else 0
            self._identical_streak_first_call_id = tool_call_id or ""

        count = self._identical_streak_count

        notice = None
        if (
            not is_stall_guard_repeatable(tool_name)
            and count >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD
        ):
            ordinal = f"{count}{'th' if 11 <= count % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(count % 10, 'th')}"
            notice = (
                f"[hermes note: this is the {ordinal} consecutive identical call to "
                f"{tool_name} with identical arguments returning the same result. "
                "Do not repeat it — change arguments, use a different tool, or "
                "proceed with what you have.]"
            )

        stub = None
        if (
            is_plain_str
            and count >= 2
            and not failed
            and len(result) >= IDENTICAL_RESULT_STUB_MIN_CHARS
        ):
            stub = self._build_result_reference_stub(tool_name, args)

        return IdenticalCallObservation(notice=notice, stub=stub)

    def record_persisted_result(self, tool_call_id: str, file_path: str) -> None:
        """Remember the spillover path a persisted result was saved to.

        When the first occurrence of a result entered context as a
        persisted-output preview, a later reference stub must carry the
        spillover file path so the reference can't dangle.
        """
        if tool_call_id and file_path:
            self._persisted_result_paths[tool_call_id] = file_path

    def _build_result_reference_stub(
        self, tool_name: str, args: Mapping[str, Any] | None
    ) -> str:
        """Build the reference stub replacing a byte-identical duplicate result.

        Carries the tool name + a canonical-args preview so that even if
        context compression later evicts the referenced result, the model
        still knows WHAT the call was (cheap dangling-reference mitigation).
        """
        try:
            args_preview = canonical_tool_args(_coerce_args(args))
        except TypeError:
            args_preview = "{}"
        if len(args_preview) > _RESULT_STUB_ARGS_PREVIEW_CHARS:
            args_preview = args_preview[:_RESULT_STUB_ARGS_PREVIEW_CHARS] + "…"
        first_id = self._identical_streak_first_call_id
        ref = f" (tool_call_id {first_id})" if first_id else ""
        stub = (
            f"[hermes note: this result is byte-identical to the {tool_name} "
            f"result earlier this turn{ref}. Refer to that result; it has not "
            f"changed. Args: {args_preview}]"
        )
        spill_path = self._persisted_result_paths.get(first_id) if first_id else None
        if spill_path:
            stub += (
                f"\n[The referenced result was persisted to: {spill_path} — "
                "page through it with read_file if you need the full content.]"
            )
        return stub

    def _check_loop_cap(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Enforce and advance the per-turn runaway-loop counters.

        Returns a ``block`` decision when the cap is already reached, otherwise
        increments the relevant counter for the allowed call and returns
        ``None``. A cap of 0 disables that limit entirely. Counters reset each
        turn via ``reset_for_turn``.
        """
        caps = self.config.loop_caps

        if tool_name == "web_search":
            cap = caps.max_web_searches
            if cap and self._turn_web_search_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_web_search_cap",
                    message=(
                        f"Blocked web_search: this turn has already made {cap} "
                        "web searches, the per-turn limit. This looks like a "
                        "runaway search loop. Work with the results you already "
                        "have and give the user your answer."
                    ),
                    tool_name=tool_name,
                    count=self._turn_web_search_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_web_search_count += 1
            return None

        if tool_name == "delegate_task":
            cap = caps.max_subagents
            if not cap:
                return None
            spawn_count = _subagent_spawn_count(args)
            if spawn_count == 0:
                # Control action (list/steer/stop) — spawns nothing. Never
                # block: once the spawn cap is hit, steering/stopping the
                # existing children is exactly what should still work.
                return None
            if self._turn_subagent_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_subagent_cap",
                    message=(
                        f"Blocked delegate_task: this turn has already spawned "
                        f"{self._turn_subagent_count} subagents (limit {cap}). "
                        "This looks like a runaway delegation loop. Finish the "
                        "work with the results you have and answer the user."
                    ),
                    tool_name=tool_name,
                    count=self._turn_subagent_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_subagent_count += spawn_count
            return None

        return None
    def _record_cross_turn_failure(self, target_key: str, failure_class: str) -> None:
        with self._cross_turn_lock:
            now = time.monotonic()
            previous = self._cross_turn_failures.get(target_key)
            if previous is not None and previous[2] == failure_class:
                count = previous[0] + 1
            else:
                count = 1
            self._cross_turn_failures[target_key] = (count, now, failure_class)
            self._prune_cross_turn_failures(now=now)

    def _prune_cross_turn_failures(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        ttl = self.config.cross_turn_ttl_seconds
        expired = [
            key for key, (_count, last_seen, _failure_class) in self._cross_turn_failures.items()
            if now - last_seen > ttl
        ]
        for key in expired:
            self._cross_turn_failures.pop(key, None)
        overflow = len(self._cross_turn_failures) - self.config.cross_turn_ledger_max_entries
        if overflow > 0:
            oldest = sorted(
                self._cross_turn_failures.items(), key=lambda item: item[1][1]
            )[:overflow]
            for key, _value in oldest:
                self._cross_turn_failures.pop(key, None)


@dataclass(frozen=True)
class TurnToolOutcome:
    """What this turn's tool calls actually did."""

    attempted: int
    failed: int
    failed_tools: tuple[str, ...]

    @property
    def all_failed(self) -> bool:
        return self.attempted > 0 and self.failed == self.attempted


def format_tool_outcome_footer(
    outcome: "TurnToolOutcome", *, unattended: bool
) -> str:
    """Footer stating what the tool calls did, or "" when nothing is owed.

    FABRICATION-REMEDY-001. On 2026-08-19 an agent's two tool calls both
    failed and it reported a detailed success -- four lanes, per-lane runtimes
    to 10ms, a verifier pass -- that no store recorded. Nothing in the loop
    compared the response against what the tools returned.

    Two behaviours, deliberately separated:

    A1 -- every call this turn failed. The response cannot be reporting work
    that happened, so say so unconditionally, on every surface. This is a
    counter comparison, not a judgement about the response text: reading the
    prose to decide whether it "claims success" is the general problem
    (option A2 in the ticket) and is not solved here.

    B -- some calls failed. State the counts on unattended surfaces only.
    An interactive user watched the tool calls scroll past; a Telegram reader
    saw only the prose and cannot tell a real result from a fabricated one.

    Silent when no call failed. Always-on was considered and rejected as
    noise; the cost is that absence now carries meaning, so a bug that stops
    the footer rendering would read as success.

    Two gaps, both deliberate and neither closed here:

    - **One success defeats A1.** Three failures and one trivial success make
      ``all_failed`` false, so on an attended surface that turn gets no footer
      at all and can still report a fabricated success. A1 is the
      all-or-nothing case by construction; the mixed case is B's, and B is
      unattended-only.
    - **Nothing reads the response.** A turn whose calls all succeeded can
      still describe results they did not produce. That is A2 in the ticket
      and is not solved here.
    """

    if outcome.attempted == 0 or outcome.failed == 0:
        return ""

    tools = ", ".join(outcome.failed_tools)
    if outcome.all_failed:
        n = outcome.attempted
        calls = "tool call" if n == 1 else "tool calls"
        return (
            f"[tool outcome] All {n} {calls} this turn failed ({tools}). "
            "Nothing above that depends on their results is confirmed."
        )
    if not unattended:
        return ""
    return (
        f"[tool outcome] {outcome.failed} of {outcome.attempted} tool calls "
        f"this turn failed ({tools})."
    )


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a blocked tool call."""
    return json.dumps(
        {
            "error": decision.message,
            "guardrail": decision.to_metadata(),
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    suffix = (
        f"\n\n[{label}: "
        f"{decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result or "") + suffix


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Action-oriented guidance for recovering from repeated tool failures."""
    common = (
        f"{tool_name} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, run a small diagnostic such as `pwd && ls -la` "
            "in the same tool, then try an absolute path, a simpler command, a different "
            "working directory, or a different tool such as read_file/write_file/patch."
        )
    return common + (
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    if parsed is not None:
        try:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            canonical = str(parsed)
    else:
        canonical = result or ""
    return _sha256(canonical)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _non_negative_int(value: Any, default: int) -> int:
    """Parse a session-cap value. 0 is a valid (disable) value; negatives and
    junk fall back to the default."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    """How many subagents a single delegate_task call spawns.

    delegate_task runs in one of two modes: a batch (``tasks`` is a non-empty
    list, one child per item) or a single task (``goal``). Count the batch size
    when present, otherwise 1, so the session subagent cap reflects real spawns
    rather than delegate_task invocations. Control actions (list/steer/stop)
    spawn nothing and must not consume the cap.
    """
    if isinstance(args, Mapping):
        action = str(args.get("action") or "").strip().lower()
        if action in ("list", "steer", "stop"):
            return 0
    tasks = args.get("tasks") if isinstance(args, Mapping) else None
    if isinstance(tasks, list) and tasks:
        return len(tasks)
    return 1


def _sha256(value: str) -> str:
    # surrogatepass: tool results scraped from the web can carry unpaired
    # UTF-16 surrogates (e.g. half of a mathematical-bold pair); a strict
    # encode raises and takes down the whole conversation loop. The hash only
    # needs deterministic bytes, not valid UTF-8.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
