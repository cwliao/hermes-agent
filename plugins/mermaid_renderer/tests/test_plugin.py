import re
import threading
from pathlib import Path

import pytest

from plugins.mermaid_renderer import renderer
from tools.registry import ToolRegistry


class FakeContext:
    def __init__(self):
        self.calls = []
        self.cli_calls = []

    def register_tool(self, **kwargs):
        self.calls.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli_calls.append(kwargs)


def test_register_exposes_only_bounded_render_tool():
    from plugins.mermaid_renderer import register

    context = FakeContext()
    register(context)

    assert len(context.calls) == 1
    tool = context.calls[0]
    assert tool["name"] == "render_mermaid"
    assert tool["toolset"] == "mermaid_renderer"
    assert tool["schema"]["parameters"]["additionalProperties"] is False
    assert set(tool["schema"]["parameters"]["properties"]) == {"source", "width", "height"}
    assert "maximum" in tool["schema"]["parameters"]["properties"]["width"]["description"].lower()
    assert "task_id" not in tool["schema"]["parameters"]["properties"]
    assert "session_id" not in tool["schema"]["parameters"]["properties"]
    assert "user_task" not in tool["schema"]["parameters"]["properties"]


def test_register_exposes_operator_only_artifact_cli():
    from plugins.mermaid_renderer import register

    context = FakeContext()
    register(context)

    assert len(context.cli_calls) == 1
    command = context.cli_calls[0]
    assert command["name"] == "mermaid-renderer"
    assert command["handler_fn"] is not None


def test_registry_dispatch_accepts_injected_runtime_context(monkeypatch, tmp_path):
    from plugins.mermaid_renderer.tools import RENDER_MERMAID_SCHEMA, handle_render_mermaid

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    monkeypatch.setattr(
        "plugins.mermaid_renderer.tools.renderer.render_mermaid_to_png",
        lambda source, output_path, **kwargs: renderer.RenderResult(True, output_path, bytes_written=123),
    )

    registry = ToolRegistry()
    registry.register(
        name="render_mermaid",
        toolset="mermaid_renderer",
        schema=RENDER_MERMAID_SCHEMA,
        handler=handle_render_mermaid,
    )

    response = registry.dispatch(
        "render_mermaid",
        {"source": "flowchart TD\n A-->B", "width": 320, "height": 240},
        task_id="smoke-task",
        session_id="smoke-session",
        user_task="untrusted prompt",
    )

    directive, status = response.splitlines()
    assert re.fullmatch(rf"MEDIA:{re.escape(str(final_root))}/[0-9a-f-]+\.png", directive)
    assert status == "status=rendered"
    assert "smoke-task" not in response
    assert "smoke-session" not in response


def test_handler_returns_only_final_media_directive_on_success(monkeypatch, tmp_path):
    from plugins.mermaid_renderer.tools import handle_render_mermaid

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)

    def fake_render(source, output_path, **kwargs):
        assert source == "flowchart TD\n A-->B"
        assert output_path.parent == final_root
        assert output_path.suffix == ".png"
        return renderer.RenderResult(True, output_path, bytes_written=123)

    monkeypatch.setattr("plugins.mermaid_renderer.tools.renderer.render_mermaid_to_png", fake_render)
    response = handle_render_mermaid(
        {"source": "flowchart TD\n A-->B", "width": 320, "height": 240},
        task_id="success-task",
    )

    directive, status = response.splitlines()
    assert re.fullmatch(rf"MEDIA:{re.escape(str(final_root))}/[0-9a-f-]+\.png", directive)
    assert status == "status=rendered"


def test_handler_returns_bounded_failure_without_private_path(monkeypatch, tmp_path):
    from plugins.mermaid_renderer.tools import handle_render_mermaid

    private_path = tmp_path / ".hermes" / "media" / "result.png"
    monkeypatch.setattr(
        "plugins.mermaid_renderer.tools.renderer.render_mermaid_to_png",
        lambda *args, **kwargs: renderer.RenderResult(False, private_path, error_code="invalid_png"),
    )

    response = handle_render_mermaid(
        {"source": "flowchart TD\n A-->B"},
        task_id="failure-task",
    )

    assert response == "status=failed\nerror=invalid_png"
    assert "MEDIA:" not in response
    assert str(private_path) not in response


@pytest.mark.parametrize(
    "args",
    [
        ["not", "an", "object"],
        {},
        {"source": "flowchart TD\n A-->B", "width": "320"},
        {"source": "flowchart TD\n A-->B", "height": True},
    ],
)
def test_handler_rejects_invalid_args_without_runtime_context_leak(args):
    from plugins.mermaid_renderer.tools import handle_render_mermaid

    response = handle_render_mermaid(
        args,
        task_id="private-task-id",
        session_id="private-session-id",
        user_task="private /home/cwliao/.hermes/config.yaml",
    )

    assert response == "status=failed\nerror=invalid_arguments"
    assert "private-task-id" not in response
    assert "private-session-id" not in response
    assert "/home/cwliao/.hermes" not in response
    assert "Traceback" not in response


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _install_task_gate(monkeypatch, *, clock=None, max_states=4096, ttl_seconds=86400):
    from plugins.mermaid_renderer import tools

    gate = tools._TaskRenderGate(
        process_secret=b"x" * 32,
        clock=clock or FakeClock(),
        max_states=max_states,
        ttl_seconds=ttl_seconds,
    )
    monkeypatch.setattr(tools, "_TASK_RENDER_GATE", gate)
    return gate


