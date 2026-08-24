"""Kanban Swarm v1: thin swarm topology helpers on top of Kanban.

Deliberately no second scheduler — a small task graph written into the
existing Kanban kernel:

    planning root (completed immediately)
        ├─ parallel specialist workers (ready)
        └─ verifier (todo until all workers done)
             └─ synthesizer (todo until verifier done)

The shared blackboard is structured JSON comments on the root task, so all
state lives in existing task_comments/task_events rows and the dashboard,
notifier, slash command and dispatcher keep working without a new service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
import sqlite3
import time
import unicodedata
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb

BLACKBOARD_PREFIX = "[swarm:blackboard] "
CONTRACT_PREFIX = "[swarm:contract] "
WORKER_TOOLSETS_PREFIX = "[kanban:worker_toolsets] "
MULTI_AGENT_LANE_IDS = ("native_hermes", "claude", "grok", "agy")
REQUIRED_LANE_ID = "native_hermes"
EXTERNAL_LANE_IDS = ("claude", "grok", "agy")
MIN_EXTERNAL_LANES = 2

# GATE8-SWARM-CREATION-TOOL-001: the skill each external lane needs to reach
# its actual CLI. Before this table existed, nothing in the codebase
# constrained what skill string an agent put on a lane's worker -- observed
# live sending "HUMANIZER" for every lane (GATE8-RERUN-RESULT-001) and,
# separately, workers whose `assignee` named a Hermes profile that doesn't
# exist, which the dispatcher silently never picks up (no error anywhere).
# `tools/kanban_tools.py::_handle_swarm` uses this to fill in
# `preflight_skill_id` from `lane_id` rather than trust a model-typed value.
LANE_SKILL_IDS = {
    "claude": "claude-code",
    "grok": "grok",
    "agy": "antigravity-cli",
}
# SWARM-CLAUDE-GROK-LANE-TIMEOUT-RECURRENCE-001: a live 4-lane re-run
# (docs/plans/2026-08-20-swarm-claude-grok-lane-timeout-recurrence-001.md,
# "Resolution" section) showed every external-CLI lane (claude/grok/agy)
# hitting a 300s ceiling on both attempts under 3-way concurrent dispatch,
# while native_hermes (in-process, no external CLI subprocess) finished
# comfortably in 158s -- not because external lanes are slower per step
# (heartbeat gaps were similar across all lanes, ~60-90s), but because they
# need structurally more steps for equivalent work (subprocess spawn, cd/
# path handling, output polling -- see the companion agy ticket's own
# transcript for a concrete example). Bounded like DEFAULT_MAX_IN_PROGRESS's
# own comment already says of its value ("nothing establishes that three
# beats two or four") -- 600s is 2x DEFAULT_WORKER_MAX_RUNTIME_SECONDS, not
# a value derived from a successful external-lane run's actual step count
# (no such run was observed in that investigation).
#
# DEFAULT_WORKER_MAX_RUNTIME_SECONDS itself was raised from 120 to 300 on
# 2026-08-21 (SWARM-LANE-TIMEOUT-RETEST-002, same day as the Tirith/
# blackboard fixes) after real-world Telegram-triggered swarms kept
# hitting the 120s ceiling on the native_hermes lane specifically --
# unrelated to the two bugs those fixes addressed. native_hermes has no
# external-CLI subprocess overhead, so it doesn't need the external
# lanes' full 600s, but 120s was too tight for anything beyond the
# original test's clean 158s run: real runs under 3-way concurrent
# dispatch (contention this whole investigation established is real and
# affects every lane's per-step latency) needed up to ~220s+. 300s
# leaves headroom above every observed native_hermes run without giving
# it the same ceiling as lanes that need it for a structurally different
# reason (more steps, not slower steps).
DEFAULT_WORKER_MAX_RUNTIME_SECONDS = 300
DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS = 600
DEFAULT_GOAL_MAX_TURNS = 5

_INTERNAL_LENGTH_MARKER_RE = re.compile(r"(?:（\s*\d+\s*字\s*）|\(\s*\d+\s*字\s*\))")
_QUOTE_PAIRS = (("「", "」"), ("“", "”"), ("（", "）"), ("(", ")"))
DEFAULT_OUTPUT_CONTRACT_POLICY = {
    "reject_internal_length_marker": True,
    "require_balanced_quotes": True,
}

# Conservative high-signal Simplified -> Traditional pairs. This is a
# rejection gate, not an automatic converter: if a synthesizer emits one of
# these glyphs, the completion is retried instead of silently rewriting the
# user's deliverable.
_SIMPLIFIED_TO_TRADITIONAL = str.maketrans({
    "\u5199": "\u5beb",  # 写 -> 寫
    "\u8bdd": "\u8a71",  # 话 -> 話
    "\u53cc": "\u96d9",  # 双 -> 雙
    "\u5173": "\u95dc",  # 关 -> 關
    "\u9f9f": "\u9f9c",  # 龟 -> 龜
    "\u5417": "\u55ce",  # 吗 -> 嗎
    "\u4e70": "\u8cb7",  # 买 -> 買
    "\u94f6": "\u9280",  # 银 -> 銀
    "\u5458": "\u54e1",  # 员 -> 員
    "\u8bf4": "\u8aaa",  # 说 -> 說
    "\u5356": "\u8ce3",  # 卖 -> 賣
    "\u6e29": "\u6eab",  # 温 -> 溫
    "\u542c": "\u807d",  # 听 -> 聽
    "\u89c1": "\u898b",  # 见 -> 見
    "\u8bcd": "\u8a5e",  # 词 -> 詞
    "\u70ed": "\u71b1",  # 热 -> 熱
    "\u732b": "\u8c93",  # 猫 -> 貓
    "\u5f53": "\u7576",  # 当 -> 當
    "\u4f1a": "\u6703",  # 会 -> 會
    "\u7ed3": "\u7d50",  # 结 -> 結
    "\u5934": "\u982d",  # 头 -> 頭
})

def _simplified_glyphs(value: str) -> set[str]:
    return {char for char in value if ord(char) in _SIMPLIFIED_TO_TRADITIONAL}


def _is_han(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _goal_anchor_terms(goal: str) -> list[str]:
    """Extract conservative subject anchors from a Chinese homophone goal."""
    marker = chr(0x8ae7) + chr(0x97f3)
    matches = re.findall(r"([\u3400-\u9fff]{1,8})" + marker, goal or "")
    terms: list[str] = []
    for value in reversed(matches):
        if chr(0x5c0f) in value:
            value = value.rsplit(chr(0x5c0f), 1)[-1]
        value = value[-4:].strip()
        if value and value not in terms:
            terms.append(value)
    return terms[:2]


def _traditional_unicode_issue(value: str) -> Optional[str]:
    if "�" in value:
        return (
            "synthesizer result contains Unicode replacement characters; "
            "regenerate clean Traditional Chinese text"
        )
    for char in value:
        if ord(char) <= 0x7F or _is_han(char):
            continue
        if unicodedata.category(char).startswith("L"):
            return (
                "synthesizer result contains a non-Chinese writing system; "
                "use Traditional Chinese (Taiwan) only"
            )
    return None


def _default_worker_max_runtime_seconds(lane_id: Optional[str]) -> int:
    """Lane-aware fallback used only when the caller leaves the swarm-wide
    ``worker_max_runtime_seconds`` unset (``None``) -- an explicit value
    still applies uniformly to every worker, preserving prior behavior."""
    if lane_id in EXTERNAL_LANE_IDS:
        return DEFAULT_EXTERNAL_LANE_WORKER_MAX_RUNTIME_SECONDS
    return DEFAULT_WORKER_MAX_RUNTIME_SECONDS


@dataclass(frozen=True)
class SwarmWorkerSpec:
    """A single parallel worker card in a swarm."""

    profile: str
    title: str
    body: str
    skills: list[str] = field(default_factory=list)
    priority: int = 0
    max_runtime_seconds: Optional[int] = None
    lane_id: Optional[str] = None
    preflight_skill_id: str = ""
    toolsets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SwarmCreated:
    """IDs produced by :func:`create_swarm`."""

    root_id: str
    worker_ids: list[str]
    verifier_id: str
    synthesizer_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require_text(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _swarm_context(root_id: str, goal: str) -> str:
    # SWARM-CLAUDE-GROK-LANE-TIMEOUT-RECURRENCE-001 retest (2026-08-21): two
    # independent lanes, in two independent live runs, exhausted most of
    # their runtime budget failing to post a result at all -- not because
    # they were slow, but because "using structured comments" didn't tell
    # them WHICH tool does that. Both improvised: one hand-wrote raw SQL
    # against kanban.db via the shell (a bash quoting bug), the other used
    # execute_code (BLOCKED outright for unattended workers by design). The
    # kanban_comment tool call they actually needed was available and each
    # lane's own transcript shows it using that same tool correctly earlier
    # in the very same turn (kanban_show/kanban_comment against its OWN
    # task) -- the ambiguity was specific to "how do I write to the shared
    # blackboard", not general tool unfamiliarity. Spelling out the tool
    # name and exact call shape, and explicitly ruling out the two failure
    # modes actually observed, directly addresses what the transcripts show
    # went wrong.
    return (
        "\n\n## Swarm protocol\n"
        f"- Swarm root / shared blackboard: `{root_id}`.\n"
        "- Read sibling/parent handoffs from Kanban context before working.\n"
        "- Treat the Goal line as authoritative. Do not invent extra deliverables "
        "or ask the user for PNGs, diagrams, files, or other artifacts unless "
        "the goal explicitly requests them. For a text-only goal such as "
        "autumn homophone jokes, return text and complete; an optional artifact "
        "is never a blocker.\n"
        "- Put machine-readable facts in completion metadata.\n"
        "- To post cross-worker notes on the shared blackboard, call the "
        f'`kanban_comment` tool with task_id="{root_id}" and your note as '
        "`body`. Do NOT write directly to kanban.db via shell/sqlite3 or "
        "execute_code -- execute_code is blocked outright for unattended "
        "workers, and hand-written SQL bypasses the audit trail even when "
        "it works.\n"
        f"- Goal: {goal.strip()}\n"
    )


def _activate_root_inline(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    summary: str,
    metadata: dict[str, Any],
) -> bool:
    """Inline blocked→done CAS flip + event insert for the swarm root.

    Runs INSIDE create_swarm's outer write_txn, so it must not call
    ``kb.complete_task`` — that helper opens its own transaction and fires
    post-commit side effects (workspace cleanup, failure-counter clear,
    ``recompute_ready``) that would execute while the outer transaction can
    still roll back. Instead we do the minimal durable writes here and let
    the caller run ``recompute_ready`` after the outer commit.
    """
    import time as _time

    now = int(_time.time())
    cur = conn.execute(
        """
        UPDATE tasks
           SET status       = 'done',
               completed_at = ?,
               claim_lock   = NULL,
               claim_expires= NULL,
               worker_pid   = NULL
         WHERE id = ?
           AND status = 'blocked'
        """,
        (now, root_id),
    )
    if cur.rowcount != 1:
        return False
    run_id = kb._synthesize_ended_run(
        conn,
        root_id,
        outcome="completed",
        summary=summary,
        metadata=metadata,
    )
    kb._append_event(
        conn,
        root_id,
        "completed",
        {"result_len": 0, "summary": summary[:400] or None},
        run_id=run_id,
    )
    return True
def _contract_line(contract: dict[str, Any]) -> str:
    return CONTRACT_PREFIX + json.dumps(contract, ensure_ascii=False, sort_keys=True)


def _completion_requirements(contract: dict[str, Any]) -> str:
    """Spell out, in the task body, exactly what ``validate_completion``
    enforces for this role.

    These two must agree. When they disagreed the agent obeyed the body,
    was rejected by the kernel, and blocked asking an operator for help --
    which is how the first real four-lane run deadlocked at both the
    verifier and the synthesizer. The workers survived only because the
    caller had hand-written the contract into their task text; nothing in
    this module put it there.

    ``test_completion_requirements_satisfy_validate_completion`` builds a
    metadata dict from the literal values named below and asserts
    ``validate_completion`` accepts it, for every role. That test is what
    keeps this text and the checker from drifting apart again.
    """

    role = contract.get("role")
    lines = [
        "",
        "Completion contract (the kernel rejects a completion that omits any of these):",
        f'  role = "{role}"',
        f'  root_id = "{contract.get("root_id")}"',
    ]
    output_policy = contract.get("output_policy") or {}
    if role == "worker":
        lines += [
            f'  lane_id = "{contract.get("expected_lane_id")}"',
            f'  preflight_skill_id = "{contract.get("preflight_skill_id") or ""}"',
            '  outcome = "completed"',
            "  verified_clean = true",
        ]
        if output_policy.get("reject_internal_length_marker"):
            lines.append("  output text must not contain an internal length marker")
        if output_policy.get("require_balanced_quotes"):
            lines.append("  output text must not contain unbalanced quotes/brackets")
    elif role == "verifier":
        expected = contract.get("expected_lane_count")
        lines += [
            '  gate = "pass"',
            f"  expected_lane_count = {expected}",
            f"  verified_lane_count = {expected}",
            "  (every expected lane must be verified; a smaller count is rejected)",
        ]
    elif role == "synthesizer":
        lines += [
            '  outcome = "completed"',
            "  result_present = true",
            "  and the task result itself must be non-empty",
            "  result must be the exact final user-facing deliverable, not a progress/status report",
            "  if an artifact is used, inline its complete human-readable contents in result; do not rely on attachment-only delivery",
        ]
        if output_policy.get("reject_internal_length_marker"):
            lines.append("  result text must not contain an internal length marker")
        if output_policy.get("require_balanced_quotes"):
            lines.append("  result text must not contain unbalanced quotes/brackets")
        if output_policy.get("required_language") == "zh-Hant-TW":
            lines.append(
                "  result text must use Traditional Chinese (Taiwan) only; "
                "do not output Simplified Chinese"
            )
        if contract.get("goal_anchor_terms"):
            lines.append(
                "  result text must reference the current goal anchor(s): "
                + ", ".join(str(term) for term in contract["goal_anchor_terms"])
            )
    lines.append(
        "  artifact metadata is optional; omit artifacts unless the task explicitly requests a file deliverable"
    )
    lines.append(
        "  do not invent artifact paths, URLs, base64 payloads, or workspace filenames"
    )
    lines.append(
        "Send these as completion metadata. Do not complete with a subset."
    )
    lines.append("")
    lines.append(_completion_call_example(contract))
    return "\n".join(lines)


def validate_swarm_output_text(
    text: Optional[str], *, contract: Optional[dict[str, Any]]
) -> Optional[str]:
    """Validate only the narrow output policy attached to swarm contracts."""
    if not contract or contract.get("role") not in {"worker", "synthesizer"}:
        return None
    value = (text or "").strip()
    if not value:
        return "swarm output text is empty"
    policy = contract.get("output_policy") or {}
    if contract.get('role') == 'synthesizer':
        lower = value.lower()
        if ('verified' in lower and 'processed' in lower
                and 'final synthesized output ready' in lower
                and 'completion metadata provided' in lower):
            return ('synthesizer result is status-only; include the exact final'
                    ' user-facing deliverable text')
        if policy.get("required_language") == "zh-Hant-TW":
            unicode_issue = _traditional_unicode_issue(value)
            if unicode_issue:
                return unicode_issue
            if _simplified_glyphs(value):
                return (
                    "synthesizer result contains Simplified Chinese; use "
                    "Traditional Chinese (Taiwan) only"
                )
            if not any("\u4e00" <= char <= "\u9fff" for char in value):
                return "synthesizer result must contain Traditional Chinese text"
            anchor_terms = [
                str(term).strip()
                for term in (contract.get("goal_anchor_terms") or [])
                if str(term).strip()
            ]
            if anchor_terms and not any(term in value for term in anchor_terms):
                return (
                    "synthesizer result does not reference the current swarm "
                    "goal; regenerate for the current request"
                )
    if policy.get("reject_internal_length_marker") and _INTERNAL_LENGTH_MARKER_RE.search(value):
        return "swarm output contains an internal length marker"
    if policy.get("require_balanced_quotes"):
        for opening, closing in _QUOTE_PAIRS:
            if value.count(closing) != value.count(opening):
                side = "closing" if value.count(closing) > value.count(opening) else "opening"
                return f"swarm output has an unmatched {side} {closing if side == 'closing' else opening!r}"
    return None


def swarm_output_metadata(
    text: Optional[str], *, contract: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    if not contract or contract.get("role") not in {"worker", "synthesizer"}:
        return None
    value = (text or "").strip()
    reason = validate_swarm_output_text(value, contract=contract)
    return {"char_count": len(value), "format_valid": reason is None, "validation_reason": reason or "ok"}


def _completion_call_example(contract: dict[str, Any]) -> str:
    """A literal, copy-pasteable kanban_complete call shape for this role.

    SWARM-LANE-TIMEOUT-RETEST-002 (2026-08-21): a real synthesizer got
    stuck in a loop, failing kanban_complete 19 times over ~10 minutes
    with a different missing field each try (role, result_present,
    outcome, root_id -- never all at once), then self-blocked claiming
    "the kernel validator is buggy, developer must fix it." Independently
    disproven: a correctly-shaped call passes validate_completion cleanly
    on the very same task. The contract's field list above was accurate
    but abstract ("field = value" lines) -- it never showed which fields
    are top-level tool-call parameters (task_id, result, summary) versus
    which belong nested inside metadata, which is the exact ambiguity a
    weak model kept tripping on. This renders one concrete, directly
    copy-pasteable example of the actual tool call, removing that
    ambiguity outright instead of asking the model to infer it.
    """
    role = contract.get("role")
    root_id = contract.get("root_id")
    if role == "worker":
        metadata = {
            "role": "worker",
            "root_id": root_id,
            "lane_id": contract.get("expected_lane_id"),
            "preflight_skill_id": contract.get("preflight_skill_id") or "",
            "outcome": "completed",
            "verified_clean": True,
        }
        example = {
            "task_id": "<this task's id>",
            "summary": "<1-3 sentence handoff>",
            "metadata": metadata,
        }
    elif role == "verifier":
        expected = contract.get("expected_lane_count")
        metadata = {
            "role": "verifier",
            "root_id": root_id,
            "gate": "pass",
            "expected_lane_count": expected,
            "verified_lane_count": expected,
        }
        example = {
            "task_id": "<this task's id>",
            "summary": "<1-3 sentence handoff>",
            "metadata": metadata,
        }
    else:  # synthesizer
        metadata = {
            "role": "synthesizer",
            "root_id": root_id,
            "outcome": "completed",
            "result_present": True,
        }
        example = {
            "task_id": "<this task's id>",
            "result": "冬天的笑話：雪人說，今天真冷。",
            "metadata": metadata,
        }
    return (
        "Example call (task_id/result/summary are top-level kanban_complete "
        "parameters; everything else goes inside metadata):\n  kanban_complete("
        + json.dumps(example, ensure_ascii=False, sort_keys=False)
        + ")"
    )


def extract_contract(body: Optional[str]) -> Optional[dict[str, Any]]:
    """Read the last machine-readable swarm contract from a task body."""
    for line in reversed((body or "").splitlines()):
        if not line.startswith(CONTRACT_PREFIX):
            continue
        try:
            value = json.loads(line[len(CONTRACT_PREFIX):])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def validate_completion(
    task: Any, *, metadata: Optional[dict[str, Any]], result: Optional[str] = None,
    summary: Optional[str] = None,
) -> Optional[str]:
    """Return a rejection reason for a contract-bound task, else ``None``."""
    contract = extract_contract(getattr(task, "body", None))
    if not contract:
        return None
    metadata = metadata if isinstance(metadata, dict) else {}
    role = contract.get("role")
    errors: list[str] = []
    if metadata.get("role") != role:
        errors.append(f"swarm {role} completion requires metadata role={role!r}")
    if metadata.get("root_id") != contract.get("root_id"):
        errors.append("swarm completion root_id does not match the task contract")
    output_text = result if (result or "").strip() else summary
    if (output_text or "").strip():
        output_reason = validate_swarm_output_text(output_text, contract=contract)
        if output_reason:
            errors.append(output_reason)
    if role == "worker":
        if metadata.get("lane_id") != contract.get("expected_lane_id"):
            errors.append("worker lane_id does not match the expected lane")
        expected_skill = contract.get("preflight_skill_id") or ""
        if metadata.get("preflight_skill_id", "") != expected_skill:
            errors.append("worker preflight_skill_id does not match the expected skill")
        if metadata.get("outcome") != "completed":
            errors.append("worker completion requires outcome='completed'")
        if metadata.get("verified_clean") is not True:
            errors.append("worker completion requires verified_clean=true")
    elif role == "verifier":
        if metadata.get("gate") != "pass":
            errors.append("verifier completion requires gate='pass'")
        expected = contract.get("expected_lane_count")
        if metadata.get("expected_lane_count") != expected:
            errors.append("verifier completion requires the expected lane count")
        if metadata.get("verified_lane_count") != expected:
            errors.append("verifier completion requires all expected lanes verified")
    elif role == "synthesizer":
        if metadata.get("outcome") != "completed":
            errors.append("synthesizer completion requires outcome='completed'")
        if metadata.get("result_present") is not True or not (result or "").strip():
            errors.append("synthesizer completion requires result_present=true and a result")
    if not errors:
        return None
    # Returning only the first failure forced an inefficient one-field-at-a-
    # time repair loop. Keep the legacy message for a single defect, but
    # report every defect together when more than one is present so the model
    # can correct one complete call without weakening the fail-closed gate.
    if len(errors) == 1:
        return errors[0]
    return (
        "swarm completion rejected: " + "; ".join(errors) + ". "
        "Retry one kanban_complete call using the copy-pasteable example in "
        "the task body; task_id/result/summary are top-level parameters and "
        "the contract fields belong inside metadata."
    )


def _activate_root_inline(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    summary: str,
    metadata: dict[str, Any],
) -> bool:
    """Inline blocked→done CAS flip + event insert for the swarm root.

    Runs INSIDE create_swarm's write_txn, so it must not call
    ``kb.complete_task`` (own transaction + post-commit side effects that
    would run while the outer txn can still roll back). The caller runs
    ``recompute_ready`` after the outer commit.
    """
    cur = conn.execute(
        """
        UPDATE tasks
           SET status       = 'done',
               completed_at = ?,
               claim_lock   = NULL,
               claim_expires= NULL,
               worker_pid   = NULL
         WHERE id = ?
           AND status = 'blocked'
        """,
        (int(time.time()), root_id),
    )
    if cur.rowcount != 1:
        return False
    run_id = kb._synthesize_ended_run(conn, root_id, outcome="completed", summary=summary, metadata=metadata)
    kb._append_event(
        conn, root_id, "completed", {"result_len": 0, "summary": summary[:400] or None}, run_id=run_id,
    )
    return True


def create_swarm(
    conn: sqlite3.Connection,
    *,
    goal: str,
    workers: Iterable[SwarmWorkerSpec],
    verifier_assignee: str,
    synthesizer_assignee: str,
    root_title: Optional[str] = None,
    verifier_title: str = "Verify swarm outputs",
    synthesizer_title: str = "Synthesize swarm outputs",
    tenant: Optional[str] = None,
    created_by: str = "swarm-orchestrator",
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    priority: int = 0,
    idempotency_key: Optional[str] = None,
    goal_max_turns: int = DEFAULT_GOAL_MAX_TURNS,
    worker_max_runtime_seconds: Optional[int] = None,
    worker_quorum: Optional[int] = None,
    origin: Optional[dict] = None,
) -> SwarmCreated:
    """Atomically create a durable, immediately dispatchable Kanban swarm."""
    activation_summary = "Swarm topology planned; root remains the shared blackboard."
    activated = False
    with kb.write_txn(conn):
        created = _create_swarm_uncommitted(
            conn,
            goal=goal,
            workers=workers,
            verifier_assignee=verifier_assignee,
            synthesizer_assignee=synthesizer_assignee,
            root_title=root_title,
            verifier_title=verifier_title,
            synthesizer_title=synthesizer_title,
            tenant=tenant,
            created_by=created_by,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            priority=priority,
            idempotency_key=idempotency_key,
            goal_max_turns=goal_max_turns,
            worker_max_runtime_seconds=worker_max_runtime_seconds,
            worker_quorum=worker_quorum,
            origin=origin,
        )
        root = kb.get_task(conn, created.root_id)
        if root is not None and root.status == "blocked":
            if not _activate_root_inline(
                conn,
                created.root_id,
                summary=activation_summary,
                metadata={
                    "kind": "kanban_swarm_v1",
                    "goal": goal.strip(),
                    "worker_count": len(created.worker_ids),
                },
            ):
                raise RuntimeError("could not activate the completed swarm topology")
            activated = True
    if activated:
        # After commit: recompute_ready opens its own txn and must never run
        # under an open write_txn.
        kb.recompute_ready(conn)
        root = kb.get_task(conn, created.root_id)
        run = kb.latest_run(conn, created.root_id)
        kb._fire_kanban_lifecycle_hook(
            "kanban_task_completed",
            created.root_id,
            board=kb.get_current_board(),
            assignee=root.assignee if root else None,
            run_id=run.id if run else None,
            summary=activation_summary,
        )
    return created


def _create_swarm_uncommitted(
    conn: sqlite3.Connection, *, goal: str, workers: Iterable[SwarmWorkerSpec],
    verifier_assignee: str, synthesizer_assignee: str, root_title: Optional[str],
    verifier_title: str, synthesizer_title: str, tenant: Optional[str], created_by: str,
    workspace_kind: str, workspace_path: Optional[str], priority: int, idempotency_key: Optional[str],
    goal_max_turns: int = DEFAULT_GOAL_MAX_TURNS,
    worker_max_runtime_seconds: int = DEFAULT_WORKER_MAX_RUNTIME_SECONDS,
    worker_quorum: Optional[int] = None,
    origin: Optional[dict] = None,
) -> SwarmCreated:
    """Create a durable Kanban swarm graph.

    The returned graph is immediately dispatchable: the planning root is marked
    ``done`` with topology metadata, parallel workers are ``ready``, the verifier
    waits for every worker, and the synthesizer waits for the verifier.

    ``worker_quorum`` (SWARM-PARTIAL-QUORUM-001, opt-in, ``None`` by
    default): when set, the swarm can complete once this many workers
    reach ``done``, instead of requiring literally every worker.
    Without it, one permanently failed lane (a worker that exhausts the
    dispatcher's retry budget and lands in ``blocked``) deadlocks the
    verifier forever -- ``recompute_ready`` only promotes a task once
    *every* parent is ``done`` or ``archived``, and a ``blocked`` worker
    is neither. This is exactly what happened repeatedly to real 4-lane
    swarms in docs/plans/2026-08-21-swarm-lane-timeout-retest-findings.md's
    follow-up testing -- three lanes would finish and the swarm would
    still never deliver a result over Telegram, because the fourth
    lane's dispatcher-level circuit breaker tripped and nothing ever
    excused it.

    Setting a quorum does two things together, both required -- neither
    alone is sufficient:

    1. The verifier's own completion contract requires
       ``verified_lane_count == worker_quorum`` instead of the full
       worker count, so the verifier can actually pass with partial
       evidence (previously hard-coded to require every lane; see
       ``_completion_requirements``'s own docstring for why that
       equality is load-bearing).
    2. ``excuse_blocked_workers_below_quorum`` (called from the
       dispatcher's periodic tick, see ``kanban_db.dispatch_once``)
       archives a swarm worker once it's ``blocked`` (permanently
       failed) AND enough of its siblings have already reached
       ``done`` to satisfy the quorum -- which lets
       ``recompute_ready``'s existing, unmodified "every parent done
       or archived" rule promote the verifier normally. This is
       deliberately reactive/lazy (checked once per dispatcher tick),
       not synchronous with the failure itself, to avoid adding
       swarm-specific logic into ``_record_task_failure``'s generic,
       every-task-type failure-counting path.

    Swarms created without ``worker_quorum`` (``None``, the default)
    are completely unaffected -- ``excuse_blocked_workers_below_quorum``
    is a no-op for them, and the verifier's contract still requires
    every lane, exactly as before this parameter existed.

    ``origin`` (WORKER-SUBPROCESS-SESSION-ENV-001), when given, is a dict of
    ``origin_platform``/``origin_chat_id``/``origin_thread_id``/
    ``origin_user_id``/``origin_session_key``/``origin_profile`` kwargs
    (see ``kb.create_task``) stamped onto the root task only -- every worker,
    the verifier, and the synthesizer inherit it automatically from their
    parent via ``create_task``'s own inheritance, since they're all created
    with ``parents=`` pointing back into this same tree.
    """
    goal = _require_text(goal, "goal")
    verifier_assignee = _require_text(verifier_assignee, "verifier_assignee")
    synthesizer_assignee = _require_text(synthesizer_assignee, "synthesizer_assignee")
    worker_specs = list(workers)
    if not worker_specs:
        raise ValueError("at least one worker is required")
    for i, spec in enumerate(worker_specs, start=1):
        _require_text(spec.profile, f"workers[{i}].profile")
        _require_text(spec.title, f"workers[{i}].title")

    lane_mode = any(spec.lane_id for spec in worker_specs)
    if lane_mode:
        lane_ids = [str(spec.lane_id or "").strip() for spec in worker_specs]
        if any(not lane for lane in lane_ids):
            raise ValueError("lane-bound swarms require a lane_id for every worker")
        if len(set(lane_ids)) != len(lane_ids):
            raise ValueError("worker lane_id values must be unique")
        unknown_lanes = set(lane_ids) - set(MULTI_AGENT_LANE_IDS)
        if unknown_lanes:
            raise ValueError(
                "lane-bound swarms only accept lane ids: "
                + ", ".join(MULTI_AGENT_LANE_IDS)
            )
        if REQUIRED_LANE_ID not in lane_ids:
            raise ValueError(f"lane-bound swarms require the {REQUIRED_LANE_ID} lane")
        external_present = set(lane_ids) & set(EXTERNAL_LANE_IDS)
        if len(external_present) < MIN_EXTERNAL_LANES:
            raise ValueError(
                f"lane-bound swarms require at least {MIN_EXTERNAL_LANES} of "
                + ", ".join(EXTERNAL_LANE_IDS)
            )
        if goal_max_turns < 1 or (
            worker_max_runtime_seconds is not None and worker_max_runtime_seconds < 1
        ):
            raise ValueError("goal_max_turns and worker_max_runtime_seconds must be positive")
        if worker_quorum is not None and not (1 <= worker_quorum <= len(worker_specs)):
            raise ValueError(
                f"worker_quorum must be between 1 and {len(worker_specs)} "
                "(the number of workers in this swarm)"
            )
    elif worker_quorum is not None:
        raise ValueError("worker_quorum is only meaningful for lane-bound swarms")

    # Resolve and validate every worker BEFORE creating any card.
    #
    # SWARM-E2E-DEFECTS-001 Defect 1. This check used to sit inside the
    # creation loop, so a swarm whose second worker was invalid still left a
    # root and one live worker behind -- and the dispatcher picked them up and
    # ran them. Observed in production on 2026-08-19, not only in a test:
    # a partial graph consumed real compute on work no verifier would ever
    # consume, because no verifier had been created.
    #
    # This makes the failure happen before anything exists. It does NOT make
    # creation atomic: `create_task` opens its own write transaction, so
    # `create_swarm` cannot wrap the sequence in one, and a failure *inside*
    # card creation (a database error, a disk fault) can still leave a partial
    # graph. That is a smaller and different exposure than a validation error,
    # which is deterministic and entirely predictable from the arguments.
    resolved_skills: list[str] = []
    for i, spec in enumerate(worker_specs, start=1):
        expected_skill = (
            spec.preflight_skill_id.strip()
            if spec.preflight_skill_id.strip()
            else (spec.skills[0].strip() if len(spec.skills) == 1 else "")
        )
        if lane_mode:
            worker_lane = str(spec.lane_id).strip()
            if worker_lane != REQUIRED_LANE_ID and not expected_skill:
                raise ValueError(f"worker {worker_lane} requires a preflight skill id")
        resolved_skills.append(expected_skill)

    common = dict(
        created_by=created_by, tenant=tenant,
        workspace_kind=workspace_kind, workspace_path=workspace_path,
    )
    root = kb.create_task(
        conn,
        title=root_title or f"Swarm: {goal.splitlines()[0][:80]}",
        body="Kanban Swarm v1 planning/root card. This card is completed "
             "immediately so parallel workers can start while it remains the "
             f"shared blackboard and audit anchor.\n\nGoal:\n{goal}",
        assignee=created_by,
        priority=priority,
        idempotency_key=idempotency_key,
        initial_status="blocked",
        goal_mode=lane_mode,
        goal_max_turns=goal_max_turns if lane_mode else None,
        **common,
        **(origin or {}),
    )

    # Idempotency may return an existing root: recover its topology from the
    # blackboard instead of duplicating the graph.
    existing = latest_blackboard(conn, root).get("topology")
    if isinstance(existing, dict):
        worker_ids = [str(x) for x in existing.get("worker_ids", []) if x]
        verifier_id = existing.get("verifier_id")
        synthesizer_id = existing.get("synthesizer_id")
        if worker_ids and verifier_id and synthesizer_id:
            return SwarmCreated(root, worker_ids, str(verifier_id), str(synthesizer_id))

    context_suffix = _swarm_context(root, goal)
    worker_ids: list[str] = []
    for spec, expected_skill in zip(worker_specs, resolved_skills):
        worker_lane = str(spec.lane_id).strip() if lane_mode else None
        contract = None
        if lane_mode:
            contract = {
                "version": 1, "role": "worker", "root_id": root,
                "expected_lane_id": worker_lane, "preflight_skill_id": expected_skill,
                "output_policy": dict(DEFAULT_OUTPUT_CONTRACT_POLICY),
            }
        worker_body = (spec.body or "") + context_suffix
        if contract:
            worker_body += "\n" + _completion_requirements(contract)
            worker_body += "\n" + _contract_line(contract)
        if spec.toolsets:
            requested_toolsets = [str(name).strip() for name in spec.toolsets if str(name).strip()]
            if requested_toolsets:
                worker_body += "\n" + WORKER_TOOLSETS_PREFIX + json.dumps(
                    requested_toolsets, ensure_ascii=False
                )
        worker_id = kb.create_task(
            conn,
            title=spec.title,
            body=worker_body,
            assignee=spec.profile,
            parents=[root],
            priority=spec.priority or priority,
            skills=spec.skills or None,
            max_runtime_seconds=(
                spec.max_runtime_seconds
                if spec.max_runtime_seconds is not None
                else (
                    (
                        worker_max_runtime_seconds
                        if worker_max_runtime_seconds is not None
                        else _default_worker_max_runtime_seconds(worker_lane)
                    )
                    if lane_mode
                    else None
                )
            ),
            goal_mode=lane_mode,
            goal_max_turns=goal_max_turns if lane_mode else None,
            **common,
        )
        worker_ids.append(worker_id)
    verifier_body = (
        "Review every worker handoff and blackboard update. Gate the swarm: "
        "pass only when the evidence is sufficient; otherwise block with the "
        "exact missing work."
        + context_suffix
    )
    if worker_quorum is not None:
        verifier_body += (
            f"\n\nThis swarm has a quorum of {worker_quorum} out of "
            f"{len(worker_specs)} workers -- verify and pass once at least "
            f"{worker_quorum} worker lanes have usable results, even if one "
            "or more other lanes never produced one. Do not wait for or "
            "demand evidence from a lane that never completed."
        )
    if lane_mode:
        verifier_contract = {
            "version": 1,
            "role": "verifier",
            "root_id": root,
            "expected_lane_count": (
                worker_quorum if worker_quorum is not None else len(worker_specs)
            ),
        }
        verifier_body += "\n" + _completion_requirements(verifier_contract)
        verifier_body += "\n" + _contract_line(verifier_contract)
    verifier = kb.create_task(
        conn,
        title=verifier_title,
        body=verifier_body,
        assignee=verifier_assignee,
        parents=worker_ids,
        priority=priority,
        skills=["requesting-code-review"],
        goal_mode=lane_mode,
        goal_max_turns=goal_max_turns if lane_mode else None,
        **common,
        max_runtime_seconds=DEFAULT_WORKER_MAX_RUNTIME_SECONDS,
    )
    synthesizer_body = (
        "Synthesize the verified worker outputs into the final deliverable. "
        "Do not start until the verifier has passed the gate. "
        "Return only coherent, user-facing Traditional Chinese (Taiwan) text; "
        "do not output Simplified Chinese, status metadata, invented artifacts, "
        "or stale task IDs."
        + context_suffix
    )
    if lane_mode:
        synthesizer_contract = {
            "version": 1,
            "role": "synthesizer",
            "root_id": root,
            "verifier_id": verifier,
            "output_policy": {
                **DEFAULT_OUTPUT_CONTRACT_POLICY,
                "required_language": "zh-Hant-TW",
            },
            "goal_anchor_terms": _goal_anchor_terms(goal),
        }
        synthesizer_body += "\n" + _completion_requirements(synthesizer_contract)
        synthesizer_body += "\n" + _contract_line(synthesizer_contract)
    synthesizer = kb.create_task(
        conn,
        title=synthesizer_title,
        body=synthesizer_body,
        assignee=synthesizer_assignee,
        parents=[verifier],
        priority=priority,
        skills=["humanizer"],
        max_runtime_seconds=DEFAULT_WORKER_MAX_RUNTIME_SECONDS,
        goal_mode=lane_mode,
        goal_max_turns=goal_max_turns if lane_mode else None,
        **common,
    )

    created = SwarmCreated(root, worker_ids, verifier, synthesizer)
    post_blackboard_update(
        conn,
        root,
        author=created_by,
        key="topology",
        value=created.as_dict() | {"goal": goal, "worker_quorum": worker_quorum},
    )
    return created


def excuse_blocked_workers_below_quorum(conn: sqlite3.Connection) -> int:
    """Archive ``blocked`` swarm workers once enough siblings already
    reached ``done`` to satisfy their swarm's ``worker_quorum`` -- see
    ``create_swarm``'s ``worker_quorum`` docstring section for the full
    rationale. No-op for swarms created without a quorum.

    Meant to be called once per dispatcher tick, before
    ``recompute_ready`` (``archive_task`` calls ``recompute_ready``
    itself on every excuse, so the verifier can become ``ready`` in the
    same tick it's finally unblocked). Cheap when there is nothing to
    do: only swarm workers matching ``role=worker`` in a
    ``[swarm:contract]`` line can ever be selected, and most boards
    have zero of those at any given moment.

    **Never touches a sticky (worker/operator-initiated ``kanban_block``)
    block** -- caught by independent cross-review before this ever
    merged: a first version selected on ``status = 'blocked'`` alone,
    which is exactly what the dispatcher's own circuit breaker
    (``_record_task_failure``, emits a ``"gave_up"`` event) AND a
    deliberate worker/operator ``kanban_block`` call (``block_task``,
    emits a ``"blocked"`` event) both set -- there was nothing
    distinguishing "permanently gave up, safe to excuse" from
    "deliberately paused for human review, must stay put until an
    explicit unblock." Reuses ``kb._has_sticky_block``, the exact same
    primitive ``recompute_ready`` already relies on for this identical
    distinction (see its own docstring, case 1) -- so this function and
    ``recompute_ready`` can never disagree about what counts as sticky.

    Also re-checks the task is still ``blocked`` immediately before
    archiving (defense in depth against a human running
    ``kanban unblock`` concurrently with this call, between the initial
    snapshot query and the archive; SQLite's write-txn locking under
    this repo's single-instance dispatcher makes the actual window
    negligible, but the check is cheap and removes the TOCTOU
    entirely rather than relying on that alone).

    Returns the number of tasks archived this call.
    """
    excused = 0
    rows = conn.execute(
        "SELECT id, body FROM tasks WHERE status = 'blocked'"
    ).fetchall()
    for row in rows:
        contract = extract_contract(row["body"])
        if not contract or contract.get("role") != "worker":
            continue
        root_id = contract.get("root_id")
        if not root_id:
            continue
        topology = latest_blackboard(conn, root_id).get("topology")
        if not isinstance(topology, dict):
            continue
        quorum = topology.get("worker_quorum")
        if not isinstance(quorum, int) or quorum < 1:
            continue
        worker_ids = [str(w) for w in topology.get("worker_ids", []) if w]
        if row["id"] not in worker_ids:
            continue
        if kb._has_sticky_block(conn, row["id"]):
            # Deliberately paused for human review -- never auto-excuse.
            continue
        done_count = 0
        for worker_id in worker_ids:
            if worker_id == row["id"]:
                continue
            sibling = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (worker_id,)
            ).fetchone()
            if sibling is not None and sibling["status"] == "done":
                done_count += 1
        if done_count >= quorum:
            still_blocked = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (row["id"],)
            ).fetchone()
            if still_blocked is None or still_blocked["status"] != "blocked":
                continue
            if kb.archive_task(conn, row["id"]):
                excused += 1
    return excused


def post_blackboard_update(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    author: str,
    key: str,
    value: Any,
) -> int:
    """Append one structured update to the swarm root blackboard."""
    _require_text(root_id, "root_id")
    author = _require_text(author, "author")
    key = _require_text(key, "key")
    payload = json.dumps({"key": key, "value": value}, ensure_ascii=False, sort_keys=True)
    return kb.add_comment(conn, root_id, author=author, body=BLACKBOARD_PREFIX + payload)


def latest_blackboard(conn: sqlite3.Connection, root_id: str) -> dict[str, Any]:
    """Merge structured blackboard comments on a root card. Later comments
    replace earlier values for the same key; ``_authors`` records the author
    of the winning value for traceability."""
    merged: dict[str, Any] = {}
    authors: dict[str, str] = {}
    for comment in kb.list_comments(conn, root_id):
        body = comment.body or ""
        if not body.startswith(BLACKBOARD_PREFIX):
            continue
        try:
            payload = json.loads(body[len(BLACKBOARD_PREFIX):])
        except json.JSONDecodeError:
            continue
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            continue
        merged[key] = payload.get("value")
        authors[key] = comment.author
    if authors:
        merged["_authors"] = authors
    return merged


def parse_worker_arg(raw: str) -> SwarmWorkerSpec:
    """Parse CLI ``--worker profile:title[:skill,skill]`` values."""
    parts = [p.strip() for p in raw.split(":", 2)]
    if len(parts) < 2:
        raise ValueError("worker must be profile:title or profile:title:skill,skill")
    skills = [s.strip() for s in parts[2].split(",") if s.strip()] if len(parts) == 3 and parts[2] else []
    return SwarmWorkerSpec(profile=parts[0], title=parts[1], body=parts[1], skills=skills)
