from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fin_analyse.official_records.evidence import (
    OfficialRecordCapture,
    OfficialRecordDocument,
    OfficialRecordEvidenceRequest,
    OfficialRecordEvidenceService,
    OfficialRecordSourceResult,
    build_default_official_record_evidence,
    official_record_artifact_root,
)


@dataclass
class _Source:
    result: OfficialRecordSourceResult

    def read(self, request: OfficialRecordEvidenceRequest) -> OfficialRecordSourceResult:
        del request
        return self.result


def test_reader_projects_official_documents_and_keeps_a_confirmed_empty_result_distinct() -> None:
    as_of = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    capture = OfficialRecordCapture(
        provider="cninfo",
        revision="a" * 64,
        retrieved_at=as_of - timedelta(minutes=1),
    )
    reader = OfficialRecordEvidenceService(
        source=_Source(
            OfficialRecordSourceResult(
                documents={
                    "600519.SH": (
                        OfficialRecordDocument(
                            symbol="600519.SH",
                            document_id="1200000001",
                            document_kind="financial_report",
                            title="2025年年度报告",
                            source_event_at=as_of - timedelta(days=100),
                            url="https://static.cninfo.com.cn/finalpage/report.pdf",
                            content_hash="b" * 64,
                        ),
                    ),
                    "000001.SZ": (),
                },
                captures={
                    "600519.SH": capture,
                    "000001.SZ": capture,
                },
            )
        )
    )

    payload = reader.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH", "000001.SZ"),
            as_of=as_of,
        )
    ).to_agent_dict()

    assert payload["schema_version"] == "fin.official-record-evidence/v1"
    assert payload["source_trust"] == "non_g"
    assert payload["status"] == "READY"
    assert payload["valid_until"] == (as_of + timedelta(minutes=14)).isoformat()
    [report, empty] = payload["instruments"]
    assert report == {
        "symbol": "600519.SH",
        "status": "READY",
        "source": {
            "provider": "cninfo",
            "revision": "a" * 64,
            "payload_sha256": "a" * 64,
            "retrieved_at": (as_of - timedelta(minutes=1)).isoformat(),
            "stale": False,
        },
        "documents": [
            {
                "document_id": "1200000001",
                "document_kind": "financial_report",
                "title": "2025年年度报告",
                "source_event_at": (as_of - timedelta(days=100)).isoformat(),
                "url": "https://static.cninfo.com.cn/finalpage/report.pdf",
                "content_hash": "b" * 64,
            }
        ],
        "data_gaps": [],
    }
    assert empty["symbol"] == "000001.SZ"
    assert empty["status"] == "EMPTY"
    assert empty["documents"] == []
    assert empty["data_gaps"] == []
    assert "OFFICIAL_RECORD_EVIDENCE_UNAVAILABLE" not in payload["data_gaps"]


def test_default_reader_keeps_artifacts_outside_checkout_until_a_real_read(tmp_path) -> None:
    now = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    environ = {
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    root = official_record_artifact_root(environ=environ)

    payload = (
        build_default_official_record_evidence(
            environ=environ,
            clock=lambda: now,
        )
        .read(
            OfficialRecordEvidenceRequest(
                instruments=("600519.SH",),
                as_of=now,
                deadline_at=now,
            )
        )
        .to_agent_dict()
    )

    assert root == (tmp_path / "state" / "fin-analyse" / "official-record-evidence-v1").resolve()
    assert not root.exists()
    assert "OFFICIAL_RECORD_EVIDENCE_DEADLINE_REACHED:600519.SH" in payload["data_gaps"]
    assert "OFFICIAL_RECORD_EVIDENCE_UNAVAILABLE" in payload["data_gaps"]


def test_reader_fails_closed_when_a_source_result_has_an_invalid_container() -> None:
    as_of = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    reader = OfficialRecordEvidenceService(
        source=_Source(
            OfficialRecordSourceResult(
                documents=[],  # type: ignore[arg-type]
                captures={},
            )
        )
    )

    result = reader.read(OfficialRecordEvidenceRequest(instruments=("600519.SH",), as_of=as_of))

    assert result.status == "UNKNOWN"
    assert result.instruments[0]["status"] == "UNKNOWN"
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_SOURCE_RESULT_INVALID",)


def test_reader_strips_an_insecure_document_url_from_official_evidence() -> None:
    as_of = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    capture = OfficialRecordCapture(
        provider="cninfo",
        revision="a" * 64,
        retrieved_at=as_of,
    )
    reader = OfficialRecordEvidenceService(
        source=_Source(
            OfficialRecordSourceResult(
                documents={
                    "600519.SH": (
                        OfficialRecordDocument(
                            symbol="600519.SH",
                            document_id="1200000001",
                            document_kind="financial_report",
                            title="2025年年度报告",
                            source_event_at=as_of,
                            url="http://static.cninfo.com.cn/report.pdf",
                            content_hash="b" * 64,
                        ),
                    )
                },
                captures={"600519.SH": capture},
            )
        )
    )

    result = reader.read(OfficialRecordEvidenceRequest(instruments=("600519.SH",), as_of=as_of))

    assert result.status == "PARTIAL"
    assert result.instruments[0]["documents"] == []
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_DOCUMENT_INVALID",)
