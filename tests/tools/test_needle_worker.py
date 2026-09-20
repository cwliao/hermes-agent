"""Tests for tools.needle_worker."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from tools import needle_worker


def test_extract_triage_hints_hard_timeout_safety(monkeypatch):
    """Regression test: when the underlying native extraction hangs or runs long (8s),
    extract_triage_hints must return None within the bounded ~5s budget (under ~6-7s)
    without blocking for the full duration of the underlying call."""
    def slow_extract_sync(text: str):
        time.sleep(8.0)
        return {"intent": "should not be reached", "confidence": 0.95}

    monkeypatch.setattr(needle_worker, "_extract_sync", slow_extract_sync)
    t0 = time.monotonic()
    result = needle_worker.extract_triage_hints("Task title and body")
    elapsed = time.monotonic() - t0

    assert result is None
    # 5s timeout plus generous margin, well under the 8s sleep duration
    assert elapsed >= 4.8
    assert elapsed < 7.0


def test_extract_triage_hints_empty_or_whitespace():
    assert needle_worker.extract_triage_hints("") is None
    assert needle_worker.extract_triage_hints("   \n\t  ") is None


def test_extract_triage_hints_high_confidence(monkeypatch):
    expected = {
        "intent": "fix bug",
        "entities": ["module_a"],
        "confidence": 0.85,
    }
    monkeypatch.setattr(needle_worker, "_extract_sync", MagicMock(return_value=expected))
    res = needle_worker.extract_triage_hints("some text")
    assert res == expected


def test_extract_triage_hints_low_confidence(monkeypatch):
    payload = {
        "intent": "low confidence task",
        "entities": [],
        "confidence": 0.4,
    }
    monkeypatch.setattr(needle_worker, "_extract_sync", MagicMock(return_value=payload))
    assert needle_worker.extract_triage_hints("some text") is None


def test_extract_triage_hints_none_confidence(monkeypatch):
    payload = {
        "intent": "unknown confidence task",
        "entities": [],
        "confidence": None,
    }
    monkeypatch.setattr(needle_worker, "_extract_sync", MagicMock(return_value=payload))
    assert needle_worker.extract_triage_hints("some text") is None


def test_extract_triage_hints_exception_handling(monkeypatch):
    def failing_extract_sync(text: str):
        raise RuntimeError("engine crash")

    monkeypatch.setattr(needle_worker, "_extract_sync", failing_extract_sync)
    assert needle_worker.extract_triage_hints("some text") is None
