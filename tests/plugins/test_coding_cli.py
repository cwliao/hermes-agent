"""Tests for the coding-cli plugin.

Covers the bundled plugin at ``plugins/coding-cli/``:

  * ``cli_bridge`` library: argv construction / output parsing for both
    codex and claude, timeout/non-zero-exit handling, allowed-dir
    resolution, per-chat state persistence.
  * Plugin ``__init__``: ``/codex``/``/claude`` command handlers -- help,
    reset, dir validation, fail-closed when disabled/unconfigured, prompt
    flow persisting the new resume id.
  * Bundled-plugin discovery via ``PluginManager.discover_and_load``.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    hermes_home.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _load_lib():
    """Import the plugin's library module directly from the repo path."""
    repo_root = Path(__file__).resolve().parents[2]
    lib_path = repo_root / "plugins" / "coding-cli" / "cli_bridge.py"
    spec = importlib.util.spec_from_file_location("cli_bridge_under_test", lib_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the plugin's __init__.py (which depends on the library)."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "coding-cli"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.coding_cli",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.coding_cli"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.coding_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# cli_bridge: state persistence
# ---------------------------------------------------------------------------

class TestChatState:
    def test_get_chat_state_defaults_empty(self, _isolate_env):
        cb = _load_lib()
        assert cb.get_chat_state("telegram:1:codex") == {}

    def test_set_and_get_chat_state_round_trips(self, _isolate_env):
        cb = _load_lib()
        cb.set_chat_state("telegram:1:codex", resume_id="abc", cwd="/tmp")
        assert cb.get_chat_state("telegram:1:codex") == {
            "resume_id": "abc",
            "cwd": "/tmp",
        }

    def test_set_chat_state_merges_not_replaces(self, _isolate_env):
        cb = _load_lib()
        cb.set_chat_state("telegram:1:codex", cwd="/tmp")
        cb.set_chat_state("telegram:1:codex", resume_id="abc")
        assert cb.get_chat_state("telegram:1:codex") == {
            "resume_id": "abc",
            "cwd": "/tmp",
        }

    def test_clear_resume_id_keeps_cwd(self, _isolate_env):
        cb = _load_lib()
        cb.set_chat_state("telegram:1:codex", resume_id="abc", cwd="/tmp")
        cb.clear_resume_id("telegram:1:codex")
        assert cb.get_chat_state("telegram:1:codex") == {"cwd": "/tmp"}

    def test_state_isolated_per_key(self, _isolate_env):
        cb = _load_lib()
        cb.set_chat_state("telegram:1:codex", resume_id="a")
        cb.set_chat_state("telegram:1:claude", resume_id="b")
        assert cb.get_chat_state("telegram:1:codex")["resume_id"] == "a"
        assert cb.get_chat_state("telegram:1:claude")["resume_id"] == "b"

    def test_corrupt_state_file_recovers_to_empty(self, _isolate_env):
        cb = _load_lib()
        cb._state_path().parent.mkdir(parents=True, exist_ok=True)
        cb._state_path().write_text("not json{{{")
        assert cb.get_chat_state("telegram:1:codex") == {}


# ---------------------------------------------------------------------------
# cli_bridge: resolve_allowed_dir
# ---------------------------------------------------------------------------

class TestResolveAllowedDir:
    def test_accepts_path_under_root(self, tmp_path):
        cb = _load_lib()
        sub = tmp_path / "proj"
        sub.mkdir()
        assert cb.resolve_allowed_dir(str(sub), [str(tmp_path)]) == sub.resolve()

    def test_accepts_root_itself(self, tmp_path):
        cb = _load_lib()
        assert cb.resolve_allowed_dir(str(tmp_path), [str(tmp_path)]) == tmp_path.resolve()

    def test_rejects_path_outside_all_roots(self, tmp_path):
        cb = _load_lib()
        other = tmp_path / "outside"
        other.mkdir()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        assert cb.resolve_allowed_dir(str(other), [str(allowed)]) is None

    def test_rejects_nonexistent_path(self, tmp_path):
        cb = _load_lib()
        assert cb.resolve_allowed_dir(str(tmp_path / "nope"), [str(tmp_path)]) is None

    def test_rejects_file_not_directory(self, tmp_path):
        cb = _load_lib()
        f = tmp_path / "file.txt"
        f.write_text("x")
        assert cb.resolve_allowed_dir(str(f), [str(tmp_path)]) is None

    def test_empty_allowed_roots_rejects_everything(self, tmp_path):
        cb = _load_lib()
        assert cb.resolve_allowed_dir(str(tmp_path), []) is None


# ---------------------------------------------------------------------------
# cli_bridge: output parsing
# ---------------------------------------------------------------------------

class TestParseCodexJsonl:
    def test_parses_thread_id_and_message(self):
        cb = _load_lib()
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
            json.dumps({"type": "turn.completed"}),
        ])
        text, thread_id = cb._parse_codex_jsonl(stdout)
        assert text == "ok"
        assert thread_id == "t-1"

    def test_concatenates_multiple_agent_messages(self):
        cb = _load_lib()
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "part1"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "part2"}}),
        ])
        text, _ = cb._parse_codex_jsonl(stdout)
        assert text == "part1\n\npart2"

    def test_ignores_malformed_lines(self):
        cb = _load_lib()
        stdout = "\n".join([
            "not json",
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
        ])
        text, thread_id = cb._parse_codex_jsonl(stdout)
        assert text == "ok" and thread_id == "t-1"

    def test_missing_thread_id_raises(self):
        cb = _load_lib()
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})
        with pytest.raises(cb.CliTurnError):
            cb._parse_codex_jsonl(stdout)

    def test_no_agent_message_raises(self):
        cb = _load_lib()
        stdout = json.dumps({"type": "thread.started", "thread_id": "t-1"})
        with pytest.raises(cb.CliTurnError):
            cb._parse_codex_jsonl(stdout)


