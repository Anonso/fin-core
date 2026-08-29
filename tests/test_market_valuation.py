"""Tests for valuation & financial depth analysis."""

import pytest

from fin_analyse.market.valuation import (
    _compute_cagr,
    _compute_percentile,
    _safe_float,
    compute_valuation,
    get_financial_time_series,
)


class TestSafeFloat:
    def test_plain_number(self):
        assert _safe_float(3.14) == 3.14
        assert _safe_float(42) == 42.0

    def test_yi_unit(self):
        assert _safe_float("145.23亿") == pytest.approx(145.23e8)

    def test_percent(self):
        assert _safe_float("41.17%") == pytest.approx(0.4117)

    def test_none_and_empty(self):
        assert _safe_float(None) is None
        assert _safe_float("") is None
        assert _safe_float("False") is None


class TestPercentile:
    def test_basic(self):
        assert _compute_percentile([1, 2, 3, 4, 5], 3) == 40.0  # 2 of 5 below

    def test_all_below(self):
        assert _compute_percentile([1, 2, 3], 10) == 100.0

    def test_all_above(self):
        assert _compute_percentile([1, 2, 3], 0) == 0.0

    def test_empty(self):
        assert _compute_percentile([], 5) == 50.0


class TestCAGR:
    def test_basic(self):
        # 100 → 133.1 over 3 years = 10% CAGR
        assert _compute_cagr(100, 133.1, 3) == pytest.approx(0.1, abs=0.01)

    def test_zero_start(self):
        assert _compute_cagr(0, 100, 3) is None

    def test_negative_growth(self):
        assert _compute_cagr(100, 50, 2) == pytest.approx(-0.2929, abs=0.01)


@pytest.mark.integration
class TestRealFinancialData:
    """Integration tests with real akshare financial data."""

    def test_fetch_financials(self):
        """Fetch real financial time series for 平安银行."""
        result = get_financial_time_series("000001")
        assert result["ticker"] == "000001"
        reports = result["reports"]
        assert len(reports) >= 10
        latest = reports[-1]
        assert latest["eps"] is not None
        assert latest["roe"] is not None
        assert latest["revenue"] is not None
        print(
            f"\n平安银行 最新: EPS={latest['eps']}, ROE={latest['roe']}, "
            f"负债率={latest['debt_ratio']}, 净利率={latest['net_margin']}"
        )

    def test_compute_valuation_full(self):
        """Full valuation with financials + price data."""
        from fin_analyse.market import AKShareProvider

        provider = AKShareProvider()
        ticker = "000001"

        # Get financials + price history
        financials = get_financial_time_series(ticker)
        klines = provider.get_history(ticker, days=360)

        result = compute_valuation(ticker, klines=klines, financials=financials)

        assert result["ticker"] == ticker
        if klines:
            assert result["pe"] is not None or result["pb"] is not None
        assert result["roe_trend"] in ("up", "down", "flat", "unknown")
        assert result["cash_flow_quality"] in ("strong", "adequate", "weak", "unknown")
        assert result["eps"] is not None
        assert result["net_margin"] is not None

        print(
            f"\n完整估值: PE={result['pe']} ({result['pe_percentile']}%ile), "
            f"PB={result['pb']} ({result['pb_percentile']}%ile), "
            f"ROE趋势={result['roe_trend']}, "
            f"现金流={result['cash_flow_quality']}"
        )
        print(
            f"营收3年CAGR={result['revenue_growth_3y']}, "
            f"净利润3年CAGR={result['net_profit_growth_3y']}"
        )


