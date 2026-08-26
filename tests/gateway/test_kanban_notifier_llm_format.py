import asyncio
from types import SimpleNamespace

from gateway.config import Platform
import gateway.kanban_watchers as kanban_watchers
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _set_notifier_llm_gate(monkeypatch, enabled):
    calls = []

    def fake_cfg_get(cfg, *keys, default=None):
        calls.append((keys, default))
        return enabled

    monkeypatch.setattr(kanban_watchers, "cfg_get", fake_cfg_get)
    return calls


def _create_completed_subscription(tmp_path, monkeypatch):
    db_path = tmp_path / "notifier-llm-format.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="notify once")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
        )
        assert kb.complete_task(conn, task_id, summary=None)
        return task_id
    finally:
        conn.close()


def _raw_completed_message(task_id):
    return f"✔ [default] Kanban {task_id} done — notify once"


def test_happy_path_sends_llm_formatted_message_and_builds_fact_safe_prompt(
    tmp_path, monkeypatch,
):
    task_id = _create_completed_subscription(tmp_path, monkeypatch)
    _set_notifier_llm_gate(monkeypatch, True)
    captured = {}

    async def fake_async_call_llm(**kwargs):
        captured.update(kwargs)
        return f"✔ 任務已完成：{task_id}"

    monkeypatch.setattr(kanban_watchers, "async_call_llm", fake_async_call_llm)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent[0]["text"] == f"✔ 任務已完成：{task_id}"
    assert captured["task"] == "kanban_notifier_formatter"
    assert captured["timeout"] == 8.0
    prompt = captured["messages"][0]["content"]
    assert f"Event kind: completed" in prompt
    assert f"t_xxxxxxxx" in prompt
    assert "do not paraphrase, drop, or invent facts" in prompt
    assert "lane names, skill names, counts, error text, and URLs" in prompt
    assert task_id in prompt


def test_llm_exception_falls_back_to_raw_message(tmp_path, monkeypatch):
    task_id = _create_completed_subscription(tmp_path, monkeypatch)
    _set_notifier_llm_gate(monkeypatch, True)

    async def failing_async_call_llm(**kwargs):
        raise RuntimeError("auxiliary unavailable")

    monkeypatch.setattr(kanban_watchers, "async_call_llm", failing_async_call_llm)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent[0]["text"] == _raw_completed_message(task_id)


def test_llm_call_timeout_falls_back_to_raw_message(tmp_path, monkeypatch):
    raw_msg = "✖ Kanban t_timeout1234 crashed (pid gone); dispatcher will retry"
    monkeypatch.setattr(kanban_watchers, "_KANBAN_NOTIFIER_LLM_TIMEOUT_SECONDS", 0.01)

    async def blocked_async_call_llm(**kwargs):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(kanban_watchers, "async_call_llm", blocked_async_call_llm)
    adapter = RecordingAdapter()

    async def send_formatted_message():
        formatted = await kanban_watchers._format_notifier_message_zh(
            raw_msg, kind="crashed",
        )
        await adapter.send("chat-1", formatted)

    asyncio.run(send_formatted_message())

    assert adapter.sent[0]["text"] == raw_msg


def test_config_gate_off_sends_raw_and_never_calls_llm(tmp_path, monkeypatch):
    task_id = _create_completed_subscription(tmp_path, monkeypatch)
    calls = _set_notifier_llm_gate(monkeypatch, False)
    async def async_call_llm(**kwargs):
        raise AssertionError("must not call")

    monkeypatch.setattr(kanban_watchers, "async_call_llm", async_call_llm)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent[0]["text"] == _raw_completed_message(task_id)
    assert calls == [(('kanban', 'notifier_llm_format'), True)]


def test_formatter_accepts_openai_response_shape(monkeypatch):
    _set_notifier_llm_gate(monkeypatch, True)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  ✅ 已完成  "))],
    )
    async def fake_async_call_llm(**kwargs):
        return response

    monkeypatch.setattr(kanban_watchers, "async_call_llm", fake_async_call_llm)

    formatted = asyncio.run(
        kanban_watchers._format_notifier_message_zh("✅ task", kind="completed")
    )

    assert formatted == "✅ 已完成"


def test_missing_task_id_in_formatted_message_falls_back_and_warns(
    tmp_path, monkeypatch, caplog,
):
    task_id = _create_completed_subscription(tmp_path, monkeypatch)
    _set_notifier_llm_gate(monkeypatch, True)

    async def fake_async_call_llm(**kwargs):
        return "✔ 任務已完成，但識別碼遺失"

    monkeypatch.setattr(kanban_watchers, "async_call_llm", fake_async_call_llm)
    adapter = RecordingAdapter()

    with caplog.at_level("WARNING", logger="gateway.run"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent[0]["text"] == _raw_completed_message(task_id)
    assert any(
        task_id in record.message and "dropped task id" in record.message
        for record in caplog.records
    )
