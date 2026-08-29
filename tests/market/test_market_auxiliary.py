from fin_analyse.market.auxiliary import AuxiliaryDataProvider


def test_parse_stock_news_reference_context():
    provider = AuxiliaryDataProvider()
    record = provider.parse_stock_news(
        {
            "code": "600519",
            "title": "贵州茅台发布新品",
            "showTime": "2026-06-23 09:00:00",
            "url": "https://example.com/news",
            "summary": "新品发布",
        },
        ticker="600519",
    )

    assert record.category == "news"
    assert record.ticker == "600519"
    assert "新品" in record.summary
    assert record.is_decision_factor is False


def test_parse_dividend_reference_context():
    provider = AuxiliaryDataProvider()
    record = provider.parse_dividend(
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "REPORT_DATE": "2026-06-23",
            "ASSIGNDSCRPT": "10派300元",
        }
    )

    assert record.category == "dividend"
    assert record.ticker == "600519"
    assert "10派300元" in record.summary


def test_parse_fund_flow_reference_context():
    provider = AuxiliaryDataProvider()
    record = provider.parse_fund_flow(
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "TRADE_DATE": "2026-06-23",
            "MAIN_NET_INFLOW": 1000000,
        }
    )

    assert record.category == "capital"
    assert record.ticker == "600519"
    assert record.metadata["main_net_inflow"] == 1000000
