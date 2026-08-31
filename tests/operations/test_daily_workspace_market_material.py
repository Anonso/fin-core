from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fin_analyse.operations.daily_workspace_generator import (
    _MARKET_OVERVIEW_MAX_CHARS,
    _render_market_overview,
    _render_prompt,
)


class _OverviewResult:
    status = "PARTIAL"

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_capability_value(self) -> dict[str, object]:
        return self._payload


def _overview_payload() -> dict[str, object]:
    return {
        "effective_trade_date": "2026-08-31",
        "observation_mode": "LATEST_COMPLETED_SESSION",
        "provider_updated_at": "2026-08-31T15:00:00+00:00",
        "major_indices": [
            {
                "code": "000001",
                "name": "上证指数",
                "level": 4300.12,
                "change_pct": 0.86,
                "turnover_yuan": 576_656_606,
            },
            {
                "code": "399001",
                "name": "深证成指",
                "level": 13_400.34,
                "change_pct": 0.44,
                "turnover_yuan": 658_824_929,
            },
        ],
        "breadth": {
            "covered_instruments": 4_000,
            "advancers": 2_200,
            "decliners": 1_500,
            "unchanged": 300,
            "total_turnover_yuan": 1_235_000_000_000,
        },
        "industry": {
            "leaders_by_change": [
                {"name": "电子", "change_pct": 2.33, "turnover_yuan": 534_677_840_660}
            ],
            "leaders_by_turnover": [
                {"name": "半导体", "change_pct": 2.36, "turnover_yuan": 257_679_723_075}
            ],
        },
        "concept": {
            "leaders_by_change": [
                {"name": "半导体概念", "change_pct": 1.78, "turnover_yuan": 478_723_179_945}
            ],
            "leaders_by_turnover": [
                {"name": "华为概念", "change_pct": 1.82, "turnover_yuan": 498_905_007_079}
            ],
        },
        "turnover_leaders": [
            {"name": "中际旭创", "change_pct": -0.75, "turnover_yuan": 18_925_701_851.77}
        ],
        "limitations": [
            "MARKET_OVERVIEW_BREADTH_UNAVAILABLE",
            "MARKET_OVERVIEW_DELAYED_REFERENCE",
        ],
    }


def test_market_overview_projection_keeps_high_value_facts_without_json_cut() -> None:
    text = _render_market_overview(_OverviewResult(_overview_payload()))

    assert text is not None
    assert len(text) <= _MARKET_OVERVIEW_MAX_CHARS
    assert "上证指数（点位 4300.12，涨跌 +0.86%" in text
    assert "市场宽度：上涨2200；下跌1500；平盘300；覆盖4000" in text
    assert "行业成交额靠前：半导体" in text
    assert "概念涨幅靠前：半导体概念" in text
    assert "成交额靠前个股：中际旭创" in text
    assert "限制：市场宽度不可用；延迟行情，仅作参考" in text
    assert "{" not in text and "}" not in text


def test_unknown_or_malformed_overview_is_still_unavailable() -> None:
    assert _render_market_overview(SimpleNamespace(status="UNKNOWN")) is None
    assert _render_market_overview(_OverviewResult({"major_indices": []})) is None


def test_prompt_requires_numeric_fact_before_limitation_summary() -> None:
    prompt = _render_prompt(
        question="盘后工作区检查点：今日复盘要点、明日观察项与组合复核是什么？",
        materials={
            "portfolio": "空仓，现金 43312.33 元",
            "market_overview": "主要指数：上证指数（点位 4300.12，涨跌 +0.86%）",
            "g_context": "G 认知：周一月线 KPI 维稳预判",
            "g_reference": None,
        },
        context=None,
        as_of=datetime(2026, 8, 31, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        trading_day_id="2026-08-31",
    )

    assert "至少引用两条带数字的当日事实" in prompt
    assert "支持/未兑现/无直接证据" in prompt
    assert "不要把材料限制本身写成唯一结论" in prompt
