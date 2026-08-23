"""Tests for issue #860 — SQLite session transcript deduplication.

Verifies that:
1. _flush_messages_to_session_db uses _last_flushed_db_idx to avoid re-writing
2. Multiple _persist_session calls don't duplicate messages
3. append_to_transcript(skip_db=True) skips SQLite but writes JSONL
4. The gateway doesn't double-write messages the agent already persisted
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch



# ---------------------------------------------------------------------------
# Test: _flush_messages_to_session_db only writes new messages
# ---------------------------------------------------------------------------

class TestFlushDeduplication:
    """Verify _flush_messages_to_session_db tracks what it already wrote."""

    def _make_agent(self, session_db):
        """Create a minimal AIAgent with a real session DB."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=session_db,
                session_id="test-session-860",
                skip_context_files=True,
                skip_memory=True,
            )
        # Simulate lazy session creation (normally done by run_conversation)
        agent._ensure_db_session()
        return agent

    def test_flush_writes_only_new_messages(self):
        """First flush writes all new messages, second flush writes none."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)
            try:
                agent = self._make_agent(db)

                conversation_history = [
                    {"role": "user", "content": "old message"},
                ]
                messages = list(conversation_history) + [
                    {"role": "user", "content": "new question"},
                    {"role": "assistant", "content": "new answer"},
                ]

                # First flush — should write 2 new messages
                agent._flush_messages_to_session_db(messages, conversation_history)

                rows = db.get_messages(agent.session_id)
                assert len(rows) == 2, f"Expected 2 messages, got {len(rows)}"

                # Second flush with SAME messages — should write 0 new messages
                agent._flush_messages_to_session_db(messages, conversation_history)

                rows = db.get_messages(agent.session_id)
                assert len(rows) == 2, f"Expected still 2 messages after second flush, got {len(rows)}"
            finally:
                db.close()



    def test_flush_reset_after_compression(self):
        """After compression creates a new session, flush index resets."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)
            try:
                agent = self._make_agent(db)

                # Write some messages
                messages = [
                    {"role": "user", "content": "msg1"},
                    {"role": "assistant", "content": "reply1"},
                ]
                agent._flush_messages_to_session_db(messages, [])

                old_session = agent.session_id
                assert agent._last_flushed_db_idx == 2

                # Simulate what _compress_context does: new session, reset idx
                agent.session_id = "compressed-session-new"
                db.create_session(session_id=agent.session_id, source="test")
                agent._last_flushed_db_idx = 0

                # Now flush compressed messages to new session
                compressed_messages = [
                    {"role": "user", "content": "summary of conversation"},
                ]
                agent._flush_messages_to_session_db(compressed_messages, [])

                new_rows = db.get_messages(agent.session_id)
                assert len(new_rows) == 1

                # Old session should still have its 2 messages
                old_rows = db.get_messages(old_session)
                assert len(old_rows) == 2
            finally:
                db.close()

    def test_cold_copied_sqlite_history_is_not_appended_again(self):
        """Durable row ids survive history copies without duplicates."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            try:
                agent = self._make_agent(db)
                db.append_messages_batch(
                    session_id=agent.session_id,
                    messages=[
                        {"role": "user", "content": "old question"},
                        {"role": "assistant", "content": "old answer"},
                    ],
                )
                loaded = db.get_messages_as_conversation(
                    agent.session_id, include_row_ids=True
                )
                copied_history = [{**message} for message in loaded]
                copied_messages = copied_history + [
                    {"role": "user", "content": "new question"},
                    {"role": "assistant", "content": "new answer"},
                ]

                # Model a cold/resume path: object identity and in-memory
                # markers are unavailable, but durable ids remain.
                agent._db_flush_scan_prefix = None
                agent._last_flushed_db_idx = 0
                agent._flush_messages_to_session_db(copied_messages, [])

                rows = db.get_messages(agent.session_id)
                assert [row["content"] for row in rows] == [
                    "old question", "old answer", "new question", "new answer"
                ]
            finally:
                db.close()

    def test_ten_cold_resume_turns_keep_one_active_row_per_nonce(self, tmp_path):
        """Ten process-boundary turns never replay a prior nonce.

        Each child models one quiet CLI invocation: it creates a fresh
        ``AIAgent``, restores the transcript with durable row ids, copies that
        history into the turn list, and flushes one unique user/assistant pair.
        The final active transcript is the machine-checkable invariant from
        the synthetic replay probe: ten unique user rows and ten matching
        assistant rows, with no duplicate historical rows.
        """
        from hermes_state import SessionDB

        repo_root = Path(__file__).resolve().parents[2]
        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        db_path = hermes_home / "state.db"
        session_id = "ten-cold-resume-turns"

        db = SessionDB(db_path=db_path)
        db.create_session(session_id=session_id, source="cli")
        db.close()

        child_script = r"""
