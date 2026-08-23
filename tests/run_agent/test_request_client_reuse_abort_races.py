"""Races between the request-client reuse cache and the abort machinery.

Invariants pinned here:

1. A worker-side interrupt that breaks out of the SSE chunk loop must close
   the half-read stream first (owner thread). Abandoning it leaves its
   connection permanently checked out of the httpx pool while the partial
   response makes the worker's finally report a reuse-reason close, caching
   the client with the leaked connection — one more per interrupt until the
   pool hits ``max_connections`` and every request dies with PoolTimeout.
   If the close fails, the slot must be poisoned so the finally really
   closes the pool.

2. The stranger-thread abort (stale detector / interrupt loop) must read
   the holder and fire the abort atomically, under ``request_client_lock``.
   An abort landing after the lock is released races the worker's finally,
   which can pop + cache the client and let the NEXT call check it out —
   the late abort would then poison the slot and shut down an innocent
   in-flight request's sockets.

3. The same atomicity contract holds at the third holder-abort site:
   ``direct_api_call``'s ``_abort_active_request`` (the cron inline path,
   reached cross-thread via ``AIAgent.interrupt()``).

4. ``run_codex_stream``'s finally must poison the reuse slot when
   ``event_stream.close()`` fails; otherwise a failed close caches the
   client with a connection still checked out of the pool.

5. Relay's managed stream wrapper must preserve the same close failure and
   request-client identity rather than masking the signal used to poison the
   slot.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    return agent


@contextmanager
def _managed_relay_turn(agent, tmp_path, monkeypatch):
    pytest.importorskip("nemo_relay")
    from agent import relay_runtime

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id=agent.session_id,
        platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
        lease,
        turn_id="request-client-reuse-turn",
        task_id="request-client-reuse-task",
    )
    lease.host.retain_managed_execution("test.request_client_reuse")
    try:
        yield
    finally:
        lease.host.release_managed_execution("test.request_client_reuse")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def _chunk(content=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    content=content,
                    tool_calls=None,
                    reasoning_content=None,
                    reasoning=None,
                ),
                finish_reason=finish_reason,
            )
        ],
        model="test/model",
        usage=None,
    )


class _FakeStream:
    """SSE stream stand-in.

    Deliberately has NO ``choices`` attribute so ``_call_chat_completions``
    treats it as a genuine token stream (a MagicMock would auto-create
    ``choices`` and get misread as a completed response object).
    """

    response = None

    def __init__(self, chunk_iter_factory, close_raises=False):
        self._factory = chunk_iter_factory
        self._close_raises = close_raises
        self.close_calls = 0

    def __iter__(self):
        return self._factory()

    def close(self):
        self.close_calls += 1
        if self._close_raises:
            raise RuntimeError("close failed")


def _mock_wire_client(stream):
    client = MagicMock()
    client.chat.completions.create.return_value = stream
    return client


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_interrupt_break_closes_stream():
    """Interrupt noticed between chunks must close the half-read stream.

    Without the close, the connection stays checked out of the httpx pool
    while the partial-response finally caches the client for reuse — the
    leak that eventually exhausted the pool (PoolTimeout on every request).
    """
    agent = _make_agent()

    def chunks():
        yield _chunk(content="partial ")
        # /stop arrives while the provider is still streaming.
        agent._interrupt_requested = True
        yield _chunk(content="never processed")

    stream = _FakeStream(chunks)

    with patch.object(
        agent, "_create_request_openai_client", return_value=_mock_wire_client(stream)
    ), patch.object(agent, "_close_request_openai_client"):
        with pytest.raises(InterruptedError):
            agent._interruptible_streaming_api_call({})

    assert stream.close_calls == 1




def test_relay_managed_close_failure_poisons_request_client(tmp_path, monkeypatch):
    """Relay must preserve close failures needed by the reuse-slot guard."""
    agent = _make_agent()

    def chunks():
        yield _chunk(content="partial ")
        agent._interrupt_requested = True
        yield _chunk(content="never processed")

    stream = _FakeStream(chunks, close_raises=True)
    request_client = _mock_wire_client(stream)
    abort_reasons = []

    with _managed_relay_turn(agent, tmp_path, monkeypatch), patch.object(
        agent, "_create_request_openai_client", return_value=request_client
    ), patch.object(agent, "_close_request_openai_client"), patch.object(
        agent,
        "_abort_request_openai_client",
        side_effect=lambda client, *, reason: abort_reasons.append(
            (client, reason)
        ),
    ):
        with pytest.raises(InterruptedError):
            agent._interruptible_streaming_api_call(
                {
                    "model": "test/model",
                    "messages": [{"role": "user", "content": "hello"}],
                }
            )

    assert stream.close_calls == 1
    assert abort_reasons == [(request_client, "interrupt_stream_close_failed")]


def test_late_timed_out_worker_cannot_leak_into_next_request():
    """A provider response arriving after timeout stays owned by turn A.

    The stale watchdog returns control to the caller while the provider worker
    is still unwinding. Start turn B before releasing A's delayed response and
    verify that B receives only its own response. This is the narrow transport
    invariant required before adding a full two-process session fixture.
    """
    from agent.chat_completion_helpers import interruptible_api_call

    agent = _make_agent()
    agent._compute_non_stream_stale_timeout = lambda _payload: 0.05

    first_started = threading.Event()
    first_release = threading.Event()
    first_finished = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()
    created = []

    class _Completions:
        def __init__(self, label):
            self.label = label

        def create(self, **_kwargs):
            if self.label == "A":
                first_started.set()
                assert first_release.wait(timeout=5.0)
                first_finished.set()
            else:
                second_started.set()
                assert second_release.wait(timeout=5.0)
            return SimpleNamespace(turn=self.label)

    class _Client:
        def __init__(self, label):
            self.chat = SimpleNamespace(completions=_Completions(label))

    def create_client(*_args, **_kwargs):
        label = "A" if not created else "B"
        client = _Client(label)
        created.append(client)
        return client

    with patch.object(
        agent, "_create_request_openai_client", side_effect=create_client
    ), patch.object(agent, "_abort_request_openai_client"), patch.object(
        agent, "_close_request_openai_client"
    ):
        timed_out = {}

        def run_first():
            try:
                interruptible_api_call(
                    agent,
                    {"model": "test/model", "messages": [{"role": "user", "content": "A"}]},
                )
            except Exception as exc:  # expected stale timeout
                timed_out["error"] = exc

        first_thread = threading.Thread(target=run_first, daemon=True)
        first_thread.start()
        assert first_started.wait(timeout=2.0)
        first_thread.join(timeout=3.0)
        assert isinstance(timed_out.get("error"), TimeoutError)
        assert first_thread.is_alive() is False

        second_result = {}

        def run_second():
            second_result["response"] = interruptible_api_call(
                agent,
                {"model": "test/model", "messages": [{"role": "user", "content": "B"}]},
            )

        second_thread = threading.Thread(target=run_second, daemon=True)
        second_thread.start()
        assert second_started.wait(timeout=2.0)

        # A returns late while B is still in flight. Neither worker shares its
        # response slot, client holder, or persistence result with the other.
        first_release.set()
        assert first_finished.wait(timeout=2.0)
        second_release.set()
        second_thread.join(timeout=3.0)

    assert second_thread.is_alive() is False
    assert second_result["response"].turn == "B"


def test_two_process_late_provider_is_fenced_from_next_session_turn(tmp_path):
    """A late provider worker cannot append after process B owns the turn.

    This is the full SessionDB acceptance shape: two independent Python
    processes share a temporary HERMES_HOME. Process A times out and releases
    its durable turn lease while its provider worker remains blocked. Process
    B then completes the next turn. Only after B is done is A's worker
    released; its old holder must be rejected by the SQLite write fence, and
    neither B's response nor the durable transcript may contain A's late row.
    """
    from hermes_state import SessionDB

    repo_root = Path(__file__).resolve().parents[2]
    hermes_home = tmp_path / "hermes-home"
    sync_dir = tmp_path / "sync"
    hermes_home.mkdir()
    sync_dir.mkdir()
    session_id = "two-process-timeout-race"

    db = SessionDB(hermes_home / "state.db")
    db.create_session(session_id, source="test")
    db.close()

    common = os.environ.copy()
    common["HERMES_HOME"] = str(hermes_home)
    common["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), common.get("PYTHONPATH", "")) if part
    )

    def _wait_flag(path: Path, timeout: float = 12.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {path.name}")
            time.sleep(0.02)

    def _touch(name: str) -> None:
        (sync_dir / name).touch()

    worker_script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path
        import threading
        import time

        from hermes_state import SessionDB, SessionTurnLeaseLostError

        home = Path(os.environ["HERMES_HOME"])
        sync = Path(os.environ["RACE_SYNC_DIR"])
        result_path = sync / "a-result.json"
        db = SessionDB(home / "state.db")
        sid = "two-process-timeout-race"
        holder = f"pid={os.getpid()}:turn=A"

        def wait_flag(name):
            path = sync / name
            deadline = time.monotonic() + 12
            while not path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {name}")
                time.sleep(0.02)

        assert db.acquire_session_turn_lease(sid, holder, ttl_seconds=20)
        db.append_messages_batch(
            sid,
            [{"role": "user", "content": "A request"}],
            turn_lease_holder=holder,
            turn_lease_ttl_seconds=20,
        )

        late_result = {}

        def late_provider_worker():
            wait_flag("release-a-late-provider")
            try:
                db.append_messages_batch(
                    sid,
                    [{"role": "assistant", "content": "A late provider result"}],
                    turn_lease_holder=holder,
                    turn_lease_ttl_seconds=20,
                )
            except SessionTurnLeaseLostError:
                late_result["status"] = "rejected"
            else:
                late_result["status"] = "accepted"

        late_worker = threading.Thread(target=late_provider_worker, daemon=True)
        late_worker.start()
        (sync / "a-ready").touch()

        # The caller timed out: it returns without a final assistant row and
        # releases the durable lease while the provider worker is still alive.
        wait_flag("timeout-a")
        db.release_session_turn_lease(sid, holder)
        (sync / "a-released").touch()

        wait_flag("b-done")
        (sync / "release-a-late-provider").touch()
        late_worker.join(timeout=8)
        result_path.write_text(json.dumps(late_result), encoding="utf-8")
        db.close()
        """
    )

    next_turn_script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        import time

        from hermes_state import SessionDB

        home = Path(os.environ["HERMES_HOME"])
        sync = Path(os.environ["RACE_SYNC_DIR"])
        sid = "two-process-timeout-race"
        holder = f"pid={os.getpid()}:turn=B"

        def wait_flag(name):
            path = sync / name
            deadline = time.monotonic() + 12
            while not path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {name}")
                time.sleep(0.02)

        wait_flag("a-released")
        db = SessionDB(home / "state.db")
        assert db.acquire_session_turn_lease(sid, holder, ttl_seconds=20, wait_seconds=8)
        db.append_messages_batch(
            sid,
            [
                {"role": "user", "content": "B request"},
                {"role": "assistant", "content": "B current response"},
            ],
            turn_lease_holder=holder,
            turn_lease_ttl_seconds=20,
        )
        (sync / "b-response.txt").write_text("B current response", encoding="utf-8")
        db.release_session_turn_lease(sid, holder)
        (sync / "b-done").touch()
        db.close()
        """
    )

    env = dict(common)
    env["RACE_SYNC_DIR"] = str(sync_dir)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker_script],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(
            [sys.executable, "-c", next_turn_script],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
    ]
    try:
        _wait_flag(sync_dir / "a-ready")
        _touch("timeout-a")
        _wait_flag(sync_dir / "b-done")
        _wait_flag(sync_dir / "a-result.json")
        outputs = [proc.communicate(timeout=5) for proc in processes]
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

    for output, error in outputs:
        assert error == "", error
        assert output == "", output

    assert json.loads((sync_dir / "a-result.json").read_text()) == {
        "status": "rejected"
    }
    assert (sync_dir / "b-response.txt").read_text() == "B current response"

    db = SessionDB(hermes_home / "state.db")
    try:
        rows = db.get_messages_as_conversation(session_id)
    finally:
        db.close()
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "A request"),
        ("user", "B request"),
        ("assistant", "B current response"),
    ]


def test_stale_abort_is_atomic_with_holder_read(monkeypatch):
    """The stranger-thread abort must complete before the worker's finally
    can pop + cache the client.

    The abort runs under ``request_client_lock``; a worker that finishes
    while the abort is in flight must block in its finally until the abort
    returns. Without that, the worker could cache the client (and the next
    call check it out) between the holder read and the abort — the abort
    then killed an innocent request's sockets.
    """
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.05")
    agent = _make_agent()

    allow_finish = threading.Event()
    worker_close_reasons = []
    abort_reasons = []
    observed = {"worker_finished_during_abort": None}

    def chunks():
        yield _chunk(content="hello")
        # Stall long enough for the stale detector to fire, then finish
        # cleanly the moment the abort (below) unblocks us.
        allow_finish.wait(timeout=5.0)
        yield _chunk(finish_reason="stop")

    stream = _FakeStream(chunks)

    def fake_abort(client, *, reason):
        abort_reasons.append(reason)
        # Let the worker race toward its finally while the abort is still
        # in flight. Under the fix it must block on the holder lock, so the
        # owner-side close cannot land until this abort returns.
        allow_finish.set()
        deadline = time.time() + 0.6
        while time.time() < deadline and not worker_close_reasons:
            time.sleep(0.02)
        observed["worker_finished_during_abort"] = bool(worker_close_reasons)

    with patch.object(
        agent, "_create_request_openai_client", return_value=_mock_wire_client(stream)
    ), patch.object(
        agent,
        "_close_request_openai_client",
        side_effect=lambda client, *, reason: worker_close_reasons.append(reason),
    ), patch.object(
        agent, "_abort_request_openai_client", side_effect=fake_abort
    ), patch.object(
        agent, "_replace_primary_openai_client"
    ):
        response = agent._interruptible_streaming_api_call({})

    assert response is not None
    assert "stale_stream_kill" in abort_reasons
    # The atomicity contract: no owner-side close slipped in mid-abort.
    assert observed["worker_finished_during_abort"] is False
    # ...and the worker's own finally still performed its close afterwards.
    # The stale kill cancels the stream attempt before aborting, so the
    # worker's late clean finish is treated as a superseded stream and the
    # finally reports the error-cleanup reason — really closing the
    # socket-aborted (poisoned) client instead of caching it for reuse.
    assert worker_close_reasons == ["stream_error_cleanup"]


def test_direct_api_call_abort_is_atomic_with_holder_read():
    """Same atomicity contract for the cron inline path's holder abort.

    ``direct_api_call``'s ``_abort_active_request`` (fired cross-thread via
    ``AIAgent.interrupt()`` -> ``_active_request_abort``) must complete
    before the inline finally can pop + cache the client — otherwise the
    delayed abort poisons the slot and kills the NEXT request's sockets.
    """
    from agent.chat_completion_helpers import direct_api_call

    agent = _make_agent()

    allow_finish = threading.Event()
    close_reasons = []
    abort_reasons = []
    observed = {"close_during_abort": None}

    client = MagicMock()

    def blocking_create(**kwargs):
        # Block until the aborting thread lets us race toward the finally.
        allow_finish.wait(timeout=5.0)
        return SimpleNamespace(choices=[])

    client.chat.completions.create.side_effect = blocking_create

    def fake_abort(aborted_client, *, reason):
        abort_reasons.append(reason)
        # Unblock the owner mid-abort. Under the fix it must block in its
        # finally on the holder lock until this abort returns.
        allow_finish.set()
        deadline = time.time() + 0.6
        while time.time() < deadline and not close_reasons:
            time.sleep(0.02)
        observed["close_during_abort"] = bool(close_reasons)

    with patch.object(
        agent, "_create_request_openai_client", return_value=client
    ), patch.object(
        agent,
        "_close_request_openai_client",
        side_effect=lambda c, *, reason: close_reasons.append(reason),
    ), patch.object(
        agent, "_abort_request_openai_client", side_effect=fake_abort
    ):
        result = {}

        def owner():
            result["response"] = direct_api_call(agent, {})

        owner_thread = threading.Thread(target=owner, daemon=True)
        owner_thread.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and getattr(agent, "_active_request_abort", None) is None:
            time.sleep(0.01)
        abort_fn = getattr(agent, "_active_request_abort", None)
        assert abort_fn is not None
        # Stranger-thread abort (this thread) racing the inline finally.
        abort_fn("test_stranger_abort")
        owner_thread.join(timeout=5.0)
        assert not owner_thread.is_alive()

    assert abort_reasons == ["test_stranger_abort"]
    # The atomicity contract: no owner-side close slipped in mid-abort.
    assert observed["close_during_abort"] is False
    # ...and the inline finally still performed its close afterwards.
    assert close_reasons == ["request_complete"]
    assert result["response"] is not None


class _FakeCodexEventStream:
    """Codex Responses SSE stream stand-in (iterable, no ``output`` attr)."""

    def __init__(self, events, close_raises=False):
        self._events = events
        self._close_raises = close_raises
        self.close_calls = 0

    def __iter__(self):
        return iter(self._events)

    def close(self):
        self.close_calls += 1
        if self._close_raises:
            raise RuntimeError("close failed")


def _codex_delta_event(text):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _codex_completed_event():
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(usage=None, id="resp_1", status="completed"),
    )


def test_codex_stream_close_failure_poisons_slot():
    """A failed ``event_stream.close()`` must poison the reuse slot.

    Otherwise the connection stays checked out of the pool while the
    worker's finally reports ``request_complete`` and caches the client
    with the leaked connection (the codex sibling of the chat-streaming
    interrupt-break leak).
    """
    from agent.codex_runtime import run_codex_stream

    agent = _make_agent()
    stream = _FakeCodexEventStream(
        [_codex_delta_event("partial "), _codex_completed_event()],
        close_raises=True,
    )
    client = MagicMock()
    client.responses.create.return_value = stream
    abort_reasons = []

    with patch.object(
        agent,
        "_abort_request_openai_client",
        side_effect=lambda c, *, reason: abort_reasons.append(reason),
    ):
        final = run_codex_stream(agent, {"model": "test/model"}, client=client)

    assert final.output_text == "partial "
    assert stream.close_calls == 1
    assert abort_reasons == ["codex_stream_close_failed"]


def test_codex_stream_close_failure_on_primary_client_does_not_abort():
    """``client=None`` means the shared primary client — never reuse-cached,
    and its sockets must not be force-shut by the close-failure handler."""
    from agent.codex_runtime import run_codex_stream

    agent = _make_agent()
    stream = _FakeCodexEventStream(
        [_codex_delta_event("hello"), _codex_completed_event()],
        close_raises=True,
    )
    primary = MagicMock()
    primary.responses.create.return_value = stream

    with patch.object(
        agent, "_ensure_primary_openai_client", return_value=primary
    ), patch.object(agent, "_abort_request_openai_client") as abort_mock:
        final = run_codex_stream(agent, {"model": "test/model"}, client=None)

    assert final.output_text == "hello"
    assert stream.close_calls == 1
    abort_mock.assert_not_called()
