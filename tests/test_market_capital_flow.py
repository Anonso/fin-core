"""Tests for capital flow data."""

import pytest

from fin_analyse.market.capital_flow import (
    _latest_trading_day,
    _safe_float,
    get_capital_flow_summary,
    get_margin_detail,
    get_northbound_detail,
)


class TestSafeFloat:
    def test_number(self):
        assert _safe_float(3.14) == 3.14

    def test_none(self):
        assert _safe_float(None) is None

    def test_string(self):
        assert _safe_float("123") == 123.0


class TestLatestTradingDay:
    def test_returns_valid_date(self):
        d = _latest_trading_day()
        assert len(d) == 8
        assert d.startswith("2026")


@pytest.mark.integration
class TestRealCapitalFlow:
    def test_margin_sse(self):
        """SSE margin data for 平安银行."""
        result = get_margin_detail("000001")
        assert result["date"]
        print(
            f"\n平安银行 融资融券: date={result['date']} bal={result['margin_balance']} buy={result['margin_buy']}"
        )

    def test_northbound(self):
        """Northbound data for 平安银行."""
        result = get_northbound_detail("000001")
        assert result["date"]
        assert result["shares_held"] is not None
        print(
            f"平安银行 北向: date={result['date']} shares={result['shares_held']} pct={result['pct_of_float']}%"
        )

    def test_summary(self):
        """Combined capital flow summary."""
        result = get_capital_flow_summary("000001")
        assert "flow_score" in result
        assert 0 <= result["flow_score"] <= 100
        print(f"平安银行 资金面: score={result['flow_score']}")

    def test_akshare_provider_integration(self):
        """AKShareProvider returns CapitalFlow via new method."""
        from fin_analyse.market.providers.akshare import AKShareProvider

        p = AKShareProvider()
        flows = p.get_capital_flow("000001")
        if flows:
            cf = flows[0]
            assert cf.date
            print(
                f"AKShareProvider capital_flow: date={cf.date} northbound={cf.northbound_net} margin={cf.margin_balance}"
            )
