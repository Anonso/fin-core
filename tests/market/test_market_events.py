from fin_analyse.market.events import MarketEventProvider


def test_parse_dragon_tiger_row_to_context_record():
    provider = MarketEventProvider()
    row = {
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "TRADE_DATE": "2026-06-23 00:00:00",
        "EXPLANATION": "日涨幅偏离值达7%",
        "NET_BUY_AMT": 12345678.0,
    }

    record = provider.parse_dragon_tiger(row)

    assert record.record_id == "dragon_tiger:600519:2026-06-23"
    assert record.category == "event"
    assert record.ticker == "600519"
    assert "龙虎榜" in record.title
    assert "日涨幅偏离值达7%" in record.summary
    assert record.is_decision_factor is False
    assert record.metadata["net_buy_amount"] == 12345678.0


def test_get_dragon_tiger_uses_eastmoney_datacenter(monkeypatch):
    provider = MarketEventProvider()
    rows = [
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "TRADE_DATE": "2026-06-23 00:00:00",
            "EXPLANATION": "日涨幅偏离值达7%",
            "NET_BUY_AMT": 1.0,
        }
    ]
    calls = []

    def fake_datacenter(**kwargs):
        calls.append(kwargs)
        return rows

    monkeypatch.setattr("fin_analyse.market.events.eastmoney_datacenter", fake_datacenter)

    records = provider.get_dragon_tiger("600519", days=30)

    assert len(records) == 1
    assert records[0].ticker == "600519"
    assert calls[0]["report_name"] == "RPT_DAILYBILLBOARD_DETAILS"
    assert "600519" in calls[0]["filter_str"]


def test_parse_lockup_block_shareholder_and_hot_theme_records_are_reference_only():
    provider = MarketEventProvider()

    lockup = provider.parse_lockup_release(
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "FREE_DATE": "2026-07-01",
            "FREE_SHARES": 1000,
        }
    )
    block = provider.parse_block_trade(
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "TRADE_DATE": "2026-06-20",
            "DEAL_PRICE": 1500,
            "PREMIUM_RATIO": -2.5,
        }
    )
    holder = provider.parse_shareholder_count(
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "END_DATE": "2026-03-31",
            "HOLDER_NUM": 100000,
        }
    )
    theme = provider.parse_hot_theme(
        {"code": "600519", "name": "贵州茅台", "reason": "白酒板块走强", "date": "2026-06-23"}
    )

    for record in [lockup, block, holder, theme]:
        assert record.ticker == "600519"
        assert record.is_decision_factor is False
        assert record.summary
