from fin_analyse.claims.models import Claim


def test_claim_carries_provenance_and_signal_fields():
    claim = Claim(
        claim_id="c1",
        source_id="zsxq",
        document_id="zsxq:doc1",
        subject="华为",
        predicate="mentioned_in",
        object_value="doc1",
        claim_type="company_mention",
        polarity="neutral",
        horizon="180d",
        confidence=0.8,
        evidence_ids=["e1"],
    )

    assert claim.source_id == "zsxq"
    assert claim.document_id == "zsxq:doc1"
    assert claim.evidence_ids == ["e1"]
    assert claim.status == "active"
    assert claim.extracted_by == "rule"
