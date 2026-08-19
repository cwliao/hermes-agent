"""One-shot runs must keep writing to the log file.

GATE8-OBSERVABILITY-001. `hermes_cli/oneshot.py` used to call
`logging.disable(logging.CRITICAL)` under a comment claiming file handlers
kept working "because they're attached to the root logger's handler list, not
affected by level". They did not: `logging.disable` is a module-global
threshold checked in `Logger.isEnabledFor`, before any handler is consulted.

The consequence was that `hermes -z` wrote nothing to agent.log, which is why
the gate 8 isolation diagnostic could not obtain a turn-end reason for its own
run. Nothing tested the comment's claim, which is why it survived.
"""

import logging

import pytest


class _Rec(logging.Handler):
    """Records what actually reaches a handler."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        self.seen.append(record.getMessage())


@pytest.fixture
def isolated_root():
    root = logging.getLogger()
    prior_handlers, prior_level = root.handlers[:], root.level
    root.handlers = []
    root.setLevel(logging.INFO)
    logging.disable(logging.NOTSET)
    try:
        yield root
    finally:
        root.handlers = prior_handlers
        root.setLevel(prior_level)
        logging.disable(logging.NOTSET)


def test_logging_disable_would_silence_an_attached_handler(isolated_root):
    """The premise, pinned. If this ever stops being true the fix below is
    unnecessary and this file should be revisited -- but as long as it holds,
    `logging.disable` cannot be used to silence only the terminal.
    """
    rec = _Rec()
    isolated_root.addHandler(rec)
    logging.disable(logging.CRITICAL)
    logging.getLogger("probe").info("hello")
    logging.disable(logging.NOTSET)
    assert rec.seen == [], "logging.disable let a record through; premise changed"


def test_oneshot_does_not_call_logging_disable():
    """A behavioural test would need to run a real agent turn. This asserts
    the specific mechanism instead, and names why: the module-global disable
    is the thing that cannot be scoped to one output.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "hermes_cli" / "oneshot.py"
    body = src.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "logging.disable(" not in code, (
        "one-shot re-introduced logging.disable, which suppresses file "
        "handlers as well as the terminal"
    )


def test_file_handlers_survive_the_muting_helper(isolated_root, tmp_path):
    """Calls the real helper, not a copy of its predicate. A duplicated
    predicate cannot detect drift from the code it claims to describe."""
    from hermes_cli.oneshot import _silence_stream_handlers

    stream_handler = logging.StreamHandler()
    file_handler = logging.FileHandler(tmp_path / "agent.log", encoding="utf-8")
    rec = _Rec()
    isolated_root.addHandler(stream_handler)
    isolated_root.addHandler(file_handler)
    isolated_root.addHandler(rec)

    _silence_stream_handlers()
    logging.getLogger("probe").info("recorded")
    file_handler.flush()

    assert "recorded" in (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert stream_handler.level > logging.CRITICAL
    assert file_handler.level <= logging.INFO, "file handler was muted"
    # _Rec is not a StreamHandler, so it is untouched -- which also documents
    # that the predicate is type-based, not name-based.
    assert rec.seen == ["recorded"]


def test_a_stream_handler_on_a_named_logger_is_muted(isolated_root):
    """hermes_cli/plugins.py attaches a stderr handler to the `plugins`
    logger at import time under HERMES_PLUGINS_DEBUG=1, not to the root.

    An earlier version of this fix walked only the root and the queue
    listener, so those lines reached the terminal while the code comment
    claimed they were muted. A reviewer found it; this pins it.
    """
    from hermes_cli.oneshot import _silence_stream_handlers

    named = logging.getLogger("test_named_stream_logger")
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    named.addHandler(handler)
    try:
        _silence_stream_handlers()
        assert handler.level > logging.CRITICAL, (
            "a stream handler on a named logger was left unmuted"
        )
    finally:
        named.removeHandler(handler)
