"""Test source labeler."""

from pathlib import Path

from fin_analyse.cognition.labeling import SourceLabeler

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "cognition"


def test_labels_first_person_original_teacher_content():
    content = (FIXTURES / "guo_original_policy_article.md").read_text(encoding="utf-8")
    labeler = SourceLabeler(default_teacher_id="guo", min_original_chars=50)

    label = labeler.label(title="政策变化后的行业判断", content=content, author="郭老师")

    assert label.label == "teacher_original"
    assert label.teacher_id == "guo"
    assert label.confidence >= 0.7


def test_labels_ai_assisted_report_as_not_teacher_original():
    content = (FIXTURES / "guo_ai_assisted_research.md").read_text(encoding="utf-8")
    labeler = SourceLabeler(default_teacher_id="guo")

    label = labeler.label(title="AI分析：某行业研报摘要", content=content, author="郭老师")

    assert label.label == "ai_assisted"
    assert label.teacher_id == "guo"
    assert label.confidence >= 0.8


def test_short_content_is_unknown():
    labeler = SourceLabeler(default_teacher_id="guo", min_original_chars=30)

    label = labeler.label(title="短消息", content="关注。", author="郭老师")

    assert label.label == "unknown"
