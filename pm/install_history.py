"""Stdlib-only rendering of the per-plugin install history sidecar."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")


def _scrub_source(source: str) -> str:
    """Remove credentials and query data while retaining canonical fragments."""
    if "://" not in source:
        at = source.rfind("@")
        slash = source.find("/")
        return source[at + 1:] if at >= 0 and (slash < 0 or at < slash) else source
    scheme, rest = source.split("://", 1)
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    fragment = ""
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
        fragment = fragment.split("?", 1)[0]
        fragment = f"#{fragment}"
    query = rest.find("?")
    if query >= 0:
        rest = rest[:query]
    return f"{scheme}://{rest}{fragment}"


def _cell(value: object) -> str:
    """Make a value safe for one Markdown table cell."""
    text = str(value)
    text = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in text)
    return (text.replace("\\", r"\\")
            .replace("|", r"\|")
            .translate(str.maketrans({"`": r"\`", "[": r"\[", "]": r"\]", "<": r"\<", ">": r"\>"})))


def _short_revision(record: dict) -> str:
    return str(record.get("revision") or "")[:7]


def _events(previous: dict, current: dict, event_override: str | None) -> list[tuple[str, str, str, str, str]]:
    events = []
    for name in sorted(set(previous) | set(current)):
        old = previous.get(name) if isinstance(previous.get(name), dict) else {}
        new = current.get(name) if isinstance(current.get(name), dict) else {}
        if name not in previous:
            event, revision, record = event_override or "install", _short_revision(new), new
        elif name not in current:
            event, revision, record = "remove", _short_revision(old), old
        elif old.get("revision") != new.get("revision"):
            old_revision, new_revision = _short_revision(old), _short_revision(new)
            if not old_revision and not new_revision:
                continue
            event, revision, record = "update", f"{old_revision}->{new_revision}", new
        else:
            continue
        catalog = record.get("catalog") if isinstance(record.get("catalog"), dict) else {}
        tier = catalog.get("tier") or record.get("catalog_tier") or "community"
        source = _scrub_source(str(record.get("source") or ""))
        events.append((event, str(name), str(tier), revision, source))
    return events


def append_install_history(
    metadata_path: Path,
    previous: dict,
    current: dict,
    *,
    event_override: str | None = None,
    rows: list[str] | None = None,
) -> None:
    """Append metadata changes beside the supplied metadata sidecar."""
    rendered = rows if rows is not None else render_history_rows(previous, current, event_override=event_override)
    if not rendered:
        return
    history = metadata_path.parent / "INSTALL-HISTORY.md"
    lock_path = history.with_name(f"{history.name}.lock")
    history.parent.mkdir(parents=True, exist_ok=True)
    from pm.filesystem import lock_fd
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock_fd(fd, wait=True)
        existing = history.read_text(encoding="utf-8") if history.exists() else ""
        existing_lines = set(existing.splitlines())
        # Dedupe is by exact rendered line, including its timestamp.
        lines = [row for row in rendered if row not in existing_lines]
        header = "| time | event | plugin | tier | revision | source |\n|---|---|---|---|---|---|\n"
        prefix = header if not existing else ""
        if not lines and not prefix:
            return
        append_fd = os.open(history, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            payload = (prefix + "".join(f"{row}\n" for row in lines)).encode("utf-8")
            written = os.write(append_fd, payload)
            if written != len(payload):
                raise OSError("short write while appending plugin install history")
        finally:
            os.close(append_fd)
    finally:
        os.close(fd)


def render_history_rows(
    previous: dict,
    current: dict,
    *,
    event_override: str | None = None,
    timestamp: str | None = None,
) -> list[str]:
    """Render complete, stable history lines for a metadata transition."""
    stamp = timestamp or _timestamp()
    rows = []
    for event, name, tier, revision, source in _events(previous, current, event_override):
        cells = (_cell(stamp), _cell(event), _cell(name), _cell(tier), _cell(revision).replace(r"\>", ">"), _cell(source))
        rows.append("| " + " | ".join(cells) + " |")
    return rows