class TestParseClaudeJson:
    def test_parses_result_and_session_id(self):
        cb = _load_lib()
        stdout = json.dumps({"is_error": False, "result": "ok", "session_id": "s-1"})
        text, session_id = cb._parse_claude_json(stdout)
        assert text == "ok" and session_id == "s-1"

    def test_invalid_json_raises(self):
        cb = _load_lib()
        with pytest.raises(cb.CliTurnError):
            cb._parse_claude_json("not json")

    def test_is_error_true_raises(self):
        cb = _load_lib()
        stdout = json.dumps({"is_error": True, "result": "boom", "session_id": "s-1"})
        with pytest.raises(cb.CliTurnError):
            cb._parse_claude_json(stdout)

    def test_missing_session_id_raises(self):
        cb = _load_lib()
        stdout = json.dumps({"is_error": False, "result": "ok"})
        with pytest.raises(cb.CliTurnError):
            cb._parse_claude_json(stdout)


# ---------------------------------------------------------------------------
# cli_bridge: subprocess execution (argv + timeout/exit-code handling)
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(10)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class TestRunCodexTurn:
    def test_builds_fresh_argv_without_resume(self, monkeypatch, tmp_path):
        cb = _load_lib()
        seen = {}

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = argv
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
            ]).encode()
            return _FakeProc(stdout=stdout)

        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cb.shutil, "which", lambda x: x)
        text, thread_id = _run(cb.run_codex_turn(
            "hi", str(tmp_path), None, sandbox="workspace-write", timeout=5, profile_home=str(tmp_path),
        ))
        assert text == "ok" and thread_id == "t-1"
        assert "resume" not in seen["argv"]
        assert "hi" in seen["argv"]

    def test_builds_resume_argv_when_resume_id_present(self, monkeypatch, tmp_path):
        cb = _load_lib()
        seen = {}

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = argv
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
            ]).encode()
            return _FakeProc(stdout=stdout)

        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cb.shutil, "which", lambda x: x)
        _run(cb.run_codex_turn(
            "hi", str(tmp_path), "prev-thread", sandbox="workspace-write", timeout=5, profile_home=str(tmp_path),
        ))
        assert "resume" in seen["argv"]
        assert "prev-thread" in seen["argv"]

    def test_timeout_raises_cli_turn_error(self, monkeypatch, tmp_path):
        cb = _load_lib()

        async def fake_exec(*argv, **kwargs):
            return _FakeProc(hang=True)

        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cb.shutil, "which", lambda x: x)
        with pytest.raises(cb.CliTurnError, match="timed out"):
            _run(cb.run_codex_turn(
                "hi", str(tmp_path), None, sandbox="workspace-write", timeout=0.05, profile_home=str(tmp_path),
            ))

    def test_nonzero_exit_raises_cli_turn_error(self, monkeypatch, tmp_path):
        cb = _load_lib()

        async def fake_exec(*argv, **kwargs):
            return _FakeProc(stderr=b"boom", returncode=1)

        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cb.shutil, "which", lambda x: x)
        with pytest.raises(cb.CliTurnError, match="exited 1"):
            _run(cb.run_codex_turn(
                "hi", str(tmp_path), None, sandbox="workspace-write", timeout=5, profile_home=str(tmp_path),
            ))


