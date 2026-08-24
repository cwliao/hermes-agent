from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_swarm import (
    MULTI_AGENT_LANE_IDS,
    SwarmWorkerSpec,
    create_swarm,
    extract_contract,
    swarm_output_metadata,
    validate_completion,
    validate_swarm_output_text,
)


def _lane_specs():
    return [
        SwarmWorkerSpec(
            profile=lane,
            title=f"{lane} joke",
            body="Return one bounded joke.",
            skills=[] if lane == "native_hermes" else ["kanban-worker"],
            lane_id=lane,
        )
        for lane in MULTI_AGENT_LANE_IDS
    ]


def test_worker_context_aggregate_cap_preserves_swarm_contract(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Generate four bounded autumn jokes.",
            workers=_lane_specs(),
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        worker_id = created.worker_ids[0]
        for i in range(45):
            kb.add_comment(conn, worker_id, author="worker", body=f"comment {i} " + "x" * 1800)

        context = kb.build_worker_context(conn, worker_id)
        assert len(context) <= kb._CTX_MAX_TOTAL_CHARS
        assert "[swarm:contract]" in context
        assert "kanban_complete(" in context
        assert "Do not invent extra deliverables" in context
        assert "optional artifact is never a blocker" in context
        assert created.root_id in context
    finally:
        conn.close()


def test_swarm_output_validation_is_narrow_and_auditable():
    worker_contract = {
        "role": "worker",
        "root_id": "t_root",
        "expected_lane_id": "claude",
        "preflight_skill_id": "claude-code",
        "output_policy": {
            "reject_internal_length_marker": True,
            "require_balanced_quotes": True,
        },
    }
    assert validate_swarm_output_text(
        "秋天的葉子很會變色。", contract=worker_contract
    ) is None
    assert validate_swarm_output_text(
        "秋天的葉子很會變色（30字）", contract=worker_contract
    ) == "swarm output contains an internal length marker"
    assert validate_swarm_output_text(
        "秋天的葉子很會變色」", contract=worker_contract
    ) == "swarm output has an unmatched closing '」'"
    assert validate_swarm_output_text(
        "他說「秋天很美」。", contract=worker_contract
    ) is None
    assert validate_swarm_output_text(
        "使用者要求原樣輸出（30字）",
        contract={**worker_contract, "output_policy": {}},
    ) is None

    metadata = swarm_output_metadata("秋天的葉子很會變色。", contract=worker_contract)
    assert metadata == {
        "char_count": 10,
        "format_valid": True,
        "validation_reason": "ok",
    }

    synthesizer_contract = {
        "role": "synthesizer",
        "root_id": "t_root",
        "output_policy": {
            "reject_internal_length_marker": True,
            "require_balanced_quotes": True,
        },
    }
    assert validate_swarm_output_text(
        "Verified all four lane outputs processed; final synthesized output ready "
        "as JSONL; file existence confirmed by read_file; completion metadata provided",
        contract=synthesizer_contract,
    ) == (
        "synthesizer result is status-only; include the exact final user-facing "
        "deliverable text"
    )
    assert validate_swarm_output_text(
        "Joke A: 秋天的葉子會變色。\nJoke B: 秋風一吹，笑聲就落葉。",
        contract={**synthesizer_contract, "output_policy": {
            **synthesizer_contract["output_policy"],
            "required_language": "zh-Hant-TW",
        }},
    ) is None
    assert validate_swarm_output_text(
        "故意写假话：冬季双关冷笑话。",
        contract={**synthesizer_contract, "output_policy": {
            **synthesizer_contract["output_policy"],
            "required_language": "zh-Hant-TW",
        }},
    ) == (
        "synthesizer result contains Simplified Chinese; use "
        "Traditional Chinese (Taiwan) only"
    )


def test_synthesizer_rejects_corrupted_and_mixed_script_text():
    contract = {
        "role": "synthesizer",
        "root_id": "t_root",
        "output_policy": {"required_language": "zh-Hant-TW"},
    }
    assert validate_swarm_output_text(
        "純貓揄竿� পরিচিত",
        contract=contract,
    ) == (
        "synthesizer result contains Unicode replacement characters; "
        "regenerate clean Traditional Chinese text"
    )
    assert validate_swarm_output_text(
        "純貓揄竿 পরিচিত",
        contract=contract,
    ) == (
        "synthesizer result contains a non-Chinese writing system; "
        "use Traditional Chinese (Taiwan) only"
    )


def test_synthesizer_rejects_result_for_previous_goal():
    contract = {
        "role": "synthesizer",
        "root_id": "t_root",
        "goal_anchor_terms": [chr(0x72d7)],
        "output_policy": {"required_language": "zh-Hant-TW"},
    }
    old_result = (
        chr(0x665a) + chr(0x5b89) + chr(0x8ae7) + chr(0x97f3) + ": "
        + "1. " + chr(0x96ea) + chr(0x4e4b) + chr(0x9999)
        + chr(0x53f0) + chr(0x7063) + " 2. " + chr(0x51ac)
        + chr(0x591c) + chr(0x88d4) + "3. " + chr(0x7236)
        + chr(0x8a9e) + chr(0x51b0) + "4. " + chr(0x7236)
        + chr(0x7b56) + chr(0x5177)
    )
    assert validate_swarm_output_text(old_result, contract=contract) == (
        "synthesizer result does not reference the current swarm "
        "goal; regenerate for the current request"
    )
    assert validate_swarm_output_text(
        chr(0x5c0f) + chr(0x72d7) + chr(0x8ae7) + chr(0x97f3) + chr(0x6897),
        contract=contract,
    ) is None


def test_completion_rejects_malformed_worker_summary_without_rewriting(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Generate four bounded autumn jokes.",
            workers=_lane_specs(),
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        task = kb.get_task(conn, created.worker_ids[1])
        contract = extract_contract(task.body)
        metadata = {
            "role": "worker",
            "root_id": created.root_id,
            "lane_id": "claude",
            "preflight_skill_id": contract["preflight_skill_id"],
            "outcome": "completed",
            "verified_clean": True,
        }
        reason = validate_completion(
            task,
            metadata=metadata,
            summary="秋天的葉子很會變色（30字）",
        )
        assert reason == "swarm output contains an internal length marker"
        assert contract["role"] == "worker"
    finally:
        conn.close()


def test_completion_reports_all_contract_defects_in_one_retry(tmp_path):
    """A bad first call must not make the model repair one field per turn."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Generate four bounded autumn jokes.",
            workers=_lane_specs(),
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        task = kb.get_task(conn, created.worker_ids[1])
        reason = validate_completion(
            task,
            metadata={"role": "wrong", "lane_id": "wrong"},
            summary="秋天的葉子很會變色（30字）",
        )
        assert reason is not None
        assert reason.startswith("swarm completion rejected:")
        assert "metadata role='worker'" in reason
        assert "root_id does not match" in reason
        assert "worker lane_id does not match" in reason
        assert "preflight_skill_id" in reason
        assert "verified_clean=true" in reason
        assert "copy-pasteable example" in reason
    finally:
        conn.close()
