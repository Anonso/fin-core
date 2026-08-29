"""CDP article capture — detail page scraping, completeness scoring, image binding.

Captures ZSXQ articles from detail pages and short article URLs,
computing completeness scores and binding downloaded images.

Usage:
    from fin_analyse.scraper.cdp_article_capture import (
        ArticleCapture, ArticleCaptureResult, ImageCapture,
        build_article_detail_url, is_valid_article_url,
    )
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── URL validation ─────────────────────────────────────────────

# Pattern for fake article URLs: articles.zsxq.com/id_<topic_id>.html
# These are constructed from topic_id and don't point to real short articles.
_FAKE_ARTICLE_RE = re.compile(r"https?://articles\.zsxq\.com/id_(\d+)\.html")


def is_valid_article_url(url: str | None, topic_id: str = "") -> bool:
    """Return True if the URL is a real article URL (not a fake constructed one).

    Fake URLs are articles.zsxq.com/id_<topic_id>.html where the numeric ID
    matches the topic_id — these are auto-constructed and not real short articles.
    """
    if not url or not isinstance(url, str):
        return False

    m = _FAKE_ARTICLE_RE.match(url.strip())
    if m and topic_id and m.group(1) == topic_id:
        return False
    # If it matches the pattern but with a different ID, it's a real short URL
    if m:
        return True
    # Any other URL (topic detail, etc.) is valid
    return bool(url.strip())


def make_fake_article_url_guard(topic_id: str):
    """Return a guard function for the given topic_id."""

    def guard(url: str | None) -> bool:
        return is_valid_article_url(url, topic_id)

    return guard


def build_article_detail_url(
    topic_id: str,
    group_id: str = "15522441811252",
    article_url: str = "",
) -> str:
    """Build the best URL to reach an article's full content.

    Priority:
    1. Real short article URL (validated, not fake)
    2. Group topic detail URL: wx.zsxq.com/group/<gid>/topic/<tid>
    """
    if article_url and is_valid_article_url(article_url, topic_id):
        return article_url

    return f"https://wx.zsxq.com/group/{group_id}/topic/{topic_id}"


# ── Completeness ───────────────────────────────────────────────

# Minimum character count for non-star columns
_MIN_CONTENT_CHARS = 300
# Stricter threshold for 星大派 columns
_MIN_STAR_CONTENT_CHARS = 500


def compute_completeness(
    content: str,
    content_source: str,
    column: str = "普通",
) -> tuple[float, bool, str]:
    """Compute completeness score and determine if article is incomplete.

    Returns:
        (completeness_score, is_incomplete, incomplete_reason)
    """
    content_len = len(content.strip())
    min_chars = _MIN_STAR_CONTENT_CHARS if "星大派" in column else _MIN_CONTENT_CHARS

    incomplete = False
    reasons: list[str] = []

    if content_source == "dom_card_fallback":
        incomplete = True
        reasons.append("dom_card_fallback")
        score = min(0.5, content_len / max(min_chars * 2, 1))
    elif content_len < min_chars:
        incomplete = True
        reasons.append(f"content_too_short({content_len}<{min_chars})")
        score = max(0.1, min(0.7, content_len / max(min_chars, 1)))
    else:
        score = min(1.0, content_len / max(min_chars, 1))

    reason = "; ".join(reasons) if reasons else ""
    return score, incomplete, reason


# ── Data types ─────────────────────────────────────────────────


@dataclass
class ImageCapture:
    """A single image captured from an article with vision/OCR provenance."""

    path: str  # relative path like "images/<article_id>/000.png"
    source_url: str
    llm_desc: str = ""
    ocr_text: str = ""
    vision_provider: str = ""  # vision.chain 条目名: "glm53_flash" | "glm-vision" | "vision" | "mimo" | "none"
    vision_model: str = ""
    fallback_chain: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "source_url": self.source_url,
            "llm_desc": self.llm_desc,
            "ocr_text": self.ocr_text,
            "vision_provider": self.vision_provider,
            "vision_model": self.vision_model,
            "fallback_chain": list(self.fallback_chain),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageCapture:
        return cls(
            path=str(data.get("path", "")),
            source_url=str(data.get("source_url", "")),
            llm_desc=str(data.get("llm_desc", "")),
            ocr_text=str(data.get("ocr_text", "")),
            vision_provider=str(data.get("vision_provider", "")),
            vision_model=str(data.get("vision_model", "")),
            fallback_chain=list(data.get("fallback_chain", [])),
            error=str(data.get("error", "")),
        )


@dataclass
class ArticleCaptureResult:
    """Result of capturing a single article from ZSXQ."""

    topic_id: str
    title: str
    content: str
    content_source: str  # "topic_detail" | "column_topic_detail" | "dom_card_fallback"
    article_type: str = "talk"  # "talk" | "q&a" | "column_topic"
    article_url: str = ""
    column: str = "普通"
    score: float | None = None
    images: list[ImageCapture] = field(default_factory=list)
    completeness_score: float = 0.0
    incomplete: bool = False
    incomplete_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.incomplete_reason and not self.completeness_score:
            score, incomplete, reason = compute_completeness(
                self.content, self.content_source, self.column
            )
            self.completeness_score = score
            self.incomplete = incomplete
            self.incomplete_reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "content": self.content,
            "content_source": self.content_source,
            "article_type": self.article_type,
            "article_url": self.article_url,
            "column": self.column,
            "score": self.score,
            "images": [img.to_dict() for img in self.images],
            "completeness_score": self.completeness_score,
            "incomplete": self.incomplete,
            "incomplete_reason": self.incomplete_reason,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArticleCaptureResult:
        images = [ImageCapture.from_dict(i) for i in data.get("images", [])]
        return cls(
            topic_id=str(data.get("topic_id", "")),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            content_source=str(data.get("content_source", "")),
            article_type=str(data.get("article_type", "talk")),
            article_url=str(data.get("article_url", "")),
            column=str(data.get("column", "普通")),
            score=float(data["score"]) if data.get("score") is not None else None,
            images=images,
            completeness_score=float(data.get("completeness_score", 0)),
            incomplete=bool(data.get("incomplete", False)),
            incomplete_reason=str(data.get("incomplete_reason", "")),
            extra=dict(data.get("extra", {})),
        )


class ArticleCapture:
    """Capture full article content from ZSXQ detail pages.

    Usage:
        capture = ArticleCapture(client=cdp_client)
        result = capture.capture_talk("topic_id_123")
        result = capture.capture_qa("topic_id_456")
        result = capture.capture_column_topic("topic_id_789", "星大派特刊")
    """

    def __init__(self, client=None):
        """client is a CdpBridgeClient instance for CDP operations."""
        self._client = client

    def capture_talk(
        self,
        topic_id: str,
        article_url: str = "",
        title_hint: str = "",
    ) -> ArticleCaptureResult:
        """Capture a talk (普通发言) article from its detail page."""
        detail_url = build_article_detail_url(topic_id, article_url=article_url)

        if self._client:
            self._client.navigate(detail_url, wait=3.0)
            # Extract content via JS
            title = self._client.js(
                "document.querySelector('.topic-title, .detail-title, h1')?.innerText || document.title"
            ).strip()
            content = self._client.js("document.body.innerText")
            content_source = "topic_detail"
        else:
            title = title_hint
            content = ""
            content_source = "dom_card_fallback"

        title = title or title_hint or ""

        score, incomplete, reason = compute_completeness(content, content_source)

        return ArticleCaptureResult(
            topic_id=topic_id,
            title=title,
            content=content,
            content_source=content_source,
            article_url=detail_url,
            completeness_score=score,
            incomplete=incomplete,
            incomplete_reason=reason,
        )

    def capture_qa(
        self,
        topic_id: str,
        article_url: str = "",
    ) -> ArticleCaptureResult:
        """Capture a Q&A article, extracting both question and answer."""
        result = self.capture_talk(topic_id, article_url)
        result.article_type = "q&a"
        return result

    def capture_column_topic(
        self,
        topic_id: str,
        column: str,
        article_url: str = "",
    ) -> ArticleCaptureResult:
        """Capture a column topic (专栏文章), e.g. 星大派特刊."""
        result = self.capture_talk(topic_id, article_url)
        result.article_type = "column_topic"
        result.column = column
        result.content_source = "column_topic_detail"
        # Recompute completeness with column context
        score, incomplete, reason = compute_completeness(
            result.content, result.content_source, column
        )
        result.completeness_score = score
        result.incomplete = incomplete
        result.incomplete_reason = reason
        return result
