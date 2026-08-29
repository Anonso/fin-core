"""Tests for ArticleClusterer — Phase 1 LLM-driven incremental clustering."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fin_analyse.cognition.cross_article.article_clusterer import ArticleClusterer
from fin_analyse.cognition.cross_article.model_policy import CrossArticleModelPolicy
from fin_analyse.cognition.cross_article.models import ArticleRef
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


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield SynthesisStore(root=Path(tmp))


@pytest.fixture
def policy():
    t0 = Backend(
        '{"core_topic":"半导体材料","sectors":["半导体"],'
        '"mentioned_companies":[{"name":"雅克科技","reference_type":"direct_mention","context":"材料突破"}],'
        '"viewpoint_type":"新观点","key_claims":["国产替代加速"],'
        '"half_life_category":"medium",'
        '"cluster_hint":{"relation_to_existing":"新建 cluster","target_cluster_id":null,"reason":"新主题"}}'
    )
    return CrossArticleModelPolicy(t0_backend=t0, t1_backend=None)


def _make_article(article_id: str = "a1") -> ArticleRef:
    return ArticleRef(
        article_id=article_id,
        title="星大派锐评：半导体材料",
        published_at="2026-06-30",
        column="星大派锐评",
        path="knowledge-base/articles/a1.md",
        source_classification="teacher_original",
        persona_eligible=True,
        content_excerpt="材料国产替代进入加速期。",
    )


def test_cluster_new_article(store, policy):
    """First article creates a new cluster."""
    clusterer = ArticleClusterer(store=store, policy=policy)
    article = _make_article("a1")

    result = clusterer.cluster_article(article)
    assert result["action"] == "created"
    assert result["cluster_id"] is not None

    # Verify store has the mapping
    assert store.get_article_cluster("a1") == result["cluster_id"]


def test_cluster_article_idempotent(store, policy):
    """Re-clustering same article returns existing assignment."""
    clusterer = ArticleClusterer(store=store, policy=policy)
    article = _make_article("a1")

    r1 = clusterer.cluster_article(article)
    r2 = clusterer.cluster_article(article)

    assert r2["action"] == "skipped"
    assert r2["cluster_id"] == r1["cluster_id"]


def test_cluster_add_to_existing(store, policy):
    """When LLM says article belongs to existing cluster, add it."""
    # Create an existing cluster first
    existing_id = "cluster_semi"
    store.set_article_cluster("a0", existing_id)
    from fin_analyse.cognition.cross_article.models import ClusterInfo

    store.save_cluster_info(
        ClusterInfo(
            cluster_id=existing_id,
            theme="半导体材料国产替代",
            created_at="2026-06-29T12:00:00+08:00",
            updated_at="2026-06-29T12:00:00+08:00",
            article_ids=["a0"],
        )
    )

    # Override T0 to say it belongs to existing
    t0 = Backend(
        '{"core_topic":"半导体材料","sectors":["半导体"],'
        '"mentioned_companies":[],"viewpoint_type":"强化","key_claims":["持续关注"],'
        '"half_life_category":"medium",'
        '"cluster_hint":{"relation_to_existing":"属于已有 cluster","target_cluster_id":"cluster_semi","reason":"主题一致"}}'
    )
    p = CrossArticleModelPolicy(t0_backend=t0, t1_backend=None)
    clusterer = ArticleClusterer(store=store, policy=p)
    article = _make_article("a2")

    result = clusterer.cluster_article(article)
    assert result["action"] == "added"
    assert result["cluster_id"] == existing_id


def test_cluster_with_degraded_fingerprint(store):
    """When LLM fails, degraded fingerprint creates new cluster."""
    t0 = Backend(raises=RuntimeError("down"))
    policy = CrossArticleModelPolicy(t0_backend=t0, t1_backend=None)
    clusterer = ArticleClusterer(store=store, policy=policy)
    article = _make_article("a1")

    result = clusterer.cluster_article(article)
    assert result["action"] == "created"
    assert result["degraded"] is True


def test_rejects_unsafe_cluster_id_from_llm(store, policy):
    """When LLM returns a path-traversal cluster_id, create new instead."""
    t0 = Backend(
        '{"core_topic":"test","sectors":[],"mentioned_companies":[],'
        '"viewpoint_type":"新观点","key_claims":[],"half_life_category":"short",'
        '"cluster_hint":{"relation_to_existing":"属于已有 cluster",'
        '"target_cluster_id":"../../../etc/passwd","reason":"test"}}'
    )
    p = CrossArticleModelPolicy(t0_backend=t0, t1_backend=None)
    clusterer = ArticleClusterer(store=store, policy=p)
    article = _make_article("a_unsafe")

    result = clusterer.cluster_article(article)
    assert result["action"] == "created"  # rejected unsafe target, new cluster
    assert result["cluster_id"] != "../../../etc/passwd"


def test_accepts_chinese_cluster_id(store, policy):
    """Chinese characters in cluster_id should be accepted."""
    assert store.validate_cluster_id("cluster_达链投资主线梳理_abc123") is True
    assert store.validate_cluster_id("cluster_半导体ai卡脖子材料_a58e9912") is True


def test_rejects_path_traversal_in_cluster_id(store):
    """Path traversal attempts are still rejected."""
    assert store.validate_cluster_id("../../../etc/passwd") is False
    assert store.validate_cluster_id("cluster/../escape") is False
    assert store.validate_cluster_id("..") is False
    assert store.validate_cluster_id("") is False
