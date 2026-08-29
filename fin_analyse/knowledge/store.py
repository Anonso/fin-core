"""In-memory knowledge store."""

from dataclasses import dataclass, field

from fin_analyse.claims import Claim, RuleBasedClaimExtractor
from fin_analyse.common.protocols import ClaimRepository
from fin_analyse.ingestion.adapters import SourceAdapter
from fin_analyse.ingestion.models import Evidence, RawDocument


@dataclass(frozen=True)
class KnowledgeStore(ClaimRepository):
    documents: list[RawDocument]
    evidence: list[Evidence]
    claims: list[Claim]
    _doc_dates: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_adapter(
        cls, adapter: SourceAdapter, extractor: RuleBasedClaimExtractor
    ) -> "KnowledgeStore":
        documents = adapter.fetch()
        evidence = []
        doc_dates: dict[str, str] = {}
        for document in documents:
            if hasattr(adapter, "extract_evidence"):
                evidence.extend(adapter.extract_evidence(document))
            date_val = document.metadata.get("date", "")
            if date_val:
                doc_dates[document.document_id] = str(date_val)[:10]
        claims = [claim for item in evidence for claim in extractor.extract(item)]
        return cls(documents=documents, evidence=evidence, claims=claims, _doc_dates=doc_dates)

    def find_claims(
        self,
        company: str | None = None,
        topic: str | None = None,
        claim_type: str | None = None,
        min_score: float | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[Claim]:
        results = self.claims
        if since is not None or until is not None:
            results = [claim for claim in results if self._claim_in_window(claim, since, until)]
        if company is not None:
            results = [
                claim
                for claim in results
                if claim.claim_type == "company_mention" and claim.subject == company
            ]
        if topic is not None:
            results = [
                claim
                for claim in results
                if claim.claim_type == "topic_tag" and claim.subject == topic
            ]
        if claim_type is not None:
            results = [claim for claim in results if claim.claim_type == claim_type]
        if min_score is not None:
            results = [
                claim
                for claim in results
                if claim.claim_type == "article_score" and float(claim.object_value) >= min_score
            ]
        return results

    def get_document(self, doc_id: str) -> RawDocument | None:
        for doc in self.documents:
            if doc.document_id == doc_id:
                return doc
        return None

    def _claim_in_window(self, claim: Claim, since: str | None, until: str | None) -> bool:
        doc_date = self._doc_dates.get(claim.document_id, "")
        if not doc_date:
            return False
        if since is not None and doc_date < since:
            return False
        return not (until is not None and doc_date > until)