class TestRunClaudeTurn:
    def test_builds_resume_argv_when_present(self, monkeypatch, tmp_path):
        cb = _load_lib()
        seen = {}

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = argv
            stdout = json.dumps({"is_error": False, "result": "ok", "session_id": "s-2"}).encode()
            return _FakeProc(stdout=stdout)

        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cb.shutil, "which", lambda x: x)
        text, session_id = _run(cb.run_claude_turn(
            "hi", str(tmp_path), "prev-session",
            permission_mode="acceptEdits", timeout=5, profile_home=str(tmp_path),
        ))
        assert text == "ok" and session_id == "s-2"
        assert "--resume" in seen["argv"]
        assert "prev-session" in seen["argv"]

    def test_omits_resume_flag_when_absent(self, monkeypatch, tmp_path):
        cb = _load_lib()
        seen = {}

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = argv
            stdout = json.dumps({"is_error": False, "result": "ok", "session_id": "s-2"}).encode()
            return _FakeProc(stdout=stdout)

        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cb.shutil, "which", lambda x: x)
        _run(cb.run_claude_turn(
            "hi", str(tmp_path), None, permission_mode="acceptEdits", timeout=5, profile_home=str(tmp_path),
        ))
        assert "--resume" not in seen["argv"]


# ---------------------------------------------------------------------------
# Plugin __init__: command handlers
# ---------------------------------------------------------------------------

def _mock_session_context(monkeypatch, plugin_init, platform="telegram", chat_id="42"):
    fake = types.SimpleNamespace(
        get_session_env=lambda name, default="": {
            "HERMES_SESSION_PLATFORM": platform,
            "HERMES_SESSION_CHAT_ID": chat_id,
        }.get(name, default)
    )
    monkeypatch.setitem(sys.modules, "gateway.session_context", fake)


def _mock_config(monkeypatch, plugin_init, config_block):
    fake_config_mod = types.SimpleNamespace(load_config=lambda: {"external_cli": config_block})
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config_mod)


