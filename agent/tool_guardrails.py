"""Pure tool-call loop guardrail primitives.

The controller is side-effect free: it tracks per-turn tool-call observations
and returns decisions. Runtime code decides whether a decision becomes warning
guidance, a synthetic tool result, or a controlled turn halt.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed


IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "web_search", "web_extract", "session_search", "skill_view", "skills_list",
    "browser_snapshot", "browser_console", "browser_get_images", "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file", "mcp_filesystem_read_multiple_files", "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes", "mcp_filesystem_directory_tree", "mcp_filesystem_get_file_info",
    "mcp_filesystem_search_files",
})

MUTATING_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "write_file", "patch", "todo_list", "memory", "skill_manage",
    "browser_click", "browser_type", "browser_press", "browser_scroll", "browser_navigate",
    "send_message", "cronjob_manage", "delegate_task", "process_manage",
})

# Pollers: legitimately re-invoked with identical args; the identical-call NOTICE never fires.
STALL_GUARD_REPEATABLE_TOOLS = frozenset({"process_manage"})
_STALL_GUARD_REPEATABLE_SUFFIXES = ("_get_result", "_poll")  # generated / MCP poller conventions
# Nth consecutive identical (tool, args, result) call that fires the notice; 3 tolerates one double-check.
STALL_GUARD_IDENTICAL_CALL_THRESHOLD = 3
# From the 2nd byte-identical repeat the duplicate payload becomes a reference stub; smaller results
# aren't worth it, errors never are. The args preview keeps WHAT was called if compression evicts the original.
IDENTICAL_RESULT_STUB_MIN_CHARS = 512
_RESULT_STUB_ARGS_PREVIEW_CHARS = 120

# Tools whose "failure" is normal work output (red test run, empty grep, page timeout).
# same_tool_failure (DIFFERENT commands) never halts these; only an exact-args replay with
# no intervening change, or an identical-result streak, can.
FAILURE_TOLERANT_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "process_manage", "process", "browser_navigate", "web_extract",
})

# A successful call to one of these marks progress for every failing signature still counted
# this turn: the next retry is a new experiment (edit -> re-run), not a replay.
PROGRESS_RESET_TOOL_NAMES = frozenset({
    "write_file", "patch", "terminal", "execute_code", "browser_click", "browser_type", "browser_press",
    "browser_navigate", "process_manage", "process", "delegate_task", "send_message", "cronjob",
    "cronjob_manage", "todo", "todo_list", "memory", "skill_manage",
})

_BOOL_FIELDS = ("warnings_enabled", "hard_stop_enabled", "non_interactive_hard_stop_enabled")
# Threshold field -> (nested section, nested key). The flat legacy key is the field name itself.
_THRESHOLD_SOURCES: dict[str, tuple[str, str]] = {
    "exact_failure_warn_after": ("warn_after", "exact_failure"),
    "same_tool_failure_warn_after": ("warn_after", "same_tool_failure"),
    "no_progress_warn_after": ("warn_after", "idempotent_no_progress"),
    "exact_failure_block_after": ("hard_stop_after", "exact_failure"),
    "same_tool_failure_halt_after": ("hard_stop_after", "same_tool_failure"),
    "no_progress_block_after": ("hard_stop_after", "idempotent_no_progress"),
}

# Per-turn caps on runaway-prone tools (counters reset in reset_for_turn).
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50

# Interactive surfaces plus bounded supervised task loops (subagent stopped by its parent;
# api_server has a live client) doing real edit -> re-run work keep the warn-only default.
_ATTENDED_PLATFORMS = frozenset({"cli", "tui", "desktop", "acp", "subagent", "api_server"})


def is_stall_guard_repeatable(tool_name: str) -> bool:
    """Whether a tool is exempt from the identical-call loop notice."""
    return tool_name in STALL_GUARD_REPEATABLE_TOOLS or tool_name.endswith(_STALL_GUARD_REPEATABLE_SUFFIXES)


DETERMINISTIC_BLOCKER_CLASSES = frozenset(
    {"missing_target", "permission", "invalid_workdir", "malformed_input"}
)
_TARGET_KEYS = frozenset({
    "path", "file", "file_path", "filename", "target", "destination", "dest", "source", "src",
    "workdir", "cwd", "directory", "url", "key", "name", "job_id", "message_id", "chat_id",
    "channel", "ref", "element", "goal", "script",
})


def _is_non_interactive_platform(platform: str | None) -> bool:
    """True for gateway/cron sessions where tool loops are unattended."""
    if not isinstance(platform, str) or not platform.strip():
        return False
    return platform.strip().lower() not in _ATTENDED_PLATFORMS


@dataclass(frozen=True)
class LoopCapConfig:
    """Per-turn hard ceilings on web_search calls / subagent spawns; count total calls (not
    repeats), fire regardless of ``hard_stop_enabled``; ``0`` disables a cap."""

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        """Build config from the ``tool_loop_guardrails.loop_caps`` section."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(**{f.name: _int_at_least(data.get(f.name), f.default, 0) for f in fields(cls)})


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection. Warnings never prevent execution; hard
    stops are opt-in on interactive platforms, default on for unattended gateway/cron platforms."""

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    non_interactive_hard_stop_enabled: bool = True
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
    loop_caps: LoopCapConfig = field(default_factory=LoopCapConfig)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
        *,
        platform: str | None = None,
        default_hard_stop_enabled: bool | None = None,
    ) -> "ToolCallGuardrailConfig":
        """Build config from `tool_loop_guardrails`; nested ``warn_after`` / ``hard_stop_after`` win over flat legacy keys."""
        if not isinstance(data, Mapping):
            data = {}
        d = cls()
        hard_stop_value = data.get("hard_stop_enabled")
        soft_mode = _as_bool(data.get("unattended_soft_mode"), False)
        configuration_warning = ""
        runtime_hard_stop = bool(default_hard_stop_enabled) if default_hard_stop_enabled is not None else (
            _is_non_interactive_platform(platform)
            and _as_bool(data.get("non_interactive_hard_stop_enabled"), True)
        )
        if isinstance(hard_stop_value, str) and hard_stop_value.strip().lower() == "auto":
            hard_stop = runtime_hard_stop
        else:
            hard_stop = _as_bool(hard_stop_value, getattr(d, "hard_stop_enabled"))
        if runtime_hard_stop and not hard_stop and not soft_mode:
            hard_stop = True
            configuration_warning = (
                "tool_loop_guardrails.hard_stop_enabled=false was ignored for an unattended surface; "
                "set unattended_soft_mode=true to opt into visibly degraded soft mode"
            )
        flags = {name: _as_bool(data.get(name), getattr(d, name)) for name in _BOOL_FIELDS}
        flags["hard_stop_enabled"] = hard_stop
        if flags["non_interactive_hard_stop_enabled"] and _is_non_interactive_platform(platform):
            if not soft_mode:
                flags["hard_stop_enabled"] = True

        def threshold(name: str, section_name: str, key: str) -> int:
            section = data.get(section_name)
            nested = section.get(key, data.get(name)) if isinstance(section, Mapping) else data.get(name)
            return _int_at_least(nested, getattr(d, name), 1)

        thresholds = {name: threshold(name, *src) for name, src in _THRESHOLD_SOURCES.items()}
        return cls(
            loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")),
            cross_turn_failure_halt_after=_int_at_least(data.get("cross_turn_failure_halt_after"), d.cross_turn_failure_halt_after, 1),
            cross_turn_ledger_max_entries=_int_at_least(data.get("cross_turn_ledger_max_entries"), d.cross_turn_ledger_max_entries, 1),
            cross_turn_ttl_seconds=_int_at_least(data.get("cross_turn_ttl_seconds"), d.cross_turn_ttl_seconds, 1),
            unattended_soft_mode=soft_mode,
            configuration_warning=configuration_warning,
            **flags,
            **thresholds,
        )


@dataclass(frozen=True)
class IdenticalCallObservation:
    """``notice`` is appended after the result, ``stub`` replaces a byte-identical duplicate result."""

    notice: str | None = None
    stub: str | None = None


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        return cls(tool_name=tool_name, args_hash=_sha256(canonical_tool_args(args or {})))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return asdict(self)


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
        data = asdict(self)
        data.pop("signature", None)
        return data


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return _canonical_json(args)


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Fallback classifier used only when callers don't pass ``failed``; mirrors
    ``agent.display._detect_tool_failure`` so the guardrail never disagrees with the CLI's ``[error]`` tag."""
    if result is None or file_mutation_result_landed(tool_name, result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        return (True, f" [exit {exit_code}]") if exit_code is not None and exit_code != 0 else (False, "")

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict) and data.get("success") is False and "exceed the limit" in data.get("error", ""):
            return True, " [full]"
    lower = result[:500].lower()
    return (True, " [error]") if '"error"' in lower or '"failed"' in lower or result.startswith("Error") else (False, "")


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
        if str(key).lower() in _TARGET_KEYS:
            targets.append(f"{str(key).lower()}={value}")
    if not targets and tool_name == "terminal":
        command = str(args.get("command", ""))
        targets.extend(re.findall(r"(?:/[^\s'\"]+|[A-Za-z]:[\\/][^\s'\"]+)", command)[:4])
    if not targets:
        targets.append(tool_name)
    canonical = json.dumps(sorted(targets), ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{tool_name}:{_sha256(canonical)}"


# Guardrail verdict text injected into the conversation, keyed by decision code.
# ``same_tool_failure_warning`` is built by _tool_failure_recovery_hint (tool-specific).
_DECISION_MESSAGES: dict[str, str] = {
    "repeated_exact_failure_block": (
        "Blocked {tool_name}: the same tool call failed {count} times with identical arguments. "
        "Stop retrying it unchanged; change strategy or explain the blocker."
    ),
    "idempotent_no_progress_block": (
        "Blocked {tool_name}: this read-only call returned the same result {count} times. "
        "Stop repeating it unchanged; use the result already provided or try a different query."
    ),
    "same_tool_failure_halt": (
        "Stopped {tool_name}: it failed {count} times this turn. "
        "Stop retrying the same failing tool path and choose a different approach."
    ),
    "repeated_exact_failure_warning": (
        "{tool_name} has failed {count} times with identical arguments. This looks like a loop; "
        "inspect the error and change strategy instead of retrying it unchanged."
    ),
    "idempotent_no_progress_warning": (
        "{tool_name} returned the same result {count} times. Use the result already provided "
        "or change the query instead of repeating it unchanged."
    ),
    "identical_call_streak_halt": (
        "Stopped {tool_name}: the same call with identical arguments returned the same result "
        "{count} times in a row. Stop repeating it unchanged; use the result already provided or change strategy."
    ),
    "loop_web_search_cap": (
        "Blocked web_search: this turn has already made {cap} web searches, the per-turn limit. "
        "This looks like a runaway search loop. Work with the results you already have and give the user your answer."
    ),
    "loop_subagent_cap": (
        "Blocked delegate_task: this turn has already spawned {count} subagents (limit {cap}). "
        "This looks like a runaway delegation loop. Finish the work with the results you have and answer the user."
    ),
}

_IDENTICAL_CALL_NOTICE = (
    "[hermes note: this is the {ordinal} consecutive identical call to "
    "{tool_name} with identical arguments returning the same result. "
    "Do not repeat it — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

# tool -> (LoopCapConfig field, controller counter attribute, decision code)
_LOOP_CAPS: dict[str, tuple[str, str, str]] = {
    "web_search": ("max_web_searches", "_turn_web_search_count", "loop_web_search_cap"),
    "delegate_task": ("max_subagents", "_turn_subagent_count", "loop_subagent_cap"),
}


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
        # signature -> a mutating call succeeded since its last failure
        self._progress_since_failure: dict[ToolCallSignature, bool] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # Identical-call streak: CONSECUTIVE identical (tool, args, result) calls; any different call or
        # result resets it, so re-reads after edits and varied polling are never flagged.
        # Identical-call loop-breaker state (agent.stall_guards): tracks the CONSECUTIVE streak of identical
        # (tool, canonical args) calls whose results were also identical. Per-turn, like everything else
        # here. NOTE: open PR #85352 (patrykkopycinski) tracks no-progress loops ACROSS turns via a
        # detection window — a different mechanism from this per-turn consecutive streak. Coordinate future
        # work there.
        self._identical_streak_sig: ToolCallSignature | None = None
        self._identical_streak_result_hash: str = ""
        self._identical_streak_count: int = 0
        self._identical_streak_first_call_id: str = ""
        # tool_call_id -> spillover path, so a stub referencing a persisted-output preview can't dangle.
        self._persisted_result_paths: dict[str, str] = {}
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

    def _decide(
        self, action: str, code: str, tool_name: str, count: int, signature: ToolCallSignature,
        *, message: str | None = None, **fmt: Any,
    ) -> ToolGuardrailDecision:
        """Build a warn/block/halt decision; block/halt is also recorded as the turn's halt decision."""
        if message is None:
            message = _DECISION_MESSAGES[code].format(tool_name=tool_name, count=count, **fmt)
        decision = ToolGuardrailDecision(action, code, message, tool_name, count, signature)
        if decision.should_halt:
            self._halt_decision = decision
        return decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        allow = ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        # Loop caps apply regardless of hard_stop_enabled (which only governs the detector).
        cap_block = self._check_loop_cap(tool_name, args, signature)
        if cap_block is not None or not self.config.hard_stop_enabled:
            return cap_block or allow
        target_key = _target_identity(tool_name, args)
        with self._cross_turn_lock:
            self._prune_cross_turn_failures()
            cross_record = self._cross_turn_failures.get(target_key)
        if cross_record is not None:
            cross_count, _last_seen, _failure_class = cross_record
            if cross_count >= self.config.cross_turn_failure_halt_after:
                return self._decide(
                    "block", "cross_turn_deterministic_blocker", tool_name, cross_count, signature,
                    message=(
                        f"Blocked {tool_name}: the same deterministic blocker persisted for this target "
                        f"across {cross_count} failed attempts. Stop retrying this path; change the "
                        "target or report the blocker."
                    ),
                )
        # A mutation since this call last failed makes the retry a new experiment.
        exact_count = 0 if self._progress_since_failure.get(signature) else self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            return self._decide("block", "repeated_exact_failure_block", tool_name, exact_count, signature)
        record = self._no_progress.get(signature) if self._is_idempotent(tool_name) else None
        if record is not None and record[1] >= self.config.no_progress_block_after:
            return self._decide("block", "idempotent_no_progress_block", tool_name, record[1], signature)
        return allow

    def after_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)
        warnings = self.config.warnings_enabled

        self._turn_calls_attempted += 1
        if failed:
            self._turn_calls_failed += 1
            if tool_name not in self._turn_failed_tools:
                self._turn_failed_tools.append(tool_name)

        if failed:
            # An identical failing call is only a REPLAY if nothing landed in between;
            # a mutation since the last identical failure restarts the exact-args streak.
            if self._progress_since_failure.pop(signature, False):
                self._exact_failure_counts.pop(signature, None)
            exact_count = self._exact_failure_counts[signature] = self._exact_failure_counts.get(signature, 0) + 1
            same_count = self._same_tool_failure_counts[tool_name] = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._no_progress.pop(signature, None)
            failure_class = classify_failure_class(tool_name, result)
            if failure_class in DETERMINISTIC_BLOCKER_CLASSES:
                self._record_cross_turn_failure(_target_identity(tool_name, args), failure_class)
            # same_tool_failure counts DIFFERENT args on one tool; for failure-tolerant
            # tools a run of distinct red commands is diagnosis, not a loop — warn, never halt.
            if (
                # Hard-stop widening (#89069 / #100849 bundle): the per-turn no-progress BLOCK above only
                # covers tools in idempotent_tools, so a model replaying the same successful
                # `terminal`/`skill_view` call with a byte-identical result ran until the iteration budget.
                # The consecutive-identical streak is tool-agnostic; when hard stops are enabled, halt at
                # the same idempotent_no_progress threshold. Pollers stay exempt (an unchanged poll is
                # progress).
                self.config.hard_stop_enabled
                and tool_name not in FAILURE_TOLERANT_TOOL_NAMES
                and same_count >= self.config.same_tool_failure_halt_after
            ):
                return self._decide("halt", "same_tool_failure_halt", tool_name, same_count, signature)
            if warnings and exact_count >= self.config.exact_failure_warn_after:
                return self._decide("warn", "repeated_exact_failure_warning", tool_name, exact_count, signature)
            if warnings and same_count >= self.config.same_tool_failure_warn_after:
                return self._decide(
                    "warn", "same_tool_failure_warning", tool_name, same_count, signature,
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                )
            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        with self._cross_turn_lock:
            self._cross_turn_failures.pop(_target_identity(tool_name, args), None)
        # A successful mutation is progress for every failing signature still counted
        # this turn. Pure loops never mutate between attempts, so the replay detector keeps its teeth.
        if tool_name in PROGRESS_RESET_TOOL_NAMES or file_mutation_result_landed(tool_name, result):
            self._progress_since_failure.update(dict.fromkeys(self._exact_failure_counts, True))
            self._same_tool_failure_counts.clear()
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = previous[1] + 1 if previous is not None and previous[0] == result_hash else 1
        self._no_progress[signature] = (result_hash, repeat_count)
        if warnings and repeat_count >= self.config.no_progress_warn_after:
            return self._decide("warn", "idempotent_no_progress_warning", tool_name, repeat_count, signature)
        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        return tool_name not in self.config.mutating_tools and tool_name in self.config.idempotent_tools

    def observe_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, tool_call_id: str = "", failed: bool = False,
    ) -> IdenticalCallObservation:
        """Track consecutive identical calls; return notice + dedupe stub info.

        ``notice`` fires from the threshold-th consecutive identical (tool, args, result) call
        (observational, pollers exempt). ``stub`` replaces the CURRENT result from the 2nd byte-identical
        repeat — the tool still executed, only the context representation is deduplicated, so polling
        semantics survive; pollers are NOT exempt here since an unchanged poll is where it saves most.
        """
        is_plain_str = isinstance(result, str)
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        result_hash = _result_hash(result) if is_plain_str else ""

        if is_plain_str and (signature, result_hash) == (self._identical_streak_sig, self._identical_streak_result_hash):
            self._identical_streak_count += 1
        else:
            # New streak; non-string (multimodal) results never form one.
            self._identical_streak_sig = signature if is_plain_str else None
            self._identical_streak_result_hash = result_hash
            self._identical_streak_count = 1 if is_plain_str else 0
            self._identical_streak_first_call_id = tool_call_id or ""
        count = self._identical_streak_count

        notice = None
        if not is_stall_guard_repeatable(tool_name) and count >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
            notice = _IDENTICAL_CALL_NOTICE.format(ordinal=_ordinal(count), tool_name=tool_name)
            # The no-progress BLOCK in before_call only covers idempotent_tools; this streak
            # is tool-agnostic, so with hard stops on, halt at the same threshold (a model
            # replaying a successful `terminal` call otherwise runs to the budget).
            if self.config.hard_stop_enabled and count >= self.config.no_progress_block_after and self._halt_decision is None:
                self._decide("halt", "identical_call_streak_halt", tool_name, count, signature)

        stub = None
        if is_plain_str and count >= 2 and not failed and len(result) >= IDENTICAL_RESULT_STUB_MIN_CHARS:
            stub = self._build_result_reference_stub(tool_name, args)
        return IdenticalCallObservation(notice=notice, stub=stub)

    def record_persisted_result(self, tool_call_id: str, file_path: str) -> None:
        """Remember the spillover path a persisted result was saved to."""
        if tool_call_id and file_path:
            self._persisted_result_paths[tool_call_id] = file_path

    def _build_result_reference_stub(self, tool_name: str, args: Mapping[str, Any] | None) -> str:
        """Reference stub for a byte-identical duplicate result (tool + args preview)."""
        args_preview = canonical_tool_args(_coerce_args(args))
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
            stub += f"\n[The referenced result was persisted to: {spill_path} — page through it with read_file if you need the full content.]"
        return stub

    def _check_loop_cap(
        self, tool_name: str, args: Mapping[str, Any], signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Block once a per-turn cap is reached (BEFORE the call, so the (cap+1)-th is refused), else advance
        the counter and return None. delegate_task control actions spawn nothing and keep working after the cap."""
        spec = _LOOP_CAPS.get(tool_name)
        if spec is None:
            return None
        cap_field, count_attr, code = spec
        cap, count = getattr(self.config.loop_caps, cap_field), getattr(self, count_attr)
        increment = 1 if tool_name == "web_search" else (_subagent_spawn_count(args) if cap else 0)
        if increment and cap and count >= cap:
            return self._decide("block", code, tool_name, count, signature, cap=cap)
        setattr(self, count_attr, count + increment)
        return None

    def _record_cross_turn_failure(self, target_key: str, failure_class: str) -> None:
        with self._cross_turn_lock:
            now = time.monotonic()
            previous = self._cross_turn_failures.get(target_key)
            count = previous[0] + 1 if previous is not None and previous[2] == failure_class else 1
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
            oldest = sorted(self._cross_turn_failures.items(), key=lambda item: item[1][1])[:overflow]
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
    return json.dumps({"error": decision.message, "guardrail": decision.to_metadata()}, ensure_ascii=False)


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    return (result or "") + f"\n\n[{label}: {decision.code}; count={decision.count}; {decision.message}]"


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


def _ordinal(count: int) -> str:
    return f"{count}{'th' if 11 <= count % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(count % 10, 'th')}"


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    return _sha256(_canonical_json(parsed) if parsed is not None else (result or ""))


_BOOL_WORDS = {w: True for w in ("1", "true", "yes", "on", "enabled")} | {w: False for w in ("0", "false", "no", "off", "disabled")}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, str):
        return _BOOL_WORDS.get(value.strip().lower(), default)
    return default


def _int_at_least(value: Any, default: int, minimum: int) -> int:
    """junk/None/below-minimum fall back to default (caps use minimum 0 so 0 = disabled)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    """Subagents one delegate_task call spawns: ``len(tasks)`` for a non-empty batch, else 1; control actions 0."""
    if str(args.get("action") or "").strip().lower() in ("list", "steer", "stop"):
        return 0
    tasks = args.get("tasks")
    return len(tasks) if isinstance(tasks, list) and tasks else 1


def _sha256(value: str) -> str:
    # surrogatepass: web-scraped results can carry unpaired UTF-16 surrogates; a
    # strict encode would raise and take down the conversation loop.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
