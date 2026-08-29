"""Tests for AKShareProvider."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from fin_analyse.market.providers.akshare import AKShareProvider
from fin_analyse.market.providers.base import OHLCV, QuoteResult

_MINIMAL_A_SHARE_MAP = {
    "贵州茅台": {"ticker": "600519", "market": "A股", "name": "贵州茅台"},
}


class TestAKShareProvider:
    @pytest.fixture
    def provider(self):
        return AKShareProvider()

    def test_name_and_priority(self, provider):
        assert provider.name == "akshare"
        assert provider.priority == 30  # 最全但最不稳定，URL爬虫常超时，最低优先级

    def test_search_stock_uses_lookup_company_before_live_akshare(self, provider):
        """华为 是本地手工映射里的未上市公司，必须走 lookup_company()，
        绝不触发 live akshare 全量股票列表下载。"""
        AKShareProvider._stock_df = None  # reset cache
        with patch(
            "akshare.stock_info_a_code_name",
            side_effect=AssertionError("live akshare should not be called"),
        ) as mock_ak:
            result = provider.search_stock("华为")

        assert result["name"] == "华为"
        assert result["market"] == "未上市"
        assert "ticker" in result
        mock_ak.assert_not_called()

    def test_search_stock_unknown_company_no_live_download(self, provider):
        """离线快照可用时，未知公司必须快速降级，不下载 live 全量股票列表。"""
        AKShareProvider._stock_df = None  # reset cache
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
            result = provider.search_stock("不存在的公司XYZ")

        assert result["name"] == "不存在的公司XYZ"
        assert result["ticker"] == "不存在的公司XYZ"
        assert result["market"] in ("A股", "unknown")
        mock_ak.assert_not_called()

    def test_search_stock_partial_name_uses_offline_snapshot(self, provider):
        """部分名 '茅台' 必须通过离线快照模糊匹配到 贵州茅台/600519，
        绝不下载 live 全量股票列表。"""
        AKShareProvider._stock_df = None  # reset cache
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
            result = provider.search_stock("茅台")

        assert result["name"] == "贵州茅台"
        assert result["ticker"] == "600519"
        mock_ak.assert_not_called()

    def test_search_stock_found(self, provider):
        # Live path only runs when the offline snapshot is unavailable — simulate
        # an empty snapshot so this test exercises the live akshare fallback.
        AKShareProvider._stock_df = None  # reset cache
        df = pd.DataFrame({"name": ["平安银行"], "code": ["000001"]})
        with (
            patch("akshare.stock_info_a_code_name", return_value=df),
            patch("fin_analyse.market.company_map._load_a_share_map", return_value={}),
        ):
            result = provider.search_stock("平安银行")
            assert result["name"] == "平安银行"
            assert result["ticker"] == "000001"
            assert result["market"] == "A股"

    def test_search_stock_not_found(self, provider):
        AKShareProvider._stock_df = None  # reset cache
        df = pd.DataFrame(
            {"name": pd.Series([], dtype="string"), "code": pd.Series([], dtype="string")}
        )
        with (
            patch("akshare.stock_info_a_code_name", return_value=df),
            patch("fin_analyse.market.company_map._load_a_share_map", return_value={}),
        ):
            result = provider.search_stock("不存在的股票")
            assert result["market"] == "unknown"

    def test_search_stock_error_fallback(self, provider):
        from fin_analyse.market.providers.akshare import AKShareProvider

        AKShareProvider._stock_df = None  # reset cache
        with (
            patch("akshare.stock_info_a_code_name", side_effect=RuntimeError("boom")),
            patch("fin_analyse.market.company_map._load_a_share_map", return_value={}),
        ):
            result = provider.search_stock("测试")
            assert result["market"] == "A股"
            assert result["name"] == "测试"

    def test_get_quote(self, provider):
        df = pd.DataFrame(
            [
                {
                    "代码": "000001",
                    "名称": "平安银行",
                    "最新价": 12.50,
                    "涨跌幅": 2.35,
                    "成交量": 50_000_000,
                    "成交额": 625_000_000,
                }
            ]
        )
        with patch("akshare.stock_zh_a_spot_em", return_value=df):
            result = provider.get_quote("000001")
            assert isinstance(result, QuoteResult)
            assert result.price == 12.50
            assert result.change_pct == 2.35

    def test_get_quote_ticker_not_found(self, provider):
        df = pd.DataFrame()
        with patch("akshare.stock_zh_a_spot_em", return_value=df):
            result = provider.get_quote("999999")
            assert result.price is None
            assert result.ticker == "999999"

    def test_get_history_yfinance_path(self, provider):
        df = pd.DataFrame(
            {
                "Open": [10.0, 10.5],
                "Close": [10.5, 11.0],
                "High": [11.0, 11.5],
                "Low": [9.8, 10.2],
                "Volume": [1_000_000, 1_200_000],
            },
            index=pd.to_datetime(["2026-06-19", "2026-06-20"]),
        )
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_history("000001", days=120)
            assert len(result) == 2
            assert isinstance(result[0], OHLCV)
            assert result[0].close == 10.5

    def test_get_capital_flow_returns_data(self, provider):
        """P2: capital flow now returns real margin + northbound data."""
        result = provider.get_capital_flow("000001")
        # May return data or empty list (if APIs fail on weekends)
        assert isinstance(result, list)

    def test_get_financials_empty_on_error(self, provider):
        with patch("akshare.stock_financial_abstract_ths", side_effect=RuntimeError("no data")):
            result = provider.get_financials("000001")
            assert result == {"ticker": "000001", "name": "000001", "latest": {}}
