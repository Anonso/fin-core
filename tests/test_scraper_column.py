"""Tests for ZSXQ column scraping."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fin_analyse.scraper.scraper import ZsxqScraper


class TestColumnScraping:
    @pytest.fixture
    def scraper(self, tmp_path):
        """Create a scraper with mocked browser, writing to tmp dir."""
        articles_dir = tmp_path / "articles"
        images_dir = tmp_path / "images"
        index_file = tmp_path / "index.json"

        with (
            patch("fin_analyse.scraper.scraper.BrowserManager") as bm_cls,
            patch("fin_analyse.scraper.scraper.ImageDownloader"),
            patch("fin_analyse.scraper.config.ARTICLES_DIR", articles_dir),
            patch("fin_analyse.scraper.config.IMAGES_DIR", images_dir),
            patch("fin_analyse.scraper.config.INDEX_FILE", index_file),
        ):
            s = ZsxqScraper(headless=True)
            s._browser = bm_cls.return_value
            s._parser = MagicMock()
            s._downloader = MagicMock()
            s._browser.get_cookies.return_value = {}
            s._browser._detect_author.return_value = "test_author"
            s.save_article = MagicMock(return_value="/tmp/articles/test.md")
            s.load_index_ids = MagicMock(return_value=set())
            yield s

    def test_run_column_skips_existing_ids(self, scraper):
        """Articles already in index are skipped."""
        scraper._browser.fetch_columns.return_value = [
            {"column_id": "col_qa", "name": "星大派好问题", "articles_count": 1}
        ]
        scraper._browser.fetch_column_topics.return_value = [
            {
                "topic_id": "existing_topic",
                "title": "Already Scraped",
                "create_time": "2026-06-20T10:00:00.000+0800",
                "talk": {"text": "Already Scraped"},
            }
        ]
        scraper._make_post_id = MagicMock(return_value="abc123def456")
        scraper.load_index_ids.return_value = {"abc123def456"}

        result = scraper.run_column("星大派好问题", max_articles=10)

        # Should skip — article already in index
        assert result == 0
        scraper.save_article.assert_not_called()

    def test_run_column_scrapes_new_article(self, scraper):
        """New article from column is scraped and saved."""
        scraper._browser.fetch_columns.return_value = [
            {"column_id": "col_qa", "name": "星大派好问题", "articles_count": 1}
        ]
        scraper._browser.fetch_column_topics.return_value = [
            {
                "topic_id": "new_topic",
                "title": "New Stock Analysis Question",
                "create_time": "2026-06-20T14:00:00.000+0800",
                "talk": {"text": "这是一个关于股票分析的好问题"},
            }
        ]
        scraper._make_post_id = MagicMock(return_value="new_id_0001")

        result = scraper.run_column("星大派好问题", max_articles=10)

        assert result == 1
        scraper.save_article.assert_called_once()

    def test_run_column_no_matching_column(self, scraper):
        """When column name not found, returns 0."""
        scraper._browser.fetch_columns.return_value = [
            {"column_id": "col_other", "name": "其他栏目", "articles_count": 0}
        ]

        result = scraper.run_column("不存在的栏目", max_articles=10)

        assert result == 0
        scraper._browser.fetch_column_topics.assert_not_called()

    def test_run_column_handles_empty_content(self, scraper):
        """Articles with empty content get synthesized content."""
        scraper._browser.fetch_columns.return_value = [
            {"column_id": "col_qa", "name": "星大派好问题", "articles_count": 1}
        ]
        scraper._browser.fetch_column_topics.return_value = [
            {
                "topic_id": "short_topic",
                "title": "Short Question",
                "create_time": "2026-06-20T15:00:00.000+0800",
                "talk": {"text": ""},
                "likes_count": 5,
                "comments_count": 3,
            }
        ]
        scraper._make_post_id = MagicMock(return_value="short_id_001")

        result = scraper.run_column("星大派好问题", max_articles=10)

        assert result == 1
        call_args = scraper.save_article.call_args[0][0]
        assert "Short Question" in call_args["content"]
        assert "5赞" in call_args["content"]

    def test_run_column_uses_resolver_for_embedded_article(self, scraper):
        scraper._browser.fetch_columns.return_value = [
            {"column_id": "col_special", "name": "星大派特刊", "articles_count": 1}
        ]
        scraper._browser.fetch_column_topics.return_value = [
            {
                "topic_id": "topic-shortcode",
                "title": "星大派特刊",
                "text": "短摘要 https://articles.zsxq.com/id_k7065nedkkwt.html",
                "create_time": "2026-06-20T15:00:00.000+0800",
            }
        ]
        scraper.load_index_ids.return_value = set()
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
        scraper._make_post_id = MagicMock(return_value="resolved_id")

        result = scraper.run_column("星大派特刊", max_articles=10, detail=True, ocr=False)

        assert result == 1
        saved_post = scraper.save_article.call_args[0][0]
        assert saved_post["content"] == "完整正文" * 100
        assert saved_post["article_url"] == "https://articles.zsxq.com/id_k7065nedkkwt.html"
        assert saved_post["content_source"] == "article_html"
        assert saved_post["incomplete"] is False

    def test_save_article_persists_completeness_metadata(self, scraper, tmp_path):
        post = {
            "id": "meta123",
            "topic_id": "topic-meta",
            "date": "2026-06-20 15:00",
            "score": None,
            "column": "星大派特刊",
            "companies": [],
            "tags": [],
            "is_qa": False,
            "title": "Embedded Article",
            "content": "full content",
            "type": "talk",
            "article_url": "https://articles.zsxq.com/id_k7065nedkkwt.html",
            "content_source": "article_html",
            "incomplete": False,
            "incomplete_reason": "",
            "completeness_version": 1,
            "image_count": 0,
        }
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir(parents=True, exist_ok=True)
        scraper.save_article = ZsxqScraper.save_article.__get__(scraper, ZsxqScraper)

        path = Path(scraper.save_article(post, []))
        text = path.read_text(encoding="utf-8")

        assert "type: talk" in text
        assert "article_url: https://articles.zsxq.com/id_k7065nedkkwt.html" in text
        assert "content_source: article_html" in text
        assert "incomplete: False" in text
        assert "completeness_version: 1" in text

    def test_update_index_upserts_by_topic_id(self, scraper, tmp_path):
        index_file = tmp_path / "index.json"
        index_file.write_text(
            '{"articles":[{"id":"old","topic_id":"topic-1","char_count":60,"path":"old.md"}],"total":1}',
            encoding="utf-8",
        )
        with patch("fin_analyse.scraper.config.INDEX_FILE", index_file):
            scraper.update_index(
                [
                    {
                        "id": "new",
                        "topic_id": "topic-1",
                        "date": "2026-06-20 15:00",
                        "score": None,
                        "column": "星大派特刊",
                        "companies": [],
                        "tags": [],
                        "title": "Full",
                        "char_count": 5000,
                        "type": "talk",
                        "article_url": "https://articles.zsxq.com/id_k7065nedkkwt.html",
                        "content_source": "article_html",
                        "incomplete": False,
                        "incomplete_reason": "",
                        "completeness_version": 1,
                        "image_count": 3,
                    }
                ]
            )

        data = index_file.read_text(encoding="utf-8")
        assert data.count('"topic_id": "topic-1"') == 1
        assert '"id": "new"' in data
        assert '"content_source": "article_html"' in data
