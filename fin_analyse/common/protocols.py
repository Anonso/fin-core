"""Shared protocols for dependency injection and testing.

Protocols defined here allow gateway services to depend on interfaces rather
than concrete classes, enabling test doubles and provider interchangeability.
"""

from typing import Protocol, runtime_checkable

from fin_analyse.claims import Claim
from fin_analyse.ingestion import RawDocument


@runtime_checkable
class ClaimRepository(Protocol):
    """Read-side interface for querying claims and their source documents."""

    def find_claims(
        self,
        *,
        company: str | None = None,
        since: str | None = None,
        claim_type: str | None = None,
    ) -> list[Claim]: ...

    def get_document(self, doc_id: str) -> RawDocument | None: ...
