"""Tests for degradation event logging and deduplication."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fin_analyse.cognition.cross_article.models import DegradationEvent
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield SynthesisStore(root=Path(tmp))


def test_degradation_event_append_and_read(store):
    """Events are appended and can be listed."""
    e1 = DegradationEvent(
        event_id="ev1",
        created_at="2026-06-30T12:00:00+08:00",
        fallback_reason="aggregator_error",
        cache_key="sha256:abc",
    )
    e2 = DegradationEvent(
        event_id="ev2",
        created_at="2026-06-30T13:00:00+08:00",
        fallback_reason="no_backend",
        cache_key="sha256:def",
    )

    store.append_degradation_event(e1)
    store.append_degradation_event(e2)

    events = store.list_degradation_events()
    assert len(events) == 2
    assert events[0].event_id == "ev1"
    assert events[1].event_id == "ev2"


def test_degradation_event_dedupe_key_auto_generated(store):
    """If no dedupe_key given, one is auto-generated."""
    e = DegradationEvent(
        event_id="ev_auto",
        created_at="2026-06-30T12:00:00+08:00",
        fallback_reason="test_reason",
        cache_key="test_cache",
    )

    assert e.dedupe_key  # auto-generated
    assert "test_reason" in e.dedupe_key


def test_degradation_event_notify_policy_defaults(store):
    """Default notify_policy has expected fields."""
    e = DegradationEvent(
        event_id="ev_defaults",
        created_at="2026-06-30T12:00:00+08:00",
        fallback_reason="test",
        cache_key="key",
    )

    assert e.notify_policy["immediate"] is True
    assert e.notify_policy["daily_summary"] is True
    assert e.notify_policy["delivery_owner"] == "hermes"


def test_has_recent_degradation_detects_duplicate(store):
    """Recent degradation with same reason+key is detectable."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    e = DegradationEvent(
        event_id="ev_dup",
        created_at=now,
        fallback_reason="aggregator_error",
        cache_key="sha256:dup_test",
        dedupe_key=f"{now}:aggregator_error:sha256:dup_test",
    )
    store.append_degradation_event(e)

    assert store.has_recent_degradation(
        fallback_reason="aggregator_error",
        cache_key="sha256:dup_test",
    )


def test_degradation_events_jsonl_format(store):
    """Each event is written as a valid JSONL line."""
    e = DegradationEvent(
        event_id="ev_jsonl",
        created_at="2026-06-30T12:00:00+08:00",
        fallback_reason="test",
        cache_key="k",
    )
    store.append_degradation_event(e)

    import json

    path = store.root / "events" / "degradation_events.jsonl"
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_id"] == "ev_jsonl"
