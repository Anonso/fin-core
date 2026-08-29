"""Tests for SynthesisStore — append-only/versioned file storage."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fin_analyse.cognition.cross_article.models import (
    ClusterAnalysis,
    ClusterInfo,
    DegradationEvent,
    QualityFlags,
    SynthesisReport,
)
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore


@pytest.fixture
def store():
    """Create a store with a temp directory."""
    with tempfile.TemporaryDirectory() as tmp:
        yield SynthesisStore(root=Path(tmp))


def _make_cluster_info(cluster_id: str = "c1") -> ClusterInfo:
    return ClusterInfo(
        cluster_id=cluster_id,
        theme="半导体材料",
        created_at="2026-06-30T12:00:00+08:00",
        updated_at="2026-06-30T12:00:00+08:00",
        article_ids=["a1"],
    )


def _make_cluster_analysis(cluster_id: str = "c1") -> ClusterAnalysis:
    return ClusterAnalysis(
        analysis_id="ca1",
        cluster_id=cluster_id,
        generated_at="2026-06-30T12:00:00+08:00",
        article_ids=["a1"],
        core_viewpoints=[
            {
                "claim": "材料国产替代加速",
                "claim_type": "direct_expression",
                "confidence": 0.85,
                "source_articles": ["a1"],
                "key_quotes": ["原文引用"],
                "evolution": "新观点",
            }
        ],
        mentioned_stocks=[
            {
                "company": "雅克科技",
                "reference_type": "direct_mention",
                "confidence": 0.85,
                "source_articles": ["a1"],
            }
        ],
        evidence_sufficiency={
            "sufficient": True,
            "reason": "direct evidence",
            "allowed_uses": ["focused_stock"],
            "confidence_cap": 0.85,
        },
        quality_mode="single_model",
    )


def _make_synthesis(cluster_ids: list[str] | None = None) -> SynthesisReport:
    cids = cluster_ids or ["c1"]
    return SynthesisReport(
        synthesis_id="syn1",
        generated_at="2026-06-30T12:00:00+08:00",
        source_article_ids=["a1"],
        source_cluster_ids=cids,
        sector_directions=[
            {
                "sector": "半导体材料",
                "direction": "关注国产替代",
                "source_clusters": cids,
                "source_article_ids": ["a1"],
                "strength": 0.8,
            }
        ],
        focused_stocks=[],
        viewpoint_changes=[],
        quality_flags=QualityFlags(),
        confidence=0.78,
    )


# ── Cluster lifecycle ───────────────────────────────────────────────────────


def test_save_and_load_cluster_info(store):
    """Upsert and read a ClusterInfo."""
    ci = _make_cluster_info("c1")
    store.save_cluster_info(ci)

    loaded = store.load_cluster_info("c1")
    assert loaded is not None
    assert loaded.cluster_id == "c1"
    assert loaded.theme == "半导体材料"


def test_list_clusters(store):
    """List all stored clusters."""
    store.save_cluster_info(_make_cluster_info("c1"))
    store.save_cluster_info(_make_cluster_info("c2"))

    clusters = store.list_clusters()
    assert len(clusters) == 2
    ids = {c.cluster_id for c in clusters}
    assert ids == {"c1", "c2"}


# ── Article → cluster idempotency ──────────────────────────────────────────


def test_article_cluster_map_idempotent(store):
    """Re-assigning the same article is a no-op."""
    store.set_article_cluster("a1", "c1")
    store.set_article_cluster("a1", "c2")  # should be ignored

    assert store.get_article_cluster("a1") == "c1"


def test_article_cluster_map_load_all(store):
    """Load the full article→cluster map."""
    store.set_article_cluster("a1", "c1")
    store.set_article_cluster("a2", "c2")

    mapping = store.load_article_cluster_map()
    assert mapping == {"a1": "c1", "a2": "c2"}


# ── ClusterAnalysis versioning ──────────────────────────────────────────────


def test_save_and_latest_analysis(store):
    """Save analysis and retrieve latest via pointer."""
    analysis = _make_cluster_analysis("c1")
    store.save_analysis(analysis)

    latest = store.load_latest_analysis("c1")
    assert latest is not None
    assert latest.analysis_id == "ca1"


def test_multiple_analysis_versions(store):
    """Multiple analyses keep old versions; latest pointer updates."""
    a1 = _make_cluster_analysis("c1")
    object.__setattr__(a1, "analysis_id", "ca_v1")
    store.save_analysis(a1)

    a2 = _make_cluster_analysis("c1")
    object.__setattr__(a2, "analysis_id", "ca_v2")
    store.save_analysis(a2)

    latest = store.load_latest_analysis("c1")
    assert latest.analysis_id == "ca_v2"

    # Old version still loadable
    old = store.load_analysis("c1", "ca_v1")
    assert old is not None
    assert old.analysis_id == "ca_v1"


# ── Synthesis versioning ────────────────────────────────────────────────────


def test_save_and_latest_synthesis(store):
    """Save synthesis and retrieve latest."""
    syn = _make_synthesis()
    store.save_synthesis(syn)

    latest = store.load_latest_synthesis()
    assert latest is not None
    assert latest.synthesis_id == "syn1"


def test_previous_synthesis_id_chain(store):
    """Sequential syntheses form a version chain."""
    s1 = _make_synthesis()
    store.save_synthesis(s1)

    s2 = _make_synthesis()
    object.__setattr__(s2, "synthesis_id", "syn2")
    object.__setattr__(s2, "previous_synthesis_id", "syn1")
    store.save_synthesis(s2)

    latest = store.load_latest_synthesis()
    assert latest.synthesis_id == "syn2"
    assert latest.previous_synthesis_id == "syn1"

    prev = store.load_synthesis("syn1")
    assert prev is not None


# ── State hash cache ────────────────────────────────────────────────────────


def test_state_hash_cache_hit(store):
    """Same state hash returns cached synthesis_id."""
    state = {"articles": ["a1", "a2"], "clusters": ["c1"]}
    sid = store.cache_get(state)
    assert sid is None  # no cache yet

    store.cache_set(state, "syn_cached")
    assert store.cache_get(state) == "syn_cached"


# ── Degradation events ──────────────────────────────────────────────────────


def test_append_degradation_event(store):
    """Degradation event is append-only and dedupe-aware."""
    event = DegradationEvent(
        event_id="ev1",
        created_at="2026-06-30T12:00:00+08:00",
        fallback_reason="aggregator_error",
        cache_key="sha256:abc",
        synthesis_id="syn_prev",
    )
    store.append_degradation_event(event)

    events = store.list_degradation_events()
    assert len(events) == 1
    assert events[0].event_id == "ev1"


def test_degradation_event_dedupe(store):
    """Same dedupe_key within window should be detectable."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    e1 = DegradationEvent(
        event_id="ev1",
        created_at=now,
        fallback_reason="aggregator_error",
        cache_key="sha256:abc",
        dedupe_key=f"{now}:aggregator_error:sha256:abc",
    )
    store.append_degradation_event(e1)

    assert store.has_recent_degradation(
        fallback_reason="aggregator_error",
        cache_key="sha256:abc",
        window="1d",
    )


