"""Tests for CrossClusterSynthesizer — Phase 3 MoA cross-cluster synthesis."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from fin_analyse.cognition.cross_article.cross_synthesizer import CrossClusterSynthesizer
from fin_analyse.cognition.cross_article.models import (
    ClusterAnalysis,
    QualityFlags,
    SynthesisReport,
)
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore


class FakeBackend:
    def __init__(self, response: str = "{}", raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response


MOCK_SYNTHESIS_JSON = """{
  "sector_directions": [
    {
      "sector": "半导体材料",
      "direction": "关注国产替代",
      "source_clusters": ["c1"],
      "source_article_ids": ["a1"],
      "strength": 0.8,
      "key_evidence": "多次提及材料突破",
      "evolution_trend": "强化"
    }
  ],
  "focused_stocks": [
    {
      "company": "雅克科技",
      "ticker": "002409",
      "reference_type": "direct_mention",
      "derivation_chain": "特刊点名 → 锐评确认",
      "source_clusters": ["c1"],
      "source_article_ids": ["a1", "a2"],
      "confidence": 0.82
    }
  ],
  "risks_and_blind_spots": [
    {"type": "板块遗漏", "description": "消费电子未覆盖", "severity": "medium"}
  ],
  "viewpoint_changes": [
    {
      "topic": "半导体材料",
      "previous_stance": "观察",
      "current_stance": "看好",
      "trend": "强化",
      "evidence": "多次强调",
      "source_clusters": ["c1"],
      "source_article_ids": ["a1", "a2"]
    }
  ],
  "cross_cluster_contradictions": [],
  "consensus": ["半导体材料是当前重点"],
  "disagreements": [],
  "blind_spots": ["需要关注下游需求"],
  "confidence": 0.78
}"""


def _make_analysis(
    analysis_id: str = "ca1",
    cluster_id: str = "c1",
    article_ids: list[str] | None = None,
    sufficient: bool = True,
) -> ClusterAnalysis:
    return ClusterAnalysis(
        analysis_id=analysis_id,
        cluster_id=cluster_id,
        generated_at="2026-06-30T12:00:00+08:00",
        article_ids=article_ids or ["a1"],
        core_viewpoints=[
            {
                "claim": "材料国产替代加速",
                "claim_type": "direct_expression",
                "confidence": 0.85,
                "source_articles": ["a1"],
                "key_quotes": ["原文"],
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
            "sufficient": sufficient,
            "reason": "ok",
            "allowed_uses": ["focused_stock"] if sufficient else ["observation_only"],
            "confidence_cap": 0.85 if sufficient else 0.45,
        },
        quality_mode="single_model",
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield SynthesisStore(root=Path(tmp))


@pytest.fixture
def analyses():
    return [
        _make_analysis("ca1", "c1", ["a1", "a2"]),
        _make_analysis("ca2", "c2", ["a3"]),
    ]


def test_synthesize_generates_report(store, analyses):
    """Basic synthesis with mock MoA backend produces a report."""
    backend = FakeBackend(MOCK_SYNTHESIS_JSON)
    synthesizer = CrossClusterSynthesizer(store=store, aggregator_backend=backend)

    report = synthesizer.synthesize(analyses=analyses)

    assert report.synthesis_id is not None
    assert report.advisory_only is True
    assert len(report.sector_directions) == 1
    assert report.sector_directions[0]["sector"] == "半导体材料"
    assert len(report.focused_stocks) == 1


def test_synthesize_persists_and_caches(store, analyses):
    """Synthesis result is persisted and cacheable."""
    backend = FakeBackend(MOCK_SYNTHESIS_JSON)
    synthesizer = CrossClusterSynthesizer(store=store, aggregator_backend=backend)

    report = synthesizer.synthesize(analyses=analyses)
    stored = store.load_latest_synthesis()
    assert stored is not None
    assert stored.synthesis_id == report.synthesis_id


def test_synthesize_fallback_when_backend_fails(store, analyses):
    """When aggregator fails, return fallback with raw analyses."""
    backend = FakeBackend(raises=RuntimeError("down"))
    synthesizer = CrossClusterSynthesizer(store=store, aggregator_backend=backend)

    report = synthesizer.synthesize(analyses=analyses)

    assert report.quality_flags.fallback is True
    assert report.confidence <= 0.3  # fallback confidence is low


def test_synthesize_returns_stale_when_available(store, analyses):
    """When aggregator fails but a previous synthesis exists, serve it as stale."""
    # First, save a previous synthesis
    prev = SynthesisReport(
        synthesis_id="syn_prev",
        generated_at="2026-06-29T12:00:00+08:00",
        source_article_ids=["a1"],
        source_cluster_ids=["c1"],
        sector_directions=[
            {
                "sector": "半导体",
                "direction": "关注",
                "source_clusters": ["c1"],
                "source_article_ids": ["a1"],
                "strength": 0.7,
            }
        ],
        focused_stocks=[],
        viewpoint_changes=[],
        quality_flags=QualityFlags(),
        confidence=0.7,
    )
    store.save_synthesis(prev)

    # Now try to synthesize with failing backend
    backend = FakeBackend(raises=RuntimeError("down"))
    synthesizer = CrossClusterSynthesizer(store=store, aggregator_backend=backend)

    report = synthesizer.synthesize(analyses=analyses)

    assert report.quality_flags.stale is True
    assert report.quality_flags.fallback is True
    assert report.synthesis_id == "syn_prev"


def test_synthesize_rejects_trade_fields(store, analyses):
    """Output containing trade fields is rejected/cleaned."""
    bad_json = """{
      "sector_directions": [
        {
          "sector": "半导体",
          "direction": "买入",
          "action": "BUY",
          "source_clusters": ["c1"],
          "source_article_ids": ["a1"],
          "strength": 0.8
        }
      ],
      "focused_stocks": [],
      "viewpoint_changes": [],
      "risks_and_blind_spots": [],
      "cross_cluster_contradictions": [],
      "consensus": [],
      "disagreements": [],
      "blind_spots": [],
      "confidence": 0.8
    }"""
    backend = FakeBackend(bad_json)
    synthesizer = CrossClusterSynthesizer(store=store, aggregator_backend=backend)

    report = synthesizer.synthesize(analyses=analyses)

    # Trade fields in the output should be caught by validate_no_trade_fields
    # The report may fallback due to validation error
    assert report.advisory_only is True
    # Either fallback happened or the trade fields were stripped
    assert (
        report.quality_flags.fallback
        or len(report.sector_directions) == 0
        or all("action" not in s for s in report.sector_directions)
    )


# ── MoA engine path tests ───────────────────────────────────────────────────


class FakeMoAEngine:
    """Fake MoA engine that returns a pre-built result."""

    def __init__(self, result: str | dict[str, Any] | None = None, status: str = "ok"):
        raw = result or MOCK_SYNTHESIS_JSON
        if isinstance(raw, str):
            raw = json.loads(raw)
        self._result: dict[str, Any] = raw
        self._status = status
        self.last_request = None

    def deliberate(self, request):
        self.last_request = request
        from fin_analyse.moa.models import MoAResult

        return MoAResult(
            task_id=request.task_id,
            task_type=request.task_type,
            status=self._status,
            final=self._result,
            reference_outputs=[],
            consensus=["共识"],
            disagreements=[],
            blind_spots=[],
            confidence=0.82,
            warnings=[],
            metadata={},
        )


def test_synthesize_with_moa_engine(store, analyses):
    """Full MoA path with 4 capability slots produces a synthesis."""
    moa = FakeMoAEngine(MOCK_SYNTHESIS_JSON)
    t0 = FakeBackend("{}")
    t1 = FakeBackend("{}")

    synthesizer = CrossClusterSynthesizer(
        store=store,
        moa_engine=moa,
        reference_backends={"t0": t0, "t1": t1},
    )

    report = synthesizer.synthesize(analyses=analyses)

    assert report.advisory_only is True
    assert len(report.sector_directions) == 1
    assert report.sector_directions[0]["sector"] == "半导体材料"
    # Verify MoA request was built with 4 capability slots
    assert moa.last_request is not None
    assert len(moa.last_request.reference_roles) == 4
    slot_names = {role.name for role in moa.last_request.reference_roles}
    assert slot_names == {
        "core_reasoning",
        "cross_view_risk",
        "boundary_schema_guard",
        "independent_strong_reasoning",
    }


def test_synthesize_moa_fallback_on_failure(store, analyses):
    """When MoA returns fallback status, use stale or raw fallback."""
    moa = FakeMoAEngine(status="fallback")
    synthesizer = CrossClusterSynthesizer(
        store=store,
        moa_engine=moa,
        reference_backends={"t0": FakeBackend("{}"), "t1": FakeBackend("{}")},
    )

    report = synthesizer.synthesize(analyses=analyses)

    assert report.quality_flags.fallback is True
