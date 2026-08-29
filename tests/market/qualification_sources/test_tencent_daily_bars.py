"""Behavior tests for the Tencent qfq daily-bar reader."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
from fin_analyse.market.qualification_sources.tencent_daily_bars import (
    TencentDailyBarReader,
    TencentDailyBarSourceError,
    _build_on_demand_tencent_daily_bar_reader,
)
from fin_analyse.market.qualified_daily_bars import QualifiedDailyBarReadRequest

_SAMPLE = QualifiedDailyBarReadRequest(
    symbol="600549",
    trade_date=date(2026, 8, 2),
    decision_cutoff_at=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
    minimum_completed_bars=2,
)

_QFQ_ROWS = [
    ["2026-07-27", "49.110", "50.200", "50.330", "47.750", "450131.000"],
    ["2026-07-28", "49.000", "46.990", "49.980", "46.830", "438642.000"],
    ["2026-07-29", "47.000", "47.370", "48.070", "45.010", "541512.000"],
    ["2026-07-30", "47.300", "47.900", "48.200", "46.900", "300000.000"],
]


def _payload(symbol: str = "sh600549", *, rows: list[list[str]] | None = None) -> bytes:
    return json.dumps(
        {"code": 0, "msg": "", "data": {symbol: {"qfqday": rows if rows is not None else _QFQ_ROWS}}},
        separators=(",", ":"),
    ).encode("utf-8")


class _Response:
    def __init__(self, content: bytes, *, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


def test_reader_parses_qfq_rows_with_ohlc_order_conversion() -> None:
    """腾讯行序 date/open/close/high/low/volume → OHLCV 的 open/high/low/close。"""
    captured: list[tuple[str, dict[str, str], float]] = []

    def http_get(url: str, *, params: dict[str, str], timeout: float) -> _Response:
        captured.append((url, params, timeout))
        return _Response(_payload())

    reader = TencentDailyBarReader(http_get=http_get)  # type: ignore[arg-type]
    series = reader.read(_SAMPLE)

    assert len(captured) == 1
    url, params, timeout = captured[0]
    assert "ifzq.gtimg.cn/appstock/app/fqkline/get" in url
    assert "param=sh600549%2Cday%2C" in url  # urlencode 转义逗号（腾讯接受）
    assert "%2Cqfq" in url
    assert timeout == 10.0
    assert params == {}
    assert series.provider_id == "tencent_daily_bars"
    assert series.source_revision == hashlib.sha256(_payload()).hexdigest()
    assert len(series.completed_bars) == 4
    first = series.completed_bars[0]
    assert first.date == "2026-07-27"
    assert first.open == 49.11
    assert first.high == 50.33  # 行内第 4 个（腾讯高）
    assert first.low == 47.75
    assert first.close == 50.20  # 行内第 3 个（腾讯收）
    assert first.volume == 450131.0


def test_reader_filters_bars_at_or_after_cutoff() -> None:
    """cutoff 当日及之后的 bar 不得出现在 completed_bars。"""
    cutoff = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)

    def http_get(url: str, **kwargs: object) -> _Response:
        return _Response(_payload())

    reader = TencentDailyBarReader(http_get=http_get)  # type: ignore[arg-type]
    series = reader.read(
        QualifiedDailyBarReadRequest(
            symbol="600549",
            trade_date=date(2026, 7, 29),
            decision_cutoff_at=cutoff,
            minimum_completed_bars=2,
        )
    )

    assert [bar.date for bar in series.completed_bars] == ["2026-07-27", "2026-07-28"]


def test_reader_fails_closed_on_insufficient_bars() -> None:
    def http_get(url: str, **kwargs: object) -> _Response:
        return _Response(_payload(rows=[_QFQ_ROWS[0]]))

    reader = TencentDailyBarReader(http_get=http_get)  # type: ignore[arg-type]
    with pytest.raises(TencentDailyBarSourceError, match="INSUFFICIENT_BARS"):
        reader.read(
            QualifiedDailyBarReadRequest(
                symbol="600549",
                trade_date=date(2026, 8, 2),
                decision_cutoff_at=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
                minimum_completed_bars=2,
            )
        )


def test_reader_fails_closed_on_upstream_status() -> None:
    def http_get(url: str, **kwargs: object) -> _Response:
        return _Response(b"{}", status_code=503)

    reader = TencentDailyBarReader(http_get=http_get)  # type: ignore[arg-type]
    with pytest.raises(TencentDailyBarSourceError, match="HTTP_STATUS_ERROR"):
        reader.read(_SAMPLE)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{bad json",
        b'{"code":1,"msg":"error"}',
        b'{"code":0,"data":{}}',
        b'{"code":0,"data":{"sh600549":{}}}',
        b'{"code":0,"data":{"sh600549":{"qfqday":[["bad"]]}}}',
        b'{"code":0,"data":{"sh600549":{"qfqday":[["2026-07-27","x","y","z","w","v"]]}}}',
    ],
)
def test_reader_fails_closed_on_invalid_payload(payload: bytes) -> None:
    def http_get(url: str, **kwargs: object) -> _Response:
        return _Response(payload)

    reader = TencentDailyBarReader(http_get=http_get)  # type: ignore[arg-type]
    with pytest.raises(TencentDailyBarSourceError, match="PAYLOAD_INVALID"):
        reader.read(_SAMPLE)


def test_reader_rejects_invalid_symbol_before_network() -> None:
    def forbidden_http_get(*args: object, **kwargs: object) -> _Response:
        raise AssertionError("invalid symbol unexpectedly reached the network")

    reader = TencentDailyBarReader(http_get=forbidden_http_get)  # type: ignore[arg-type]
    with pytest.raises(TencentDailyBarSourceError, match="SYMBOL_INVALID"):
        reader.read(
            QualifiedDailyBarReadRequest(
                symbol="600549&secid=0.000001",
                trade_date=date(2026, 8, 2),
                decision_cutoff_at=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
            )
        )


def test_reader_accepts_canonical_suffixed_symbol_and_uses_code_in_request() -> None:
    # 回归：on-demand 采集传 FIN canonical "600519.SH"，腾讯 fallback 曾因
    # 只接受裸代码而 SYMBOL_INVALID——东财 primary 不可达时全链失败。
    captured: list[str] = []

    def recording_http_get(url: str, *, params: dict[str, str], timeout: float) -> _Response:
        captured.append(url)
        assert "sh600519" in url, "request must use the venue+code form"
        assert "600519.SH" not in url, "canonical suffix must not leak into the request"
        return _Response(_payload("sh600519"))

    reader = TencentDailyBarReader(http_get=recording_http_get)  # type: ignore[arg-type]
    series = reader.read(
        QualifiedDailyBarReadRequest(
            symbol="600519.SH",
            trade_date=date(2026, 8, 2),
            decision_cutoff_at=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
            minimum_completed_bars=2,
        )
    )
    assert len(series.completed_bars) == 4
    assert captured, "canonical symbol unexpectedly rejected before the network"


def test_on_demand_reader_uses_live_evidence_origin() -> None:
    reader = _build_on_demand_tencent_daily_bar_reader(timeout_seconds=8.0)
    assert reader.evidence_origin is ObservationEvidenceOrigin.LIVE_CAPTURE
    assert "live" in reader.provider_version
