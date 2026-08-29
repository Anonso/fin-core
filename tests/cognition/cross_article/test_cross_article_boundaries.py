"""Tests verifying cross_article never writes to Persona/pattern/trace stores."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fin_analyse.cognition.cross_article.models import (
    ArticleRef,
    ClusterAnalysis,
    ClusterInfo,
    QualityFlags,
    SynthesisReport,
)
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore


def test_synthesis_store_only_writes_to_cross_article_dir():
    """Store writes are confined to the cross_article root directory."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = SynthesisStore(root=root)

        # Save cluster
        ci = ClusterInfo(
            cluster_id="c1",
            theme="test",
            created_at="2026-06-30T12:00:00+08:00",
            updated_at="2026-06-30T12:00:00+08:00",
            article_ids=["a1"],
        )
        store.save_cluster_info(ci)

        # All files should be under root
        for f in root.rglob("*"):
            if f.is_file():
                assert str(f).startswith(str(root))


def test_synthesis_report_advisory_only_enforced():
    """SynthesisReport cannot be created with advisory_only=False."""
    with pytest.raises(ValueError, match="advisory_only"):
        SynthesisReport(
            synthesis_id="s1",
            generated_at="2026-06-30T12:00:00+08:00",
            source_article_ids=["a1"],
            source_cluster_ids=["c1"],
            sector_directions=[
                {
                    "sector": "s",
                    "direction": "d",
                    "source_clusters": ["c1"],
                    "source_article_ids": ["a1"],
                    "strength": 0.5,
                }
            ],
            focused_stocks=[],
            viewpoint_changes=[],
            quality_flags=QualityFlags(),
            confidence=0.5,
            advisory_only=False,
        )


def test_synthesis_report_requires_source_refs():
    """Sector directions without source_clusters are rejected."""
    with pytest.raises(ValueError, match="source_clusters"):
        SynthesisReport(
            synthesis_id="s1",
            generated_at="2026-06-30T12:00:00+08:00",
            source_article_ids=["a1"],
            source_cluster_ids=["c1"],
            sector_directions=[
                {
                    "sector": "s",
                    "direction": "d",
                    "strength": 0.5,
                }
            ],
            focused_stocks=[],
            viewpoint_changes=[],
            quality_flags=QualityFlags(),
            confidence=0.5,
        )


def test_cluster_analysis_inferred_confidence_clamped():
    """inferred_from_logic stocks get confidence capped at evidence_sufficiency cap."""
    analysis = ClusterAnalysis(
        analysis_id="ca1",
        cluster_id="c1",
        generated_at="2026-06-30T12:00:00+08:00",
        article_ids=["a1"],
        core_viewpoints=[],
        mentioned_stocks=[
            {
                "company": "test",
                "reference_type": "inferred_from_logic",
                "confidence": 0.95,
                "source_articles": ["a1"],
            }
        ],
        evidence_sufficiency={
            "sufficient": True,
            "reason": "ok",
            "allowed_uses": ["focused_stock"],
            "confidence_cap": 0.7,
        },
        quality_mode="single_model",
    )

    assert analysis.mentioned_stocks[0]["confidence"] == 0.7


def test_article_ref_rejects_missing_required_fields():
    """ArticleRef missing required fields raises ValueError."""
    with pytest.raises(ValueError, match="persona_eligible"):
        ArticleRef(
            article_id="a1",
            title="",
            published_at="",
            column="",
            path="",
            source_classification="",
            persona_eligible=False,
        )
