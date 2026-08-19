"""Guard: pytest must never write into the operator's real Hermes logs.

Regression cover for a live incident. ``hermes_cli/main.py`` calls
``setup_logging()`` at module level, which binds rotating FILE handlers for
``agent.log`` / ``errors.log`` to the ROOT logger using ``get_hermes_home()``.
Because test modules import ``hermes_cli.main`` during collection — before any
fixture runs — the whole session's file logging used to bind to the operator's
real ``~/.hermes/logs/agent.log``. A ``pytest tests/gateway`` run wrote 61
records (fixture email addresses, ``MagicMock`` tracebacks) into that real log
inside a 2-second window, which then tripped an hourly secret-audit cron with
62 false-positive findings.

``tests/conftest.py`` closes this in ``pytest_configure`` by unconditionally
pointing ``HERMES_HOME`` at a throwaway session tempdir before collection.

WHY THIS FILE IS SHAPED THE WAY IT IS
-------------------------------------
Two details are load-bearing; without them these tests pass even when the fix
is removed, which was verified by deleting the fix and watching an earlier
draft stay green:

1. ``hermes_cli.main`` is imported at MODULE scope, not inside a test. The
   leak happens at collection time. By the time a test function body runs, the
   autouse ``_hermetic_environment`` fixture has already repointed
   ``HERMES_HOME`` at a per-test tmpdir, so an import there proves nothing.
2. ``HERMES_HOME`` is snapshotted at MODULE scope too. Reading ``os.environ``
   inside a test reads the per-test fixture value, which is sandboxed either
   way — again proving nothing. The interesting moment is collection.
"""

import logging
import os
from pathlib import Path

from tests.conftest import _REAL_HERMES_HOME

# Import at module scope, exactly like the test modules that triggered the
# original leak. This runs during collection, before any fixture.
import hermes_cli.main  # noqa: F401  — imported for its logging side effect

#: HERMES_HOME as it stood during collection — i.e. after pytest_configure's
#: sandbox but before any per-test fixture. Reading os.environ inside a test
#: instead would always look sandboxed and could never fail.
_HERMES_HOME_AT_IMPORT = os.environ.get("HERMES_HOME", "")


def _real_log_dir() -> Path:
    return Path(_REAL_HERMES_HOME) / "logs"


def _file_handlers_targeting(directory: Path) -> list[str]:
    """Return every live Hermes file-handler path that lives in *directory*.

    Hermes does not attach ``RotatingFileHandler``s to the root logger
    directly: it attaches a ``_NonFormattingQueueHandler`` and keeps the real
    file handlers on module-level globals in ``hermes_logging``
    (``_queued_file_handlers``, plus ``_queue_listener.handlers``). Walking
    only ``logging.getLogger().handlers`` therefore finds nothing and would
    make these tests vacuously pass — verified by removing the fix and
    watching an earlier draft stay green.
    """
    import hermes_logging

    target_dir = directory.resolve()
    hits: list[str] = []
    seen: set[int] = set()

    def _check(handler) -> None:
        if handler is None or id(handler) in seen:
            return
        seen.add(id(handler))
        filename = getattr(handler, "baseFilename", None)
        if not filename:
            return
        try:
            resolved = Path(filename).resolve()
        except OSError:  # pragma: no cover - defensive
            return
        if resolved.parent == target_dir:
            hits.append(str(resolved))

    for handler in list(logging.getLogger().handlers):
        _check(handler)
    for handler in list(getattr(hermes_logging, "_queued_file_handlers", ()) or ()):
        _check(handler)
    listener = getattr(hermes_logging, "_queue_listener", None)
    for handler in list(getattr(listener, "handlers", ()) or ()):
        _check(handler)

    return hits


def test_hermes_home_was_sandboxed_before_collection():
    """The sandbox must already be active when test modules are imported."""
    assert _HERMES_HOME_AT_IMPORT, (
        "HERMES_HOME was unset during collection — pytest_configure's sandbox "
        "did not run before test modules were imported."
    )
    assert (
        Path(_HERMES_HOME_AT_IMPORT).resolve() != Path(_REAL_HERMES_HOME).resolve()
    ), (
        "At collection time HERMES_HOME still pointed at the operator's real "
        "Hermes home, so module-level setup_logging() in imported test "
        "modules binds file handlers to the real agent.log."
    )


def test_no_root_log_handler_points_at_the_real_log_dir():
    """No file handler may target the operator's real ``logs/`` directory.

    This is the assertion that would have caught the original incident: the
    leak was a handler holding an absolute path, not a wrong env var value at
    assertion time.
    """
    offenders = _file_handlers_targeting(_real_log_dir())
    assert not offenders, (
        "Root logger has file handler(s) bound to the operator's real log "
        f"directory: {offenders}. Test output is being written into the real "
        "agent.log / errors.log."
    )


def test_isolated_log_dir_fixture_stays_inside_tmp_path(isolated_log_dir, tmp_path):
    """The opt-in fixture must bind logging under the per-test tmp_path."""
    assert isolated_log_dir.exists()
    assert tmp_path in isolated_log_dir.parents, (
        f"isolated_log_dir {isolated_log_dir} is not under tmp_path {tmp_path}"
    )

    logging.getLogger("tests.log_isolation").warning("marker-for-isolation-test")

    assert not _file_handlers_targeting(_real_log_dir()), (
        "isolated_log_dir left a handler pointing at the real log directory."
    )
    assert _file_handlers_targeting(isolated_log_dir), (
        "isolated_log_dir did not actually bind a file handler under tmp_path."
    )
