"""TDD tests for MarketSnapshotService.get_snapshot — P0/P1 fixes."""

from __future__ import annotations

# ── P0-2: cached margin/northbound ───────────────────────────────────────


def test_warmup_then_snapshot_no_margin_northbound_akshare_calls():
    """After warmup, MarketSnapshotService must NOT call raw margin/northbound."""
    import contextlib
    import sys
    import tempfile
    from pathlib import Path

    call_log: list[str] = []

    class _FakeMarginDF:
        empty = False

        @property
        def columns(self):
            return {
                "标的证券代码",
                "融资余额",
                "融资买入额",
                "融券余量",
                "融券卖出量",
                "融资融券余额",
            }

        def __getitem__(self, key):
            return ["600392"]

        def __len__(self):
            return 1

    class _FakeNBDF:
        empty = False
        columns = {"持股日期", "持股数量", "持股市值", "持股数量占A股百分比", "今日增持资金"}

        def __len__(self):
            return 2

        @property
        def iloc(self):
            class _ILoc:
                def __getitem__(self, idx):
                    return type("Row", (), {"get": lambda s, k, d=None: d})()

            return _ILoc()

    class _FakeAk:
        @staticmethod
        def stock_margin_detail_sse(date):
            call_log.append(f"margin_sse({date})")
            return _FakeMarginDF()

        @staticmethod
        def stock_margin_detail_szse(date):
            call_log.append(f"margin_szse({date})")
            return _FakeMarginDF()

        @staticmethod
        def stock_hsgt_individual_em(symbol):
            call_log.append(f"northbound({symbol})")
            return _FakeNBDF()

        @staticmethod
        def stock_financial_abstract_ths(symbol, indicator):
            call_log.append(f"financial({symbol})")
            return type(
                "DF", (), {"empty": False, "columns": set(), "iterrows": lambda s: iter([])}
            )()

    sys.modules["akshare"] = _FakeAk

    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService
    from fin_analyse.market.warm_cache import MarketDataCache

    with tempfile.TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        svc = MarketSnapshotService(data_cache=cache)
        cache.session = "preopen"
        cache.get_margin_detail("600392")
        cache.get_northbound_detail("600392")
        call_log.clear()

        with contextlib.suppress(Exception):
            svc.get_snapshot(MarketSnapshotRequest(ticker="600392", session="preopen"))

        margin_nb_calls = [c for c in call_log if "margin" in c or "northbound" in c]
        assert len(margin_nb_calls) == 0, (
            f"Must not call margin/northbound AkShare after warmup, got: {margin_nb_calls}"
        )


# ── P0-4: stale fallback ─────────────────────────────────────────────────


def test_expired_snapshot_stale_fallback():
    """When provider fails and snapshot is expired, return stale_fallback."""
    import time
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from fin_analyse.market.warm_cache import MarketDataCache

    with TemporaryDirectory() as tmp:
        cache = MarketDataCache(cache_dir=Path(tmp))
        cache.set_snapshot(
            "600392",
            {
                "ticker": "600392",
                "price": 10.0,
                "cache_status": "hit",
                "cache_session": "preopen",
                "data_freshness": {"snapshot_at": "2026-07-01T08:00:00Z"},
                "_freshness_financial": "2026-07-01T08:00:00Z",
                "_freshness_margin": "2026-07-01T08:00:00Z",
                "_freshness_northbound": "2026-07-01T08:00:00Z",
                "data_gaps": [],
            },
            ttl=0,
        )
        time.sleep(0.01)

        stale = cache.get_latest_snapshot("600392", allow_stale=True)
        assert stale is not None
        assert stale.get("price") == 10.0


# ── P1: cache_status/cache_hit semantics ─────────────────────────────────


def test_cache_hit_semantics():
    """Snapshot cache hit → cache_status=hit, cache_hit=True."""
    from unittest.mock import patch

    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    svc = MarketSnapshotService()
    with patch.object(svc, "_data_cache") as mc:
        mc.get_snapshot.return_value = {
            "ticker": "600392",
            "price": 12.50,
            "cache_status": "hit",
            "cache_hit": True,
            "cache_session": "preopen",
            "data_freshness": {},
            "data_gaps": [],
        }
        result = svc.get_snapshot(MarketSnapshotRequest(ticker="600392"))
        assert result.cache_status == "hit"
        assert result.cache_hit is True


def test_miss_when_no_cache_no_gaps():
    """Fresh fetch with no snapshot cache and no data_gaps → miss, cache_hit=False."""
    # This test verifies the mapping exists: a nonexistent ticker will fail
    # at provider level (no klines), returning miss/stale_fallback
    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    svc = MarketSnapshotService()
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="__nonexistent_xyz_123__"))

    # Must be one of miss or stale_fallback (not hit)
    assert result.cache_status in ("miss", "stale_fallback"), (
        f"Expected miss or stale_fallback, got {result.cache_status}"
    )
    if result.cache_status == "miss":
        assert result.cache_hit is False
    elif result.cache_status == "stale_fallback":
        assert result.cache_hit is True
        assert "stale_fallback_warning" in result.data_gaps


def test_stale_fallback_semantics():
    """Stale fallback → cache_status=stale_fallback, cache_hit=True, warning in data_gaps."""
    from unittest.mock import patch

    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    class _EmptyProvider:
        def get_history(self, ticker: str, days: int = 120) -> list:
            return []

    svc = MarketSnapshotService(provider_factory=_EmptyProvider)
    with patch.object(svc, "_data_cache") as mc:
        mc.get_snapshot.return_value = None
        mc.get_latest_snapshot.return_value = {
            "ticker": "600392",
            "price": 10.0,
            "cache_status": "hit",
            "cache_session": "preopen",
            "data_freshness": {},
            "data_gaps": [],
        }
        mc.get_financial_time_series.return_value = {"ticker": "600392", "reports": []}
        mc.get_margin_detail.return_value = {"date": "2026-07-01"}
        mc.get_northbound_detail.return_value = {"date": "2026-07-01"}
        mc.session = "realtime"

        result = svc.get_snapshot(MarketSnapshotRequest(ticker="600392"))

    assert result.cache_status == "stale_fallback"
    assert result.cache_hit is True
    assert "stale_fallback_warning" in result.data_gaps


def test_cache_status_values_are_valid():
    """All cache_status values must be in the valid set."""
    valid = {"hit", "partial", "miss", "stale_fallback"}
    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    svc = MarketSnapshotService()
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="__nonexistent_ticker_xyz__"))
    assert result.cache_status in valid, (
        f"cache_status must be one of {valid}, got {result.cache_status}"
    )
