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
