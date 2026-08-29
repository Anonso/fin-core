"""Tests for knowledge window filtering in KnowledgeQueryService and query_knowledge.

Verifies that TextSearch hits and direct document matches are filtered by
the shared Knowledge Query module window parameter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from fin_analyse.knowledge.query import KnowledgeQueryRequest, KnowledgeQueryService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_store(
    claims: list | None = None,
    documents: list | None = None,
    text_search_hits: list | None = None,
) -> Mock:
    """Build a mock KnowledgeStore with canned claims, documents, and TextSearch results."""
    store = Mock()
    store.find_claims.return_value = claims or []
    store.documents = documents or []

    # Mock TextSearch so that its .search() returns canned hits
    def _fake_text_search(store_arg, query, top_k=50):
        mock_ts = Mock()
        mock_ts.search.return_value = text_search_hits or []
        return mock_ts

    return store, _fake_text_search


def _query_articles(repo: Mock, query: str, window: str) -> list[dict]:
    return (
        KnowledgeQueryService(repo)
        .query(KnowledgeQueryRequest(query=query, window=window, limit=50))
        .to_article_dicts()
    )


# ---------------------------------------------------------------------------
# Window filtering tests
# ---------------------------------------------------------------------------


class TestArticlesWindowFiltersTextSearchHits:
    """KnowledgeQueryService must filter TextSearch hits by window."""

    def test_hits_outside_window_are_excluded(self):
        """A TextSearch hit dated before the window boundary must NOT appear in results."""
        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = []

        # A hit from 200 days ago — outside a 7d window
        old_date = (datetime.now(UTC).replace(day=1) - __import__("datetime").timedelta(days=200)).strftime("%Y-%m-%d")
        old_hit = {"document_id": "old_doc", "title": "Old Article", "date": old_date}

        # TextSearch is imported inside KnowledgeQueryService; patch the source module.
        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = [old_hit]
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "test query", "7d")

            # The old hit should be excluded
            doc_ids = [r["document_id"] for r in results]
            assert "old_doc" not in doc_ids, (
                f"TextSearch hit outside 7d window should be excluded, got: {doc_ids}"
            )

    def test_hits_within_window_are_included(self):
        """A TextSearch hit within the window must appear in results."""
        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = []

        recent_date = datetime.now(UTC).strftime("%Y-%m-%d")
        recent_hit = {"document_id": "recent_doc", "title": "Recent Article", "date": recent_date}

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = [recent_hit]
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "test query", "90d")

            doc_ids = [r["document_id"] for r in results]
            assert "recent_doc" in doc_ids, (
                f"TextSearch hit within 90d window should be included, got: {doc_ids}"
            )

    def test_window_all_does_not_filter(self):
        """window='all' must return ALL TextSearch hits, including very old ones."""
        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = []

        old_date = "2020-01-01"
        old_hit = {"document_id": "ancient_doc", "title": "Ancient", "date": old_date}

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = [old_hit]
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "test query", "all")

            doc_ids = [r["document_id"] for r in results]
            assert "ancient_doc" in doc_ids, (
                f"window='all' should not filter, got: {doc_ids}"
            )

    def test_hit_missing_date_is_excluded(self):
        """Hits without a date field should be excluded when window is active."""
        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = []

        no_date_hit = {"document_id": "nodate_doc", "title": "No Date"}

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = [no_date_hit]
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "test query", "7d")

            doc_ids = [r["document_id"] for r in results]
            assert "nodate_doc" not in doc_ids, (
                f"Hits without date should be excluded from window-filtered results, got: {doc_ids}"
            )

    def test_unknown_window_defaults_to_180d(self):
        """Unknown window values should default to 180d filtering."""
        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = []

        # Date 150 days ago — within 180d window but outside some smaller windows
        mid_date = (datetime.now(UTC) - __import__("datetime").timedelta(days=150)).strftime("%Y-%m-%d")
        mid_hit = {"document_id": "mid_doc", "title": "Mid", "date": mid_date}

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = [mid_hit]
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "test query", "bogus_window")

            doc_ids = [r["document_id"] for r in results]
            assert "mid_doc" in doc_ids, (
                f"Unknown window should default to 180d — 150d-old hit should be included, got: {doc_ids}"
            )


# ---------------------------------------------------------------------------
# Direct document match window filter tests (P0 gap closure)
# ---------------------------------------------------------------------------


class TestArticlesWindowFiltersDirectDocumentMatches:
    """KnowledgeQueryService must apply window filter to direct document matches too.

    Before the P0 fix, only TextSearch hits were filtered by window; documents
    matched directly via title/content/claim-subject were returned regardless
    of their date.
    """

    def test_direct_document_match_outside_window_is_excluded(self):
        """A document whose title matches the query but whose date is outside
        the window must NOT appear in results."""
        from datetime import timedelta

        from fin_analyse.ingestion.models import RawDocument

        old_date = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        old_doc = RawDocument(
            source_id="zsxq",
            external_id="old_article_1",
            title="Important query match in title",
            content="Some content about the query topic",
            metadata={"date": old_date},
        )

        repo = Mock()
        # No claims match → direct doc path will be exercised
        repo.find_claims.return_value = []
        repo.documents = [old_doc]

        # Patch TextSearch to return no hits so only direct match is tested
        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = []
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "query match", "7d")

            doc_ids = [r["document_id"] for r in results]
            assert "zsxq:old_article_1" not in doc_ids, (
                f"Direct document match outside 7d window should be excluded, got: {doc_ids}"
            )

    def test_direct_document_match_within_window_is_included(self):
        """A document whose title matches the query and whose date is within
        the window must appear in results."""
        from fin_analyse.ingestion.models import RawDocument

        recent_date = datetime.now(UTC).strftime("%Y-%m-%d")
        recent_doc = RawDocument(
            source_id="zsxq",
            external_id="recent_article_1",
            title="Recent query match in title",
            content="Some content",
            metadata={"date": recent_date},
        )

        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = [recent_doc]

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = []
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "query match", "90d")

            doc_ids = [r["document_id"] for r in results]
            assert "zsxq:recent_article_1" in doc_ids, (
                f"Direct document match within 90d window should be included, got: {doc_ids}"
            )

    def test_direct_doc_match_window_all_includes_old(self):
        """window='all' must include old direct document matches."""
        from fin_analyse.ingestion.models import RawDocument

        old_date = "2020-01-01"
        old_doc = RawDocument(
            source_id="zsxq",
            external_id="ancient",
            title="Ancient query match",
            content="Very old content about query",
            metadata={"date": old_date},
        )

        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = [old_doc]

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = []
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "Ancient query", "all")

            doc_ids = [r["document_id"] for r in results]
            assert "zsxq:ancient" in doc_ids, (
                f"window='all' should include old direct matches, got: {doc_ids}"
            )

    def test_direct_doc_match_missing_date_is_excluded(self):
        """Direct document matches without a date should be excluded when window is active."""
        from fin_analyse.ingestion.models import RawDocument

        no_date_doc = RawDocument(
            source_id="zsxq",
            external_id="nodate",
            title="Query match no date",
            content="Some content",
            metadata={},  # no date
        )

        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = [no_date_doc]

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = []
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "Query match", "7d")

            doc_ids = [r["document_id"] for r in results]
            assert "zsxq:nodate" not in doc_ids, (
                f"Direct doc match without date should be excluded, got: {doc_ids}"
            )

    def test_direct_doc_match_content_query_outside_window_is_excluded(self):
        """A document whose content (not title) matches the query but whose date
        is outside the window must NOT appear in results."""
        from datetime import timedelta

        from fin_analyse.ingestion.models import RawDocument

        old_date = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        old_doc = RawDocument(
            source_id="zsxq",
            external_id="old_content_match",
            title="Unrelated title",
            content="This content contains the query keyword deep inside",
            metadata={"date": old_date},
        )

        repo = Mock()
        repo.find_claims.return_value = []
        repo.documents = [old_doc]

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = []
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "query keyword", "7d")

            doc_ids = [r["document_id"] for r in results]
            assert "zsxq:old_content_match" not in doc_ids, (
                f"Content-matched doc outside 7d window should be excluded, got: {doc_ids}"
            )

    def test_claim_matched_doc_outside_window_is_excluded(self):
        """A document matched via claim subject but whose date is outside
        the window must NOT appear in results."""
        from datetime import timedelta

        from fin_analyse.claims.models import Claim
        from fin_analyse.ingestion.models import RawDocument

        old_date = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")

        # Create a claim whose subject matches the query
        claim = Claim(
            claim_id="claim:old",
            source_id="zsxq",
            document_id="zsxq:old_claim_doc",
            subject="query target company",
            predicate="mentioned_in",
            object_value="zsxq:old_claim_doc",
            claim_type="company_mention",
            polarity="neutral",
            horizon="180d",
            confidence=0.75,
            evidence_ids=[],
            metadata={"title": "Old Claim Article"},
        )

        old_doc = RawDocument(
            source_id="zsxq",
            external_id="old_claim_doc",
            title="Old Claim Article",
            content="Some content",
            metadata={"date": old_date},
        )

        repo = Mock()
        # find_claims is called with since=... which filters claims by window.
        # But the bug is that since is derived from window_days, and the claim's
        # document date is checked via _doc_dates in _claim_in_window.
        # Since we mock find_claims, we need to simulate the claim being
        # returned despite being outside the window (the mock doesn't filter).
        # This simulates the scenario where find_claims returns a claim for a
        # document whose date is actually outside the window.
        repo.find_claims.return_value = [claim]
        repo.documents = [old_doc]

        with patch(
            "fin_analyse.knowledge.search.TextSearch",
            autospec=True,
        ) as mock_ts_cls:
            mock_ts_instance = Mock()
            mock_ts_instance.search.return_value = []
            mock_ts_cls.return_value = mock_ts_instance

            results = _query_articles(repo, "query target", "7d")

            doc_ids = [r["document_id"] for r in results]
            assert "zsxq:old_claim_doc" not in doc_ids, (
                f"Claim-matched doc outside 7d window should be excluded, got: {doc_ids}"
            )
