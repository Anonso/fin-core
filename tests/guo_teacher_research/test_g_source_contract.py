"""Frozen R1-6 source-family contract for the G/ZSXQ mainline."""

from __future__ import annotations

import pytest

from fin_analyse.guo_teacher_research.source_contract import classify_g_source


@pytest.mark.parametrize(
    ("column", "is_qa", "family", "content_type", "usage"),
    [
        ("星大派特刊", False, "星大派", "特刊", "systematic_framework"),
        ("星大派锐评", False, "星大派", "锐评", "recent_change_risk"),
        ("星大派好问题", True, "星大派", "好问题", "question_explanation_method"),
        ("星大派每日热点", False, "星大派", "每日热点", "recent_change_risk"),
        ("星大派人脉", False, "星大派", "人脉", "systematic_framework"),
        ("凤仙郡小故事", False, "凤仙郡小故事", "长期故事", "long_term_framework"),
    ],
)
def test_exact_source_types_are_the_only_g_source_families(
    column: str,
    is_qa: bool,
    family: str,
    content_type: str,
    usage: str,
) -> None:
    decision = classify_g_source(
        column,
        teacher_original=True,
        is_qa=is_qa,
    )

    assert decision.eligible is True
    assert decision.data_gap is None
    assert decision.classification is not None
    assert decision.classification.source_family == family
    assert decision.classification.content_type == content_type
    assert decision.classification.usage == usage


@pytest.mark.parametrize("column", ["星大派", "好问题", "合格好问题"])
def test_ambiguous_legacy_labels_never_guess_a_g_content_type(column: str) -> None:
    decision = classify_g_source(column, teacher_original=True, is_qa=True)

    assert decision.eligible is False
    assert decision.classification is None
    assert decision.data_gap == "g_source_type_ambiguous"


def test_good_question_requires_confirmed_teacher_answer() -> None:
    unconfirmed = classify_g_source("星大派好问题", teacher_original=False, is_qa=True)
    non_answer = classify_g_source("星大派好问题", teacher_original=True, is_qa=False)

    assert unconfirmed.eligible is False
    assert unconfirmed.data_gap == "g_source_original_provenance_unconfirmed"
    assert non_answer.eligible is False
    assert non_answer.data_gap == "g_source_question_answer_unconfirmed"


def test_priority_label_is_orthogonal_to_source_type_and_time() -> None:
    plain = classify_g_source("凤仙郡小故事", teacher_original=True, is_qa=False)
    labeled = classify_g_source(
        "凤仙郡小故事",
        teacher_original=True,
        is_qa=False,
        priority_label="重中之重",
    )

    assert plain.classification is not None
    assert labeled.classification is not None
    assert plain.classification.source_family == labeled.classification.source_family
    assert plain.classification.content_type == labeled.classification.content_type
    assert plain.classification.usage == labeled.classification.usage
    assert labeled.classification.priority_label == "重中之重"
    assert not hasattr(labeled.classification, "valid_until")
    assert not hasattr(labeled.classification, "publish_freshness")
