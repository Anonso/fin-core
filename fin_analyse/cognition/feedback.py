"""Human feedback helpers for cognition artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha1

from fin_analyse.cognition.evidence_store import JsonlRepository
from fin_analyse.cognition.models import CognitiveFeedback


class FeedbackRecorder:
    def __init__(self, repo: JsonlRepository[CognitiveFeedback]) -> None:
        self.repo = repo

    def record(
        self,
        *,
        target_type: str,
        target_id: str,
        feedback_type: str,
        note: str | None = None,
        now: str | None = None,
    ) -> CognitiveFeedback:
        created_at = now or datetime.now(UTC).isoformat()
        seed = f"{target_type}:{target_id}:{feedback_type}:{note or ''}:{created_at}"
        feedback = CognitiveFeedback(
            feedback_id="fb-" + sha1(seed.encode("utf-8")).hexdigest()[:12],
            target_type=target_type,
            target_id=target_id,
            feedback_type=feedback_type,
            note=note,
            created_at=created_at,
        )
        self.repo.append(feedback)
        return feedback
