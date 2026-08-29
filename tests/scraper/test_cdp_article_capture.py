"""Tests for CDP article capture — talk, Q&A, column topic, inline article URL.

Proves:
- Fake articles.zsxq.com/id_<topic_id>.html URLs are rejected
- Content source provenance is recorded
- Completeness scoring works
- Image binding to article capture is supported
"""

from fin_analyse.scraper.cdp_article_capture import (
    ArticleCaptureResult,
    ImageCapture,
    build_article_detail_url,
    compute_completeness,
    is_valid_article_url,
    make_fake_article_url_guard,
)


class TestFakeArticleUrlGuard:
    """Reject fake articles.zsxq.com/id_<topic_id>.html URLs."""

    def test_rejects_fake_article_url_pattern(self):
        """articles.zsxq.com/id_<topic_id>.html with only topic_id is fake."""
        assert not is_valid_article_url(
            "https://articles.zsxq.com/id_45544511842515448.html",
            topic_id="45544511842515448",
        )

    def test_rejects_fake_article_url_with_wrong_topic_id(self):
        """Any URL matching the fake pattern is invalid."""
        assert not is_valid_article_url(
            "https://articles.zsxq.com/id_12345.html",
            topic_id="12345",
        )

    def test_accepts_real_short_article_url(self):
        """Real short article URLs have a different ID than topic_id."""
        assert is_valid_article_url(
            "https://articles.zsxq.com/id_nlk6l8t4m4fh.html",
            topic_id="45544511842515448",
        )

    def test_accepts_topic_detail_url(self):
        """Topic detail URLs are always valid."""
        assert is_valid_article_url(
            "https://wx.zsxq.com/group/15522441811252/topic/45544511842515448",
            topic_id="45544511842515448",
        )

    def test_empty_url_is_invalid(self):
        assert not is_valid_article_url("", topic_id="123")

    def test_none_url_is_invalid(self):
        assert not is_valid_article_url(None, topic_id="123")

    def test_make_fake_article_url_guard_returns_false(self):
        """Guard function returns False for fake URLs."""
        guard = make_fake_article_url_guard("45544511842515448")
        assert not guard("https://articles.zsxq.com/id_45544511842515448.html")

    def test_make_fake_article_url_guard_returns_true_for_real(self):
        guard = make_fake_article_url_guard("45544511842515448")
        assert guard("https://articles.zsxq.com/id_nlk6l8t4m4fh.html")


class TestBuildArticleDetailUrl:
    """URL construction for different ZSXQ content types."""

    def test_group_topic_detail_url(self):
        url = build_article_detail_url(
            topic_id="123",
            group_id="15522441811252",
        )
        assert "group/15522441811252/topic/123" in url

    def test_article_short_url_preferred(self):
        """When a real short article URL is available, use it directly."""
        url = build_article_detail_url(
            topic_id="123",
            group_id="15522441811252",
            article_url="https://articles.zsxq.com/id_real_short.html",
        )
        assert url == "https://articles.zsxq.com/id_real_short.html"

    def test_fake_article_url_ignored(self):
        """Fake article URL is ignored, fall back to topic detail URL."""
        url = build_article_detail_url(
            topic_id="123",
            group_id="15522441811252",
            article_url="https://articles.zsxq.com/id_123.html",
        )
        assert "group/15522441811252/topic/123" in url


class TestComputeCompleteness:
    """Completeness scoring for captured articles."""

    def test_full_content_is_complete(self):
        content = "这是一篇完整的文章，包含详细的分析内容。" * 20  # >300 chars
        score, incomplete, reason = compute_completeness(
            content=content,
            content_source="topic_detail",
        )
        assert score >= 0.8
        assert not incomplete

    def test_short_content_is_incomplete(self):
        content = "Short"  # < 300 chars
        score, incomplete, reason = compute_completeness(
            content=content,
            content_source="topic_detail",
        )
        assert incomplete
        assert score < 0.8

    def test_dom_card_fallback_is_incomplete(self):
        content = "足够的正文内容。" * 20
        score, incomplete, reason = compute_completeness(
            content=content,
            content_source="dom_card_fallback",
        )
        assert incomplete
        assert "dom_card_fallback" in reason

    def test_star_column_stricter_threshold(self):
        """星大派 articles have stricter completeness requirements."""
        content = "内容。" * 100  # ~300 chars, borderline
        score, incomplete, reason = compute_completeness(
            content=content,
            content_source="topic_detail",
            column="星大派特刊",
        )
        # Star columns need >= 500 chars
        assert incomplete or score < 1.0

    def test_no_images_is_fine(self):
        content = "完整内容。" * 100  # ~500 chars, well above threshold
        score, incomplete, reason = compute_completeness(
            content=content,
            content_source="topic_detail",
        )
        assert not incomplete


class TestArticleCaptureResult:
    def test_minimal_result(self):
        result = ArticleCaptureResult(
            topic_id="123",
            title="Test",
            content="A" * 500,  # Enough content to be complete
            content_source="topic_detail",
        )
        assert result.topic_id == "123"
        assert not result.incomplete

    def test_result_with_images(self):
        images = [
            ImageCapture(
                path="images/123/000.png",
                source_url="https://images.zsxq.com/abc.png",
                llm_desc="A chart",
                ocr_text="Data",
                vision_provider="gpt5",
                vision_model="gpt-5.4",
                fallback_chain=["gpt5:ok"],
            )
        ]
        result = ArticleCaptureResult(
            topic_id="123",
            title="Test",
            content="Content",
            content_source="topic_detail",
            images=images,
        )
        assert len(result.images) == 1
        assert result.images[0].vision_provider == "gpt5"

    def test_to_dict_includes_images(self):
        img = ImageCapture(
            path="images/123/000.png",
            source_url="https://images.zsxq.com/abc.png",
            llm_desc="A chart",
            ocr_text="",
            vision_provider="gpt5",
            vision_model="gpt-5.4",
            fallback_chain=["gpt5:ok"],
        )
        result = ArticleCaptureResult(
            topic_id="123",
            title="Test",
            content="Content",
            content_source="topic_detail",
            article_url="https://articles.zsxq.com/id_real.html",
            images=[img],
            completeness_score=0.95,
        )
        d = result.to_dict()
        assert d["topic_id"] == "123"
        assert d["completeness_score"] == 0.95
        assert len(d["images"]) == 1
        assert d["images"][0]["vision_provider"] == "gpt5"


class TestArticleCaptureContentTypes:
    """Verify capture result records content source type."""

    def test_talk_type(self):
        result = ArticleCaptureResult(
            topic_id="1",
            title="Talk",
            content="Talk content",
            content_source="topic_detail",
            article_type="talk",
        )
        assert result.article_type == "talk"

    def test_qa_type(self):
        result = ArticleCaptureResult(
            topic_id="1",
            title="Q&A",
            content="Question\n\nAnswer",
            content_source="topic_detail",
            article_type="q&a",
        )
        assert result.article_type == "q&a"

    def test_column_topic_type(self):
        result = ArticleCaptureResult(
            topic_id="1",
            title="Column",
            content="Column content",
            content_source="column_topic_detail",
            article_type="column_topic",
        )
        assert result.article_type == "column_topic"
        assert result.content_source == "column_topic_detail"
