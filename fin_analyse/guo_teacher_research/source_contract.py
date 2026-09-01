"""Exact source-family contract for ZSXQ material entering the G mainline.

This module deliberately classifies only the frozen R1-6 source labels.  It
does not infer a type from a partial name, rank a source, or assign a temporal
expiry.  Claim-level time sensitivity remains owned by the temporal/claim
paths, not by a column label.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GSourceClassification:
    """One source axis projection, independent of freshness or confidence."""

    source_family: str
    content_type: str
    usage: str
    priority_label: str | None


@dataclass(frozen=True)
class GSourceDecision:
    """A fail-closed eligibility decision with a narrow, auditable gap."""

    classification: GSourceClassification | None = None
    data_gap: str | None = None

    @property
    def eligible(self) -> bool:
        return self.classification is not None and self.data_gap is None


_EXACT_SOURCE_TYPES = {
    "星大派特刊": ("星大派", "特刊", "systematic_framework"),
    "星大派锐评": ("星大派", "锐评", "recent_change_risk"),
    "星大派好问题": ("星大派", "好问题", "question_explanation_method"),
    # owner 2026-09-01 拍板：每日热点=锐评同档（时效强、commentary 窗口）；
    # 人脉=特刊同档（systematic_framework、special 窗口）。
    "星大派每日热点": ("星大派", "每日热点", "recent_change_risk"),
    "星大派人脉": ("星大派", "人脉", "systematic_framework"),
    "凤仙郡小故事": ("凤仙郡小故事", "长期故事", "long_term_framework"),
    # 「普通」栏 owner 撤项（2026-08-28 晚）：曾在 BUG-006③ 短暂放行为 general G，
    # 全库质量审计显示 86% 为券商研报转载/总结（非老师原创），owner 拍板撤销
    # G 准入与深化资格——普通栏内容留在 reference lane 检索，不进 G 认知库。
}
_AMBIGUOUS_SOURCE_LABELS = frozenset({"星大派", "好问题", "合格好问题"})
_PRIORITY_LABEL = "重中之重"


def classify_g_source(
    column: object,
    *,
    teacher_original: bool,
    is_qa: bool,
    priority_label: object = None,
) -> GSourceDecision:
    """Classify a source only when its exact type and provenance are known.

    ``星大派好问题`` has an extra boundary: it is usable only for a confirmed
    teacher-original Q&A answer.  ``重中之重`` is preserved as an orthogonal
    retrieval/recheck label; this function intentionally has no freshness,
    confidence, horizon, or ``valid_until`` output.
    """

    normalized_column = column.strip() if isinstance(column, str) else ""
    if normalized_column in _AMBIGUOUS_SOURCE_LABELS:
        return GSourceDecision(data_gap="g_source_type_ambiguous")

    source_type = _EXACT_SOURCE_TYPES.get(normalized_column)
    if source_type is None:
        return GSourceDecision(classification=None)

    source_family, content_type, usage = source_type
    classification = GSourceClassification(
        source_family=source_family,
        content_type=content_type,
        usage=usage,
        priority_label=(
            _PRIORITY_LABEL if priority_label == _PRIORITY_LABEL else None
        ),
    )
    if not teacher_original:
        return GSourceDecision(
            classification=classification,
            data_gap="g_source_original_provenance_unconfirmed",
        )
    if normalized_column == "星大派好问题" and not is_qa:
        return GSourceDecision(
            classification=classification,
            data_gap="g_source_question_answer_unconfirmed",
        )
    return GSourceDecision(classification=classification)
