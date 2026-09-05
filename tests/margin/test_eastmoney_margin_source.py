from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fin_analyse.margin.eastmoney import EastmoneyMarginEvidenceSource
from fin_analyse.margin.evidence import MarginEvidenceRequest
from fin_analyse.market.data_qualification import ObservationEvidenceOrigin

CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class _Response:
    content: bytes
    status_code: int = 200


@dataclass
class _Transport:
    stock_balance: str = "1000"
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url, *, params, headers, timeout, allow_redirects):
        del headers, timeout, allow_redirects
        self.calls.append((url, dict(params)))
        report = params.get("reportName")
        if report == "RPTA_WEB_RZRQ_LSSH":
            return _Response(
                _json_bytes(
                    {
                        "success": True,
                        "result": {
                            "data": [
                                {
                                    "DIM_DATE": "2026-08-07 00:00:00",
                                    "RZYE": "10000",
                                    "RQYE": "100",
                                    "RZRQYE": "10100",
                                }
                            ]
                        },
                    }
                )
            )
        if report == "RPTA_WEB_RZRQ_GGMX":
            return _Response(
                _json_bytes(
                    {
                        "success": True,
                        "result": {
                            "data": [
                                {
                                    "DATE": "2026-08-07 00:00:00",
                                    "SCODE": "600519",
                                    "RZYE": self.stock_balance,
                                    "RQYE": "20",
                                    "RZRQYE": str(int(self.stock_balance) + 20),
                                    "RZYEZB": "2",
                                }
                            ]
                        },
                    }
                )
            )
        if "push2his.eastmoney.com" in url:
            return _Response(
                _json_bytes({"data": {"klines": ["2026-08-07,9,10,11,8,100,10000,0,0,0,0"]}})
            )
        if "push2delay.eastmoney.com" in url:
            # 基座 transport:stock/get 无市值字段(不影响既有分母断言)
            return _Response(_json_bytes({"data": {"f116": None, "f117": None}}))
        raise AssertionError(f"unexpected request: {url} {params}")


def test_source_preserves_raw_revisions_and_merges_same_day_stock_denominators(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ)
    source = EastmoneyMarginEvidenceSource(
        artifact_root=tmp_path / "margin-artifacts",
        http_get=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    request = MarginEvidenceRequest(instruments=("600519.SH",), as_of=captured_at)

    first = source.read(request)
    transport.stock_balance = "1100"
    second = source.read(request)

    assert set(first.markets) == {"SH", "SZ"}
    [first_stock] = first.instruments["600519.SH"]
    [second_stock] = second.instruments["600519.SH"]
    assert first_stock.financing_balance == Decimal("1000")
    assert first_stock.free_float_market_cap == Decimal("50000")
    assert first_stock.turnover == Decimal("10000")
    assert first_stock.denominator_trade_date == first_stock.trade_date
    assert first_stock.denominator_source_id == "eastmoney"
    assert second_stock.financing_balance == Decimal("1100")
    assert first_stock.source_revision != second_stock.source_revision
    # 5 个原 capture + 1 个 stock-quote 市值捕获(行情能力扩展验收 3)
    assert len(list((tmp_path / "margin-artifacts" / "artifacts").rglob("raw.bin"))) == 6
    assert len(transport.calls) == 10  # 8 原 + 2 stock-quote 市值捕获


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def test_source_merges_same_day_free_float_cap_from_stock_quote(tmp_path: Path) -> None:
    """行情能力扩展验收 3:stock/get f117 当日自由流通市值合并到最新点。"""

    class _CapTransport(_Transport):
        def __call__(self, url, params=None, timeout=None, **kwargs):
            if "datacenter-web.eastmoney.com" in url:
                return _Response(
                    _json_bytes(
                        {
                            "result": {
                                "data": [
                                    {
                                        "SCODE": "600519",
                                        "DATE": "2026-08-07",
                                        "RZYE": "1000",
                                        "RQYE": "20",
                                        "RZRQYE": "1020",
                                        "RZYEZB": "2",
                                    }
                                ]
                            }
                        }
                    )
                )
            if "push2his.eastmoney.com" in url:
                return _Response(
                    _json_bytes({"data": {"klines": ["2026-08-07,9,10,11,8,100,10000,0,0,0,0"]}})
                )
            if "push2delay.eastmoney.com" in url:
                return _Response(_json_bytes({"data": {"f116": 600000, "f117": 400000}}))
            raise AssertionError(f"unexpected request: {url} {params}")

    transport = _CapTransport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ)
    source = EastmoneyMarginEvidenceSource(
        artifact_root=tmp_path / "margin-artifacts",
        http_get=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    result = source.read(MarginEvidenceRequest(instruments=("600519.SH",), as_of=captured_at))
    [stock] = result.instruments["600519.SH"]
    # 反推路径(50000)被当日 f117 覆盖为 400000
    assert stock.free_float_market_cap == Decimal("400000")


def test_http_403_replays_stale_cache_with_reason_gaps(tmp_path: Path) -> None:
    """BUG-040：HTTP 非 200（反爬 403/5xx）与传输异常同走 stale 回放。

    回放成功时同时保留原因码 gap 与 STALE_CACHE gap，不随故障类型漂移。
    """
    warm_transport = _Transport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ)
    artifact_root = tmp_path / "margin-artifacts"
    warm_source = EastmoneyMarginEvidenceSource(
        artifact_root=artifact_root,
        http_get=warm_transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    request = MarginEvidenceRequest(instruments=("600519.SH",), as_of=captured_at)
    warm_source.read(request)

    def blocked_get(url, *, params, headers, timeout, allow_redirects):
        return _Response(b"blocked", status_code=403)

    blocked_source = EastmoneyMarginEvidenceSource(
        artifact_root=artifact_root,
        http_get=blocked_get,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    result = blocked_source.read(request)

    assert result.markets, "回放后仍应提供市场级陈旧数据"
    assert "MARGIN_EVIDENCE_HTTP_RESPONSE_INVALID:SH" in result.data_gaps
    assert "MARGIN_EVIDENCE_STALE_CACHE:SH" in result.data_gaps
    assert "MARGIN_EVIDENCE_HTTP_RESPONSE_INVALID:600519.SH" in result.data_gaps
    assert "MARGIN_EVIDENCE_STALE_CACHE:600519.SH" in result.data_gaps


def test_http_403_without_cache_reports_http_invalid_gap(tmp_path: Path) -> None:
    """无缓存可回放时，原因码照旧上抛为 typed gap（不伪造数据）。"""

    def blocked_get(url, *, params, headers, timeout, allow_redirects):
        return _Response(b"blocked", status_code=403)

    source = EastmoneyMarginEvidenceSource(
        artifact_root=tmp_path / "margin-artifacts-cold",
        http_get=blocked_get,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ),
    )
    result = source.read(
        MarginEvidenceRequest(
            instruments=("600519.SH",),
            as_of=datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ),
        )
    )

    assert "MARGIN_EVIDENCE_HTTP_RESPONSE_INVALID:SH" in result.data_gaps
    assert not any(gap.startswith("MARGIN_EVIDENCE_STALE_CACHE") for gap in result.data_gaps)
