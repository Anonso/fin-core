"""Tests for MarketSnapshotService.get_snapshot — new FIN internal seam."""

from __future__ import annotations

import json
from typing import Any

from fin_analyse.market.snapshot import (
    MarketSnapshotRequest,
    MarketSnapshotService,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_klines(count: int = 120):
    """Return a list of fake OHLCV objects for testing."""
    from fin_analyse.market.providers.base import OHLCV

    return [
        OHLCV(
            date=f"2026-07-{max(1, i):02d}",
            open=10.0 + i * 0.01,
            high=10.5 + i * 0.01,
            low=9.5 + i * 0.01,
            close=10.2 + i * 0.01,
            volume=1000000 + i * 1000,
        )
        for i in range(count)
    ]


def _fake_cache_with_snapshot(ticker: str, snapshot: dict[str, Any] | None = None):
    """Create a mock MarketDataCache with optional snapshot."""
    mc = _FakeCache()
    mc._snapshots[ticker] = snapshot
    return mc


class _FakeCache:
    """Minimal fake MarketDataCache for unit tests."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any] | None] = {}
        self._latest_snapshots: dict[str, dict[str, Any] | None] = {}
        self._financials: dict[str, Any] = {}
        self._margins: dict[str, dict[str, Any]] = {}
        self._northbounds: dict[str, dict[str, Any]] = {}
        self.session: str = "realtime"

    def get_snapshot(self, ticker: str) -> dict[str, Any] | None:
        return self._snapshots.get(ticker)

    def peek_snapshot(self, ticker: str) -> tuple[dict[str, Any] | None, bool]:
        """Side-effect-free peek: _snapshots is fresh, _latest_snapshots is stale."""
        data = self._snapshots.get(ticker)
        if data is not None:
            return data, True
        data = self._latest_snapshots.get(ticker)
        if data is not None:
            return data, False
        return None, False

    def get_latest_snapshot(
        self, ticker: str, *, allow_stale: bool = False
    ) -> dict[str, Any] | None:
        return self._latest_snapshots.get(ticker)

    def get_financial_time_series(self, ticker: str) -> Any:
        return self._financials.get(ticker)

    def get_margin_detail(self, ticker: str) -> dict[str, Any]:
        return self._margins.get(ticker, {})

    def get_northbound_detail(self, ticker: str) -> dict[str, Any]:
        return self._northbounds.get(ticker, {})

    def set_snapshot(self, ticker: str, data: dict[str, Any], ttl: int | None = None) -> None:
        self._snapshots[ticker] = data


class _FakeProvider:
    """Fake provider that returns controlled klines."""

    def __init__(self, klines: list | None = None) -> None:
        self._klines = _fake_klines(120) if klines is None else klines

    def get_history(self, ticker: str, days: int = 120) -> list:
        return self._klines


# ── cache_only: missing ──────────────────────────────────────────────────────


def test_cache_only_missing_returns_cache_missing_without_live_provider():
    """cache_only with no snapshot → miss, market_data_cache_missing, no error."""
    cache = _FakeCache()
    cache._snapshots["000001"] = None
    cache._latest_snapshots["000001"] = None

    svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))

    assert result.cache_status == "miss"
    assert result.cache_hit is False
    assert "market_data_cache_missing" in result.data_gaps
    assert result.error is None


# ── cache_only: hit ─────────────────────────────────────────────────────────


def test_cache_only_hits_snapshot_returns_data():
    """cache_only with existing snapshot returns it with cache_status=hit."""
    cache = _FakeCache()
    cache._snapshots["000001"] = {
        "ticker": "000001",
        "price": 12.50,
        "cache_status": "hit",
        "cache_hit": True,
        "cache_session": "realtime",
    }

    svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))

    assert result.cache_status == "hit"
    assert result.cache_hit is True
    assert result.snapshot.get("price") == 12.50


# ── cache_only: stale fallback ──────────────────────────────────────────────


def test_cache_only_stale_fallback_returns_warning():
    """cache_only with no fresh snapshot but stale exists → stale_fallback + warning."""
    cache = _FakeCache()
    cache._snapshots["000001"] = None
    cache._latest_snapshots["000001"] = {
        "ticker": "000001",
        "price": 10.0,
        "cache_status": "hit",
        "data_gaps": [],
    }

    svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))

    assert result.cache_status == "stale_fallback"
    assert result.cache_hit is True
    assert "stale_fallback_warning" in result.data_gaps


def test_cache_only_real_stale_disk_peek_has_zero_mutation_and_zero_provider_calls(
    tmp_path,
):
    """A supported stale peek is terminal: cache-only never falls into destructive get()."""
    from fin_analyse.market.warm_cache import MarketDataCache

    writer = MarketDataCache(cache_dir=tmp_path)
    writer.set_snapshot(
        "000001",
        {
            "ticker": "000001",
            "venue": "SZ",
            "price": 10.0,
            "data_gaps": [],
        },
        ttl=-1,
    )
    disk_path = next(tmp_path.glob("*.json"))
    before_stat = disk_path.stat()
    before_bytes = disk_path.read_bytes()
    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        return _FakeProvider([])

    reader = MarketDataCache(cache_dir=tmp_path)
    result = MarketSnapshotService(
        data_cache=reader,
        provider_factory=provider_factory,
    ).get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))

    after_stat = disk_path.stat()
    assert result.cache_status == "stale_fallback"
    assert result.snapshot["price"] == 10.0
    assert provider_calls == 0
    assert disk_path.read_bytes() == before_bytes
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_cache_only_malformed_disk_peek_is_terminal_and_has_zero_mutation(
    tmp_path,
):
    """A malformed supported peek must not fall into destructive legacy reads."""
    from fin_analyse.market.warm_cache import MarketDataCache

    writer = MarketDataCache(cache_dir=tmp_path)
    writer.set_snapshot("000001", {"ticker": "000001"}, ttl=-1)
    disk_path = next(tmp_path.glob("*.json"))
    envelope = json.loads(disk_path.read_text(encoding="utf-8"))
    envelope["_value"] = ["malformed", "snapshot"]
    disk_path.write_text(json.dumps(envelope), encoding="utf-8")
    before_stat = disk_path.stat()
    before_bytes = disk_path.read_bytes()
    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        return _FakeProvider([])

    result = MarketSnapshotService(
        data_cache=MarketDataCache(cache_dir=tmp_path),
        provider_factory=provider_factory,
    ).get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))

    after_stat = disk_path.stat()
    assert result.cache_status == "miss"
    assert result.cache_hit is False
    assert result.data_gaps == ["market_data_cache_invalid"]
    assert provider_calls == 0
    assert disk_path.read_bytes() == before_bytes
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_cache_only_exploding_supported_peek_is_terminal() -> None:
    """A supported peek that fails must not invoke either legacy cache read."""

    class ExplodingPeekCache(_FakeCache):
        def peek_snapshot(self, ticker: str):
            raise RuntimeError("corrupt read")

        def get_snapshot(self, ticker: str):
            raise AssertionError("destructive legacy read must not run")

        def get_latest_snapshot(self, ticker: str, *, allow_stale: bool = False):
            raise AssertionError("legacy stale read must not run")

    result = MarketSnapshotService(
        data_cache=ExplodingPeekCache(),
        provider_factory=lambda: _FakeProvider([]),
    ).get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))

    assert result.cache_status == "miss"
    assert result.data_gaps == ["market_data_cache_invalid"]


# ── stale fallback when provider fails ──────────────────────────────────────


def test_stale_fallback_when_provider_history_missing():
    """When provider returns empty klines, try stale fallback across sessions."""
    cache = _FakeCache()
    cache._snapshots["000001"] = None
    cache._latest_snapshots["000001"] = {
        "ticker": "000001",
        "price": 11.0,
        "cache_status": "hit",
        "cache_session": "preopen",
        "data_freshness": {},
        "data_gaps": [],
    }

    svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001"))

    assert result.cache_status == "stale_fallback"
    assert result.cache_hit is True
    assert "stale_fallback_warning" in result.data_gaps


# ── fresh fetch outputs expected schema ──────────────────────────────────────


def test_fresh_fetch_outputs_existing_schema():
    """Fresh provider fetch produces expected snapshot fields."""
    cache = _FakeCache()
    cache._snapshots["000001"] = None
    cache._financials["000001"] = {"ticker": "000001", "reports": []}
    cache._margins["000001"] = {"date": "2026-07-06"}
    cache._northbounds["000001"] = {"date": "2026-07-06"}

    svc = MarketSnapshotService(
        data_cache=cache, provider_factory=lambda: _FakeProvider(_fake_klines(120))
    )
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001"))

    # Result-level fields
    assert result.ticker == "000001"
    assert result.cache_hit is False
    assert result.cache_status in ("miss", "partial")
    assert result.error is None
    assert result.error_code is None
    assert isinstance(result.data_freshness, dict)
    assert "snapshot_at" in result.data_freshness
    assert isinstance(result.data_gaps, list)

    # Snapshot dict fields
    snap = result.snapshot
    assert "price" in snap
    assert snap["price"] is not None
    assert "ma5" in snap
    assert "ma20" in snap
    assert "rsi14" in snap
    assert "macd_histogram" in snap
    assert "flow_score" in snap
    assert "cache_status" in snap
    assert "cache_hit" in snap
    assert "cache_session" in snap


# ── to_dict compatibility ───────────────────────────────────────────────────


def test_result_to_dict_preserves_gateway_fields():
    """to_dict() output must match old gateway response shape."""
    cache = _FakeCache()
    cache._snapshots["000001"] = None
    cache._financials["000001"] = {"ticker": "000001", "reports": []}
    cache._margins["000001"] = {"date": "2026-07-06"}
    cache._northbounds["000001"] = {"date": "2026-07-06"}

    svc = MarketSnapshotService(
        data_cache=cache, provider_factory=lambda: _FakeProvider(_fake_klines(120))
    )
    result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001"))
    d = result.to_dict()

    # Top-level fields that gateway callers expect
    assert d["ticker"] == "000001"
    assert "price" in d
    assert "ma5" in d
    assert "ma20" in d
    assert "rsi14" in d
    assert "macd_histogram" in d
    assert "pe" in d
    assert "flow_score" in d
    assert "cache_status" in d
    assert "cache_hit" in d
    assert "cache_session" in d
    assert "data_freshness" in d
    assert isinstance(d["data_freshness"], dict)
    assert "data_gaps" in d
    assert isinstance(d["data_gaps"], list)
    # Error fields must be absent on success
    assert "error" not in d


# ── Provider degradation integration (additive, optional) ────────────────────


class TestProviderDegradationIntegration:
    """When provider_health is provided, provider_degradation is added to snapshot."""

    def test_no_provider_health_produces_no_degradation_field(self):
        """Without provider_health, snapshot has no provider_degradation field."""
        cache = _FakeCache()
        cache._snapshots["000001"] = {
            "ticker": "000001",
            "price": 12.50,
            "cache_status": "hit",
            "cache_hit": True,
            "cache_session": "realtime",
        }
        svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
        result = svc.get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))
        assert "provider_degradation" not in result.snapshot

    def test_with_provider_health_adds_provider_degradation(self):
        """With provider_health, snapshot includes provider_degradation payload."""
        from fin_analyse.runtime.provider_health import (
            ProviderHealthResult,
            ProviderRuntimeStatus,
        )

        health = ProviderHealthResult(
            statuses=[
                ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="eastmoney",
                    status="unavailable",
                    reason="circuit_open",
                ),
                ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="baostock",
                    status="healthy",
                ),
            ],
        )
        cache = _FakeCache()
        cache._snapshots["000001"] = {
            "ticker": "000001",
            "price": 12.50,
            "cache_status": "hit",
            "cache_hit": True,
            "cache_session": "realtime",
        }
        svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
        result = svc.get_snapshot(
            MarketSnapshotRequest(ticker="000001", data_mode="cache_only", provider_health=health)
        )
        assert "provider_degradation" in result.snapshot
        pd = result.snapshot["provider_degradation"]
        assert pd["consumer"] == "market_snapshot"
        assert pd["status"] == "degraded"
        assert pd["fallback_recommendation"] == "prefer_cache_or_stale_fallback"
        assert pd["routing_changed"] is False
        assert pd["engineering_quality_only"] is True

    def test_provider_degradation_on_cache_miss(self):
        """Provider degradation works on cache miss paths too."""
        from fin_analyse.runtime.provider_health import (
            ProviderHealthResult,
            ProviderRuntimeStatus,
        )

        health = ProviderHealthResult(
            statuses=[
                ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="eastmoney",
                    status="healthy",
                ),
            ],
        )
        cache = _FakeCache()
        cache._snapshots["000001"] = None
        cache._latest_snapshots["000001"] = None
        svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
        result = svc.get_snapshot(
            MarketSnapshotRequest(ticker="000001", data_mode="cache_only", provider_health=health)
        )
        assert "provider_degradation" in result.snapshot
        pd = result.snapshot["provider_degradation"]
        assert pd["consumer"] == "market_snapshot"
        assert pd["status"] == "healthy"

    def test_provider_degradation_does_not_change_cache_status(self):
        """Provider degradation is additive — cache_status unchanged."""
        from fin_analyse.runtime.provider_health import (
            ProviderHealthResult,
            ProviderRuntimeStatus,
        )

        health = ProviderHealthResult(
            statuses=[
                ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="eastmoney",
                    status="unavailable",
                    reason="circuit_open",
                ),
            ],
        )
        cache = _FakeCache()
        cache._snapshots["000001"] = {
            "ticker": "000001",
            "price": 12.50,
            "cache_status": "hit",
            "cache_hit": True,
            "cache_session": "realtime",
        }
        svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))
        result = svc.get_snapshot(
            MarketSnapshotRequest(ticker="000001", data_mode="cache_only", provider_health=health)
        )
        # cache_status must remain "hit" regardless of provider degradation
        assert result.cache_status == "hit"
        assert result.cache_hit is True

    def test_provider_degradation_on_cache_hit_does_not_pollute_cache(self):
        """A cache-hit request with provider_health must not leak
        provider_degradation into the cached snapshot for later requests."""
        from fin_analyse.runtime.provider_health import (
            ProviderHealthResult,
            ProviderRuntimeStatus,
        )

        cache = _FakeCache()
        cache._snapshots["000001"] = {
            "ticker": "000001",
            "price": 12.50,
            "cache_status": "hit",
            "cache_hit": True,
            "cache_session": "realtime",
        }
        svc = MarketSnapshotService(data_cache=cache, provider_factory=lambda: _FakeProvider([]))

        health = ProviderHealthResult(
            statuses=[
                ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="eastmoney",
                    status="unavailable",
                    reason="circuit_open",
                ),
            ],
        )

        # First request WITH provider_health → result has degradation
        result1 = svc.get_snapshot(
            MarketSnapshotRequest(ticker="000001", data_mode="cache_only", provider_health=health)
        )
        assert "provider_degradation" in result1.snapshot

        # Cache must NOT be polluted
        cached = cache.get_snapshot("000001")
        assert cached is not None
        assert "provider_degradation" not in cached, (
            "BUG: provider_degradation leaked into cache on cache-hit path"
        )

        # Second request WITHOUT provider_health → must NOT have degradation
        result2 = svc.get_snapshot(MarketSnapshotRequest(ticker="000001", data_mode="cache_only"))
        assert "provider_degradation" not in result2.snapshot, (
            "BUG: later request without provider_health got provider_degradation "
            "from polluted cache"
        )

    def test_provider_degradation_on_live_fetch_does_not_pollute_cache(self):
        """A live-fetch request with provider_health must not leak
        provider_degradation into the cached snapshot."""
        from fin_analyse.runtime.provider_health import (
            ProviderHealthResult,
            ProviderRuntimeStatus,
        )

        cache = _FakeCache()
        cache._snapshots["000001"] = None
        cache._financials["000001"] = {"ticker": "000001", "reports": []}
        cache._margins["000001"] = {"date": "2026-07-06"}
        cache._northbounds["000001"] = {"date": "2026-07-06"}

        health = ProviderHealthResult(
            statuses=[
                ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="eastmoney",
                    status="unavailable",
                    reason="circuit_open",
                ),
            ],
        )

        svc = MarketSnapshotService(
            data_cache=cache, provider_factory=lambda: _FakeProvider(_fake_klines(120))
        )

        # First request WITH provider_health → result has degradation
        result1 = svc.get_snapshot(MarketSnapshotRequest(ticker="000001", provider_health=health))
        assert "provider_degradation" in result1.snapshot

        # Cache must NOT be polluted
        cached = cache.get_snapshot("000001")
        assert cached is not None
        assert "provider_degradation" not in cached, (
            "BUG: provider_degradation leaked into cache on live-fetch path"
        )

        # Second request WITHOUT provider_health (now a cache hit) → must NOT have degradation
        result2 = svc.get_snapshot(MarketSnapshotRequest(ticker="000001"))
        assert "provider_degradation" not in result2.snapshot, (
            "BUG: later request without provider_health got provider_degradation "
            "from polluted cache"
        )
