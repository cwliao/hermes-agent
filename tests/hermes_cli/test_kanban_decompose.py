"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from tools import needle_worker


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _mock_needle_prefilter(monkeypatch):
    """Keep the pre-existing decomposer tests on their original no-hint path."""
    monkeypatch.setattr(needle_worker, "extract_triage_hints", AsyncMock(return_value=None))


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def _single_task_llm_payload() -> str:
    return jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Concrete worker instructions.",
    })


def _run_with_captured_aux_prompt(task_id: str, llm_payload: str):
    captured = MagicMock(return_value=_fake_aux_response(llm_payload))
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", new=captured):
            outcome = decomp.decompose_task(task_id, author="me")
    finally:
        for p in patches:
            p.stop()
    messages = captured.call_args.kwargs["messages"]
    return outcome, messages[1]["content"]


def test_decompose_injects_high_confidence_needle_hint_into_aux_prompt(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Dashboard drops telemetry after overnight restart",
            body=(
                "The Hsinchu fab energy dashboard intermittently loses SCADA data after a "
                "nightly restart. Identify the likely integration failure and propose a "
                "recovery check, but keep the production service unchanged."
            ),
            triage=True,
        )

    hint = {
        "intent": "diagnose SCADA telemetry loss and propose a safe recovery check",
        "entities": ["Hsinchu fab", "SCADA", "energy dashboard"],
        "confidence": 0.91,
    }
    with patch(
        "tools.needle_worker.extract_triage_hints",
        new=AsyncMock(return_value=hint),
    ) as extract_mock:
        outcome, prompt = _run_with_captured_aux_prompt(tid, _single_task_llm_payload())

    assert outcome.ok, outcome.reason
    extract_mock.assert_awaited_once()
    assert hint["intent"] in prompt
    assert "Hsinchu fab, SCADA, energy dashboard" in prompt
    assert "Dashboard drops telemetry after overnight restart" in prompt


def test_decompose_ignores_none_confidence_needle_hint(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Refresh the weekly carbon-capture summary",
            body="Use the existing source data and preserve the current reporting format.",
            triage=True,
        )

    hint_with_unknown_confidence = {
        "intent": "rewrite the carbon-capture summary",
        "entities": ["weekly report"],
        "confidence": None,
    }
    with patch(
        "tools.needle_worker.extract_triage_hints",
        new=AsyncMock(return_value=hint_with_unknown_confidence),
    ):
        outcome, prompt = _run_with_captured_aux_prompt(tid, _single_task_llm_payload())

    assert outcome.ok, outcome.reason
    assert "Auto-extracted hint" not in prompt
    assert hint_with_unknown_confidence["intent"] not in prompt
    assert "Refresh the weekly carbon-capture summary" in prompt


def test_decompose_falls_back_when_needle_prefilter_raises(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Investigate an intermittent API timeout",
            body="Reproduce the timeout and document the existing retry behaviour.",
            triage=True,
        )

    with patch(
        "tools.needle_worker.extract_triage_hints",
        new=AsyncMock(side_effect=TimeoutError("Needle timed out")),
    ) as extract_mock:
        outcome, prompt = _run_with_captured_aux_prompt(tid, _single_task_llm_payload())

    assert outcome.ok, outcome.reason
    extract_mock.assert_awaited_once()
    assert "Auto-extracted hint" not in prompt
    assert "Investigate an intermittent API timeout" in prompt


def test_decompose_with_fanout_creates_children(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_children_inherit_root_assignee_when_unrouted(kanban_home):
    """Unrouted children fall back to the ROOT task's assignee, not
    the decomposer's active profile (#114294). The active profile here is ``private``
    (an incognito profile with no credentials), so the old fallback spawned
    workers that deadlocked on capability blockers."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="zdr", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    # get_active_profile_name() is mocked to names[0] = "private" — the
    # global default chain would resolve there without kanban.default_assignee.
    patches = _patch_list_profiles(["private", "zdr"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "zdr"
    assert c1.assignee == "zdr"
    # Same class for the root: no ``orchestrator_profile`` must not hand the
    # orchestration card to the dispatcher's own (here: incognito) profile.
    assert root.assignee == "zdr"


def test_decompose_explicit_default_assignee_wins_over_root_assignee(kanban_home):
    """An explicitly configured ``kanban.default_assignee`` stays
    authoritative for unroutable children; the root task's assignee only
    fills in when no explicit default is set (explicit config → card
    assignee → active profile)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="engineer", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["engineer", "docs", "private"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "docs"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "docs"
    assert c1.assignee == "docs"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_load_routing_falls_back_to_defaults_when_config_unreadable(kanban_home, monkeypatch):
    """decompose_task promises ok=False on expected failures; a config read that raises (missing
    profile home, HomeInitializationError) must not escape _load_routing as an exception."""
    from hermes_cli import config as config_mod

    def _boom():
        raise FileNotFoundError("profile home is gone")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    routing = decomp._load_routing()
    assert routing.default_assignee == "default" and routing.auto_promote is True


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_refuses_task_with_contract_fanout_true(kanban_home):
    contract_line = '[swarm:contract] {"role": "verifier", "root_id": "t_root", "verifier_id": "t_v"}'
    body_with_contract = f"Review work.\n{contract_line}"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="swarm verifier", body=body_with_contract, triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "child 1", "body": "c1", "assignee": "researcher", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "refusing to auto-decompose" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.body == body_with_contract
        events = [e for e in kb.list_events(conn, tid) if e.kind == "verifier_gate_rejected"]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["stall_key"] == f"triage-refused:{tid}"
        assert payload["source"] == "decompose_task"


def test_decompose_refuses_task_with_contract_fanout_false(kanban_home):
    contract_line = '[swarm:contract] {"role": "worker", "root_id": "t_root"}'
    body_with_contract = f"Do work.\n{contract_line}"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="swarm worker", body=body_with_contract, triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single",
        "title": "Rewritten title",
        "body": "Rewritten body without contract",
    })

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "refusing to auto-decompose" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.body == body_with_contract
        events = [e for e in kb.list_events(conn, tid) if e.kind == "verifier_gate_rejected"]
        assert len(events) == 1


def test_decompose_refuses_task_with_malformed_contract(kanban_home):
    body_with_malformed = "Do work.\n[swarm:contract] {invalid-json"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="broken swarm", body=body_with_malformed, triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "refusing to auto-decompose" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.body == body_with_malformed
        events = [e for e in kb.list_events(conn, tid) if e.kind == "verifier_gate_rejected"]
        assert len(events) == 1


