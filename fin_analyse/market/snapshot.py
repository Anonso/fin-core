"""Market Snapshot Service — FIN internal composite snapshot seam.

Provides MarketSnapshotService.get_snapshot() as the sole upper-layer
entry point for market snapshots (technical, valuation, capital flow,
cache freshness, data gaps).  Replaces the old gateway MarketService
composite seam.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fin_analyse.market.providers.mootdx import MootdxProvider
from fin_analyse.market.technical import compute_all
from fin_analyse.market.valuation import compute_valuation, enrich_signals_with_llm


@dataclass(frozen=True)
class MarketSnapshotRequest:
    """Input for MarketSnapshotService.get_snapshot().

    Optional provider_health enables provider degradation policy evaluation
    and additive provider_degradation output in the snapshot dict.
    """

    ticker: str
    session: str = "realtime"
    data_mode: str = "cache_first"
    provider_health: Any | None = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class MarketSnapshotResult:
    """Output of MarketSnapshotService.get_snapshot()."""

    ticker: str
    snapshot: dict[str, Any]
    cache_status: str
    cache_hit: bool
    cache_session: str
    data_freshness: dict[str, Any] = field(default_factory=dict)
    data_gaps: list[str] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the snapshot dict as-is — preserves existing gateway schema.

        Callers that used to consume the old gateway dict directly should
        switch to this method for an equivalent output shape.
        """
        return self.snapshot


@dataclass(frozen=True, slots=True)
class _CachePeekOutcome:
    state: Literal["UNSUPPORTED_ADAPTER", "INVALID_PEEK_RESULT", "MISS", "FRESH", "STALE"]
    data: dict[str, Any] | None = None


def _compute_flow_score(margin: dict[str, Any], northbound: dict[str, Any]) -> float:
    """Compute flow_score from cached margin/northbound data."""
    flow_score = 50.0
    if margin.get("margin_buy") and margin.get("margin_balance"):
        try:
            mb = float(margin["margin_buy"])
            mbal = float(margin["margin_balance"])
            if mbal > 0 and mb > 0:
                ratio = mb / mbal
                if ratio > 0.05:
                    flow_score += 10
                elif ratio > 0.02:
                    flow_score += 5
        except (ValueError, TypeError):
            pass
    if northbound.get("daily_change_shares"):
        try:
            change = float(northbound["daily_change_shares"])
            if change > 0:
                flow_score += 15
            elif change < 0:
                flow_score -= 10
        except (ValueError, TypeError):
            pass
    if northbound.get("pct_of_float"):
        try:
            pct = float(northbound["pct_of_float"])
            if pct > 5:
                flow_score += 10
            elif pct > 2:
                flow_score += 5
        except (ValueError, TypeError):
            pass
    return round(min(100.0, max(0.0, flow_score)), 1)


