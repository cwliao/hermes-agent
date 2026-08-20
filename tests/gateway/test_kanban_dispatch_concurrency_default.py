"""The dispatcher must not default to unlimited worker concurrency.

WORKER-TIMEOUT-CONTENTION-001. `kanban.max_in_progress` was read with a
default of None, and None means no cap. On 2026-08-20 a four-lane swarm
dispatched every worker at once against a host serving one local model;
summed per-call latency was 2538s inside a 360s window -- seven inference
calls in flight -- per-call latency rose from 18-39s to 32-129s, and every
worker exhausted its 300s cap after two calls.

These tests pin the resolution rules, not a throughput claim. The unset
default is memory-derived (OOF-30/OOF-77, `derive_default_max_in_progress`)
rather than a fixed constant; that it is *bounded* is the point.
"""

import pytest

from hermes_cli.kanban_db import (
    derive_default_max_in_progress,
    resolve_max_in_progress as _resolve,
)


@pytest.fixture(autouse=True)
def _no_memory_sample(monkeypatch):
    """Pin the derived default so these tests don't depend on host memory."""
    from hermes_cli import kanban_db

    monkeypatch.setattr(kanban_db, "_system_memory_sample", lambda: {})


def test_unset_is_bounded_not_unlimited():
    """The defect itself: absent config used to mean no cap."""
    assert _resolve(None) == derive_default_max_in_progress()


def test_zero_is_an_explicit_opt_out():
    """Unlimited must still be reachable, but only by asking for it. This is
    what stops the fix from being a lock-in."""
    assert _resolve(0) is None


def test_negative_falls_back_to_the_default_not_to_unlimited():
    """A typo like -1 previously disabled the cap. Failing open on a
    nonsensical value is how the original defect would come back."""
    assert _resolve(-1) == derive_default_max_in_progress()


def test_an_explicit_value_wins():
    assert _resolve(1) == 1
    assert _resolve(8) == 8


def test_a_non_numeric_value_does_not_crash_the_dispatcher():
    """Logging a warning and continuing is preferable to a dispatcher that
    will not start because of one bad config key."""
    assert _resolve("banana") is None


def test_both_dispatch_paths_use_the_shared_resolver():
    """The gateway-embedded dispatcher and `hermes kanban dispatch` must
    resolve this key identically.

    They diverged once already: the CLI kept its own coercion in which unset
    meant unlimited, on the path cron invokes, while its docstring claimed
    parity with the gateway. A reviewer found it. This asserts the call rather
    than the claim.
    """
    import inspect

    from gateway import kanban_watchers
    from hermes_cli import kanban as kanban_cli

    for mod in (kanban_watchers, kanban_cli):
        src = inspect.getsource(mod)
        assert "resolve_max_in_progress(" in src, (
            f"{mod.__name__} does not call the shared resolver"
        )
