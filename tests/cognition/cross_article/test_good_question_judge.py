"""Tests for LlmGoodQuestionJudge."""

from __future__ import annotations

from fin_analyse.cognition.cross_article.good_question_judge import LlmGoodQuestionJudge


class FakeBackend:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self.response


def test_judge_returns_true_for_eligible_article(tmp_path):
    """Article with methodology content passes the gate."""
    article_path = tmp_path / "test.md"
    article_path.write_text("锅老师分析了半导体产业链上下游，指出前驱体材料是关键瓶颈。")

    judge = LlmGoodQuestionJudge(
        backend=FakeBackend(
            '{"persona_eligible": true, "reason": "含产业链分析", '
            '"has_methodology": true, "has_industry_insight": true, "confidence": 0.85}'
        )
    )

    result = judge(
        {
            "id": "q1",
            "title": "好问题：半导体怎么看",
            "date": "2026-06-30",
            "column": "星大派好问题",
            "path": str(article_path),
        }
    )

    assert result is True


def test_judge_returns_false_for_chat_article(tmp_path):
    """Simple chat/emotional support content fails the gate."""
    article_path = tmp_path / "test2.md"
    article_path.write_text("谢谢锅老师，我会继续努力的。")

    judge = LlmGoodQuestionJudge(
        backend=FakeBackend(
            '{"persona_eligible": false, "reason": "简单闲聊", '
            '"has_methodology": false, "has_industry_insight": false, "confidence": 0.9}'
        )
    )

    result = judge(
        {
            "id": "q2",
            "title": "好问题：心态怎么调整",
            "date": "2026-06-30",
            "column": "星大派好问题",
            "path": str(article_path),
        }
    )

    assert result is False


def test_judge_defaults_to_false_without_backend():
    """No backend → default deny (safe)."""
    judge = LlmGoodQuestionJudge(backend=None)
    result = judge({"id": "q3", "title": "test", "date": "", "column": "好问题"})
    assert result is False
