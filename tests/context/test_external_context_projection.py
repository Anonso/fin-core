from datetime import UTC, datetime

from fin_analyse.claims.models import Claim
from fin_analyse.context.models import ExternalContextBundle, ExternalContextRecord
from fin_analyse.context.projection import project_external_context
from fin_analyse.ingestion.models import Evidence, RawDocument
from fin_analyse.knowledge.store import KnowledgeStore


def _base_store() -> KnowledgeStore:
    doc = RawDocument(
        "zsxq",
        "doc1",
        "贵州茅台文章",
        "看好贵州茅台的长期逻辑",
        metadata={"date": "2026-06-20"},
    )
    evidence = Evidence("e1", "zsxq", doc.document_id, "text", "看好贵州茅台")
    claim = Claim(
        "c1",
        "zsxq",
        doc.document_id,
        "贵州茅台",
        "mentioned",
        "贵州茅台",
        "company_mention",
        "neutral",
        "medium",
        0.8,
        ["e1"],
        observed_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    return KnowledgeStore(
        documents=[doc],
        evidence=[evidence],
        claims=[claim],
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
                "半年度报告",
                "公告参考",
                "2026-06-21",
                metadata={"announcement_id": "A1"},
            )
        ],
        warnings=["news: 数据源暂未启用，当前仅支持 parser 测试"],
    )


def test_project_external_context_returns_new_store_without_mutating_base():
    base = _base_store()

    result = project_external_context(base, _bundle())

    assert result.projected_store is not base
    assert len(base.documents) == 1
    assert len(base.claims) == 1
    assert len(result.documents) == 1
    assert len(result.projected_store.documents) == 2
    assert result.projected_store.evidence == base.evidence
    assert result.projected_store.claims == base.claims
    assert result.warnings == ["news: 数据源暂未启用，当前仅支持 parser 测试"]


def test_projected_external_document_has_search_safe_metadata():
    base = _base_store()

    result = project_external_context(base, _bundle())
    doc = result.documents[0]

    assert doc.source_id == "external-context"
    assert doc.document_id == "external-context:announcement:600519:A1"
    assert doc.metadata["source"] == "external_context"
    assert doc.metadata["external_source"] == "cninfo"
    assert doc.metadata["category"] == "filing"
    assert doc.metadata["ticker"] == "600519"
    assert doc.metadata["reference_only"] is True
    assert doc.metadata["is_decision_factor"] is False
    assert result.projected_store._doc_dates[doc.document_id] == "2026-06-21"


def test_empty_bundle_projects_no_documents_and_preserves_base_content():
    base = _base_store()
    bundle = ExternalContextBundle(ticker="600519", records=[])

    result = project_external_context(base, bundle)

    assert result.documents == []
    assert result.projected_store.documents == base.documents
    assert result.projected_store.evidence == base.evidence
    assert result.projected_store.claims == base.claims
    assert result.projected_store._doc_dates == base._doc_dates
