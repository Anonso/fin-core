"""Interface-level tests for KnowledgeQueryService."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fin_analyse.claims.models import Claim
from fin_analyse.ingestion.models import RawDocument
from fin_analyse.knowledge.query import KnowledgeQueryRequest, KnowledgeQueryService
from fin_analyse.knowledge.store import KnowledgeStore


@dataclass
class _FakeTextSearch:
    hits: list[dict]

    def search(self, query: str, top_k: int = 50) -> list[dict]:
        return list(self.hits)


def _claim(document_id: str, subject: str, title: str) -> Claim:
    return Claim(
        claim_id=f"claim:{document_id}",
        source_id="zsxq",
        document_id=document_id,
        subject=subject,
        predicate="mentioned_in",
        object_value=document_id,
        claim_type="company_mention",
        polarity="neutral",
        horizon="180d",
        confidence=0.75,
        evidence_ids=[],
        metadata={"title": title},
    )


def _doc(external_id: str, title: str, content: str, date: str) -> RawDocument:
    return RawDocument(
        source_id="zsxq",
        external_id=external_id,
        title=title,
        content=content,
        metadata={"date": date},
    )


def _days_ago(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def test_query_service_filters_direct_claim_and_text_hits_by_window():
    recent_date = _days_ago(1)
    old_date = _days_ago(30)
    claim_date = _days_ago(2)
    recent_doc = _doc(
        "recent",
        "query match recent title",
        "recent content",
        recent_date,
    )
    old_doc = _doc(
        "old",
        "query match old title",
        "old content",
        old_date,
    )
    claim_doc = _doc(
        "claim_recent",
        "claim article",
        "claim content",
        claim_date,
    )
    store = KnowledgeStore(
        documents=[recent_doc, old_doc, claim_doc],
        evidence=[],
        claims=[_claim(claim_doc.document_id, "query target", "claim article")],
        _doc_dates={
            recent_doc.document_id: recent_date,
            old_doc.document_id: old_date,
            claim_doc.document_id: claim_date,
        },
    )

    service = KnowledgeQueryService(
        store,
        text_search_factory=lambda _: _FakeTextSearch(
            [
                {
                    "document_id": recent_doc.document_id,
                    "title": recent_doc.title,
                    "date": recent_date,
                },
                {"document_id": old_doc.document_id, "title": old_doc.title, "date": old_date},
            ]
        ),
    )

    result = service.query(KnowledgeQueryRequest(query="query", window="7d", limit=50))

    doc_ids = [hit["document_id"] for hit in result.hits]
    assert recent_doc.document_id in doc_ids
    assert claim_doc.document_id in doc_ids
    assert old_doc.document_id not in doc_ids
    assert doc_ids.count(recent_doc.document_id) == 1
    assert all(hit["source"] == "zsxq" for hit in result.hits)
    assert all(hit["reference_only"] is False for hit in result.hits)


# ---------------------------------------------------------------------------
# External context and failure behaviour tests
# ---------------------------------------------------------------------------


from fin_analyse.context.models import ExternalContextBundle, ExternalContextRecord  # noqa: E402


class _FakeExternalContextService:
    def collect_for_ticker(self, ticker: str) -> ExternalContextBundle:
        return ExternalContextBundle(
            ticker=ticker,
            records=[
                ExternalContextRecord(
                    "announcement:600519:A1",
                    "cninfo",
                    "filing",
                    ticker,
                    "贵州茅台半年度报告",
                    "贵州茅台发布半年度报告，公告为外部参考。",
                    _days_ago(1),
                    metadata={"announcement_id": "A1"},
                )
            ],
        )


class _BrokenExternalContextService:
    def collect_for_ticker(self, ticker: str) -> ExternalContextBundle:
        raise RuntimeError("external context unavailable")


def test_query_service_adds_external_context_only_when_requested():
    store = KnowledgeStore(documents=[], evidence=[], claims=[], _doc_dates={})
    service = KnowledgeQueryService(store, external_context_service=_FakeExternalContextService())

    without_external = service.query(
        KnowledgeQueryRequest(
            query="半年度报告",
            window="7d",
            include_external_context=False,
            ticker="600519",
        )
    )
    assert without_external.hits == []

    with_external = service.query(
        KnowledgeQueryRequest(
            query="半年度报告",
            window="7d",
            include_external_context=True,
            ticker="600519",
        )
    )
    assert len(with_external.hits) == 1
    hit = with_external.hits[0]
    assert hit["source"] == "external_context"
    assert hit["reference_only"] is True
    assert hit["external_source"] == "cninfo"


def test_query_service_records_external_context_data_gap_without_raising():
    store = KnowledgeStore(documents=[], evidence=[], claims=[], _doc_dates={})
    service = KnowledgeQueryService(store, external_context_service=_BrokenExternalContextService())

    result = service.query(
        KnowledgeQueryRequest(
            query="半年度报告",
            window="7d",
            include_external_context=True,
            ticker="600519",
        )
    )

    assert result.hits == []
    assert "external_context_unavailable" in result.data_gaps


def test_query_service_reports_filtered_out_count_for_windowed_hits():
    old_date = _days_ago(30)
    old_doc = _doc("old_counted", "query old", "query content", old_date)
    store = KnowledgeStore(
        documents=[old_doc],
        evidence=[],
        claims=[],
        _doc_dates={old_doc.document_id: old_date},
    )
    service = KnowledgeQueryService(
        store,
        text_search_factory=lambda _: _FakeTextSearch(
            [{"document_id": old_doc.document_id, "title": old_doc.title, "date": old_date}]
        ),
    )

    result = service.query(KnowledgeQueryRequest(query="query", window="7d", limit=50))

    assert result.hits == []
    assert result.filtered_out_count >= 1


def test_query_service_reuses_one_text_index_for_immutable_store() -> None:
    store = KnowledgeStore(documents=[], evidence=[], claims=[], _doc_dates={})
    constructed: list[KnowledgeStore] = []

    def factory(candidate: KnowledgeStore) -> _FakeTextSearch:
        constructed.append(candidate)
        return _FakeTextSearch([])

    service = KnowledgeQueryService(store, text_search_factory=factory)

    service.query(KnowledgeQueryRequest(query="第一次"))
    service.query(KnowledgeQueryRequest(query="第二次"))

    assert constructed == [store]
