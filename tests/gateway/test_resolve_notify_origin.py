"""Unit coverage for ``gateway.session_context.resolve_notify_origin``.

WORKER-SUBPROCESS-SESSION-ENV-001. This is the resolver kanban task-creation
call sites use to stamp ``origin_*`` onto a new task row -- see
``tools/kanban_tools.py::_handle_create``/``_handle_swarm`` and
``hermes_cli/kanban_swarm.py::create_swarm``. It intentionally mirrors
``tools/kanban_tools.py::_maybe_auto_subscribe``'s resolution order without
duplicating that function or touching it.
"""
import pytest

from gateway.session_context import (
    reset_session_vars,
    resolve_notify_origin,
    set_session_vars,
)


@pytest.fixture(autouse=True)
def _isolated_session_context():
    """Every test in this file starts and ends with all ContextVars at the
    true ``_UNSET`` sentinel, not just cleared to ``""`` -- ``_UNSET`` is
    what restores the ``os.environ`` fallback in ``get_session_env``, which
    some tests below deliberately exercise. Without this, test order could
    leak a "set to ''" state from one test's ``set_session_vars`` call into
    the next test's os.environ-fallback assertion."""
    reset_session_vars()
    yield
    reset_session_vars()


class TestResolveNotifyOrigin:
    def test_no_session_context_returns_empty(self, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
        monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
        monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
        assert resolve_notify_origin() == {}

    def test_platform_and_chat_id_resolve_directly(self, monkeypatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        set_session_vars(
            platform="telegram", chat_id="-100123",
            thread_id="7", user_id="9", profile="default",
        )
        origin = resolve_notify_origin()
        assert origin == {
            "origin_platform": "telegram",
            "origin_chat_id": "-100123",
            "origin_thread_id": "7",
            "origin_user_id": "9",
            "origin_session_key": None,
            "origin_profile": "default",
        }

    def test_tui_fallback_via_session_key(self, monkeypatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        set_session_vars(session_key="sesskey1")
        origin = resolve_notify_origin()
        assert origin["origin_platform"] == "tui"
        assert origin["origin_chat_id"] == "sesskey1"
        assert origin["origin_session_key"] == "sesskey1"

    def test_cli_fallback_via_os_environ(self, monkeypatch):
        """No ContextVar bound at all (plain CLI process) still resolves
        through the os.environ fallback in get_session_env."""
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-100777")
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        origin = resolve_notify_origin()
        assert origin["origin_platform"] == "telegram"
        assert origin["origin_chat_id"] == "-100777"
