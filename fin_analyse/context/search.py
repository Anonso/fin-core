"""Opt-in search over runtime-projected external context."""

from __future__ import annotations

from typing import Any

from fin_analyse.knowledge.search import TextSearch
from fin_analyse.knowledge.store import KnowledgeStore

from .models import ExternalContextBundle
from .projection import project_external_context


def _source_for(document_id: str, doc_metadata: dict[str, Any]) -> str:
    source = doc_metadata.get("source")
    if source == "external_context":
        return "external_context"
    return "zsxq"


def search_with_external_context(
    store: KnowledgeStore,
    query: str,
    bundle: ExternalContextBundle,
    limit: int = 10,
) -> list[dict]:
    """Search a temporary store that includes reference-only external context."""
    projection = project_external_context(store, bundle)
    projected_store = projection.projected_store
    doc_map = {doc.document_id: doc for doc in projected_store.documents}
    results: list[dict] = []
    for hit in TextSearch(projected_store).search(query, top_k=limit):
        document = doc_map.get(hit["document_id"])
        metadata = document.metadata if document else {}
        source = _source_for(hit["document_id"], metadata)
        enriched = dict(hit)
        enriched["source"] = source
        enriched["reference_only"] = source == "external_context"
        if source == "external_context":
            enriched["source_category"] = metadata.get("category", "")
            enriched["external_source"] = metadata.get("external_source", "")
        results.append(enriched)
    return results
