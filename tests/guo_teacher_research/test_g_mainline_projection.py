"""Focused tests for the stateless G mainline projection (P1)."""

from __future__ import annotations

from fin_analyse.guo_teacher_research.g_mainline_projection import (
    GMainlineProjection,
    project_mainline,
)

_ARTICLES = (
    {
        "source_ref": "zsxq-a1",
        "title": "星大派特刊：算力主线",
        "published_at": "2026-08-10T11:35:00+08:00",
        "theme_clusters": ["AI 算力", "半导体"],
        "thesis_heads": ("InP 硬约束", "双路线抢产能"),
    },
    {
        "source_ref": "zsxq-a2",
        "title": "星大派特刊：科技分化",
        "published_at": "2026-07-30T06:17:00+00:00",
        "theme_clusters": ["科技主线"],
        "thesis_heads": ("科技仍是主线", "失速风险清单"),
    },
    {
        "source_ref": "zsxq-a3",
        "title": "星大派锐评：量化沟通",
        "published_at": "2026-08-11T03:39:00+00:00",
        "theme_clusters": ["AI 算力"],
        "thesis_heads": ("波动总量不变",),
    },
)


def _project(*args, **kwargs):
    return project_mainline(*args, **kwargs)


def test_projects_articles_grouped_by_theme() -> None:
    result = _project(_ARTICLES, generation="gen-1", manifest_sha256="a" * 64)
    assert result.data_gaps == ()
    themes = {t.theme: t for t in result.themes}
    assert set(themes) == {"AI 算力", "半导体", "科技主线"}
    # 每个主题下的 thesis 带时间与来源
    ai = themes["AI 算力"]
    assert len(ai.theses) == 2
    for thesis in ai.theses:
        assert thesis.source_ref in {"zsxq-a1", "zsxq-a3"}
        assert thesis.published_at
        assert thesis.generation == "gen-1"


def test_deterministic_ordering() -> None:
    first = _project(_ARTICLES, generation="gen-1", manifest_sha256="a" * 64)
    second = _project(tuple(reversed(_ARTICLES)), generation="gen-1", manifest_sha256="a" * 64)
    assert first.themes == second.themes
    assert first == second


def test_budget_truncation_is_typed_gap() -> None:
    result = _project(_ARTICLES, generation="gen-1", manifest_sha256="a" * 64, budget=2)
    assert "g_mainline_budget_truncated" in result.data_gaps
    total = sum(len(t.theses) for t in result.themes)
    assert total <= 2


def test_empty_articles_give_no_samples_gap() -> None:
    result = _project((), generation="gen-1", manifest_sha256="a" * 64)
    assert result.data_gaps == ("g_mainline_no_samples",)
    assert result.themes == ()


def test_invalid_entries_skipped_with_gap() -> None:
    result = _project(
        (
            {"source_ref": "zsxq-bad", "title": "无主题文章"},  # 缺 theme_clusters
            _ARTICLES[0],
        ),
        generation="gen-1",
        manifest_sha256="a" * 64,
    )
    assert "g_mainline_entry_invalid" in result.data_gaps
    assert any(t.theme == "AI 算力" for t in result.themes)


def test_generation_and_hash_bound() -> None:
    result = _project(_ARTICLES, generation="gen-1", manifest_sha256="a" * 64)
    assert result.generation == "gen-1"
    assert result.manifest_sha256 == "a" * 64
    # 无状态:重复调用不依赖任何外部状态
    again = _project(_ARTICLES, generation="gen-1", manifest_sha256="a" * 64)
    assert result == again


def test_class_is_stateless_and_pure() -> None:
    """投影无状态:同输入必然同输出,且不写任何东西。"""
    projection = GMainlineProjection()
    r1 = projection.project(_ARTICLES, generation="g", manifest_sha256="b" * 64)
    r2 = projection.project(_ARTICLES, generation="g", manifest_sha256="b" * 64)
    assert r1 == r2
