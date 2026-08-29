"""MarketDataCache — cache-first seam over existing MarketCache.

Raw data cache keys (financial / margin / northbound) do NOT include
session.  Session is only used for market_snapshot composite keys so
that preopen / midday / realtime snapshots can coexist.

Provides:
- Cache-first reads for slow raw data
- Snapshot cache with session-aware keys
- get_latest_snapshot(allow_stale=True) for stale fallback
- get_snapshot_freshness() returning per-source timestamps
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from fin_analyse.market.cache import MarketCache

logger = logging.getLogger(__name__)

TTL_FINANCIAL = 86400  # 24h
TTL_MARGIN = 43200  # 12h
TTL_NORTHBOUND = 604800  # 7d
TTL_SNAPSHOT = 900  # 15min


class MarketDataCache:
    """Cache-first wrapper around MarketCache for slow market data sources."""

    def __init__(self, cache_dir: Path | None = None):
        if cache_dir is None:
            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            cache_dir = default_knowledge_base_root() / "market-cache"
        self._cache = MarketCache(cache_dir=cache_dir)
        self._session: str = "realtime"

    @property
    def session(self) -> str:
        return self._session

    @session.setter
    def session(self, value: str) -> None:
        self._session = value

    # ── key helpers (raw data: no session; snapshot: with session) ─────

    def _raw_key(self, provider: str, method: str, ticker: str) -> str:
        """Raw data key — session-independent so preopen warmup
        benefits realtime reads."""
        return self._cache.make_key(provider, method, ticker)

    def _snapshot_key(self, ticker: str) -> str:
        """Snapshot key — session-aware so preopen/midday/realtime coexist."""
        return self._cache.make_key("fin", "market_snapshot", ticker, params=self._session)

    # ── cached raw data reads (session-independent) ────────────────────

    def get_financial_time_series(self, ticker: str) -> Any:
        key = self._raw_key("akshare", "financial_time_series", ticker)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        from fin_analyse.market.valuation import get_financial_time_series as _real

        result = _real(ticker)
        self._cache.set(key, result, TTL_FINANCIAL, persist=True)
        return result

    def get_margin_detail(self, ticker: str) -> dict[str, Any]:
        key = self._raw_key("akshare", "margin_detail", ticker)
        cached = self._cache.get(key, allow_stale=True)
        if cached is not None:
            return cast("dict[str, Any]", cached)
        from fin_analyse.market.capital_flow import get_margin_detail as _real

        result = _real(ticker)
        self._cache.set(key, result, TTL_MARGIN, persist=True)
        return result

    def get_northbound_detail(self, ticker: str) -> dict[str, Any]:
        key = self._raw_key("akshare", "northbound_detail", ticker)
        cached = self._cache.get(key, allow_stale=True)
        if cached is not None:
            return cast("dict[str, Any]", cached)
        from fin_analyse.market.capital_flow import get_northbound_detail as _real

        result = _real(ticker)
        self._cache.set(key, result, TTL_NORTHBOUND, persist=True)
        return result

    # ── snapshot cache (session-aware) ─────────────────────────────────

    def get_snapshot(self, ticker: str) -> dict[str, Any] | None:
        return self._cache.get(self._snapshot_key(ticker))

    def peek_snapshot(self, ticker: str) -> tuple[dict[str, Any] | None, bool]:
        """Read snapshot without ANY side effects. Returns (data, is_fresh).

        Never deletes expired disk files, never sets/refreshes memory,
        never touches the filesystem except to read.  Freshness is
        determined from the persisted ``_expires_at`` field (wall-clock
        epoch seconds) so that a fresh disk artifact written by another
        process is correctly recognised as fresh.
        """
        import time

        key = self._snapshot_key(ticker)
        entry = self._cache._store.get(key)
        if entry is not None:
            expires_at, value = entry
            if time.monotonic() <= expires_at:
                return value, True
            return value, False
        # Not in memory — try disk with TTL check.
        loaded, disk_expires_at = self._load_disk_snapshot_with_expiry(ticker)
        if loaded is not None:
            if disk_expires_at is not None and disk_expires_at > time.time():
                return loaded, True
            return loaded, False
        return None, False

    def _load_disk_snapshot_with_expiry(
        self, ticker: str
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Load latest snapshot from disk; return (value, _expires_at)."""
        import json

        pattern = self._snapshot_key(ticker)
        disk_dir = self._cache._cache_dir
        if disk_dir is None:
            return None, None
        safe = pattern.replace("/", "_").replace("\\", "_")
        candidates = sorted(
            disk_dir.glob(f"{safe}*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                expires_at = data.get("_expires_at")
                if isinstance(expires_at, (int, float)):
                    return cast("dict[str, Any]", data.get("_value")), float(expires_at)
                return cast("dict[str, Any]", data.get("_value")), None
            except (json.JSONDecodeError, OSError):
                continue
        return None, None

    def get_latest_snapshot(
        self,
        ticker: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        """Return latest snapshot even if expired, when allow_stale=True.

        Does NOT go through MarketCache.get() which deletes expired entries.
        Reads directly from memory first (if not expired), then disk.
        """
        # Check memory first (non-expired only)
        key = self._snapshot_key(ticker)
        entry = self._cache._store.get(key)
        if entry is not None:
            expires_at, value = entry
            import time

            if time.monotonic() <= expires_at:
                return cast("dict[str, Any]", value)
            # Expired in memory — don't delete, may still want it as stale
        if allow_stale:
            # Check memory (expired OK) then disk
            if entry is not None:
                return cast("dict[str, Any]", entry[1])
            return self._load_any_disk_snapshot(ticker)
        return None

    def set_snapshot(
        self,
        ticker: str,
        data: dict[str, Any],
        ttl: int | None = None,
    ) -> None:
        # Use TTL_SNAPSHOT default, but allow ttl=0 (immediate expiry for testing)
        effective_ttl = ttl if ttl is not None else TTL_SNAPSHOT
        self._cache.set(
            self._snapshot_key(ticker),
            data,
            effective_ttl,
            persist=True,
        )

    def get_snapshot_freshness(self, ticker: str) -> dict[str, Any]:
        """Return per-source freshness timestamps for a snapshot."""
        snap = self.get_latest_snapshot(ticker, allow_stale=True)
        if not snap:
            return {
                "financial_time_series": None,
                "margin_detail": None,
                "northbound_detail": None,
                "snapshot": None,
            }
        return {
            "financial_time_series": snap.get("_freshness_financial"),
            "margin_detail": snap.get("_freshness_margin"),
            "northbound_detail": snap.get("_freshness_northbound"),
            "snapshot": snap.get("data_freshness"),
        }

    def invalidate_ticker(self, ticker: str) -> None:
        self._cache.invalidate(f":{ticker}:")

    # ── internal ───────────────────────────────────────────────────────

    def _load_any_disk_snapshot(self, ticker: str) -> dict[str, Any] | None:
        """Load the latest snapshot from disk regardless of TTL expiry."""
        import json

        pattern = self._snapshot_key(ticker)
        disk_dir = self._cache._cache_dir
        if disk_dir is None:
            return None
        safe = pattern.replace("/", "_").replace("\\", "_")
        candidates = sorted(
            disk_dir.glob(f"{safe}*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cast("dict[str, Any]", data.get("_value"))
            except (json.JSONDecodeError, OSError):
                continue
        return None
