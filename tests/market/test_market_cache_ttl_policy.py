"""TDD tests for MarketDataCache TTL policy — slow data cache alignment.

Margin detail (融资融券明细): daily update → 12h cache
Northbound detail (北向资金): weekly-ish update → 7d cache
Financial series (财务报表): quarterly-ish → 24h cache (unchanged)
Market snapshot (实时快照): realtime → 15min cache (unchanged)
"""

from __future__ import annotations


def test_margin_detail_cache_ttl_is_not_intraday_short_lived():
    """Margin detail updates ~daily (after market close); cache >= 12h is appropriate."""
    from fin_analyse.market.warm_cache import TTL_MARGIN

    assert TTL_MARGIN >= 12 * 60 * 60, (
        f"TTL_MARGIN={TTL_MARGIN}s is too short for daily-updated margin data; "
        f"expected >= {12 * 60 * 60}s (12h)"
    )


def test_northbound_detail_cache_ttl_is_weekly_scale():
    """Northbound detail updates ~weekly; cache >= 7d is appropriate."""
    from fin_analyse.market.warm_cache import TTL_NORTHBOUND

    assert TTL_NORTHBOUND >= 7 * 24 * 60 * 60, (
        f"TTL_NORTHBOUND={TTL_NORTHBOUND}s is too short for weekly-updated northbound data; "
        f"expected >= {7 * 24 * 60 * 60}s (7d)"
    )


def test_financial_series_cache_ttl_is_24h():
    """Financial series cache must remain at 24h."""
    from fin_analyse.market.warm_cache import TTL_FINANCIAL

    assert TTL_FINANCIAL == 24 * 60 * 60, (
        f"TTL_FINANCIAL={TTL_FINANCIAL}s; expected exactly {24 * 60 * 60}s (24h)"
    )


def test_snapshot_cache_ttl_is_15min():
    """Market snapshot cache must remain at 15min."""
    from fin_analyse.market.warm_cache import TTL_SNAPSHOT

    assert TTL_SNAPSHOT == 15 * 60, (
        f"TTL_SNAPSHOT={TTL_SNAPSHOT}s; expected exactly {15 * 60}s (15min)"
    )
