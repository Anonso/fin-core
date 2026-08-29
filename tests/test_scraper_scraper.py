"""Tests for ZSXQ scraper."""

from fin_analyse.scraper.scraper import ZsxqScraper


class TestZsxqScraper:
    def test_scraper_initialization(self):
        scraper = ZsxqScraper(headless=True)
        assert scraper.headless is True

    def test_scraper_initialization_not_headless(self):
        scraper = ZsxqScraper(headless=False)
        assert scraper.headless is False

    def test_make_post_id(self):
        scraper = ZsxqScraper(headless=True)
        post_id = scraper._make_post_id("test content", "test content")
        assert isinstance(post_id, str)
        assert len(post_id) > 0

    def test_make_post_id_deterministic(self):
        scraper = ZsxqScraper(headless=True)
        id1 = scraper._make_post_id("same content", "same content")
        id2 = scraper._make_post_id("same content", "same content")
        assert id1 == id2