def test_same_task_renders_once_even_when_second_request_changes_source(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    _install_task_gate(monkeypatch)
    calls = []

    def fake_render(source, output_path, **kwargs):
        calls.append((source, output_path, kwargs))
        return renderer.RenderResult(True, output_path, bytes_written=123)

    monkeypatch.setattr(tools.renderer, "render_mermaid_to_png", fake_render)

    first = tools.handle_render_mermaid(
        {"source": "flowchart TD\n A-->B", "width": 320, "height": 240},
        task_id="private-task-id",
        session_id="private-session-id",
    )
    second = tools.handle_render_mermaid(
        {"source": "flowchart LR\n C-->D", "width": 640, "height": 480},
        task_id="private-task-id",
        session_id="private-session-id",
    )

    assert len(calls) == 1
    assert first.endswith("status=rendered")
    assert second == "status=skipped\nreason=render_already_completed"
    assert "MEDIA:" not in second
    assert "private-task-id" not in first + second
    assert "private-session-id" not in first + second


def test_different_tasks_each_render_once(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    _install_task_gate(monkeypatch)
    calls = []
    monkeypatch.setattr(
        tools.renderer,
        "render_mermaid_to_png",
        lambda source, output_path, **kwargs: (
            calls.append(source) or renderer.RenderResult(True, output_path, bytes_written=123)
        ),
    )

    first = tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"}, task_id="task-one")
    second = tools.handle_render_mermaid({"source": "flowchart TD\n C-->D"}, task_id="task-two")

    assert len(calls) == 2
    assert first.endswith("status=rendered")
    assert second.endswith("status=rendered")


def test_concurrent_same_task_suppresses_second_render(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    _install_task_gate(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_render(source, output_path, **kwargs):
        calls.append(source)
        started.set()
        assert release.wait(timeout=2)
        return renderer.RenderResult(True, output_path, bytes_written=123)

    monkeypatch.setattr(tools.renderer, "render_mermaid_to_png", fake_render)
    first_response = []
    worker = threading.Thread(
        target=lambda: first_response.append(
            tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"}, task_id="same-task")
        )
    )
    worker.start()
    assert started.wait(timeout=2)
    second = tools.handle_render_mermaid({"source": "flowchart TD\n C-->D"}, task_id="same-task")
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(calls) == 1
    assert first_response[0].endswith("status=rendered")
    assert second == "status=skipped\nreason=render_in_progress"
    assert "MEDIA:" not in second


def test_failed_or_raised_render_releases_task_for_retry(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    _install_task_gate(monkeypatch)
    attempts = []

    def fake_render(source, output_path, **kwargs):
        attempts.append(source)
        if len(attempts) == 1:
            raise RuntimeError("private /home/cwliao/.hermes/staging")
        return renderer.RenderResult(True, output_path, bytes_written=123)

    monkeypatch.setattr(tools.renderer, "render_mermaid_to_png", fake_render)

    failed = tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"}, task_id="retry-task")
    retried = tools.handle_render_mermaid({"source": "flowchart TD\n C-->D"}, task_id="retry-task")

    assert failed == "status=failed\nerror=render_failed"
    assert retried.endswith("status=rendered")
    assert len(attempts) == 2
    assert "/home/cwliao/.hermes" not in failed
    assert "Traceback" not in failed


def test_unsuccessful_render_result_releases_task_for_retry(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    _install_task_gate(monkeypatch)
    attempts = []

    def fake_render(source, output_path, **kwargs):
        attempts.append(source)
        if len(attempts) == 1:
            return renderer.RenderResult(False, output_path, error_code="invalid_png")
        return renderer.RenderResult(True, output_path, bytes_written=123)

    monkeypatch.setattr(tools.renderer, "render_mermaid_to_png", fake_render)

    failed = tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"}, task_id="retry-result-task")
    retried = tools.handle_render_mermaid({"source": "flowchart TD\n C-->D"}, task_id="retry-result-task")

    assert failed == "status=failed\nerror=invalid_png"
    assert retried.endswith("status=rendered")
    assert len(attempts) == 2


def test_gate_sweeps_expired_completed_state_before_capacity_check(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    clock = FakeClock()
    _install_task_gate(monkeypatch, clock=clock, max_states=1, ttl_seconds=10)
    calls = []
    monkeypatch.setattr(
        tools.renderer,
        "render_mermaid_to_png",
        lambda source, output_path, **kwargs: (
            calls.append(source) or renderer.RenderResult(True, output_path, bytes_written=123)
        ),
    )

    assert tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"}, task_id="old-task").endswith("status=rendered")
    clock.advance(10)
    fresh = tools.handle_render_mermaid({"source": "flowchart TD\n C-->D"}, task_id="new-task")

    assert fresh.endswith("status=rendered")
    assert len(calls) == 2


def test_gate_fails_closed_when_unexpired_capacity_is_full(monkeypatch, tmp_path):
    from plugins.mermaid_renderer import tools

    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    _install_task_gate(monkeypatch, max_states=1)
    calls = []
    monkeypatch.setattr(
        tools.renderer,
        "render_mermaid_to_png",
        lambda source, output_path, **kwargs: (
            calls.append(source) or renderer.RenderResult(True, output_path, bytes_written=123)
        ),
    )

    assert tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"}, task_id="first-task").endswith("status=rendered")
    full = tools.handle_render_mermaid({"source": "flowchart TD\n C-->D"}, task_id="second-task")

    assert full == "status=failed\nerror=render_capacity_exceeded"
    assert len(calls) == 1
    assert "second-task" not in full


def test_missing_task_context_does_not_invoke_renderer(monkeypatch):
    from plugins.mermaid_renderer import tools

    _install_task_gate(monkeypatch)
    monkeypatch.setattr(
        tools.renderer,
        "render_mermaid_to_png",
        lambda *args, **kwargs: pytest.fail("renderer must not run without task context"),
    )

    response = tools.handle_render_mermaid({"source": "flowchart TD\n A-->B"})

    assert response == "status=failed\nerror=missing_task_context"
