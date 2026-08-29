"""Interface tests for deterministic knowledge reference evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fin_analyse.ingestion.models import RawDocument
from fin_analyse.knowledge.query import KnowledgeQueryRequest, KnowledgeQueryResult
from fin_analyse.knowledge.reference_evidence import (
    KnowledgeReferenceReader,
    KnowledgeReferenceRequest,
)
from fin_analyse.knowledge.store import KnowledgeStore


def _document(
    external_id: str,
    *,
    title: str,
    content: str = "有效参考资料",
    date: str = "2026-07-20 10:00",
    **metadata: object,
) -> RawDocument:
    return RawDocument(
        source_id="zsxq",
        external_id=external_id,
        title=title,
        content=content,
        metadata={"date": date, **metadata},
    )


@dataclass
class _Query:
    hits: list[dict[str, Any]]
    requests: list[KnowledgeQueryRequest] = field(default_factory=list)

    def query(self, request: KnowledgeQueryRequest) -> KnowledgeQueryResult:
        self.requests.append(request)
        return KnowledgeQueryResult(
            query=request.query,
            window=request.window,
            since=None,
            hits=list(self.hits),
        )


def _hit(document: RawDocument) -> dict[str, object]:
    return {"document_id": document.document_id}


def test_read_classifies_every_knowledge_item_as_non_g_reference() -> None:
    qa = _document("qa", title="用户 提问：雅克科技怎么看", is_qa=True)
    curated = _document("curated", title="产业链观察", column="星大派特刊")
    generic = _document("generic", title="普通分享", column="普通")
    store = KnowledgeStore(documents=[qa, curated, generic], evidence=[], claims=[])
    query = _Query([_hit(qa), _hit(curated), _hit(generic)])

    bundle = KnowledgeReferenceReader(store=store, query_service=query).read(
        KnowledgeReferenceRequest(
            query="雅克科技风险",
            window="180d",
            as_of=datetime(2026, 7, 23, tzinfo=UTC),
        )
    )

    assert bundle.status == "READY"
    assert [item.source_class for item in bundle.items] == [
        "ZSXQ_QA_REFERENCE",
        "ZSXQ_CURATED_REFERENCE",
        "ZSXQ_REFERENCE",
    ]
    assert all(item.source_trust == "NON_G_REFERENCE" for item in bundle.items)
    assert all(item.reference_only is True for item in bundle.items)
    assert all(item.instruction_authority == "none" for item in bundle.items)
    assert all(len(item.content_sha256) == 64 for item in bundle.items)
    assert query.requests == [
        KnowledgeQueryRequest(
            query="雅克科技风险",
            window="180d",
            include_external_context=False,
            ticker=None,
            limit=10,
        )
    ]


def test_read_excludes_future_material_and_preserves_source_dates() -> None:
    current = _document("current", title="当前资料", date="2026-07-22 09:30")
    future = _document("future", title="未来资料", date="2026-07-24 09:30")
    store = KnowledgeStore(documents=[current, future], evidence=[], claims=[])

    bundle = KnowledgeReferenceReader(
        store=store,
        query_service=_Query([_hit(current), _hit(future)]),
    ).read(
        KnowledgeReferenceRequest(
            query="资料",
            as_of=datetime(2026, 7, 23, tzinfo=UTC),
        )
    )

    assert [item.source_ref for item in bundle.items] == [current.document_id]
    assert bundle.items[0].published_at == "2026-07-22T09:30:00+08:00"
    assert bundle.items[0].available_at == bundle.items[0].published_at
    assert "knowledge_reference_future_excluded" in bundle.data_gaps


def test_read_deduplicates_bounds_context_and_rejects_invalid_refs() -> None:
    documents = [
        _document(str(index), title=f"资料 {index}", content="证据" * 10_000) for index in range(12)
    ]
    store = KnowledgeStore(documents=documents, evidence=[], claims=[])
    hits = [
        _hit(documents[0]),
        _hit(documents[0]),
        {"document_id": "bad\nref"},
        *(_hit(document) for document in documents[1:]),
    ]

    bundle = KnowledgeReferenceReader(
        store=store,
        query_service=_Query(hits),
    ).read(KnowledgeReferenceRequest(query="资料"))

    assert bundle.status == "READY"
    assert len({item.source_ref for item in bundle.items}) == len(bundle.items)
    assert len(bundle.items) <= 10
    assert sum(len(item.content) for item in bundle.items) < 32_000
    assert "knowledge_reference_context_truncated" in bundle.data_gaps


def test_invalid_request_and_empty_evidence_are_typed_without_side_effects() -> None:
    query = _Query([])
    reader = KnowledgeReferenceReader(
        store=KnowledgeStore(documents=[], evidence=[], claims=[]),
        query_service=query,
    )

    invalid = reader.read(KnowledgeReferenceRequest(query=" "))
    empty = reader.read(KnowledgeReferenceRequest(query="有效问题"))

    assert invalid.status == "UNKNOWN"
    assert invalid.data_gaps == ("knowledge_reference_query_invalid",)
    assert query.requests == [
        KnowledgeQueryRequest(
            query="有效问题",
            window="180d",
            include_external_context=False,
            ticker=None,
            limit=10,
        )
    ]
    assert empty.status == "EMPTY"
    assert empty.data_gaps == ("knowledge_reference_evidence_unavailable",)


def test_root_reader_reuses_snapshot_until_source_fingerprint_changes(tmp_path: Path) -> None:
    articles = tmp_path / "articles"
    articles.mkdir()
    index = tmp_path / "index.json"
    article = articles / "a.md"
    index.write_text('{"articles":[]}', encoding="utf-8")
    article.write_text(
        "---\nid: a\ndate: 2026-07-20 10:00\ncolumn: 普通\n---\n# 雅克科技旧资料\n旧观察",
        encoding="utf-8",
    )
    reader = KnowledgeReferenceReader.from_root(tmp_path)

    first = reader.read(KnowledgeReferenceRequest(query="雅克科技"))
    unchanged = reader.read(KnowledgeReferenceRequest(query="雅克科技"))

    assert first.status == "READY"
    assert unchanged.items == first.items
    assert first.items[0].content == "旧观察"

    article.write_text(
        "---\nid: a\ndate: 2026-07-21 10:00\ncolumn: 普通\n---\n# 雅克科技新资料\n新观察",
        encoding="utf-8",
    )
    index.write_text('{"articles":[{"id":"a"}]}', encoding="utf-8")

    refreshed = reader.read(KnowledgeReferenceRequest(query="雅克科技"))

    assert refreshed.status == "READY"
    assert refreshed.items[0].content == "新观察"
    assert refreshed.items[0].content_sha256 != first.items[0].content_sha256
