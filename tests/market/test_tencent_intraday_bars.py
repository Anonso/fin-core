"""Tests for Tencent 30-minute bar reader (行情能力扩展验收 1).

TDD: tests must FAIL before implementation exists.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from fin_analyse.market.qualification_sources.tencent_intraday_bars import (
    TencentIntradayBarReader,
)


def _mkline_payload(bars: list[list[Any]]) -> bytes:
    import json

    return json.dumps(
        {
            "code": 0,
            "msg": "",
            "data": {"sz002409": {"m30": bars}},
        }
    ).encode()


def _bar(ts: str, o: float, c: float, h: float, low: float, v: int) -> list[Any]:
    return [ts, o, c, h, low, v]


def test_read_returns_completed_30m_bars_before_cutoff() -> None:
    """cutoff 前完成的 30m bar 返回;未完成/未来 bar 排除。"""
    from zoneinfo import ZoneInfo

    cutoff = datetime(2026, 8, 12, 14, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    bars = [
        _bar("202608121000", 10.0, 10.2, 10.3, 9.9, 1000),
        _bar("202608121030", 10.2, 10.4, 10.5, 10.1, 1200),
        _bar("202608121400", 10.4, 10.6, 10.7, 10.3, 1500),  # 未完成(14:20 cutoff)
    ]

    class _FakeHttp:
        def __call__(self, url, params=None, timeout=None):
            class _R:
                status_code = 200
                content = _mkline_payload(bars)

            return _R()

    reader = TencentIntradayBarReader(http_get=_FakeHttp())
    series = reader.read(
        type(
            "Req",
            (),
            {
                "symbol": "002409.SZ",
                "trade_date": date(2026, 8, 12),
                "decision_cutoff_at": cutoff,
                "minimum_completed_bars": 1,
                "deadline_at": None,
            },
        )()  # type: ignore[arg-type]
    )
    assert len(series.completed_bars) == 2
    assert series.completed_bars[0].date == "2026-08-12T10:00:00+08:00"
    assert series.completed_bars[0].close == 10.2
    assert series.completed_bars[1].volume == 1200
    assert series.provider_id == "tencent_intraday_bars"


def test_read_fails_closed_on_empty_or_bad_payload() -> None:
    from zoneinfo import ZoneInfo

    cutoff = datetime(2026, 8, 12, 14, 20, tzinfo=ZoneInfo("Asia/Shanghai"))

    class _EmptyHttp:
        def __call__(self, url, params=None, timeout=None):
            class _R:
                status_code = 200
                content = _mkline_payload([])

            return _R()

    reader = TencentIntradayBarReader(http_get=_EmptyHttp())
    with pytest.raises(Exception) as exc:
        reader.read(
            type(
                "Req",
                (),
                {
                    "symbol": "002409.SZ",
                    "trade_date": date(2026, 8, 12),
                    "decision_cutoff_at": cutoff,
                    "minimum_completed_bars": 1,
                    "deadline_at": None,
                },
            )()  # type: ignore[arg-type]
        )
    assert "INSUFFICIENT" in str(exc.value)
