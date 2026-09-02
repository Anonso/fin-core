"""deep_read 公司名提取：current units schema + legacy core_theses。"""

from __future__ import annotations

from fin_analyse.guo_teacher_research.runtime_context import (
    _extract_companies_from_core_theses,
)


def test_extract_from_units_schema() -> None:
    payload = {
        "units": [
            {
                "unit_id": "u1",
                "related_companies": ["长电科技", "通富微电"],
            },
            {
                "unit_id": "u2",
                "related_companies": ["太极实业"],
            },
        ]
    }
    names = _extract_companies_from_core_theses(payload)
    assert names == ["太极实业", "通富微电", "长电科技"]


def test_extract_legacy_core_theses_still_works() -> None:
    payload = {
        "core_theses": [
            {"title": "t1", "related_companies": ["紫金矿业"]},
            {"title": "t2", "related_companies": ["洛阳钼业"]},
        ]
    }
    names = _extract_companies_from_core_theses(payload)
    assert names == ["洛阳钼业", "紫金矿业"]
