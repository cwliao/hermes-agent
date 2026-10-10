"""Plugin install metadata changes are recorded in the profile-local history."""

from __future__ import annotations

import json
from multiprocessing import get_context
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _record(source: str = "https://example.test/owner/plugin.git", revision: str = "a" * 40,
            tier: str = "community") -> dict:
    return {"source": source, "revision": revision, "catalog": {"tier": tier}}


def _history(home: Path) -> list[str]:
    path = home / "plugins" / "INSTALL-HISTORY.md"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() and path.is_file() else []


def _append_history_process(args):
    from pm.install_history import append_install_history
    path, row = args
    append_install_history(Path(path), {}, {}, rows=[row])


def test_install_appends_row_with_plugin_tier_and_short_sha(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({"demo": _record()})

    row = _history(home)[2]
    assert "| install | demo | community | aaaaaaa |" in row


def test_revision_change_appends_update_with_old_and_new_short_sha(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({"demo": _record()})
    pc._write_install_metadata({"demo": _record(revision="b" * 40)})

    rows = _history(home)
    assert "| update | demo | community | aaaaaaa->bbbbbbb |" in rows[-1]


def test_removal_appends_remove_row(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({"demo": _record()})
    pc._write_install_metadata({})

    assert "| remove | demo | community | aaaaaaa |" in _history(home)[-1]


def test_update_install_record_removes_only_selected_record(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({"a": _record(), "b": _record(revision="b" * 40)})
    pc._update_install_record("a", lambda _current: None)

    metadata = json.loads((home / "plugins" / ".install-metadata.json").read_text())
    assert metadata == {"b": _record(revision="b" * 40)}
    rows = _history(home)[2:]
    assert [row for row in rows if "| remove | a | community | aaaaaaa |" in row] == [rows[-1]]
    assert not any("| remove | b |" in row for row in rows)


def test_direct_replace_removes_omitted_record_and_logs_removal(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({"a": _record(), "b": _record(revision="b" * 40)})
    pc._write_install_metadata({"b": _record(revision="b" * 40)})

    metadata = json.loads((home / "plugins" / ".install-metadata.json").read_text())
    assert metadata == {"b": _record(revision="b" * 40)}
    rows = _history(home)[2:]
    assert "| remove | a | community | aaaaaaa |" in rows[-1]
    assert not any("| remove | b |" in row for row in rows)


def test_remove_module_path_removes_only_selected_record(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli import plugins_cmd_remove as remove

    home = _home(monkeypatch, tmp_path)
    plugins = home / "plugins"
    target = plugins / "a"
    target.mkdir(parents=True)
    (plugins / "b").mkdir()
    pc._write_install_metadata({"a": _record(), "b": _record(revision="b" * 40)})
    monkeypatch.setattr(pc, "_discover_all_plugins", lambda: [])
    monkeypatch.setattr(pc, "_plugin_aliases", lambda _name: set())
    monkeypatch.setattr(pc, "_toggle_plugin_toolset", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pc, "_forget_plugin_config", lambda _names: {})

    assert remove._remove_user_plugin(plugins, "a", target)["ok"] is True

    metadata = json.loads((home / "plugins" / ".install-metadata.json").read_text())
    assert metadata == {"b": _record(revision="b" * 40)}
    rows = _history(home)[2:]
    assert sum("| remove | a | community | aaaaaaa |" in row for row in rows) == 1
    assert not any("| remove | b |" in row for row in rows)


def test_identical_metadata_does_not_append_row(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    metadata = {"demo": _record()}
    pc._write_install_metadata(metadata)
    before = _history(home)
    pc._write_install_metadata(metadata)

    assert _history(home) == before


def test_history_failure_does_not_fail_metadata_write(monkeypatch, tmp_path, caplog):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    history = home / "plugins" / "INSTALL-HISTORY.md"
    history.parent.mkdir(parents=True)
    history.mkdir()

    with caplog.at_level("WARNING"):
        pc._write_install_metadata({"demo": _record()})

    assert json.loads((home / "plugins" / ".install-metadata.json").read_text()) == {"demo": _record()}
    assert "Could not record plugin install history" in caplog.text


def test_history_scrubs_credentials_from_source(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({
        "demo": _record("https://user:secret@example.test/owner/plugin.git?token=secret"),
    })

    content = "\n".join(_history(home))
    assert "https://example.test/owner/plugin.git" in content
    assert "secret" not in content


def test_scrubber_handles_userinfo_ipv6_fragments_and_queries():
    from pm.install_history import _scrub_source

    assert _scrub_source("https://u/p:secret@example.test/repo.git") == "https://example.test/repo.git"
    assert _scrub_source("https://user:pa/ss@host/path") == "https://host/path"
    assert _scrub_source("ssh://u:secret@h/o/p.git") == "ssh://h/o/p.git"
    assert _scrub_source("https://[::1]:8080/repo.git") == "https://[::1]:8080/repo.git"
    assert _scrub_source("https://host/repo.git#subdir?token=secret") == "https://host/repo.git#subdir"
    assert _scrub_source("https://host/repo.git?token=secret#subdir") == "https://host/repo.git#subdir"
    assert _scrub_source("https://u:p#x@h/r.git") == "https://h/r.git"
    assert _scrub_source("https://u/p:secret@h/r.git") == "https://h/r.git"
    assert _scrub_source("https://u:p@h/r.git?x#sub") == "https://h/r.git#sub"
    assert _scrub_source("git@github.com:o/r.git") == "github.com:o/r.git"
    assert _scrub_source("tok@host:path.git") == "host:path.git"
    assert _scrub_source("plain/path@name") == "plain/path@name"


def test_cell_escapes_backslashes_and_markup():
    from pm.install_history import _cell

    assert _cell(r"x\|y") == r"x\\\|y"
    assert _cell("x](http://evil)`<") == r"x\](http://evil)\`\<"


def test_multiprocess_history_append_has_one_header_and_intact_rows(tmp_path):
    from pm.install_history import append_install_history

    metadata = tmp_path / ".install-metadata.json"
    rows = [f"| 2026-10-10 00:00:0{i}+08:00 | install | demo-{i} | community | {i} | source-{i} |" for i in range(8)]
    context = get_context("fork")
    with context.Pool(len(rows)) as pool:
        pool.map(_append_history_process, [(str(metadata), row) for row in rows])

    lines = (tmp_path / "INSTALL-HISTORY.md").read_text(encoding="utf-8").splitlines()
    assert lines.count("| time | event | plugin | tier | revision | source |") == 1
    assert lines.count("|---|---|---|---|---|---|") == 1
    assert set(lines[2:]) == set(rows)


def test_concurrent_locked_writers_append_one_row_each(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)

    def install(index: int) -> None:
        name = f"demo-{index}"
        pc._update_install_record(name, lambda _old: _record(revision=f"{index:040x}"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(install, range(4)))

    rows = _history(home)[2:]
    assert len(rows) == 4
    assert {row.split(" | ")[2] for row in rows} == {f"demo-{i}" for i in range(4)}


def test_corrupt_sidecar_overwritten_without_history(monkeypatch, tmp_path, caplog):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    sidecar = home / "plugins" / ".install-metadata.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{corrupt json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        pc._write_install_metadata({"demo": _record()})

    assert json.loads(sidecar.read_text(encoding="utf-8-sig")) == {"demo": _record()}
    assert _history(home) == []
    assert "Could not record plugin install history" in caplog.text


def test_staged_publish_records_only_after_commit_and_tracks_update(monkeypatch, tmp_path):
    from pm.publication import StagedPlugin
    from pm.store import tree_digest
    from hermes_cli.runtime_state import finish_publication

    home = _home(monkeypatch, tmp_path)
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    target = plugins / "demo"

    def publish(revision: str, old: dict, target_digest):
        staged = tmp_path / f"staged-{revision}"
        staged.mkdir()
        (staged / "plugin.yaml").write_text("name: demo\n", encoding="utf-8")
        change = StagedPlugin({
            "staged": str(staged), "target": str(target), "target_digest": target_digest,
            "old_metadata": old, "new_metadata": {"demo": _record(revision=revision)},
        })
        before = _history(home)
        change.publish(project)
        assert _history(home) == before
        finish_publication(project)
        assert len(_history(home)) == max(len(before), 2) + 1

    publish("a" * 40, {}, None)
    first_digest = tree_digest(target)
    publish("b" * 40, {"demo": _record()}, first_digest)
    rows = _history(home)[2:]
    assert len(rows) == 2
    assert "| install | demo | community | aaaaaaa |" in rows[0]
    assert "| update | demo | community | aaaaaaa->bbbbbbb |" in rows[1]


def test_failed_finish_history_removes_journal_without_undoing_publication(monkeypatch, tmp_path, caplog):
    from pm.environments import install_state_dir
    from pm.publication import StagedPlugin
    from hermes_cli.runtime_state import finish_publication

    home = _home(monkeypatch, tmp_path)
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "plugin.yaml").write_text("name: demo\n", encoding="utf-8")
    StagedPlugin({"staged": str(staged), "target": str(plugins / "demo"), "target_digest": None,
                  "old_metadata": {}, "new_metadata": {"demo": _record()}}).publish(project)

    def fail(*_args, **_kwargs):
        raise OSError("history unavailable")

    monkeypatch.setattr("pm.install_history.append_install_history", fail)
    with caplog.at_level("WARNING"):
        finish_publication(project)
    journal = install_state_dir(project) / "publication.json"
    assert "Could not record plugin install history" in caplog.text
    assert not journal.exists()
    assert not list(plugins.glob(".previous-*"))
    assert (plugins / "demo" / "plugin.yaml").read_text(encoding="utf-8") == "name: demo\n"
    assert json.loads((plugins / ".install-metadata.json").read_text(encoding="utf-8"))["demo"] == _record()
    assert _history(home) == []


def test_old_publication_journal_without_history_keys_recovers(monkeypatch, tmp_path):
    from pm.environments import install_state_dir
    from pm.publication import StagedPlugin
    from hermes_cli.runtime_state import recover_publication, runtime_lock

    home = _home(monkeypatch, tmp_path)
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "plugin.yaml").write_text("name: demo\n", encoding="utf-8")
    StagedPlugin({"staged": str(staged), "target": str(plugins / "demo"), "target_digest": None,
                  "old_metadata": {}, "new_metadata": {"demo": _record()}}).publish(project)
    journal = install_state_dir(project) / "publication.json"
    row = json.loads(journal.read_text())
    row["committed"] = True
    row.pop("history_rows", None)
    journal.write_text(json.dumps(row), encoding="utf-8")
    with runtime_lock(project):
        recover_publication(project)
    assert not journal.exists()
    assert _history(home) == []


def test_update_writers_serialized_without_losing_different_records(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    barrier = Barrier(2)

    def write(name: str, revision: str) -> None:
        barrier.wait()
        pc._update_install_record(name, lambda _old: _record(revision=revision))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: write(*item), (("one", "1" * 40), ("two", "2" * 40))))
    metadata = json.loads((home / "plugins" / ".install-metadata.json").read_text())
    assert set(metadata) == {"one", "two"}
    rows = _history(home)[2:]
    assert len(rows) == 2
    assert {row.split(" | ")[2] for row in rows} == {"one", "two"}


def test_history_scrubs_ssh_credentials_and_escapes_cells(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({
        "bad|name\nnext": _record("ssh://git:secret@example.test:22/owner/plugin.git?token=secret#x"),
    })
    pc._write_install_metadata({
        "bad|name\nnext": _record("git+ssh://u:secret@example.test/owner/plugin.git#x", revision="b" * 40),
    })
    rows = _history(home)
    assert len(rows) == 4
    assert all("secret" not in row for row in rows)
    assert rows[2].count(" | ") == 5
    assert r"bad\|name next" in rows[2]
    assert "git+ssh://example.test/owner/plugin.git" in rows[3]


def test_missing_revisions_have_explicit_rendering(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc

    home = _home(monkeypatch, tmp_path)
    pc._write_install_metadata({"empty": _record(revision="")})
    pc._write_install_metadata({"empty": _record(revision="abcdefg1234567")})
    pc._write_install_metadata({"empty": _record(revision="")})
    pc._write_install_metadata({"empty": _record(revision="") , "none": _record(revision="")})
    rows = _history(home)[2:]
    assert "| install | empty | community |  |" in rows[0]
    assert "| update | empty | community | ->abcdefg |" in rows[1]
    assert "| update | empty | community | abcdefg-> |" in rows[2]
    assert "| install | none | community |  |" in rows[3]


def test_cmd_adopt_records_adopt_event(monkeypatch, tmp_path):
    from hermes_cli import plugins_cmd as pc
    import hermes_cli.plugins_cmd_update as update
    from hermes_cli.plugins_provenance import Provenance, ProvenanceClass

    home = _home(monkeypatch, tmp_path)
    target = home / "plugins" / "demo"
    target.mkdir(parents=True)
    monkeypatch.setattr(pc, "_plugins_dir", lambda: home / "plugins")
    monkeypatch.setattr(pc, "_require_installed_plugin", lambda name, _dir, _console: target)
    monkeypatch.setattr(pc, "_resolve_git_url", lambda _url: None)
    monkeypatch.setattr(pc, "_resolve_git_executable", lambda: "git")
    monkeypatch.setattr(pc, "_git_head_revision", lambda _target, _git: "a" * 40)
    monkeypatch.setattr(pc, "_canonical_source", lambda url, _subdir: url)
    monkeypatch.setattr(update, "_pc", lambda: pc)
    monkeypatch.setattr(
        "hermes_cli.plugins_provenance.plugins_provenance",
        lambda _dir: [Provenance("demo", ProvenanceClass.SELF_CLONED, target,
                                  origin_url="https://example.test/demo.git")],
    )
    update.cmd_adopt("demo")
    assert "| adopt | demo | community | aaaaaaa |" in _history(home)[2]
