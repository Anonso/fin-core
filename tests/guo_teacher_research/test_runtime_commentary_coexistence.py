"""Latest 锐评 and latest 每日热点 coexist as two independent dimensions.

Owner 2026-09-01: 锐评 = 锅老师的看法；每日热点 = 锅老师用 AI 总结的参考
信息。Each dimension injects at most one article; they must not replace each
other, and older commentary articles must not be selected as fallback.
"""

from __future__ import annotations

from fin_analyse.guo_teacher_research.runtime_context import (
    _latest_commentary,
    _select_context_candidates,
)


def _candidate(
    article_id: str,
    column: str,
    published_at: str,
) -> dict[str, str]:
    return {
        "article_id": article_id,
        "column": column,
        "published_at": published_at,
        "title": f"{column}:{article_id}",
    }


def test_latest_commentary_filters_by_column() -> None:
    candidates = [
        _candidate("old-ping", "星大派锐评", "2026-08-31T07:27:00+00:00"),
        _candidate("new-ping", "星大派锐评", "2026-09-01T03:00:00+00:00"),
        _candidate("new-hot", "星大派每日热点", "2026-09-01T00:33:00+00:00"),
    ]

    assert _latest_commentary(candidates, column="星大派锐评")["article_id"] == "new-ping"
    assert _latest_commentary(candidates, column="星大派每日热点")["article_id"] == "new-hot"


def test_select_coexists_latest_commentary_and_daily_hot() -> None:
    candidates = [
        _candidate("old-ping", "星大派锐评", "2026-08-31T07:27:00+00:00"),
        _candidate("new-ping", "星大派锐评", "2026-09-01T03:00:00+00:00"),
        _candidate("new-hot", "星大派每日热点", "2026-09-01T00:33:00+00:00"),
    ]

    selected = _select_context_candidates(candidates, intent_tokens={}, max_events=2)

    assert {c["article_id"] for c in selected} == {"new-ping", "new-hot"}
    assert all(c["_fresh_g_selection_bucket"] == "latest_commentary" for c in selected)


def test_select_keeps_only_newest_per_dimension_with_extra_budget() -> None:
    candidates = [
        _candidate("old-ping", "星大派锐评", "2026-08-31T07:27:00+00:00"),
        _candidate("new-ping", "星大派锐评", "2026-09-01T03:00:00+00:00"),
        _candidate("new-hot", "星大派每日热点", "2026-09-01T00:33:00+00:00"),
    ]

    selected = _select_context_candidates(candidates, intent_tokens={}, max_events=3)

    assert {c["article_id"] for c in selected} == {"new-ping", "new-hot"}


def test_select_single_dimension_when_other_absent() -> None:
    candidates = [
        _candidate("new-ping", "星大派锐评", "2026-09-01T03:00:00+00:00"),
    ]

    selected = _select_context_candidates(candidates, intent_tokens={}, max_events=2)

    assert [c["article_id"] for c in selected] == ["new-ping"]
