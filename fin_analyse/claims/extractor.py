"""Rule-based claim extraction."""

from __future__ import annotations

from typing import Any

from fin_analyse.ingestion.models import Evidence
from fin_analyse.utils.ids import stable_id

from .models import Claim


class RuleBasedClaimExtractor:
    def extract(self, evidence: Evidence) -> list[Claim]:
        claims: list[Claim] = []
        temporal = self._temporal_metadata(evidence)
        claims.extend(self._company_claims(evidence, temporal))
        claims.extend(self._topic_claims(evidence, temporal))
        score_claim = self._score_claim(evidence, temporal)
        if score_claim:
            claims.append(score_claim)
        return claims

    def _temporal_metadata(self, evidence: Evidence) -> dict[str, Any]:
        """Resolve deterministic temporal horizon and metadata from evidence text.

        Uses rule-based keyword classification (no LLM). Falls back to
        horizon="180d" with data_gap marker when assessment is unknown or fails.
        """
        title = str(evidence.metadata.get("title", ""))
        content = evidence.content
        column = str(evidence.metadata.get("column", ""))

        try:
            from fin_analyse.cognition.time_sensitivity import assess_text_time_sensitivity

            assessment = assess_text_time_sensitivity(
                title=title,
                content=content,
                column=column,
            )
        except Exception:
            # On any error, preserve old behaviour with data gap marker
            return {
                "time_sensitivity": "180d",
                "temporal_category": "unknown",
                "time_sensitivity_reason": "temporal assessment failed, fallback to 180d",
                "unit_type": "days",
                "horizon": "180d",
                "data_gap": "temporal_assessment_error",
            }

        horizon = assessment.horizon
        if not horizon or horizon == "unknown":
            # No temporal clues found → keep existing 180d default
            horizon = "180d"

        return {
            "time_sensitivity": assessment.horizon if assessment.horizon != "unknown" else "180d",
            "temporal_category": assessment.category,
            "time_sensitivity_reason": assessment.reason,
            "unit_type": "days",
            "horizon": horizon,
            "temporal_confidence": assessment.confidence,
            "temporal_evidence": assessment.evidence[:5] if assessment.evidence else [],
            "data_gap": assessment.data_gaps[0] if assessment.data_gaps else None,
        }

    def _company_claims(
        self, evidence: Evidence, temporal: dict[str, Any] | None = None
    ) -> list[Claim]:
        companies = evidence.metadata.get("companies") or []
        score = self._score(evidence)
        polarity = "positive" if score is not None and score >= 7 else "neutral"
        temporal = temporal or {}
        horizon = temporal.get("horizon", "180d")
        meta = {"title": evidence.metadata.get("title", "")}
        # Merge temporal metadata into claim metadata
        for key in (
            "time_sensitivity",
            "temporal_category",
            "time_sensitivity_reason",
            "unit_type",
            "temporal_confidence",
            "temporal_evidence",
            "data_gap",
        ):
            if key in temporal and temporal[key] is not None:
                meta[key] = temporal[key]
        return [
            Claim(
                claim_id=self._claim_id(evidence, "company_mention", company),
                source_id=evidence.source_id,
                document_id=evidence.document_id,
                subject=company,
                predicate="mentioned_in",
                object_value=evidence.document_id,
                claim_type="company_mention",
                polarity=polarity,
                horizon=horizon,
                confidence=0.75,
                evidence_ids=[evidence.evidence_id],
                metadata=meta,
            )
            for company in companies
        ]

    def _topic_claims(
        self, evidence: Evidence, temporal: dict[str, Any] | None = None
    ) -> list[Claim]:
        tags = evidence.metadata.get("tags") or []
        temporal = temporal or {}
        horizon = temporal.get("horizon", "180d")
        meta = {"title": evidence.metadata.get("title", "")}
        for key in (
            "time_sensitivity",
            "temporal_category",
            "time_sensitivity_reason",
            "unit_type",
            "temporal_confidence",
            "temporal_evidence",
            "data_gap",
        ):
            if key in temporal and temporal[key] is not None:
                meta[key] = temporal[key]
        return [
            Claim(
                claim_id=self._claim_id(evidence, "topic_tag", tag),
                source_id=evidence.source_id,
                document_id=evidence.document_id,
                subject=tag,
                predicate="tagged_in",
                object_value=evidence.document_id,
                claim_type="topic_tag",
                polarity="neutral",
                horizon=horizon,
                confidence=0.9,
                evidence_ids=[evidence.evidence_id],
                metadata=meta,
            )
            for tag in tags
        ]

    def _score_claim(
        self, evidence: Evidence, temporal: dict[str, Any] | None = None
    ) -> Claim | None:
        score = self._score(evidence)
        if score is None:
            return None
        temporal = temporal or {}
        horizon = temporal.get("horizon", "180d")
        meta = {"score": score, "title": evidence.metadata.get("title", "")}
        for key in (
            "time_sensitivity",
            "temporal_category",
            "time_sensitivity_reason",
            "unit_type",
            "temporal_confidence",
            "temporal_evidence",
            "data_gap",
        ):
            if key in temporal and temporal[key] is not None:
                meta[key] = temporal[key]
        return Claim(
            claim_id=self._claim_id(evidence, "article_score", str(score)),
            source_id=evidence.source_id,
            document_id=evidence.document_id,
            subject=evidence.document_id,
            predicate="has_score",
            object_value=str(score),
            claim_type="article_score",
            polarity="positive" if score >= 7 else "neutral",
            horizon=horizon,
            confidence=1.0,
            evidence_ids=[evidence.evidence_id],
            metadata=meta,
        )

    def _score(self, evidence: Evidence) -> float | None:
        score = evidence.metadata.get("score")
        return score if isinstance(score, int | float) else None

    def _claim_id(self, evidence: Evidence, claim_type: str, subject: str) -> str:
        return stable_id(evidence.evidence_id, ":", claim_type, ":", subject, prefix="claim:")
