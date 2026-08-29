"""TDD tests for MarketDataCache — P0/P1 fixes."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from fin_analyse.market.warm_cache import MarketDataCache

# ── Stub AkShare ──────────────────────────────────────────────────────────────

_ORIG_AKSHARE = sys.modules.get("akshare")


@pytest.fixture(autouse=True)
def _restore_akshare_module():
    """Restore sys.modules['akshare'] after each test to avoid cross-test pollution."""
    yield
    if _ORIG_AKSHARE is not None:
        sys.modules["akshare"] = _ORIG_AKSHARE
    else:
        sys.modules.pop("akshare", None)


class _FakeAkShare:
    call_count: int = 0

    def stock_financial_abstract_ths(self, symbol: str, indicator: str):
        _FakeAkShare.call_count += 1
        return _fake_financial_df()

    def stock_margin_detail_sse(self, date: str):
        _FakeAkShare.call_count += 1
        return _fake_margin_sse_df()

    def stock_margin_detail_szse(self, date: str):
        _FakeAkShare.call_count += 1
        return _fake_margin_szse_df()

    def stock_hsgt_individual_em(self, symbol: str):
        _FakeAkShare.call_count += 1
        return _fake_northbound_df()


class _FakeRow:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class _FakeDF:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    @property
    def empty(self):
        return len(self._rows) == 0

    @property
    def columns(self):
        return set(self._rows[0].keys()) if self._rows else set()

    def __getitem__(self, key):
        return [r.get(key) for r in self._rows]

    def __len__(self):
        return len(self._rows)

    @property
    def iloc(self):
        return _FakeILoc(self._rows)

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield (i, _FakeRow(r))


class _FakeILoc:
    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, idx):
        if isinstance(idx, int):
            return _FakeRow(self._rows[idx])
        return [_FakeRow(r) for r in self._rows[idx]]


def _fake_financial_df():
    return _FakeDF(
        [
            {
                "报告期": "2025-12-31",
                "净利润": "1.5e9",
                "营业总收入": "8.0e9",
                "净资产收益率": "12.5",
                "销售毛利率": "30.2",
                "每股收益": "0.85",
                "每股净资产": "6.80",
                "资产负债率": "45.3",
            },
            {
                "报告期": "2024-12-31",
                "净利润": "1.2e9",
                "营业总收入": "7.0e9",
                "净资产收益率": "11.0",
                "销售毛利率": "28.5",
                "每股收益": "0.72",
                "每股净资产": "6.10",
                "资产负债率": "47.1",
            },
        ]
    )


def _fake_margin_sse_df():
    return _FakeDF(
        [
            {
                "标的证券代码": "600392",
                "融资余额": "5.0e8",
                "融资买入额": "2.0e7",
                "融券余量": "100000",
                "融券卖出量": "5000",
                "融资融券余额": "5.5e8",
            },
        ]
    )


def _fake_margin_szse_df():
    return _FakeDF(
        [
            {
                "证券代码": "000831",
                "融资余额": "3.0e8",
                "融资买入额": "1.5e7",
                "融券余量": "50000",
                "融券卖出量": "2000",
                "融资融券余额": "3.3e8",
            },
        ]
    )


def _fake_northbound_df():
    return _FakeDF(
        [
            {
                "持股日期": "2026-06-30",
                "持股数量": "2000000",
                "持股市值": "3.0e8",
                "持股数量占A股百分比": "3.5",
                "今日增持资金": "5.0e6",
            },
            {
                "持股日期": "2026-07-01",
                "持股数量": "2100000",
                "持股市值": "3.2e8",
                "持股数量占A股百分比": "3.7",
                "今日增持资金": "3.0e6",
            },
        ]
    )


def _setup_fake():
    fake = _FakeAkShare()
    sys.modules["akshare"] = fake
    _FakeAkShare.call_count = 0
    return fake


# ── P0-1: raw cache key不含session ────────────────────────────────────────


def test_raw_cache_key_excludes_session():
    """financial/margin/northbound keys must NOT include session."""
    cache = MarketDataCache()
    cache.session = "preopen"
    k1 = cache._raw_key("akshare", "financial_time_series", "600392")
    cache.session = "realtime"
    k2 = cache._raw_key("akshare", "financial_time_series", "600392")
    assert k1 == k2, f"Raw keys must not differ by session: {k1} vs {k2}"

    cache.session = "midday"
    k3 = cache._raw_key("akshare", "margin_detail", "600392")
    cache.session = "preopen"
    k4 = cache._raw_key("akshare", "margin_detail", "600392")
    assert k3 == k4


def test_snapshot_key_includes_session():
    """Snapshot keys MUST include session."""
    cache = MarketDataCache()
    cache.session = "preopen"
    k1 = cache._snapshot_key("600392")
    cache.session = "realtime"
    k2 = cache._snapshot_key("600392")
    assert k1 != k2, f"Snapshot keys must differ by session: {k1} vs {k2}"


def test_preopen_warmup_benefits_realtime_raw_reads():
    """preopen warmup of raw data must be reused by realtime get_market_snapshot."""
    with TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        _setup_fake()

        # Simulate preopen warmup
        cache.session = "preopen"
        cache.get_financial_time_series("600392")
        cache.get_margin_detail("600392")
        cache.get_northbound_detail("600392")
        calls_after_warmup = _FakeAkShare.call_count
        assert calls_after_warmup > 0

        # Simulate realtime read — must use cached data, no new AkShare calls
        cache.session = "realtime"
        cache.get_financial_time_series("600392")
        cache.get_margin_detail("600392")
        cache.get_northbound_detail("600392")
        assert _FakeAkShare.call_count == calls_after_warmup, (
            f"Realtime must reuse preopen cache. calls: {_FakeAkShare.call_count}"
        )


def test_financial_time_series_cache_hit_skips_akshare():
    with TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        _setup_fake()

        r1 = cache.get_financial_time_series("600392")
        assert r1 is not None
        assert len(r1.get("reports", [])) > 0
        calls1 = _FakeAkShare.call_count

        cache.get_financial_time_series("600392")
        assert _FakeAkShare.call_count == calls1


def test_margin_detail_cache_hit_skips_akshare():
    with TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        _setup_fake()

        r1 = cache.get_margin_detail("600392")
        calls1 = _FakeAkShare.call_count
        assert calls1 > 0

        r2 = cache.get_margin_detail("600392")
        assert r2 == r1
        assert _FakeAkShare.call_count == calls1


def test_northbound_cache_hit_skips_akshare():
    with TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        _setup_fake()

        r1 = cache.get_northbound_detail("600392")
        calls1 = _FakeAkShare.call_count
        assert calls1 > 0

        r2 = cache.get_northbound_detail("600392")
        assert r2 == r1
        assert _FakeAkShare.call_count == calls1


def test_different_tickers_no_cross_cache():
    with TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        _setup_fake()

        cache.get_financial_time_series("600392")
        calls1 = _FakeAkShare.call_count
        cache.get_financial_time_series("000831")
        calls2 = _FakeAkShare.call_count
        assert calls2 > calls1
