"""A-share market data adapter — unified facade over ProviderRegistry with caching.

All actual data retrieval is delegated to ProviderRegistry; this adapter provides
the legacy dict-based API, caching, and remaining convenience methods.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, cast

from fin_analyse.ingestion.models import SourceInfo
from fin_analyse.market.cache import MarketCache
from fin_analyse.market.consensus import MarketConsensusService

logger = logging.getLogger(__name__)

# Suppress akshare download progress bars
os.environ["AKSHARE_DISABLE_PROGRESS"] = "1"


def _retry(func, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
    """Call *func* with exponential backoff on failure."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


class MarketDataAdapter:
    """Unified market-data facade.

    Delegates data retrieval to :class:`ProviderRegistry` and adds
    caching and dict-format compatibility for legacy callers.

    Callers that don't need the dict API can use :class:`ProviderRegistry`
    directly for typed returns.
    """

    def __init__(self, cache_dir: Path | None = None):
        self._stock_cache: dict[str, dict[str, Any]] = {}
        if cache_dir is None:
            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            cache_dir = default_knowledge_base_root() / "market-cache"
        self._cache = MarketCache(cache_dir=cache_dir)

        from fin_analyse.market import create_default_registry

        self._registry = create_default_registry()
        self._consensus: MarketConsensusService | None = None

    @property
    def consensus(self) -> MarketConsensusService:
        """Opt-in cross-source validation service for key decision paths."""
        if self._consensus is None:
            self._consensus = MarketConsensusService(list(self._registry.providers))
        return self._consensus

    @property
    def source_info(self) -> SourceInfo:
        return SourceInfo(
            source_id="akshare",
            name="A股行情",
            source_type="market_data",
            reliability=0.95,
            freshness_policy="realtime",
        )

    # ------------------------------------------------------------------
    # Cache helpers (delegated to MarketCache)
    # ------------------------------------------------------------------

    def _read_cache(self, key: str, max_age_seconds: float | None = 3600) -> dict | None:
        """Read cached data; returns None if expired or missing."""
        result: Any = self._cache.get(f"adapter:{key}")
        if isinstance(result, dict):
            return result
        return None

    def _write_cache(self, key: str, data: dict) -> None:
        """Write data to cache with TTL; persists to disk."""
        ttl = int(max(300, data.get("_cache_ttl", 3600)))
        self._cache.set(f"adapter:{key}", data, ttl_seconds=ttl, persist=True)

    # ------------------------------------------------------------------
    # Stock search — delegate to registry
    # ------------------------------------------------------------------

    def search_stock(self, name_or_ticker: str) -> dict[str, Any]:
        """搜索股票，返回 name/ticker/market. 委托给 ProviderRegistry."""
        try:
            result: Any = self._registry.execute("search_stock", name_or_ticker)
            return cast(dict[str, Any], result)
        except Exception:
            return {"name": name_or_ticker, "ticker": name_or_ticker, "market": "A股"}

    # ------------------------------------------------------------------
    # Real-time quote — delegate to registry + cache fallback
    # ------------------------------------------------------------------

    def get_quote(self, ticker: str) -> dict[str, Any]:
        """获取实时行情，委托给 ProviderRegistry，失败回退缓存。"""
        try:
            result = self._registry.execute("get_quote", ticker)
            # Convert QuoteResult to legacy dict format
            quote_dict = {
                "ticker": ticker,
                "name": str(result.name or ticker),
                "price": float(result.price or 0),
                "change_pct": float(result.change_pct or 0),
                "volume": float(result.volume or 0),
                "turnover": float(result.turnover or 0) if result.turnover else None,
                "source": "provider_registry",
            }
            if result.price is not None and result.price > 0:
                self._write_cache(f"quote_{ticker}", quote_dict)
            return quote_dict
        except Exception:
            pass

        # Cache fallback (up to 24h old)
        cached = self._read_cache(f"quote_{ticker}", max_age_seconds=86400)
        if cached:
            cached["source"] = cached.get("source", "cache")
            cached["stale"] = True
            return cached

        return {"ticker": ticker, "name": ticker, "price": None, "change_pct": None}

    # ------------------------------------------------------------------
    # Historical data — delegate to registry + cache fallback
    # ------------------------------------------------------------------

    def get_history(self, ticker: str, period: str = "monthly") -> list[dict[str, Any]]:
        """获取历史 K 线，委托给 ProviderRegistry，失败回退缓存。"""
        try:
            ohlcv_list: Any = self._registry.execute("get_history", ticker, days=120)
            results = [
                {
                    "date": o.date,
                    "open": o.open,
                    "close": o.close,
                    "high": o.high,
                    "low": o.low,
                    "volume": o.volume,
                    "source": "provider_registry",
                }
                for o in ohlcv_list
            ]
            if results:
                self._write_cache(f"history_{ticker}", {"data": results})
            return results
        except Exception:
            pass

        # Cache fallback (no max age — stale is better than nothing)
        cached = self._read_cache(f"history_{ticker}", max_age_seconds=None)
        if cached and cached.get("data"):
            for item in cached["data"]:
                item["source"] = "cache"
                item["stale"] = True
            return cast(list[dict[str, Any]], cached["data"])

        return []

    # ------------------------------------------------------------------
    # Financial data — delegated to registry (only AKShareProvider supports it)
    # ------------------------------------------------------------------

    def get_financials(self, ticker: str) -> dict[str, Any]:
        """获取财务数据，委托给 ProviderRegistry，失败回退缓存。"""
        try:
            result = self._registry.execute("get_financials", ticker)
            # Provider may return a dict or a typed object
            if isinstance(result, dict):
                fin_dict = dict(result)
            else:
                fin_dict = {
                    "ticker": ticker,
                    "name": ticker,
                    "latest": getattr(result, "latest", {}) or {},
                }
            self._write_cache(f"financials_{ticker}", fin_dict)
            return fin_dict
        except Exception:
            cached = self._read_cache(f"financials_{ticker}", max_age_seconds=86400 * 7)
            if cached:
                cached["source"] = "cache"
                cached["stale"] = True
                return cached
            return {"ticker": ticker, "name": ticker, "latest": {}}

    # ------------------------------------------------------------------
    # Helpers (kept for backward compatibility)
    # ------------------------------------------------------------------

    @staticmethod
    def _to_yfinance_symbol(ticker: str) -> str:
        """Convert A-share ticker to yfinance symbol."""
        if ticker.isdigit() and len(ticker) == 6:
            prefix = "0" if ticker.startswith(("6", "9")) else "1"
            return f"{ticker}.{'SS' if prefix == '0' else 'SZ'}"
        return ticker
