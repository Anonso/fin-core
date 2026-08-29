"""Convert external context records into existing ingestion models."""

from __future__ import annotations

from fin_analyse.ingestion.models import Evidence, RawDocument

from .models import ExternalContextRecord

_REFERENCE_NOTE = "仅供学徒认知参考，不代表老师认知，不构成交易决定。"


def _importance_to_score(importance: float) -> float | None:
    """Map importance (0.0–1.0) to a score on the 0–10 scale used by tiers."""
    if importance <= 0:
        return None
    return round(min(importance * 10.0, 10.0), 1)


class ExternalContextAdapter:
    """Adapter from reference-only context records to RawDocument/Evidence."""

    source_id = "external-context"

    def to_documents(self, records: list[ExternalContextRecord]) -> list[RawDocument]:
        documents: list[RawDocument] = []
        for record in records:
            content = f"{record.summary}\n\n{_REFERENCE_NOTE}"
            companies = [record.ticker] if record.ticker else []
            score = _importance_to_score(record.importance)
            documents.append(
                RawDocument(
                    source_id=self.source_id,
                    external_id=record.record_id,
                    title=record.title,
                    content=content,
                    url=record.url,
                    metadata={
                        "source": record.source,
                        "category": record.category,
                        "ticker": record.ticker,
                        "date": record.occurred_at,
                        "occurred_at": record.occurred_at,
                        "companies": companies,
                        "score": score,
                        "column": "外部参考",
                        "is_qa": False,
                        "importance": record.importance,
                        "is_decision_factor": record.is_decision_factor,
                        **record.metadata,
                    },
                )
            )
        return documents

    def to_evidence(self, records: list[ExternalContextRecord]) -> list[Evidence]:
        evidence: list[Evidence] = []
        for record in records:
            evidence.append(
                Evidence(
                    evidence_id=f"external-context:{record.record_id}:evidence",
                    source_id=self.source_id,
                    document_id=f"{self.source_id}:{record.record_id}",
                    evidence_type="external_context",
                    content=record.summary,
                    metadata={
                        "source": record.source,
                        "category": record.category,
                        "ticker": record.ticker,
                        "date": record.occurred_at,
                        "occurred_at": record.occurred_at,
                        "title": record.title,
                        "url": record.url,
                        "is_decision_factor": record.is_decision_factor,
                        **record.metadata,
                    },
                )
            )
        return evidence
