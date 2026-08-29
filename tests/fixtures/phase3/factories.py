from datetime import UTC, datetime

from fin_analyse.claims.models import Claim
from fin_analyse.ingestion.models import RawDocument


def fixed_now() -> datetime:
    return datetime(2026, 6, 20, 9, 0, tzinfo=UTC)


def make_document(
    article_id="a1",
    score=8.8,
    column="普通",
    is_qa=False,
    companies=None,
    tags=None,
    visible_at=None,
):
    metadata = {
        "id": article_id,
        "date": "2026-06-18 08:55",
        "column": column,
        "is_qa": is_qa,
        "companies": [] if companies is None else companies,
        "tags": [] if tags is None else tags,
    }
    if score is not None:
        metadata["score"] = score
    if visible_at is not None:
        metadata["visible_at"] = visible_at.isoformat()
    return RawDocument(
        "zsxq",
        article_id,
        f"标题{article_id}",
        "正文内容",
        metadata=metadata,
        fetched_at=fixed_now(),
    )


def make_claim(
    claim_id="c1",
    document_id="zsxq:a1",
    subject="华为",
    claim_type="company_mention",
    polarity="positive",
    confidence=0.8,
    visible_at=None,
):
    metadata = {}
    if visible_at is not None:
        metadata["visible_at"] = visible_at.isoformat()
    return Claim(
        claim_id,
        "zsxq",
        document_id,
        subject,
        "mentioned_in",
        "article",
        claim_type,
        polarity,
        "90d",
        confidence,
        [f"e-{claim_id}"],
        metadata=metadata,
        observed_at=fixed_now(),
    )
