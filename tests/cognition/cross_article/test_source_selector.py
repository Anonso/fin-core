"""Tests for CrossArticleSourceSelector."""

from __future__ import annotations

from fin_analyse.cognition.cross_article.source_selector import (
    CrossArticleSourceSelector,
)


def test_selects_xingdapai_columns():
    """星大派特刊 and 星大派锐评 should always be selected."""
    selector = CrossArticleSourceSelector()
    articles = [
        {
            "id": "a1",
            "title": "星大派特刊：材料",
            "date": "2026-06-30",
            "column": "星大派特刊",
            "path": "knowledge-base/articles/a1.md",
            "score": 9.5,
        }
    ]

    result = selector.select(articles)

    assert [a.article_id for a in result.selected] == ["a1"]
    assert result.skipped == []


def test_skips_external_research_even_when_high_score():
    """9分+研报 must be skipped with source_not_eligible reason."""
    selector = CrossArticleSourceSelector()
    result = selector.select(
        [
            {
                "id": "r1",
                "title": "半导体研报",
                "date": "2026-06-30",
                "column": "研报",
                "path": "knowledge-base/articles/r1.md",
                "score": 9.8,
            }
        ]
    )

    assert result.selected == []
    assert result.skipped[0]["reason"] == "source_not_eligible"


def test_selects_good_question_only_when_persona_gate_allows():
    """好问题 is only selected when persona gate judge returns True."""
    selector = CrossArticleSourceSelector(
        good_question_judge=lambda article: True,
    )
    result = selector.select(
        [
            {
                "id": "q1",
                "title": "好问题：产业链怎么看",
                "date": "2026-06-30",
                "column": "好问题",
                "path": "knowledge-base/articles/q1.md",
                "is_qa": True,
            }
        ]
    )

    assert result.selected[0].source_classification == "teacher_original"


def test_skips_good_question_when_judge_rejects():
    """好问题 is skipped when persona gate judge returns False."""
    selector = CrossArticleSourceSelector(
        good_question_judge=lambda article: False,
    )
    result = selector.select(
        [
            {
                "id": "q2",
                "title": "好问题：闲聊",
                "date": "2026-06-30",
                "column": "好问题",
                "path": "knowledge-base/articles/q2.md",
                "is_qa": True,
            }
        ]
    )

    assert len(result.selected) == 0
    assert result.skipped[0]["reason"] == "good_question_not_persona_eligible"


def test_skips_normal_content():
    """普通 column content without 星大派 origin must be skipped."""
    selector = CrossArticleSourceSelector()
    result = selector.select(
        [
            {
                "id": "n1",
                "title": "普通内容",
                "date": "2026-06-30",
                "column": "版本强势英雄",
                "path": "knowledge-base/articles/n1.md",
            }
        ]
    )

    assert result.selected == []
    assert result.skipped[0]["reason"] == "source_not_eligible"
