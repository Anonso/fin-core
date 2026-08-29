"""Runtime projection of external context into search-only knowledge documents."""

from __future__ import annotations

from dataclasses import dataclass, replace

from fin_analyse.ingestion.models import RawDocument
from fin_analyse.knowledge.store import KnowledgeStore

from .adapter import ExternalContextAdapter
from .models import ExternalContextBundle


@dataclass(frozen=True)
class ExternalContextProjectionResult:
    """Result of projecting reference-only external context into a temporary store."""

    documents: list[RawDocument]
    projected_store: KnowledgeStore
    warnings: list[str]


def _normalize_projected_document(document: RawDocument) -> RawDocument:
    metadata = dict(document.metadata)
    original_source = metadata.get("source", document.source_id)
    metadata.update(
        {
            "source": "external_context",
            "external_source": original_source,
            "reference_only": True,
            "is_decision_factor": False,
        }
    )
    return replace(document, metadata=metadata)


def project_external_context(
    base_store: KnowledgeStore,
    bundle: ExternalContextBundle,
) -> ExternalContextProjectionResult:
    """Return a new store with external context projected as search-only documents.

    The base store is not mutated. External records are not converted to claims.
    """
    adapter = ExternalContextAdapter()
    projected_documents = [
        _normalize_projected_document(document) for document in adapter.to_documents(bundle.records)
    ]
    doc_dates = dict(base_store._doc_dates)
    for document in projected_documents:
        date_val = document.metadata.get("date") or document.metadata.get("occurred_at")
        if date_val:
            doc_dates[document.document_id] = str(date_val)[:10]

    projected_store = KnowledgeStore(
        documents=[*base_store.documents, *projected_documents],
        evidence=list(base_store.evidence),
        claims=list(base_store.claims),
        _doc_dates=doc_dates,
    )
    return ExternalContextProjectionResult(
        documents=projected_documents,
        projected_store=projected_store,
        warnings=list(bundle.warnings),
    )