class MarketSnapshotService:
    """Composite market snapshot — the only upper-layer snapshot seam.

    Owns cache-hit, cache-only, stale-fallback, provider fetch,
    technical indicators, valuation, capital-flow, and cache-writeback.
    Does NOT own ProviderRegistry, MarketDataCache, or individual
    providers — those remain internal implementation.
    """

    def __init__(
        self,
        *,
        data_cache: Any | None = None,
        provider_factory: Callable[[], Any] | None = None,
    ) -> None:
        from fin_analyse.market.warm_cache import MarketDataCache

        self._data_cache: Any = data_cache if data_cache is not None else MarketDataCache()
        self._provider_factory: Callable[[], Any] = (
            provider_factory if provider_factory is not None else MootdxProvider
        )

    @staticmethod
    def _safe_peek(data_cache: Any, ticker: str) -> _CachePeekOutcome:
        """Normalise the optional read-only cache contract without ambiguity."""
        peek_fn = getattr(data_cache, "peek_snapshot", None)
        if not callable(peek_fn):
            return _CachePeekOutcome("UNSUPPORTED_ADAPTER")
        try:
            result = peek_fn(ticker)
        except Exception:  # Cache corruption must remain inside the read-only boundary.
            return _CachePeekOutcome("INVALID_PEEK_RESULT")
        if not isinstance(result, tuple) or len(result) != 2:
            return _CachePeekOutcome("INVALID_PEEK_RESULT")
        data, is_fresh = result
        if data is None and is_fresh is False:
            return _CachePeekOutcome("MISS")
        if isinstance(data, dict) and is_fresh is True:
            return _CachePeekOutcome("FRESH", data)
        if isinstance(data, dict) and is_fresh is False:
            return _CachePeekOutcome("STALE", data)
        return _CachePeekOutcome("INVALID_PEEK_RESULT")

    def get_snapshot(self, request: MarketSnapshotRequest) -> MarketSnapshotResult:
        """Return a composite market snapshot for *request.ticker*.

        Behaviour:
        - Snapshot cache hit → return immediately (cache_status=hit).
        - data_mode=cache_only → stale fallback or miss; no live provider.
        - Otherwise fetch via provider, compute technical/valuation/flow,
          merge slow-data cache reads, write through snapshot cache.

        When request.provider_health is provided, evaluates
        ProviderDegradationPolicy and adds additive provider_degradation
        output without changing cache_status or fallback behaviour.
        """
        ticker = request.ticker
        session = request.session
        data_mode = request.data_mode

        # ── Optional provider degradation ──────────────────────────────────
        def _apply_degradation(
            snapshot: dict[str, Any],
            gaps: list[str],
        ) -> tuple[dict[str, Any], list[str]]:
            """Apply provider degradation policy if provider_health is provided."""
            if request.provider_health is None:
                return snapshot, gaps
            from fin_analyse.runtime.provider_degradation_policy import (
                ProviderDegradationPolicy,
            )

            decision = ProviderDegradationPolicy.evaluate(
                request.provider_health, consumer="market_snapshot"
            )
            snapshot["provider_degradation"] = decision.to_dict()
            # Merge policy data gaps without duplicating
            for g in decision.data_gaps:
                if g not in gaps:
                    gaps.append(g)
            return snapshot, gaps

        self._data_cache.session = session
        now = datetime.now(UTC).isoformat()
        data_gaps: list[str] = []
        cache_status = "miss"
        fin_ts: str | None = None
        margin_ts: str | None = None
        northbound_ts: str | None = None

        # ── Snapshot cache hit ──
        # cache_only: use _safe_peek (never deletes expired files).
        # Falls back to legacy get_snapshot only when the cache adapter
        # does not expose a callable peek_snapshot.
        cache_peek: _CachePeekOutcome | None = None
        cached_snapshot: dict[str, Any] | None
        if data_mode == "cache_only":
            cache_peek = self._safe_peek(self._data_cache, ticker)
            if cache_peek.state == "FRESH":
                cached_snapshot = dict(cache_peek.data or {})
            elif cache_peek.state == "UNSUPPORTED_ADAPTER":
                # Narrow compatibility seam for legacy public-contract fakes.
                # This never invokes a provider, but may use their old cache API.
                legacy_snapshot = self._data_cache.get_snapshot(ticker)
                if legacy_snapshot is None or isinstance(legacy_snapshot, dict):
                    cached_snapshot = cast("dict[str, Any] | None", legacy_snapshot)
                else:
                    cache_peek = _CachePeekOutcome("INVALID_PEEK_RESULT")
                    cached_snapshot = None
            else:
                cached_snapshot = None
        else:
            cached_snapshot = self._data_cache.get_snapshot(ticker)
        if cached_snapshot is not None:
            cached_snapshot = dict(cached_snapshot)
            cached_snapshot["cache_status"] = "hit"
            cached_snapshot["cache_hit"] = True
            cached_snapshot["cache_session"] = cached_snapshot.get("cache_session", session)
            cached_gaps: list[str] = list(cached_snapshot.get("data_gaps", []))
            cached_snapshot, cached_gaps = _apply_degradation(cached_snapshot, cached_gaps)
            cached_snapshot["data_gaps"] = cached_gaps
            return MarketSnapshotResult(
                ticker=ticker,
                snapshot=cached_snapshot,
                cache_status="hit",
                cache_hit=True,
                cache_session=cached_snapshot.get("cache_session", session),
                data_freshness=cached_snapshot.get("data_freshness", {}),
                data_gaps=cached_gaps,
            )

        # ── cache_only: no live provider calls ──
        if data_mode == "cache_only":
            assert cache_peek is not None
            stale: dict[str, Any] | None
            if cache_peek.state == "STALE":
                stale = dict(cache_peek.data or {})
            elif cache_peek.state == "UNSUPPORTED_ADAPTER":
                legacy_stale = self._data_cache.get_latest_snapshot(ticker, allow_stale=True)
                if legacy_stale is None or isinstance(legacy_stale, dict):
                    stale = cast("dict[str, Any] | None", legacy_stale)
                else:
                    cache_peek = _CachePeekOutcome("INVALID_PEEK_RESULT")
                    stale = None
            else:
                stale = None
            if stale is not None:
                stale = dict(stale)
                stale["cache_status"] = "stale_fallback"
                stale["cache_hit"] = True
                stale["cache_session"] = stale.get("cache_session", session)
                gaps = list(stale.get("data_gaps", []))
                if "stale_fallback_warning" not in gaps:
                    gaps.append("stale_fallback_warning")
                stale, gaps = _apply_degradation(stale, gaps)
                stale["data_gaps"] = gaps
                return MarketSnapshotResult(
                    ticker=ticker,
                    snapshot=stale,
                    cache_status="stale_fallback",
                    cache_hit=True,
                    cache_session=stale.get("cache_session", session),
                    data_freshness=stale.get("data_freshness", {}),
                    data_gaps=gaps,
                )
            miss_gap = (
                "market_data_cache_invalid"
                if cache_peek.state == "INVALID_PEEK_RESULT"
                else "market_data_cache_missing"
            )
            miss_snapshot: dict[str, Any] = {
                "ticker": ticker,
                "cache_status": "miss",
                "cache_hit": False,
                "data_freshness": {"snapshot_at": now},
                "data_gaps": [miss_gap],
            }
            miss_gaps: list[str] = [miss_gap]
            miss_snapshot, miss_gaps = _apply_degradation(miss_snapshot, miss_gaps)
            miss_snapshot["data_gaps"] = miss_gaps
            return MarketSnapshotResult(
                ticker=ticker,
                snapshot=miss_snapshot,
                cache_status="miss",
                cache_hit=False,
                cache_session=session,
                data_freshness={"snapshot_at": now},
                data_gaps=miss_gaps,
            )

        # ── Live fetch ──
        provider = self._provider_factory()
        klines = provider.get_history(ticker, days=120)
        if not klines:
            # Try stale fallback across sessions
            for try_session in [session, "preopen", "midday", "realtime"]:
                saved_session = self._data_cache.session
                try:
                    self._data_cache.session = try_session
                    stale = self._data_cache.get_latest_snapshot(ticker, allow_stale=True)
                finally:
                    self._data_cache.session = saved_session
                if stale is not None:
                    stale = dict(stale)  # copy: avoid cache pollution
                    stale["cache_status"] = "stale_fallback"
                    stale["cache_session"] = stale.get("cache_session", try_session)
                    stale["cache_hit"] = True
                    gaps = list(stale.get("data_gaps", []))
                    if "stale_fallback_warning" not in gaps:
                        gaps.append("stale_fallback_warning")
                    stale, gaps = _apply_degradation(stale, gaps)
                    stale["data_gaps"] = gaps
                    return MarketSnapshotResult(
                        ticker=ticker,
                        snapshot=stale,
                        cache_status="stale_fallback",
                        cache_hit=True,
                        cache_session=stale.get("cache_session", try_session),
                        data_freshness=stale.get("data_freshness", {}),
                        data_gaps=gaps,
                    )
            no_data_snapshot: dict[str, Any] = {
                "error": "无行情数据",
                "error_code": "MARKET_DATA_MISSING",
                "cache_status": "miss",
                "cache_hit": False,
                "data_gaps": ["market_data_missing"],
            }
            no_data_gaps: list[str] = ["market_data_missing"]
            no_data_snapshot, no_data_gaps = _apply_degradation(no_data_snapshot, no_data_gaps)
            no_data_snapshot["data_gaps"] = no_data_gaps
            return MarketSnapshotResult(
                ticker=ticker,
                snapshot=no_data_snapshot,
                cache_status="miss",
                cache_hit=False,
                cache_session=session,
                data_freshness={},
                data_gaps=no_data_gaps,
                error="无行情数据",
                error_code="MARKET_DATA_MISSING",
            )

        # ── Compute technical indicators ──
        indicators = compute_all(klines)
        rsi = [v for v in indicators.get("rsi14", []) if v is not None]
        macd = [v for v in indicators.get("macd_histogram", []) if v is not None]
        ma20_close_prices = [o.close for o in klines[-20:]]
        ma20 = (
            round(sum(ma20_close_prices) / len(ma20_close_prices), 2) if ma20_close_prices else None
        )
        ma5 = (
            round(sum(o.close for o in klines[-5:]) / min(len(klines), 5), 2)
            if len(klines) >= 5
            else None
        )
        ma30 = (
            round(sum(o.close for o in klines[-30:]) / min(len(klines), 30), 2)
            if len(klines) >= 30
            else None
        )
        ma60 = (
            round(sum(o.close for o in klines[-60:]) / min(len(klines), 60), 2)
            if len(klines) >= 60
            else None
        )

        # ── Cached slow data (margin/northbound) ──
        financials = self._data_cache.get_financial_time_series(ticker)
        if financials is None:
            data_gaps.append("financial_data_missing")
        else:
            fin_ts = now

        margin = self._data_cache.get_margin_detail(ticker)
        if not margin.get("date"):
            data_gaps.append("margin_data_missing")
        else:
            margin_ts = now

        northbound = self._data_cache.get_northbound_detail(ticker)
        if not northbound.get("date"):
            data_gaps.append("northbound_data_missing")
        else:
            northbound_ts = now

        # ── Flow score ──
        flow_score = _compute_flow_score(margin, northbound)

        # ── Valuation ──
        valuation_financials = financials if isinstance(financials, dict) else None
        valuation = compute_valuation(ticker, klines=klines, financials=valuation_financials)
        if os.environ.get("FIN_VALUATION_LLM") == "1":
            try:
                from fin_analyse.claims.openai_backend import create_backends_from_env

                backends = create_backends_from_env()
                reader = (
                    backends.get("reader")
                    or backends.get("deepseek")
                    or next(iter(backends.values()), None)
                )
                if reader:
                    valuation = enrich_signals_with_llm(valuation, backend=reader)
            except Exception:
                pass

        cache_status = "partial" if data_gaps else "miss"

        snapshot: dict[str, Any] = {
            "ticker": ticker,
            "price": klines[-1].close,
            "ma5": ma5,
            "ma20": ma20,
            "ma30": ma30,
            "ma60": ma60,
            "rsi14": round(rsi[-1], 1) if rsi else None,
            "macd_histogram": round(macd[-1], 4) if macd else None,
            "pe": valuation.get("pe"),
            "pe_percentile": valuation.get("pe_percentile"),
            "roe_trend": valuation.get("roe_trend"),
            "flow_score": flow_score,
            "signal_summary": [
                {
                    "key": s.key,
                    "label": s.label,
                    "value": s.value,
                    "trend": s.trend,
                    "strength": s.strength,
                    "direction": s.direction,
                }
                for s in valuation.get("signals", [])
            ],
            "valuation_narrative": valuation.get("valuation_narrative", ""),
            "cache_status": cache_status,
            "cache_session": session,
            "cache_hit": False,
            "data_freshness": {
                "financial_time_series": fin_ts,
                "margin_detail": margin_ts,
                "northbound_detail": northbound_ts,
                "snapshot_at": now,
            },
            "data_gaps": data_gaps,
            "_freshness_financial": fin_ts,
            "_freshness_margin": margin_ts,
            "_freshness_northbound": northbound_ts,
        }

        self._data_cache.set_snapshot(ticker, snapshot)

        return_snapshot = dict(snapshot)  # copy: avoid cache pollution
        return_gaps = list(data_gaps)
        return_snapshot, return_gaps = _apply_degradation(return_snapshot, return_gaps)
        return_snapshot["data_gaps"] = return_gaps

        return MarketSnapshotResult(
            ticker=ticker,
            snapshot=return_snapshot,
            cache_status=cache_status,
            cache_hit=False,
            cache_session=session,
            data_freshness=return_snapshot["data_freshness"],
            data_gaps=return_gaps,
        )