def test_load_synthesis_rejects_pointer_only_json(tmp_path):
    import json

    store = SynthesisStore(tmp_path)
    syn_dir = tmp_path / "syntheses"
    syn_dir.mkdir(parents=True, exist_ok=True)
    (syn_dir / "syn-pointer.json").write_text(
        json.dumps(
            {
                "synthesis_id": "syn-pointer",
                "generated_at": "2026-06-30T12:00:00+08:00",
                "previous_synthesis_id": "",
            }
        ),
        encoding="utf-8",
    )

    assert store.load_synthesis("syn-pointer") is None


def test_load_synthesis_backfills_queries_for_legacy_report(tmp_path):
    import json

    store = SynthesisStore(tmp_path)
    syn_dir = tmp_path / "syntheses"
    syn_dir.mkdir(parents=True, exist_ok=True)
    (syn_dir / "syn-legacy.json").write_text(
        json.dumps(
            {
                "synthesis_id": "syn-legacy",
                "generated_at": "2026-06-30T12:00:00+08:00",
                "source_article_ids": ["a1"],
                "source_cluster_ids": ["c1"],
                "sector_directions": [
                    {
                        "sector": "半导体",
                        "direction": "看好",
                        "source_clusters": ["c1"],
                        "source_article_ids": ["a1"],
                        "strength": 0.8,
                    }
                ],
                "focused_stocks": [
                    {
                        "company": "雅克科技",
                        "ticker": "002409",
                        "reference_type": "direct_mention",
                        "derivation_chain": "原文直接点名",
                        "source_clusters": ["c1"],
                        "source_article_ids": ["a1"],
                        "confidence": 0.9,
                        "evidence_mode": "sufficient",
                    }
                ],
                "viewpoint_changes": [],
                "quality_flags": {},
                "confidence": 0.8,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = store.load_synthesis("syn-legacy")

    assert report is not None
    payload = report.to_dict()
    assert payload["suggested_signal_queries"][0]["company"] == "雅克科技"
