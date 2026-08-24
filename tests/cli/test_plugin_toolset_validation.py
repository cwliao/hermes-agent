"""Regression coverage for plugin-backed CLI toolset validation."""

from copy import deepcopy


def test_cli_validates_real_plugin_toolset_after_discovery(monkeypatch, tmp_path):
    import cli
    import hermes_cli.plugins as plugins

    home = tmp_path / "hermes"
    home.mkdir()
    home.joinpath("config.yaml").write_text(
        "plugins:\n  enabled:\n    - mermaid_renderer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DEFER_AGENT_STARTUP", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    config = deepcopy(cli.CLI_CONFIG)
    config["plugins"] = {"enabled": ["mermaid_renderer"]}
    monkeypatch.setattr(cli, "CLI_CONFIG", config)
    plugins._reset_plugin_managers_for_tests()
    try:
        cli.HermesCLI(
            model="test-model",
            provider="openai-codex",
            toolsets=["mermaid_renderer"],
            compact=True,
        )
        assert cli.validate_toolset("mermaid_renderer") is True
    finally:
        plugins._reset_plugin_managers_for_tests()


def test_cli_preserves_deferred_discovery_without_kanban_worker(monkeypatch):
    import cli
    import hermes_cli.plugins as plugins

    events = []
    monkeypatch.setenv("HERMES_DEFER_AGENT_STARTUP", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(plugins, "discover_plugins", lambda: events.append("discover"))

    config = deepcopy(cli.CLI_CONFIG)
    monkeypatch.setattr(cli, "CLI_CONFIG", config)
    cli.HermesCLI(
        model="test-model",
        provider="openai-codex",
        toolsets=["terminal"],
        compact=True,
    )

    assert events == []
