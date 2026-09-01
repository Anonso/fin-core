"""Tests for completeness repair — recapture incomplete articles."""

from datetime import timedelta, timezone

from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

TZ = timezone(timedelta(hours=8))


class TestIncompleteArticleRepair:
    """Verify incomplete articles are recaptured when more complete version found."""

    def test_incomplete_article_is_recaptured(self, tmp_path, monkeypatch):
        """Existing topic_id with incomplete=true is recaptured and index updated."""
        scraper = CdpBridgeScraper()

        # Setup: an existing incomplete article in the index
        topic_id = "test_topic_123"
        existing_article = {
            "id": "existing_id_1",
            "topic_id": topic_id,
            "date": "2026-06-30 10:00",
            "score": 8.0,
            "title": "Old Title",
            "tags": [],
            "char_count": 100,
            "column": "普通",
            "companies": [],
            "is_qa": False,
            "image_count": 0,
            "path": str(tmp_path / "articles/20260630_existing_id_1.md"),
            "incomplete": True,
            "incomplete_reason": "dom_card_fallback",
            "completeness_version": 1,
        }

        # Mock the index
        scraper._index = {existing_article["id"]: existing_article}

        # Check: should recapture because existing is incomplete
        # (simulating a new capture with higher completeness_version)
        assert scraper._should_recapture(
            topic_id=topic_id,
            new_content_len=850,
            new_completeness_version=2,
        )

    def test_complete_article_not_recaptured_unless_higher_completeness(self, tmp_path):
        """Complete article is skipped unless new version has higher completeness."""
        scraper = CdpBridgeScraper()

        topic_id = "test_topic_456"
        existing_article = {
            "id": "complete_id",
            "topic_id": topic_id,
            "date": "2026-06-30 10:00",
            "score": 8.0,
            "title": "Complete Title",
            "tags": [],
            "char_count": 2000,
            "column": "普通",
            "companies": [],
            "is_qa": False,
            "image_count": 2,
            "path": str(tmp_path / "articles/20260630_complete_id.md"),
            "incomplete": False,
            "incomplete_reason": "",
            "completeness_version": 2,
        }

        scraper._index = {existing_article["id"]: existing_article}

        # Same completeness version — should NOT recapture
        assert not scraper._should_recapture(
            topic_id=topic_id,
            new_content_len=2100,
            new_completeness_version=2,
        )

        # Higher completeness version — SHOULD recapture
        assert scraper._should_recapture(
            topic_id=topic_id,
            new_content_len=2100,
            new_completeness_version=3,
        )

    def test_no_existing_article_always_recaptures(self):
        """If article doesn't exist in index, always recapture."""
        scraper = CdpBridgeScraper()
        scraper._index = {}

        assert scraper._should_recapture(
            topic_id="new_topic",
            new_content_len=500,
            new_completeness_version=1,
        )

    def test_lookup_by_topic_id(self):
        """Index lookup finds articles by topic_id."""
        scraper = CdpBridgeScraper()
        scraper._index = {
            "id_a": {"id": "id_a", "topic_id": "topic_1"},
            "id_b": {"id": "id_b", "topic_id": "topic_2"},
        }

        found = scraper._find_by_topic_id("topic_1")
        assert found is not None
        assert found["id"] == "id_a"

        not_found = scraper._find_by_topic_id("nonexistent")
        assert not_found is None

    def test_truncated_stored_body_upgrades_only_on_strictly_longer_capture(
        self, tmp_path
    ):
        """截断跳转文章：新正文更长才升级；否则保留，避免每轮重写。"""
        scraper = CdpBridgeScraper()
        article_path = tmp_path / "20260831_zsxq-t1.md"
        article_path.write_text(
            "---\nid: zsxq-t1\n---\n\n# 星大派特刊：报告\n\n报告日期：…\n目 ...",
            encoding="utf-8",
        )
        existing = {
            "id": "zsxq-t1",
            "topic_id": "t1",
            "char_count": 30,
            "path": str(article_path),
            "completeness_version": 1,
        }
        scraper._index = {existing["id"]: existing}

        assert scraper._existing_body_truncated(existing)
        # 新正文严格更长 → 升级
        assert scraper._should_recapture(
            "t1", new_content_len=5000, new_completeness_version=1
        )
        # 新正文不长于存稿 → 跳过
        assert not scraper._should_recapture(
            "t1", new_content_len=len("目 ..."), new_completeness_version=1
        )

    def test_complete_body_not_reclassified_as_truncated(self, tmp_path):
        """完整正文（截断尾只在 frontmatter/其他位置出现）不算截断。"""
        scraper = CdpBridgeScraper()
        article_path = tmp_path / "complete.md"
        article_path.write_text(
            "---\nid: complete\n---\n\n# 完整文章\n\n正文不截断，结尾完整。",
            encoding="utf-8",
        )
        existing = {"id": "complete", "topic_id": "t2", "path": str(article_path)}
        scraper._index = {existing["id"]: existing}
        assert not scraper._existing_body_truncated(existing)
