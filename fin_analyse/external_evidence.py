"""The one thin FIN facade for source-native non-G evidence reads."""

from __future__ import annotations

from fin_analyse.official_records.evidence import (
    OfficialRecordEvidence,
    OfficialRecordEvidenceReader,
    OfficialRecordEvidenceRequest,
    OfficialRecordEvidenceService,
    build_default_official_record_evidence,
)


class ExternalEvidenceReader:
    """Delegate official company records without owning Web, cache, or conclusions."""

    def __init__(self, *, official_records: OfficialRecordEvidenceReader | None = None) -> None:
        self._official_records = official_records

    def read(self, request: OfficialRecordEvidenceRequest) -> OfficialRecordEvidence:
        if self._official_records is None:
            return OfficialRecordEvidenceService().read(request)
        return self._official_records.read(request)


def build_default_external_evidence_reader() -> ExternalEvidenceReader:
    """Compose the facade with the source-native official-record owner."""

    return ExternalEvidenceReader(official_records=build_default_official_record_evidence())


__all__ = ["ExternalEvidenceReader", "build_default_external_evidence_reader"]
