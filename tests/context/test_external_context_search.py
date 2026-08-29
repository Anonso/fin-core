"""Tests for opt-in external context search helper."""

from fin_analyse.context.models import ExternalContextBundle, ExternalContextRecord
from fin_analyse.context.search import search_with_external_context
from fin_analyse.ingestion.models import RawDocument
from fin_analyse.knowledge.search import TextSearch
from fin_analyse.knowledge.store import KnowledgeStore


def _store() -> KnowledgeStore:
    doc = RawDocument(
        "zsxq",
        "doc1",
        "PCB产业链更新",
        "AI服务器需求带动覆铜板景气度提升。",
        metadata={"date": "2026-06-20"},
    )
    return KnowledgeStore(
        documents=[doc],
        evidence=[],
        claims=[],
        _doc_dates={doc.document_id: "2026-06-20"},
    )


def _bundle() -> ExternalContextBundle:
    return ExternalContextBundle(
        ticker="600519",
        records=[
            ExternalContextRecord(
                "announcement:600519:A1",
                "cninfo",
                "filing",
                "600519",
                "贵州茅台半年度报告",
                "贵州茅台发布半年度报告，公告为外部参考。",
                "2026-06-21",
                metadata={"announcement_id": "A1"},
            )
        ],
    )


def test_search_with_external_context_returns_external_hits_with_reference_metadata():
    results = search_with_external_context(_store(), "半年度报告", _bundle(), limit=10)

    assert len(results) == 1
    hit = results[0]
    assert hit["document_id"] == "external-context:announcement:600519:A1"
    assert hit["source"] == "external_context"
    assert hit["source_category"] == "filing"
    assert hit["external_source"] == "cninfo"
    assert hit["reference_only"] is True
    assert hit["date"] == "2026-06-21"


def test_search_with_external_context_keeps_zsxq_hits_distinguishable():
    results = search_with_external_context(_store(), "PCB 覆铜板", _bundle(), limit=10)

    assert len(results) == 1
    hit = results[0]
    assert hit["document_id"] == "zsxq:doc1"
    assert hit["source"] == "zsxq"
    assert hit["reference_only"] is False


def test_text_search_default_does_not_see_external_context_without_helper():
    results = TextSearch(_store()).search("半年度报告", top_k=10)

    assert results == []
