from unittest.mock import patch

from fin_analyse.market.adapter import MarketDataAdapter

_MINIMAL_A_SHARE_MAP = {
    "贵州茅台": {"ticker": "600519", "market": "A股", "name": "贵州茅台"},
}


def test_search_stock_huawei_uses_local_map_without_live_akshare():
    """MarketDataAdapter.search_stock('华为') 必须返回本地未上市映射，
    即使 live akshare 会抛错也不应被调用。"""
    adapter = MarketDataAdapter()
    with patch(
        "akshare.stock_info_a_code_name",
        side_effect=AssertionError("live akshare should not be called"),
    ) as mock_ak:
        result = adapter.search_stock("华为")

    assert result["name"] == "华为"
    assert result["market"] == "未上市"
    mock_ak.assert_not_called()


def test_search_stock_unknown_degrades_fast_without_live_akshare():
    """未知公司在离线快照可用时快速降级，返回 degraded shape，不触发 live 下载。"""
    adapter = MarketDataAdapter()
    with (
        patch(
            "akshare.stock_info_a_code_name",
            side_effect=AssertionError("live akshare should not be called"),
        ) as mock_ak,
        patch(
            "fin_analyse.market.company_map._load_a_share_map",
            return_value=_MINIMAL_A_SHARE_MAP,
        ),
    ):
        result = adapter.search_stock("不存在的公司XYZ")

    assert result["name"] == "不存在的公司XYZ"
    assert result["ticker"] == "不存在的公司XYZ"
    assert "market" in result
    mock_ak.assert_not_called()


def test_search_stock_partial_name_resolves_offline():
    """部分名 '茅台' 经 MarketDataAdapter 也应离线解析到 贵州茅台/600519，
    不触发 live akshare。"""
    adapter = MarketDataAdapter()
    with (
        patch(
            "akshare.stock_info_a_code_name",
            side_effect=AssertionError("live akshare should not be called"),
        ) as mock_ak,
        patch(
            "fin_analyse.market.company_map._load_a_share_map",
            return_value=_MINIMAL_A_SHARE_MAP,
        ),
    ):
        result = adapter.search_stock("茅台")

    assert result["name"] == "贵州茅台"
    assert result["ticker"] == "600519"
    mock_ak.assert_not_called()


def test_adapter_source_info():
    adapter = MarketDataAdapter()
    info = adapter.source_info

    assert info.source_id == "akshare"
    assert info.source_type == "market_data"
    assert 0 < info.reliability <= 1.0


def test_search_stock_returns_stock_info():
    adapter = MarketDataAdapter()
    with patch(
        "fin_analyse.market.company_map._load_a_share_map",
        return_value=_MINIMAL_A_SHARE_MAP,
    ):
        result = adapter.search_stock("贵州茅台")

    assert result["name"] == "贵州茅台"
    assert "ticker" in result


def test_get_quote_returns_price_data():
    adapter = MarketDataAdapter()
    quote = adapter.get_quote("600519")

    assert quote["ticker"] == "600519"
    assert "name" in quote