class TestGrowthTrend:
    """Unit tests for _compute_growth_trend inflection detection."""

    def _make_reports(self, yoy_values: list[float | None]) -> list[dict]:
        """Build synthetic reports with revenue_yoy values."""
        reports = []
        for i, v in enumerate(yoy_values):
            period = (
                f"{(2023 + i // 4)}-{(i % 4) * 3 + 1:02d}-01"
                if i < 12
                else f"2026-{(i - 12) % 4 * 3 + 1:02d}-01"
            )
            r: dict = {"period": period}
            if v is not None:
                r["revenue_yoy"] = v
            reports.append(r)
        return reports

    def test_recovering_trend(self):
        """Detect inflection and recovering trend from decline→recovery pattern."""
        # Simulate: sustained decline → recent turnaround (协鑫能科 pattern)
        yoy = [-20, -20, -21, -22, -19.8, -15, -10, 5.4]
        reports = self._make_reports(yoy)
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "revenue")
        assert result["trend"] in ("recovering", "accelerating"), (
            f"Expected recovering/accelerating, got {result['trend']}"
        )
        assert result["latest_yoy"] == 5.4
        assert result["consecutive_quarters"] >= 3
        assert result["inflection_period"] is not None

    def test_accelerating_trend(self):
        """Detect accelerating growth when YoY deltas expand recently."""
        # Steady then accelerating: recent deltas expand (3→5→8)
        yoy = [5, 5, 6, 6, 7, 10, 15, 23]  # inflection at 7→10→15
        reports = self._make_reports(yoy)
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "revenue")
        assert result["trend"] in ("accelerating", "recovering"), (
            f"Expected accelerating/recovering, got {result['trend']}"
        )
        assert result["latest_yoy"] == 23

    def test_declining_trend(self):
        """Detect persistent decline without inflection."""
        yoy = [5, 3, 1, -2, -5, -8, -12, -15]
        reports = self._make_reports(yoy)
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "revenue")
        assert result["trend"] == "declining"
        assert result["latest_yoy"] == -15

    def test_stable_trend(self):
        """Flat YoY with small noise should return stable."""
        yoy = [3, 2.5, 3.2, 2.8, 3.0, 3.1, 2.9, 3.0]
        reports = self._make_reports(yoy)
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "revenue")
        # Small fluctuations (<2pp) should be filtered as noise → stable
        assert result["trend"] == "stable", f"Expected stable, got {result['trend']}"

    def test_decelerating_trend(self):
        """Still positive but growth rate consistently shrinking."""
        yoy = [20, 18, 15, 12, 10, 8, 6, 4]
        reports = self._make_reports(yoy)
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "revenue")
        # No inflection, sequential decline but all positive → decelerating
        assert result["trend"] == "decelerating", f"Expected decelerating, got {result['trend']}"
        assert result["latest_yoy"] == 4

    def test_insufficient_data(self):
        """Less than 4 quarters → unknown."""
        yoy = [-15, -10, -3]
        reports = self._make_reports(yoy)
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "revenue")
        assert result["trend"] == "unknown"

    def test_profit_trend_field(self):
        """Works with net_profit field prefix and detects recent recovery."""
        # Long decline then recent sharp recovery
        yoy = [-15, -15, -14, -13, -12, -8, -2, 10]
        reports = []
        for i, v in enumerate(yoy):
            period = f"{(2023 + i // 4)}-{(i % 4) * 3 + 1:02d}-01"
            reports.append({"period": period, "net_profit_yoy": v})
        from fin_analyse.market.valuation import _compute_growth_trend

        result = _compute_growth_trend(reports, "net_profit")
        assert result["trend"] in ("recovering", "accelerating"), (
            f"Expected recovering/accelerating, got {result['trend']}"
        )
        assert result["latest_yoy"] == 10


