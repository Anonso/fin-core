"""TDD: cache_only market snapshot must not call live provider."""

from __future__ import annotations


def test_cache_only_market_snapshot_does_not_call_live_provider():
    """When data_mode='cache_only' and no snapshot cache exists,
    MarketSnapshotService must NOT call MootdxProvider/AkShare.  Must return
    market_data_cache_missing in data_gaps."""
    from unittest.mock import patch

    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    svc = MarketSnapshotService()
    with patch.object(svc, "_data_cache") as mc:
        mc.peek_snapshot = None
        mc.get_snapshot.return_value = None
        mc.get_latest_snapshot.return_value = None
        mc.session = "realtime"

        result = svc.get_snapshot(MarketSnapshotRequest(ticker="600392", data_mode="cache_only"))

        assert result.cache_status == "miss"
        assert "market_data_cache_missing" in result.data_gaps
        # No error — graceful degradation
        assert result.error is None


def test_cache_only_hits_snapshot_returns_data():
    """When data_mode='cache_only' and snapshot cache exists, return it."""
    from unittest.mock import patch

    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    svc = MarketSnapshotService()
    with patch.object(svc, "_data_cache") as mc:
        mc.peek_snapshot = None
        mc.get_snapshot.return_value = {
            "ticker": "600392",
            "price": 12.50,
            "cache_status": "hit",
            "cache_hit": True,
        }

        result = svc.get_snapshot(MarketSnapshotRequest(ticker="600392", data_mode="cache_only"))

        assert result.cache_status == "hit"
        assert result.cache_hit is True
        assert result.snapshot["price"] == 12.50


def test_cache_only_stale_fallback_works():
    """When data_mode='cache_only' and fresh cache miss but stale exists,
    return stale_fallback."""
    from unittest.mock import patch

    from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotService

    svc = MarketSnapshotService()
    with patch.object(svc, "_data_cache") as mc:
        mc.peek_snapshot = None
        mc.get_snapshot.return_value = None
        mc.get_latest_snapshot.return_value = {
            "ticker": "600392",
            "price": 10.0,
            "cache_status": "hit",
            "data_gaps": [],
        }

        result = svc.get_snapshot(MarketSnapshotRequest(ticker="600392", data_mode="cache_only"))

        assert result.cache_status == "stale_fallback"
        assert "stale_fallback_warning" in result.data_gaps
