"""Source selector — filters persona-eligible articles for cross_article pipeline.

Only Xingdapai columns (特刊/锐评) and persona-gate-approved good questions
enter the cross-article synthesis pipeline. High-score research reports,
external content, and normal ZSXQ content are skipped with visible reasons.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fin_analyse.cognition.cross_article.models import ArticleRef, SelectionResult

# ── Column constants ────────────────────────────────────────────────────────

_XINGDAPAI_COLUMNS = frozenset({"星大派特刊", "星大派锐评"})
_QA_COLUMNS = frozenset({"星大派好问题", "好问题", "问题回答", "回答问题"})


def _default_good_question_judge(_article: dict[str, Any]) -> bool:
    """Default: reject good questions unless a real judge is injected."""
    return False


def _is_persona_eligible_column(column: str) -> bool:
    return column in _XINGDAPAI_COLUMNS


def _is_qa_column(column: str) -> bool:
    return column in _QA_COLUMNS


class CrossArticleSourceSelector:
    """Selects only persona-eligible Xingdapai articles for cross_article.

    The injected `good_question_judge` is responsible for determining whether
    a good-question article contains sufficient teacher-original methodology
    or reasoning to qualify as persona-eligible. In production this should be
    backed by the existing persona gate or a dedicated LLM/rule judge.

    Architecture constraint: this selector is a pure converter — it reads
    raw article dicts and emits ArticleRef or skip records. It does not
    read files, call LLMs, or write to any store.
    """

    def __init__(
        self,
        good_question_judge: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self._judge_qa = good_question_judge or _default_good_question_judge

    def select(self, articles: list[dict[str, Any]]) -> SelectionResult:
        """Convert indexed article dicts into ArticleRefs or skip records."""
        selected: list[ArticleRef] = []
        skipped: list[dict[str, str]] = []

        for raw in articles:
            article_id = str(raw.get("id", ""))
            column = str(raw.get("column", ""))
            is_qa = bool(raw.get("is_qa", False))

            # ── Xingdapai columns: always eligible ──
            if _is_persona_eligible_column(column):
                selected.append(self._to_article_ref(raw, "teacher_original"))
                continue

            # ── QA / good-question columns: gate-checked ──
            if _is_qa_column(column) or is_qa:
                if self._judge_qa(raw):
                    selected.append(self._to_article_ref(raw, "teacher_original"))
                else:
                    skipped.append(
                        {
                            "article_id": article_id,
                            "reason": "good_question_not_persona_eligible",
                        }
                    )
                continue

            # ── Everything else: skip ──
            skipped.append({"article_id": article_id, "reason": "source_not_eligible"})

        return SelectionResult(selected=selected, skipped=skipped)

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _to_article_ref(raw: dict[str, Any], source_classification: str) -> ArticleRef:
        return ArticleRef(
            article_id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            published_at=str(raw.get("date", "")),
            column=str(raw.get("column", "")),
            path=str(raw.get("path", "")),
            source_classification=source_classification,
            persona_eligible=True,
            content_excerpt=str(raw.get("content_excerpt", "")),
            metadata={
                "score": raw.get("score"),
                "is_qa": bool(raw.get("is_qa", False)),
            },
        )