class TestSignalSorting:
    """Test that signals are sorted by strength descending and old fields preserved."""

    def test_signals_sorted_by_strength(self):
        """Top signal should have highest strength."""
        from fin_analyse.market.valuation import compute_valuation

        reports = []
        for i in range(12):
            period = f"{(2023 + i // 4)}-{(i % 4) * 3 + 1:02d}-28"
            reports.append(
                {
                    "period": period,
                    "revenue": 100 + i * 5,
                    "revenue_yoy": 5.0 + i * 2,
                    "net_profit": 10 + i,
                    "net_profit_yoy": 3.0 + i,
                    "eps": 1.0 + i * 0.1,
                    "bps": 8.0 + i * 0.3,
                    "roe": 12.0 + i * 0.5,
                    "debt_ratio": 60 - i * 2,
                    "net_margin": 10 + i * 0.5,
                    "cf_per_share": 1.5,
                }
            )

        result = compute_valuation("000001", financials={"ticker": "000001", "reports": reports})
        signals = list(result.get("signals", []))  # materialize
        assert len(signals) > 0, "Should have at least one signal"
        for i in range(len(signals) - 1):
            assert signals[i].strength >= signals[i + 1].strength, (
                f"Signal {signals[i].key}({signals[i].strength}) should be >= "
                f"{signals[i + 1].key}({signals[i + 1].strength})"
            )

    def test_old_fields_preserved(self):
        """Backward compat: old flat fields still exist."""
        from fin_analyse.market.valuation import compute_valuation

        reports = [
            {
                "period": "2025-12-31",
                "revenue": 100,
                "revenue_yoy": 5.0,
                "net_profit": 10,
                "net_profit_yoy": 3.0,
                "eps": 1.0,
                "bps": 8.0,
                "roe": 12.0,
                "debt_ratio": 60,
                "net_margin": 10,
                "cf_per_share": 1.5,
            },
        ]
        result = compute_valuation("000001", financials={"ticker": "000001", "reports": reports})
        assert "pe" in result
        assert "roe_trend" in result
        assert "revenue_growth_3y" in result  # deprecated but present
        assert "signals" in result
        assert "signal_map" in result
        assert "valuation_narrative" in result

    def test_signal_map_lookup(self):
        """signal_map provides O(1) access to signals by key."""
        from fin_analyse.market.valuation import compute_valuation

        reports = []
        for i in range(8):
            period = f"{(2024 + i // 4)}-{(i % 4) * 3 + 1:02d}-28"
            reports.append(
                {
                    "period": period,
                    "revenue": 100 + i * 5,
                    "revenue_yoy": 5.0 + i,
                    "net_profit": 10 + i,
                    "net_profit_yoy": 3.0 + i,
                    "eps": 1.0 + i * 0.1,
                    "bps": 8.0 + i * 0.3,
                    "roe": 12.0 + i * 0.5,
                    "debt_ratio": 60 - i * 2,
                    "net_margin": 10 + i * 0.5,
                    "cf_per_share": 1.5,
                }
            )

        result = compute_valuation("000001", financials={"ticker": "000001", "reports": reports})
        smap = result.get("signal_map", {})
        for key in ["pe_percentile", "revenue_trend", "debt_ratio", "cash_flow_quality"]:
            assert key in smap, f"signal_map missing key: {key}"
            assert smap[key].key == key


class TestLLMFallback:
    """Test that LLM enrichment falls back gracefully."""

    def test_fallback_summary(self):
        from fin_analyse.market.valuation import _fallback_summary

        s = _fallback_summary("pb_percentile", "extreme_low", 5.8, "%")
        assert "5.8" in s
        assert len(s) > 0

    def test_fallback_summary_all_trends(self):
        """All trend values produce non-empty summaries."""
        from fin_analyse.market.valuation import _fallback_summary

        trends = [
            "extreme_low",
            "extreme_high",
            "recovering",
            "accelerating",
            "decelerating",
            "declining",
            "stable",
            "improving",
            "deteriorating",
        ]
        for t in trends:
            s = _fallback_summary("test", t, 10.0, "%")
            assert s, f"Empty fallback for trend: {t}"

    def test_fallback_summary_none_value(self):
        from fin_analyse.market.valuation import _fallback_summary

        s = _fallback_summary("test", "unknown", None, "%")
        assert "缺失" in s

    def test_fallback_narrative(self):
        from fin_analyse.market.valuation import ValuationSignal, _fallback_narrative

        signals = [
            ValuationSignal(
                key="pb_percentile",
                label="PB 分位",
                category="valuation",
                value=5.8,
                unit="%",
                trend="extreme_low",
                strength=0.94,
                direction="positive",
                summary="极端低位",
            ),
        ]
        narrative = _fallback_narrative("000001", signals)
        assert "000001" in narrative
        assert "PB 分位" in narrative

    def test_enrich_without_backend_uses_fallback(self):
        """enrich_signals_with_llm with no backend fills fallback values."""
        from fin_analyse.market.valuation import ValuationSignal, enrich_signals_with_llm

        result: dict = {
            "ticker": "000001",
            "signals": [
                ValuationSignal(
                    key="pb_percentile",
                    label="PB 分位",
                    category="valuation",
                    value=5.8,
                    unit="%",
                    trend="extreme_low",
                    strength=0.94,
                    direction="positive",
                    summary="",
                ),
            ],
        }
        enriched = enrich_signals_with_llm(result, backend=None)
        assert enriched["signals"][0].summary != ""
        assert enriched["valuation_narrative"] != ""

    def test_enrich_empty_signals(self):
        """No signals → graceful empty result."""
        from fin_analyse.market.valuation import enrich_signals_with_llm

        result = {"ticker": "000001", "signals": []}
        enriched = enrich_signals_with_llm(result, backend=None)
        assert enriched["valuation_narrative"] == "无财务数据"