class TestCodingCliCommands:
    def test_help_on_empty_args(self, _isolate_env):
        mod = _load_plugin_init()
        result = _run(mod._handle_codex(""))
        assert "/codex" in result and "Usage" in result

    def test_reset_clears_resume_id(self, _isolate_env, monkeypatch):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        _mock_config(monkeypatch, mod, {"profile_home": str(_isolate_env)})
        mod.cli_bridge.set_chat_state("telegram:42:codex", profile_home=_isolate_env, resume_id="abc", cwd="/tmp")
        result = _run(mod._handle_codex("reset"))
        assert "reset" in result.lower()
        assert mod.cli_bridge.get_chat_state("telegram:42:codex", profile_home=_isolate_env) == {"cwd": "/tmp"}

    def test_dir_rejects_when_no_allowed_roots_configured(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        _mock_config(monkeypatch, mod, {"allowed_roots": []})
        result = _run(mod._handle_codex(f"dir {tmp_path}"))
        assert "no external_cli.allowed_roots" in result

    def test_dir_rejects_path_outside_allowed_roots(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _mock_config(monkeypatch, mod, {"allowed_roots": [str(allowed)], "profile_home": str(_isolate_env)})
        result = _run(mod._handle_codex(f"dir {outside}"))
        assert "not an existing directory under an allowed root" in result

    def test_dir_accepts_path_under_allowed_root(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        sub = tmp_path / "proj"
        sub.mkdir()
        _mock_config(monkeypatch, mod, {"allowed_roots": [str(tmp_path)], "profile_home": str(_isolate_env)})
        result = _run(mod._handle_codex(f"dir {sub}"))
        assert "working directory set" in result
        assert mod.cli_bridge.get_chat_state("telegram:42:codex")["cwd"] == str(sub.resolve())

    def test_prompt_fails_closed_when_disabled(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        _mock_config(monkeypatch, mod, {"enabled": False, "allowed_roots": [str(tmp_path)]})
        result = _run(mod._handle_codex("do something"))
        assert "enabled is false" in result

    def test_prompt_fails_closed_without_allowed_roots(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        _mock_config(monkeypatch, mod, {"enabled": True, "allowed_roots": []})
        result = _run(mod._handle_codex("do something"))
        assert "no external_cli.allowed_roots" in result

    def test_prompt_fails_closed_without_cwd_set(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        _mock_config(monkeypatch, mod, {"enabled": True, "allowed_roots": [str(tmp_path)], "profile_home": str(_isolate_env)})
        result = _run(mod._handle_codex("do something"))
        assert "no working directory set" in result

    def test_prompt_runs_and_persists_resume_id(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        sub = tmp_path / "proj"
        sub.mkdir()
        _mock_config(monkeypatch, mod, {
            "enabled": True,
            "allowed_roots": [str(tmp_path)],
            "profile_home": str(_isolate_env),
            "timeout_seconds": 5,
            "codex_sandbox": "workspace-write",
            "codex_bin": "codex",
        })
        mod.cli_bridge.set_chat_state("telegram:42:codex", profile_home=_isolate_env, cwd=str(sub))

        async def fake_run_codex_turn(prompt, cwd, resume_id, **kwargs):
            assert prompt == "do something"
            assert resume_id is None
            return "did it", "new-thread-id"

        monkeypatch.setattr(mod.cli_bridge, "run_codex_turn", fake_run_codex_turn)
        result = _run(mod._handle_codex("do something"))
        assert "did it" in result
        assert mod.cli_bridge.get_chat_state("telegram:42:codex", profile_home=_isolate_env)["resume_id"] == "new-thread-id"

    def test_prompt_reports_cli_turn_error(self, _isolate_env, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_session_context(monkeypatch, mod)
        sub = tmp_path / "proj"
        sub.mkdir()
        _mock_config(monkeypatch, mod, {
            "enabled": True,
            "allowed_roots": [str(tmp_path)],
            "profile_home": str(_isolate_env),
            "timeout_seconds": 5,
        })
        mod.cli_bridge.set_chat_state("telegram:42:codex", profile_home=_isolate_env, cwd=str(sub))

        async def fake_run_codex_turn(*a, **k):
            raise mod.cli_bridge.CliTurnError("boom")

        monkeypatch.setattr(mod.cli_bridge, "run_codex_turn", fake_run_codex_turn)
        result = _run(mod._handle_codex("do something"))
        assert "boom" in result


# ---------------------------------------------------------------------------
# Bundled-plugin discovery
# ---------------------------------------------------------------------------

class TestBundledDiscovery:
    def _write_enabled_config(self, hermes_home, names):
        import yaml
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({"plugins": {"enabled": list(names)}}))

    def test_coding_cli_discovered_but_not_loaded_by_default(self, _isolate_env):
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "coding-cli" in mgr._plugins
        loaded = mgr._plugins["coding-cli"]
        assert not loaded.enabled

    def test_coding_cli_loads_when_enabled(self, _isolate_env):
        self._write_enabled_config(_isolate_env, ["coding-cli"])
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["coding-cli"]
        assert loaded.enabled
        assert "codex" in loaded.commands_registered
        assert "claude" in loaded.commands_registered


def test_build_subprocess_env_uses_isolated_profile_and_strips_secrets(
    _isolate_env, monkeypatch, tmp_path
):
    cb = _load_lib()
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-secret")
    monkeypatch.setenv("HERMES_HOME", "/private/hermes")

    env = cb.build_subprocess_env(profile)

    assert env["HOME"] == str(profile)
    assert env["PATH"] == "/safe/bin"
    assert env["XDG_CONFIG_HOME"] == str(profile / "config")
    assert env["XDG_DATA_HOME"] == str(profile / "data")
    assert env["XDG_CACHE_HOME"] == str(profile / "cache")
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "PROVIDER_API_KEY" not in env
    assert "HERMES_HOME" not in env


def test_resolve_profile_home_requires_private_directory(_isolate_env, tmp_path):
    cb = _load_lib()
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o755)
    assert cb.resolve_profile_home(profile) is None
    profile.chmod(0o700)
    assert cb.resolve_profile_home(profile) == profile.resolve()


def test_prompt_fails_closed_without_profile_home(
    _isolate_env, monkeypatch, tmp_path
):
    mod = _load_plugin_init()
    _mock_session_context(monkeypatch, mod)
    _mock_config(
        monkeypatch,
        mod,
        {"enabled": True, "allowed_roots": [str(tmp_path)]},
    )
    mod.cli_bridge.set_chat_state("telegram:42:codex", cwd=str(tmp_path))

    result = _run(mod._handle_codex("do something"))

    assert "profile_home" in result


def test_prompt_passes_profile_home_to_claude(
    _isolate_env, monkeypatch, tmp_path
):
    mod = _load_plugin_init()
    _mock_session_context(monkeypatch, mod)
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    _mock_config(
        monkeypatch,
        mod,
        {
            "enabled": True,
            "allowed_roots": [str(tmp_path)],
            "profile_home": str(profile),
        },
    )
    mod.cli_bridge.set_chat_state("telegram:42:claude", profile_home=profile, cwd=str(tmp_path))
    seen = {}

    async def fake_run_claude_turn(prompt, cwd, resume_id, **kwargs):
        seen.update(kwargs)
        return "claude did it", "claude-session"

    monkeypatch.setattr(mod.cli_bridge, "run_claude_turn", fake_run_claude_turn)
    result = _run(mod._handle_claude("do something"))

    assert "claude did it" in result
    assert seen["profile_home"] == str(profile)
