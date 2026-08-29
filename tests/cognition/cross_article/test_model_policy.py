"""Tests for CrossArticleModelPolicy (T0 → T1 → text fallback)."""

from __future__ import annotations

from fin_analyse.cognition.cross_article.model_policy import CrossArticleModelPolicy


class Backend:
    """Fake backend for testing model policy fallback chain."""

    def __init__(self, response: str = "{}", raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response


def test_phase1_uses_t0_first():
    """Phase 1 fingerprint should always try T0 first."""
    t0 = Backend('{"core_topic":"半导体","cluster_hint":{"relation_to_existing":"新建 cluster"}}')
    t1 = Backend('{"core_topic":"错误"}')
    policy = CrossArticleModelPolicy(t0_backend=t0, t1_backend=t1)

    result = policy.extract_phase1_fingerprint("文章内容", existing_clusters=[])

    assert result["core_topic"] == "半导体"
    assert t0.calls == 1
    assert t1.calls == 0
    assert result["degraded"] is False


def test_phase1_falls_back_to_t1_then_text():
    """When T0 fails, try T1; when T1 also fails, use text fallback."""
    t0 = Backend(raises=RuntimeError("down"))
    t1 = Backend(raises=RuntimeError("down"))
    policy = CrossArticleModelPolicy(t0_backend=t0, t1_backend=t1)

    result = policy.extract_phase1_fingerprint("雅克科技 半导体材料", existing_clusters=[])

    assert t0.calls == 1
    assert t1.calls == 1
    assert result["degraded"] is True
    assert "core_topic" in result


def test_phase1_falls_back_to_t1_when_t0_fails():
    """When T0 fails but T1 works, use T1 result."""
    t0 = Backend(raises=RuntimeError("down"))
    t1 = Backend(
        '{"core_topic":"半导体材料","cluster_hint":{"relation_to_existing":"新建 cluster"}}'
    )
    policy = CrossArticleModelPolicy(t0_backend=t0, t1_backend=t1)

    result = policy.extract_phase1_fingerprint("文章内容", existing_clusters=[])

    assert result["core_topic"] == "半导体材料"
    assert t0.calls == 1
    assert t1.calls == 1
    assert result["degraded"] is False


def test_phase1_text_fallback_produces_basic_fingerprint():
    """Text fallback should produce a minimal fingerprint with degraded flag."""
    policy = CrossArticleModelPolicy(t0_backend=None, t1_backend=None)

    result = policy.extract_phase1_fingerprint(
        "雅克科技前驱体材料突破，半导体国产替代加速", existing_clusters=[]
    )

    assert result["degraded"] is True
    assert "core_topic" in result
    assert isinstance(result["key_claims"], list)