import os
from pathlib import Path

from hermes_state import SessionDB
from run_agent import AIAgent

home = Path(os.environ["HERMES_HOME"])
session_id = os.environ["HERMES_SESSION_ID"]
turn = int(os.environ["HERMES_TURN"])
nonce = f"SYNTH-{turn}-8f3c"
db = SessionDB(home / "state.db")
try:
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        session_db=db,
        session_id=session_id,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._ensure_db_session()
    restored = db.get_messages_as_conversation(
        session_id,
        repair_alternation=True,
        include_row_ids=True,
    )
    copied_history = [{**message} for message in restored]
    messages = copied_history + [
        {
            "role": "user",
            "content": (
                f"Synthetic replay probe only. This is turn {turn} of 10, "
                f"nonce {nonce}. Do not reuse prior results."
            ),
        },
        {"role": "assistant", "content": f"PROBE_ACK {turn} {nonce}"},
    ]
    assert agent._flush_messages_to_session_db(messages, restored)
finally:
    db.close()
"""

        env = os.environ.copy()
        env.update(
            {
                "HERMES_HOME": str(hermes_home),
                "HERMES_SESSION_ID": session_id,
                "PYTHONPATH": os.pathsep.join(
                    part
                    for part in (str(repo_root), env.get("PYTHONPATH", ""))
                    if part
                ),
                "OPENROUTER_API_KEY": "test-key",
            }
        )
        for turn in range(1, 11):
            turn_env = dict(env, HERMES_TURN=str(turn))
            result = subprocess.run(
                [sys.executable, "-c", child_script],
                cwd=repo_root,
                env=turn_env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr or result.stdout
            assert result.stdout == ""
            assert result.stderr == ""

        db = SessionDB(db_path=db_path)
        try:
            rows = db.get_messages_as_conversation(
                session_id,
                include_row_ids=True,
            )
        finally:
            db.close()

        assert len(rows) == 20
        assert [row["role"] for row in rows] == [
            role for _turn in range(1, 11) for role in ("user", "assistant")
        ]
        assert [
            row["content"] for row in rows if row["role"] == "user"
        ] == [
            (
                f"Synthetic replay probe only. This is turn {turn} of 10, "
                f"nonce SYNTH-{turn}-8f3c. Do not reuse prior results."
            )
            for turn in range(1, 11)
        ]
        assert [
            row["content"] for row in rows if row["role"] == "assistant"
        ] == [
            f"PROBE_ACK {turn} SYNTH-{turn}-8f3c" for turn in range(1, 11)
        ]
        row_ids = [row["_row_id"] for row in rows]
        assert len(set(row_ids)) == 20

    def test_rewrite_clears_row_id_and_remains_durable(self):
        """An explicit rewrite path can append the replacement row."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            try:
                agent = self._make_agent(db)
                db.append_messages_batch(
                    session_id=agent.session_id,
                    messages=[{"role": "assistant", "content": "empty"}],
                )
                loaded = db.get_messages_as_conversation(
                    agent.session_id, include_row_ids=True
                )
                rewritten = dict(loaded[0])
                rewritten["content"] = "filled final answer"
                rewritten.pop("_row_id", None)
                rewritten.pop("_db_persisted", None)

                agent._flush_messages_to_session_db([rewritten], [])

                rows = db.get_messages(agent.session_id)
                assert rows[-1]["content"] == "filled final answer"
            finally:
                db.close()


# ---------------------------------------------------------------------------
# Test: append_to_transcript skip_db parameter
# ---------------------------------------------------------------------------

class TestAppendToTranscriptSkipDb:
    """Verify skip_db=True skips the SQLite write."""

    def test_skip_db_prevents_sqlite_write(self, tmp_path):
        """With skip_db=True and a real DB, message does NOT appear in SQLite."""
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from hermes_state import SessionDB

        db_path = tmp_path / "test_skip.db"
        db = SessionDB(db_path=db_path)

        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        session_id = "test-skip-db-real"
        db.create_session(session_id=session_id, source="test")

        msg = {"role": "assistant", "content": "hello world"}
        store.append_to_transcript(session_id, msg, skip_db=True)

        # SQLite should NOT have the message
        rows = db.get_messages(session_id)
        assert len(rows) == 0, f"Expected 0 DB rows with skip_db=True, got {len(rows)}"



# ---------------------------------------------------------------------------
# Test: _last_flushed_db_idx initialization
# ---------------------------------------------------------------------------

class TestFlushIdxInit:
    """Verify _last_flushed_db_idx is properly initialized."""

    def test_init_zero(self):
        """Agent starts with _last_flushed_db_idx = 0."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        assert agent._last_flushed_db_idx == 0
