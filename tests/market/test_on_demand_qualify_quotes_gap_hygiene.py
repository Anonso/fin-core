"""BUG-011 回归：失败捕获不得级联假阳性 IDENTITY_MISMATCH。

2026-08-31 诊断：失败捕获（解析失败/http 非成功）venue=None 且已带自身
typed gap；_qualify_quotes 的身份比对把 venue=None 一律判为身份错配，
trace（08-27~08-30）中 EASTMONEY_RAW_SOURCE_PAYLOAD_PARSE_FAILED 与
EASTMONEY_RAW_IDENTITY_MISMATCH 恒成对出现，掩盖「成交额浮点解析失败」
这一真因。失败捕获以其自身 gap 为完整结论；真实身份错配仍照常上报。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fin_analyse.market.data_qualification import (
    QualificationSourceCapture,
    TradingStatus,
)
from fin_analyse.market.on_demand_tactical_context import _qualify_quotes

_REQUESTED_AT = datetime(2026, 8, 31, 3, 0, tzinfo=UTC)


def _capture(**overrides: object) -> QualificationSourceCapture:
    fields: dict[str, object] = {
        "symbol": "600519",
        "venue": "sh",
        "requested_at": _REQUESTED_AT,
        "received_at": _REQUESTED_AT,
        "fetch_duration_ms": 5,
        "source_event_at": None,
        "price": None,
        "trading_status": TradingStatus.UNKNOWN,
        "upper_limit_price": None,
        "lower_limit_price": None,
        "raw_payload": b"{}",
        "raw_payload_kind": "upstream_http_response",
    }
    fields.update(overrides)
    return QualificationSourceCapture(**fields)


def test_failed_capture_reports_only_its_own_typed_gap() -> None:
    capture = _capture(
        venue=None,
        data_gaps=("source_payload_parse_failed",),
    )

    result = _qualify_quotes(
        "600519.SH",
        [("eastmoney_raw", capture, None)],
        continuous=True,
    )

    assert result.status == "UNKNOWN"
    assert result.data_gaps == ("EASTMONEY_RAW_SOURCE_PAYLOAD_PARSE_FAILED",)


def test_genuine_venue_mismatch_is_still_reported() -> None:
    capture = _capture(venue="sz")

    result = _qualify_quotes(
        "600519.SH",
        [("eastmoney_raw", capture, None)],
        continuous=True,
    )

    assert result.status == "UNKNOWN"
    assert result.data_gaps == ("EASTMONEY_RAW_IDENTITY_MISMATCH",)
