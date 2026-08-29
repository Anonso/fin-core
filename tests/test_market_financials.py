from fin_analyse.market.adapter import MarketDataAdapter


def test_get_financials_returns_report_data():
    adapter = MarketDataAdapter()
    result = adapter.get_financials("600519")

    assert "ticker" in result
    assert result["ticker"] == "600519"
    assert "name" in result


def test_get_kline_returns_list():
    adapter = MarketDataAdapter()
    rows = adapter.get_history("600519", period="monthly")

    assert isinstance(rows, list)
    if rows:
        assert "date" in rows[0]
        assert "close" in rows[0]
