"""FIN-internal knowledge query service.

This module owns the Knowledge Query / Retrieval seam. Callers should use
KnowledgeQueryService.query() instead of composing KnowledgeStore, TextSearch,
windowing, and external context search themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from fin_analyse.knowledge.store import KnowledgeStore
from fin_analyse.knowledge.windowing import filter_hits_by_window, window_since


class _ExternalContextService(Protocol):
    def collect_for_ticker(self, ticker: str) -> Any: ...


def _default_text_search_factory(store: KnowledgeStore) -> Any:
    from fin_analyse.knowledge.search import TextSearch

    return TextSearch(store)


@dataclass(frozen=True)
class KnowledgeQueryRequest:
    query: str
    window: str = "180d"
    include_external_context: bool = False
    ticker: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class KnowledgeQueryResult:
    query: str
    window: str
    since: str | None
    hits: list[dict[str, Any]]
    data_gaps: list[str] = field(default_factory=list)
    filtered_out_count: int = 0

    def to_article_dicts(self) -> list[dict[str, Any]]:
        return list(self.hits)


class KnowledgeQueryService:
    def __init__(
        self,
        store: KnowledgeStore,
        *,
        external_context_service: _ExternalContextService | None = None,
        text_search_factory: Callable[[KnowledgeStore], Any] | None = None,
    ) -> None:
        self.store = store
        self.external_context_service = external_context_service
        self.text_search_factory = text_search_factory or _default_text_search_factory
        # The store is immutable for this service instance.  Building the text
        # index once keeps repeated consultations fast; a source-generation
        # change creates a new service through KnowledgeReferenceReader.
        self._text_search = self.text_search_factory(self.store)

    def query(self, request: KnowledgeQueryRequest) -> KnowledgeQueryResult:
        since = window_since(request.window)
        results: list[dict[str, Any]] = []
        data_gaps: list[str] = []
        seen: set[str] = set()
        filtered_out_count = 0

        matched_docs = self._matched_documents_from_claims(request.query, since)
        skipped_direct = self._append_direct_document_matches(
            request.query, since, matched_docs, results, seen
        )
        filtered_out_count += skipped_direct

        text_hits = self._text_search.search(request.query, top_k=max(request.limit, 50))
        filtered_text_hits = list(filter_hits_by_window(text_hits, self.store, since))
        skipped_text = len(text_hits) - len(filtered_text_hits)
        filtered_out_count += skipped_text

        for hit in filtered_text_hits:
            doc_id = hit["document_id"]
            if doc_id in seen:
                continue
            results.append({**hit, "source": "zsxq", "reference_only": False})
            seen.add(doc_id)

        if request.include_external_context and request.ticker:
            try:
                self._append_external_context_hits(request, results, seen)
            except Exception:
                data_gaps.append("external_context_unavailable")

        results.sort(key=lambda item: item.get("date", ""), reverse=True)
        return KnowledgeQueryResult(
            query=request.query,
            window=request.window,
            since=since,
            hits=results[: request.limit],
            data_gaps=data_gaps,
            filtered_out_count=filtered_out_count,
        )

    def _matched_documents_from_claims(self, query: str, since: str | None) -> set[str]:
        matched_docs: set[str] = set()
        for claim in self.store.find_claims(since=since):
            if query in claim.subject or query in claim.metadata.get("title", ""):
                matched_docs.add(claim.document_id)
        return matched_docs

    def _append_direct_document_matches(
        self,
        query: str,
        since: str | None,
        matched_docs: set[str],
        results: list[dict[str, Any]],
        seen: set[str],
    ) -> int:
        skipped = 0
        for doc in self.store.documents:
            if (
                doc.document_id not in matched_docs
                and query not in doc.title
                and query not in doc.content
            ):
                continue
            date = str(doc.metadata.get("date", ""))
            if since is not None and (not date or date < since):
                skipped += 1
                continue
            results.append(
                {
                    "document_id": doc.document_id,
                    "title": doc.title,
                    "date": date,
                    "source": "zsxq",
                    "reference_only": False,
                }
            )
            seen.add(doc.document_id)
        return skipped

    def _append_external_context_hits(
        self,
        request: KnowledgeQueryRequest,
        results: list[dict[str, Any]],
        seen: set[str],
    ) -> None:
        from fin_analyse.context.search import search_with_external_context
        from fin_analyse.context.service import ExternalContextService

        context_service = self.external_context_service or ExternalContextService()
        bundle = context_service.collect_for_ticker(str(request.ticker))
        for hit in search_with_external_context(
            self.store, request.query, bundle, limit=request.limit
        ):
            doc_id = hit["document_id"]
            if doc_id in seen:
                continue
            results.append(hit)
            seen.add(doc_id)
