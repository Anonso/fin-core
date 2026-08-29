"""Tests for ClusterAnalyzer — Phase 2 cluster analysis with evidence sufficiency."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fin_analyse.cognition.cross_article.cluster_analyzer import ClusterAnalyzer
from fin_analyse.cognition.cross_article.model_policy import CrossArticleModelPolicy
from fin_analyse.cognition.cross_article.models import ArticleRef, ClusterInfo
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore


class Backend:
    def __init__(self, response: str = "{}", raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response


ANALYSIS_JSON = """{
  "core_viewpoints": [
    {
      "claim": "材料国产替代加速",
      "claim_type": "direct_expression",
      "confidence": 0.85,
      "source_articles": ["a1"],
      "key_quotes": ["原文引用"],
      "evolution": "新观点"
    }
  ],
  "mentioned_stocks": [
    {
      "company": "雅克科技",
      "reference_type": "direct_mention",
      "confidence": 0.85,
      "source_articles": ["a1"]
    }
  ],
  "viewpoint_evolution": {
    "trend": "新观点",
    "timeline": []
  },
  "evidence_sufficiency": {
    "sufficient": true,
    "reason": "direct evidence",
    "allowed_uses": ["focused_stock"],
    "confidence_cap": 0.85
  },
  "contradictions": [],
  "half_life_assessment": {"category": "medium"},
  "cross_cluster_links": []
}"""


def _make_article(article_id: str = "a1") -> ArticleRef:
    return ArticleRef(
        article_id=article_id,
        title="星大派锐评：半导体材料",
        published_at="2026-06-30",
        column="星大派锐评",
        path=f"knowledge-base/articles/{article_id}.md",
        source_classification="teacher_original",
        persona_eligible=True,
        content_excerpt="材料国产替代进入加速期。雅克科技前驱体材料突破。",
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield SynthesisStore(root=Path(tmp))


@pytest.fixture
def policy():
    t0 = Backend(ANALYSIS_JSON)
    return CrossArticleModelPolicy(t0_backend=t0, t1_backend=None)


@pytest.fixture
def cluster(store):
    ci = ClusterInfo(
        cluster_id="c1",
        theme="半导体材料",
        created_at="2026-06-30T12:00:00+08:00",
        updated_at="2026-06-30T12:00:00+08:00",
        article_ids=["a1", "a2"],
    )
    store.save_cluster_info(ci)
    return ci


def test_single_article_weak_evidence_observation_only(store, policy):
    """Single short article with weak evidence → observation-only."""
    # Use a backend that returns insufficient evidence
    weak_json = """{
      "core_viewpoints": [],
      "mentioned_stocks": [],
      "viewpoint_evolution": {"trend": "insufficient"},
      "evidence_sufficiency": {
        "sufficient": false,
        "reason": "single short article",
        "allowed_uses": ["observation_only"],
        "confidence_cap": 0.45
      },
      "contradictions": [],
      "half_life_assessment": {"category": "short"},
      "cross_cluster_links": []
    }"""
    p = CrossArticleModelPolicy(t0_backend=Backend(weak_json), t1_backend=None)
    analyzer = ClusterAnalyzer(store=store, policy=p)

    analysis = analyzer.analyze_cluster(
        cluster_id="c_weak",
        articles=[_make_article("a_weak")],
    )

    assert analysis.evidence_sufficiency["sufficient"] is False
    assert analysis.quality_mode == "single_model"


def test_multi_article_triggers_quality_analysis(store, policy):
    """≥2 articles get a full analysis."""
    analyzer = ClusterAnalyzer(store=store, policy=policy)

    analysis = analyzer.analyze_cluster(
        cluster_id="c1",
        articles=[_make_article("a1"), _make_article("a2")],
    )

    assert analysis.quality_mode in ("single_model", "moa_validated")
    assert len(analysis.core_viewpoints) > 0
    assert analysis.evidence_sufficiency["sufficient"] is True


def test_analyze_cluster_stores_result(store, policy):
    """Analysis result is persisted via store."""
    analyzer = ClusterAnalyzer(store=store, policy=policy)

    analysis = analyzer.analyze_cluster(
        cluster_id="c1",
        articles=[_make_article("a1"), _make_article("a2")],
    )

    # Should be persisted
    stored = store.load_latest_analysis("c1")
    assert stored is not None
    assert stored.analysis_id == analysis.analysis_id


def test_cluster_with_empty_articles_returns_insufficient(store, policy):
    """Empty article list → insufficient evidence."""
    analyzer = ClusterAnalyzer(store=store, policy=policy)

    analysis = analyzer.analyze_cluster(
        cluster_id="c_empty",
        articles=[],
    )

    assert analysis.evidence_sufficiency["sufficient"] is False
    assert analysis.quality_mode == "single_model"
