"""Kanban Swarm v1: thin swarm topology helpers on top of Kanban.

This module intentionally does not introduce a second scheduler. It writes a
small task graph into the existing Kanban kernel:

    planning root (completed immediately)
        ├─ parallel specialist workers (ready)
        └─ verifier (todo until all workers done)
             └─ synthesizer (todo until verifier done)

The shared blackboard is also deliberately low-tech: structured JSON comments on
the root task. That keeps all state in existing task_comments/task_events rows,
so the dashboard, notifier, slash command, and dispatcher keep working without a
new service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import sqlite3
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb

BLACKBOARD_PREFIX = "[swarm:blackboard] "
CONTRACT_PREFIX = "[swarm:contract] "
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


@dataclass(frozen=True)
class SwarmCreated:
    """IDs produced by :func:`create_swarm`."""

    root_id: str
    worker_ids: list[str]
    verifier_id: str
    synthesizer_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "worker_ids": list(self.worker_ids),
            "verifier_id": self.verifier_id,
            "synthesizer_id": self.synthesizer_id,
        }


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
        "- Put machine-readable facts in completion metadata.\n"
        "- To post cross-worker notes on the shared blackboard, call the "
        f'`kanban_comment` tool with task_id="{root_id}" and your note as '
        "`body`. Do NOT write directly to kanban.db via shell/sqlite3 or "
        "execute_code -- execute_code is blocked outright for unattended "
        "workers, and hand-written SQL bypasses the audit trail even when "
        "it works.\n"
        f"- Goal: {goal.strip()}\n"
    )


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
    if role == "worker":
        lines += [
            f'  lane_id = "{contract.get("expected_lane_id")}"',
            f'  preflight_skill_id = "{contract.get("preflight_skill_id") or ""}"',
            '  outcome = "completed"',
            "  verified_clean = true",
        ]
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
        ]
    lines.append(
        "Send these as completion metadata. Do not complete with a subset."
    )
    return "\n".join(lines)


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
    task: Any,
    *,
    metadata: Optional[dict[str, Any]],
    result: Optional[str] = None,
) -> Optional[str]:
    """Return a rejection reason for a contract-bound task, else ``None``."""

    contract = extract_contract(getattr(task, "body", None))
    if not contract:
        return None
    metadata = metadata if isinstance(metadata, dict) else {}
    role = contract.get("role")
    if metadata.get("role") != role:
        return f"swarm {role} completion requires metadata role={role!r}"
    if metadata.get("root_id") != contract.get("root_id"):
        return "swarm completion root_id does not match the task contract"
    if role == "worker":
        if metadata.get("lane_id") != contract.get("expected_lane_id"):
            return "worker lane_id does not match the expected lane"
        expected_skill = contract.get("preflight_skill_id") or ""
        if metadata.get("preflight_skill_id", "") != expected_skill:
            return "worker preflight_skill_id does not match the expected skill"
        if metadata.get("outcome") != "completed":
            return "worker completion requires outcome='completed'"
        if metadata.get("verified_clean") is not True:
            return "worker completion requires verified_clean=true"
    elif role == "verifier":
        if metadata.get("gate") != "pass":
            return "verifier completion requires gate='pass'"
        expected = contract.get("expected_lane_count")
        if metadata.get("expected_lane_count") != expected:
            return "verifier completion requires the expected lane count"
        if metadata.get("verified_lane_count") != expected:
            return "verifier completion requires all expected lanes verified"
    elif role == "synthesizer":
        if metadata.get("outcome") != "completed":
            return "synthesizer completion requires outcome='completed'"
        if metadata.get("result_present") is not True or not (result or "").strip():
            return "synthesizer completion requires result_present=true and a result"
    return None


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

    root = kb.create_task(
        conn,
        title=root_title or f"Swarm: {goal.splitlines()[0][:80]}",
        body=(
            "Kanban Swarm v1 planning/root card. This card is completed "
            "immediately so parallel workers can start while it remains the "
            "shared blackboard and audit anchor.\n\n"
            f"Goal:\n{goal}"
        ),
        assignee=created_by,
        created_by=created_by,
        tenant=tenant,
        priority=priority,
        idempotency_key=idempotency_key,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        goal_mode=lane_mode,
        goal_max_turns=goal_max_turns if lane_mode else None,
        **(origin or {}),
    )

    # If idempotency returned an existing non-archived root, do not duplicate the
    # swarm graph. Recover the topology from the root's latest blackboard, if it
    # was created by this helper previously.
    existing = latest_blackboard(conn, root).get("topology")
    if isinstance(existing, dict):
        worker_ids = [str(x) for x in existing.get("worker_ids", []) if x]
        verifier_id = existing.get("verifier_id")
        synthesizer_id = existing.get("synthesizer_id")
        if worker_ids and verifier_id and synthesizer_id:
            return SwarmCreated(
                root_id=root,
                worker_ids=worker_ids,
                verifier_id=str(verifier_id),
                synthesizer_id=str(synthesizer_id),
            )

    kb.complete_task(
        conn,
        root,
        summary="Swarm topology planned; root remains the shared blackboard.",
        metadata={
            "kind": "kanban_swarm_v1",
            "goal": goal,
            "worker_count": len(worker_specs),
        },
    )

    context_suffix = _swarm_context(root, goal)
    worker_ids: list[str] = []
    for spec, expected_skill in zip(worker_specs, resolved_skills):
        worker_lane = str(spec.lane_id).strip() if lane_mode else None
        contract = None
        if lane_mode:
            contract = {
                "version": 1,
                "role": "worker",
                "root_id": root,
                "expected_lane_id": worker_lane,
                "preflight_skill_id": expected_skill,
            }
        worker_body = (spec.body or "") + context_suffix
        if contract:
            worker_body += "\n" + _completion_requirements(contract)
            worker_body += "\n" + _contract_line(contract)
        worker_id = kb.create_task(
            conn,
            title=spec.title,
            body=worker_body,
            assignee=spec.profile,
            created_by=created_by,
            parents=[root],
            tenant=tenant,
            priority=spec.priority or priority,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
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
        created_by=created_by,
        parents=worker_ids,
        tenant=tenant,
        priority=priority,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        skills=["requesting-code-review"],
    )

    synthesizer_body = (
        "Synthesize the verified worker outputs into the final deliverable. "
        "Do not start until the verifier has passed the gate."
        + context_suffix
    )
    if lane_mode:
        synthesizer_contract = {
            "version": 1,
            "role": "synthesizer",
            "root_id": root,
            "verifier_id": verifier,
        }
        synthesizer_body += "\n" + _completion_requirements(synthesizer_contract)
        synthesizer_body += "\n" + _contract_line(synthesizer_contract)
    synthesizer = kb.create_task(
        conn,
        title=synthesizer_title,
        body=synthesizer_body,
        assignee=synthesizer_assignee,
        created_by=created_by,
        parents=[verifier],
        tenant=tenant,
        priority=priority,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        skills=["humanizer"],
        goal_mode=lane_mode,
        goal_max_turns=goal_max_turns if lane_mode else None,
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
    """Merge structured blackboard comments on a root card.

    Later comments replace earlier values for the same key. ``_authors`` records
    the author of the winning value for traceability.
    """

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
    skills: list[str] = []
    if len(parts) == 3 and parts[2]:
        skills = [s.strip() for s in parts[2].split(",") if s.strip()]
    # The optional third segment is a comma-separated skill list only.  Bounded
    # worker instructions belong in the title/body, never in a skill token.
    return SwarmWorkerSpec(profile=parts[0], title=parts[1], body=parts[1], skills=skills)
