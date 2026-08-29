"""Tests for ZSXQ incremental scraper v2"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fin_analyse.scraper.browser import BrowserManager, ZsxqApiAuthError
from fin_analyse.scraper.scraper import ZsxqScraper

TZ = timezone(timedelta(hours=8))


def image(src: str, index: int = 0) -> dict:
    return {"src": src, "date": "", "index": index}


class TestBrowserExtraction:
    def test_extracts_real_short_code_article_url_from_nested_topic(self):
        topic = {
            "topic_id": "22255248888812480",
            "talk": {
                "article": {"inline_article_url": "https://articles.zsxq.com/id_k7065nedkkwt.html"}
            },
        }

        assert (
            BrowserManager.extract_article_url_from_topic(topic)
            == "https://articles.zsxq.com/id_k7065nedkkwt.html"
        )

    def test_extracts_short_code_url_from_text_without_synthesizing_topic_id(self):
        text = "正文入口 https://articles.zsxq.com/id_k7065nedkkwt.html topic=22255248888812480"

        assert BrowserManager.extract_article_urls_from_text(text) == [
            "https://articles.zsxq.com/id_k7065nedkkwt.html"
        ]
        assert (
            BrowserManager.extract_article_url_from_topic(
                {"topic_id": "22255248888812480", "text": text}
            )
            == "https://articles.zsxq.com/id_k7065nedkkwt.html"
        )
        assert (
            BrowserManager.extract_article_url_from_topic({"topic_id": "22255248888812480"}) is None
        )

    def test_extracts_images_from_talk_and_qa_payloads(self):
        talk_topic = {"talk": {"images": [{"original": {"url": "https://images.zsxq.com/a.png"}}]}}
        qa_topic = {
            "question": {"images": [{"large": {"url": "https://images.zsxq.com/q.png"}}]},
            "answer": {"images": [{"original": {"url": "https://images.zsxq.com/a.png"}}]},
        }

        assert BrowserManager.extract_images_from_topic_payload(talk_topic, "talk") == [
            image("https://images.zsxq.com/a.png")
        ]
        assert BrowserManager.extract_images_from_topic_payload(qa_topic, "q&a") == [
            image("https://images.zsxq.com/q.png"),
            image("https://images.zsxq.com/a.png", index=1),
        ]

    def test_fetch_api_raises_auth_error_on_401(self):
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.url = "https://wx.zsxq.com/"
        browser.page.title.return_value = "test"
        browser.page.screenshot.return_value = b"fake"
        browser.page.evaluate.return_value = {"status": 401, "body": {"succeeded": False}}

        with (
            patch("fin_analyse.scraper.config.DEBUG_DIR", MagicMock()),
            pytest.raises(ZsxqApiAuthError),
        ):
            browser.fetch_api("https://api.zsxq.com/v2/groups/x/topics")

    def test_fetch_article_content_prefers_browser_page_over_requests(self):
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.evaluate.return_value = {
            "text": "正文标题\n这是一段通过真实浏览器渲染出来的文章正文" * 10,
            "images": [image("https://article-images.zsxq.com/a.png")],
        }

        with patch("requests.Session") as session_cls:
            text, images = browser.fetch_article_content(
                "https://articles.zsxq.com/id_k7065nedkkwt.html"
            )

        browser.page.goto.assert_called_once_with(
            "https://articles.zsxq.com/id_k7065nedkkwt.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        session_cls.assert_not_called()
        assert "真实浏览器渲染" in text
        assert images == [image("https://article-images.zsxq.com/a.png")]

    def test_fetch_article_content_falls_back_to_requests_when_browser_page_fails(self):
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.goto.side_effect = RuntimeError("blocked")
        browser.page.url = "https://wx.zsxq.com/"
        browser.page.title.return_value = "blocked"
        browser.page.screenshot.return_value = b"fake"
        browser.page.evaluate.return_value = "body text"
        browser.context = MagicMock()
        browser.context.cookies.return_value = [{"name": "zsxq_access_token", "value": "token"}]

        response = MagicMock()
        response.status_code = 200
        response.text = """
        <html><body><h1>标题</h1><p>requests 兜底正文</p>
        <img src="https://article-images.zsxq.com/a.png" /></body></html>
        """
        session = MagicMock()
        session.get.return_value = response

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("requests.Session", return_value=session),
                patch("fin_analyse.scraper.config.DEBUG_DIR", Path(tmpdir) / "debug"),
            ):
                text, images = browser.fetch_article_content(
                    "https://articles.zsxq.com/id_k7065nedkkwt.html"
                )

            # 应保存诊断
            debug_files = list((Path(tmpdir) / "debug").rglob("*"))
            screenshots = [f for f in debug_files if f.suffix == ".png"]
            assert len(screenshots) == 1

        session.cookies.set.assert_called_once_with("zsxq_access_token", "token")
        assert "requests 兜底正文" in text
        assert images == [image("https://article-images.zsxq.com/a.png")]

    def test_save_page_diagnostics_writes_screenshot_and_state_on_failure(self, tmp_path):
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.url = "https://wx.zsxq.com/group/15522441811252"
        browser.page.title.return_value = "知识星球"
        browser.page.screenshot.return_value = b"fake_png_data"
        browser.page.evaluate.return_value = "页面正文摘要 (前 500 字符)"

        with patch("fin_analyse.scraper.config.DEBUG_DIR", tmp_path / "debug"):
            browser._save_page_diagnostics("test_context")

        debug_files = list((tmp_path / "debug").rglob("*"))
        assert len(debug_files) >= 2  # screenshot + state text
        screenshots = [f for f in debug_files if f.suffix == ".png"]
        state_files = [f for f in debug_files if f.suffix == ".txt"]
        assert len(screenshots) == 1
        assert len(state_files) == 1
        assert b"fake_png_data" in screenshots[0].read_bytes()
        state_text = state_files[0].read_text(encoding="utf-8")
        assert "test_context" in state_text
        assert "知识星球" in state_text
        assert "页面正文摘要" in state_text

    def test_fetch_api_saves_diagnostics_on_unexpected_status(self, tmp_path):
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.evaluate.return_value = {
            "status": 403,
            "body": {"code": 1001, "info": "访问太频繁"},
        }
        browser._save_page_diagnostics = MagicMock()

        with patch("fin_analyse.scraper.config.DEBUG_DIR", tmp_path / "debug"):
            result = browser.fetch_api("https://api.zsxq.com/v2/groups/x/topics")

        assert result == {}  # non-2xx, non-auth: returns empty
        browser._save_page_diagnostics.assert_called_once()
        diag_context = browser._save_page_diagnostics.call_args[0][0]
        assert "403" in diag_context
        assert "1001" in diag_context

    def test_fetch_topic_detail_from_dom_extracts_qa_content(self):
        """Layer 2: navigate to topic page, extract Q&A from DOM."""
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.inner_text = MagicMock(
            return_value=(
                "笔记\n管理后台\n"
                "三线文案大锅饭\n2026-06-18 13:18\n"
                "煎饼果子 提问：锅师好，英伟达入局AI PC...\n"
                "免责声明：锅师和助理们不是财务顾问...\n\n"
                "是的，英伟达已经正式入局，而且确实以王者姿态强势切入。\n" * 10
            )
        )
        browser.page.evaluate = MagicMock(return_value=True)

        result = browser.fetch_topic_detail_from_dom("14422412282844542")

        assert result is not None
        assert result["type"] == "q&a"
        assert "英伟达" in result.get("question", {}).get("text", "")
        assert "王者姿态" in result.get("answer", {}).get("text", "")
        browser.page.goto.assert_called_once()

    def test_fetch_topic_detail_from_dom_returns_none_on_no_permission(self):
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()
        browser.page.inner_text = MagicMock(return_value="没有查看该主题的权限\n知识星球")
        browser.page.evaluate = MagicMock(return_value=True)

        result = browser.fetch_topic_detail_from_dom("55522542514422260")

        assert result is None

    def test_fetch_topic_detail_payload_falls_back_to_dom(self):
        """When API raises 1059, fall back to DOM extraction."""
        browser = BrowserManager(headless=True)
        browser.page = MagicMock()

        call_count = [0]

        def fake_evaluate(script, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "status": 200,
                    "body": {"succeeded": False, "code": 1059, "info": "不支持非官方工具"},
                }
            return True

        browser.page.evaluate = fake_evaluate
        browser.page.url = "https://wx.zsxq.com/"
        browser.page.title.return_value = "test"
        browser.page.screenshot.return_value = b"fake"

        # Realistic page text with Q&A + footer
        browser.page.inner_text = MagicMock(
            return_value=(
                "三线文案大锅饭\n"
                "2026-06-18 13:18\n"
                "小明 提问：英伟达入局AI PC赛道会带来什么变化？\n"
                "\n"
                "是的，英伟达已经正式入局，而且确实以王者姿态强势切入。\n"
                "这会带动整个供应链扩张。\n"
                "\n"
                "等22人觉得很赞\n"
                "知识星球\n"
            )
        )

        with patch("fin_analyse.scraper.config.DEBUG_DIR", MagicMock()):
            result = browser.fetch_topic_detail_payload("12345")

        assert result is not None
        assert result["type"] == "q&a"
        assert "英伟达入局" in result.get("question", {}).get("text", "")
        assert "王者姿态" in result.get("answer", {}).get("text", "")


class TestCollectTopics:
    """测试 _collect_topics 分页收集逻辑"""

    def make_topic(self, topic_id: str, hours_ago: int, title: str = "test"):
        """构造一个 mock topic"""
        dt = datetime.now(TZ) - timedelta(hours=hours_ago)
        return {
            "topic_id": topic_id,
            "create_time": dt.strftime("%Y-%m-%d %H:%M"),
            "title": title,
        }

    def test_stops_at_window_boundary(self):
        """当遇到超过 3 天窗口的 topic 时停止收集"""
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()

        # 第1页：2个在窗口内，1个在窗口外
        page1 = [
            self.make_topic("1", 10),  # 10小时前 → 在窗口内
            self.make_topic("2", 50),  # 50小时前 → 在窗口内
            self.make_topic("3", 80),  # 80小时前 → 超出3天(72h)
        ]
        scraper._browser.fetch_topics_by_scope.return_value = page1

        result = scraper._collect_topics("all")

        # 应该只收集到前2个（第3个超出窗口）
        assert len(result) == 2
        assert result[0]["topic_id"] == "1"
        assert result[1]["topic_id"] == "2"
        # 不应该继续请求第2页
        assert scraper._browser.fetch_topics_by_scope.call_count == 1

    def test_deduplicates_by_topic_id(self):
        """同一 topic_id 只保留一次"""
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()

        page1 = [
            self.make_topic("dup", 5),
            self.make_topic("dup", 5),  # 重复
            self.make_topic("uniq", 10),
        ]
        scraper._browser.fetch_topics_by_scope.side_effect = [page1, []]

        result = scraper._collect_topics("digests")

        assert len(result) == 2
        tids = [t["topic_id"] for t in result]
        assert tids.count("dup") == 1

    def test_empty_api_returns_empty(self):
        """API 无数据时返回空列表"""
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()
        scraper._browser.fetch_topics_by_scope.return_value = []

        result = scraper._collect_topics("all")
        assert result == []

    def test_paginates_until_out_of_window(self):
        """多页数据：第1页全在窗口，第2页才遇到超窗口"""
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()

        page1 = [self.make_topic(f"p1_{i}", i * 5) for i in range(1, 7)]  # 5h~30h
        page2 = [self.make_topic("old", 90)]  # 90h 前 → 超窗口

        scraper._browser.fetch_topics_by_scope.side_effect = [page1, page2, []]

        result = scraper._collect_topics("all")

        # 应该包含第1页全部6个 + 第2页之前停止
        assert len(result) == 6


class TestDescribeImage:
    """测试 describe_image LLM 函数"""

    def test_imports_cleanly(self):
        """模块可正常导入"""
        from fin_analyse.scraper.downloader import describe_image

        assert callable(describe_image)

    def test_returns_empty_for_missing_file(self):
        """图片文件不存在时返回空字符串"""
        from fin_analyse.scraper.downloader import describe_image

        result = describe_image("/nonexistent/path.jpg")
        assert result == ""

    def test_handles_api_error_gracefully(self, monkeypatch):
        """API 出错时返回空字符串，不抛异常（mock vision client 注入）"""
        from fin_analyse.scraper import downloader
        from fin_analyse.scraper.downloader import describe_image

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API error")
        monkeypatch.setattr(
            downloader,
            "_get_vision_clients",
            lambda: [(mock_client, "mock-model", "mimo", 30, 1536)],
        )
        # 隔离全局熔断器，避免测试向真实 breaker 记失败
        monkeypatch.setattr(
            "fin_analyse.claims.backend_health.get_backend_circuit_breaker",
            lambda: None,
        )

        # 使用一个临时存在的图片文件
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            tmp_path = f.name

        try:
            result = describe_image(tmp_path)
            assert result == ""
        finally:
            Path(tmp_path).unlink()

    def test_returns_empty_without_api_key(self):
        """没有 API key 时返回空字符串"""
        from fin_analyse.scraper.downloader import describe_image

        with patch.dict("os.environ", {}, clear=True):
            result = describe_image("/tmp/test.png")
            assert result == ""


class TestCompletenessRepair:
    def test_existing_short_incomplete_topic_is_processed(self):
        scraper = ZsxqScraper(headless=True)
        existing = {"topic-1": {"topic_id": "topic-1", "char_count": 60, "path": "/tmp/a.md"}}

        should_process, reason = scraper._should_process_topic({"topic_id": "topic-1"}, existing)

        assert should_process is True
        assert reason == "repair"

    def test_existing_complete_topic_is_skipped(self):
        scraper = ZsxqScraper(headless=True)
        existing = {
            "topic-1": {
                "topic_id": "topic-1",
                "char_count": 5000,
                "completeness_version": 1,
                "incomplete": False,
            }
        }

        should_process, reason = scraper._should_process_topic({"topic_id": "topic-1"}, existing)

        assert should_process is False
        assert reason == "complete"

    def test_resolve_topic_prefers_discovered_short_code_article(self):
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()
        scraper._browser.fetch_topic_detail_payload.return_value = {
            "type": "talk",
            "talk": {
                "text": "短摘要",
                "article": {"inline_article_url": "https://articles.zsxq.com/id_k7065nedkkwt.html"},
            },
        }
        scraper._browser.extract_article_url_from_topic = (
            BrowserManager.extract_article_url_from_topic
        )
        scraper._browser.extract_images_from_topic_payload = (
            BrowserManager.extract_images_from_topic_payload
        )
        scraper._browser.fetch_article_content.return_value = (
            "完整长文" * 200,
            [image("https://article-images.zsxq.com/a.png")],
        )

        result = scraper._resolve_topic_content(
            {"topic_id": "topic-1", "type": "talk"}, "topic-1", "talk"
        )

        assert result["content_source"] == "article_html"
        assert result["article_url"] == "https://articles.zsxq.com/id_k7065nedkkwt.html"
        assert result["incomplete"] is False
        assert result["images"] == [image("https://article-images.zsxq.com/a.png")]


class TestIncrementalIntegration:
    """测试 run_incremental 整合逻辑"""

    def test_existing_incomplete_topic_is_repaired(self):
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()
        scraper._browser.navigate_to_main_feed = MagicMock()
        scraper._browser.navigate_to_digests = MagicMock()
        scraper._browser.expand_all_articles = MagicMock()
        scraper._collect_topics = MagicMock(
            side_effect=[
                [
                    {
                        "topic_id": "topic-1",
                        "create_time": "2026-06-22 10:00",
                        "title": "星大派特刊",
                        "type": "talk",
                    }
                ],
                [],
            ]
        )
        scraper.load_index_ids = MagicMock(return_value=set())
        scraper.load_index_topic_ids = MagicMock(return_value={"topic-1"})
        scraper.load_index_articles_by_topic_id = MagicMock(
            return_value={
                "topic-1": {"topic_id": "topic-1", "char_count": 60, "path": "/tmp/old.md"}
            }
        )
        scraper._resolve_topic_content = MagicMock(
            return_value={
                "text": "完整正文" * 100,
                "images": [],
                "article_url": "https://articles.zsxq.com/id_k7065nedkkwt.html",
                "content_source": "article_html",
                "incomplete": False,
                "incomplete_reason": "",
            }
        )
        scraper._is_investment_relevant = MagicMock(return_value=True)
        scraper.parse_posts = MagicMock(
            return_value=[
                {
                    "id": "newid",
                    "date": "2026-06-22 10:00",
                    "score": None,
                    "column": "星大派特刊",
                    "companies": [],
                    "tags": [],
                    "is_qa": False,
                    "title": "星大派特刊",
                    "content": "完整正文" * 100,
                    "char_count": 400,
                }
            ]
        )
        scraper._process_article_images = MagicMock(return_value=[])
        scraper.save_article = MagicMock()
        scraper.update_index = MagicMock(return_value=1)

        scraper.run_incremental(detail=True, ocr=True)

        scraper._resolve_topic_content.assert_called_once()
        scraper.save_article.assert_called_once()
        saved_post = scraper.update_index.call_args[0][0][0]
        assert saved_post["content_source"] == "article_html"
        assert saved_post["incomplete"] is False

    def test_merge_dedup_main_and_digest(self):
        """首页和精华页的 topic 按 topic_id 合并去重"""
        scraper = ZsxqScraper(headless=True)
        scraper._browser = MagicMock()
        scraper._browser.fetch_topics_by_scope = MagicMock()

        # Mock _collect_topics 返回模拟数据
        main_topics = [
            {"topic_id": "1", "create_time": "2026-06-22 10:00", "title": "A"},
            {"topic_id": "2", "create_time": "2026-06-22 09:00", "title": "B"},
        ]
        digest_topics = [
            {"topic_id": "2", "create_time": "2026-06-22 09:00", "title": "B"},  # 重复
            {"topic_id": "3", "create_time": "2026-06-22 08:00", "title": "C"},  # 补漏
        ]

        scraper._collect_topics = MagicMock(side_effect=[main_topics, digest_topics])
        scraper.load_index_ids = MagicMock(return_value=set())
        scraper.load_index_topic_ids = MagicMock(return_value=set())
        scraper.load_index_articles_by_topic_id = MagicMock(return_value={})
        scraper._make_post_id = MagicMock(return_value="mock_id")

        # Patch scrape_article_page to return empty (skip detail phase)
        scraper._browser.scrape_article_page = MagicMock(return_value=("", []))
        scraper._browser.extract_article_url_from_topic = MagicMock(return_value=None)
        scraper._browser.fetch_topic_detail_payload = MagicMock(return_value={})

        scraper.run_incremental(detail=True, ocr=False)

        # 验证合并结果：去重后应有 3 个 topic
        all_calls = scraper._collect_topics.call_args_list
        assert len(all_calls) == 2
        assert all_calls[0][0][0] == "all"
        assert all_calls[1][0][0] == "digests"
