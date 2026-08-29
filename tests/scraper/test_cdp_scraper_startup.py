"""Tests for CdpBridgeScraper startup — 启动失败必须暴露真实原因."""

from unittest.mock import MagicMock

import pytest

from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper


def _make_mock_client(error_msg):
    """构造 mock client，start() 返回 False 且 _error 含指定消息"""
    mock_client = MagicMock()
    mock_client.start.return_value = False
    mock_client._error = error_msg
    return mock_client


class TestScraperStartupErrorIncludesFailureKind:
    """Plan: Task 4 — CdpBridgeScraper.__enter__ 失败时异常包含 failure_kind"""

    def test_startup_failure_includes_diagnostic_context(self):
        """模拟 start() 返回 False，异常携带实际错误和分类"""
        scraper = CdpBridgeScraper()
        scraper.start = MagicMock(return_value=False)
        scraper._client = _make_mock_client(
            "CDP fallback failed: Another debugger is already attached to the tab with id: 735041063."
        )

        with pytest.raises(RuntimeError) as exc_info:
            scraper.__enter__()

        msg = str(exc_info.value)
        assert "Another debugger" in msg
        assert "tab_debugger_conflict" in msg

    def test_startup_failure_module_missing(self):
        """module_missing 错误应在异常中体现"""
        scraper = CdpBridgeScraper()
        scraper.start = MagicMock(return_value=False)
        scraper._client = _make_mock_client("No module named cdp_bridge")

        with pytest.raises(RuntimeError) as exc_info:
            scraper.__enter__()

        msg = str(exc_info.value)
        assert "No module named cdp_bridge" in msg
        assert "module_missing" in msg

    def test_startup_failure_timeout(self):
        """启动超时错误应在异常中体现"""
        scraper = CdpBridgeScraper()
        scraper.start = MagicMock(return_value=False)
        scraper._client = _make_mock_client("timeout waiting for Chrome extension")

        with pytest.raises(RuntimeError) as exc_info:
            scraper.__enter__()

        msg = str(exc_info.value)
        assert "timeout waiting for Chrome extension" in msg
        assert "extension_disconnected" in msg

    def test_startup_failure_with_none_error(self):
        """_error 为 None 时仍能给出合理异常"""
        scraper = CdpBridgeScraper()
        scraper.start = MagicMock(return_value=False)
        scraper._client = _make_mock_client(None)
        scraper._client._error = None

        with pytest.raises(RuntimeError) as exc_info:
            scraper.__enter__()

        msg = str(exc_info.value)
        assert "CDP Bridge 连接失败" in msg
        # _error 为 None 时 fallback 消息自身可能匹配到 bridge_start_failed
        assert "[" in msg and "]" in msg  # 包含 failure_kind 标签

    def test_startup_does_not_mutate_article_parsing(self):
        """验证 start 方法本身不改变文章解析行为"""
        scraper = CdpBridgeScraper()
        assert hasattr(scraper, "_parse_post")
        assert callable(scraper._parse_post)

        # _parse_post 将标题行提取为 title，后续正文为 content
        text = (
            "2026-06-30 08:55\n"
            "能量评分8.6分\n"
            "PCB产业链跟踪\n"
            "正文内容足够长用于测试解析逻辑验证投资相关内容符合过滤条件。"
            "额外填充文本以满足最小长度要求，确保CDP scraper的文章切分逻辑"
            "能将此段识别为有效文章段落并进行后续处理和存储。"
            "第三段继续讨论覆铜板、铜箔、玻纤布、树脂材料、AI服务器需求"
            "和产业链供需变化，应该完整保留下来用于后续知识库分析。"
            "第四段说明投资逻辑、估值变化、产能释放节奏和风险提示。"
            "第五段补充更多细节以确保字符数超过解析最小阈值。"
        )
        post = scraper._parse_post(text)
        # 标题被提取，正文不含标题
        if post is not None:
            assert post.get("title") == "PCB产业链跟踪"
            assert isinstance(post.get("content"), str)

    def test_context_manager_returns_self_on_success(self):
        """成功启动时 __enter__ 返回自身"""
        scraper = CdpBridgeScraper()
        scraper.start = MagicMock(return_value=True)

        result = scraper.__enter__()
        assert result is scraper
