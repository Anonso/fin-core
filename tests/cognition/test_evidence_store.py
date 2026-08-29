"""Test JSONL repository."""

from pathlib import Path

from fin_analyse.cognition.evidence_store import JsonlRepository
from fin_analyse.cognition.models import EvidenceItem, SourceLabel


def make_item(evidence_id: str, label: str = "teacher_original") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_type="zsxq_article",
        source_id=evidence_id,
        title="标题",
        content="我认为关键在利润分配。",
        author="郭老师",
        published_at="2026-06-21",
        collected_at="2026-06-21T00:00:00Z",
        companies=["测试公司"],
        topics=["政策"],
        source_label=SourceLabel(
            label=label, teacher_id="guo", confidence=0.9, reasons=["fixture"]
        ),
        reliability=0.8,
        metadata={},
    )


def test_repository_appends_and_reads_items(tmp_path: Path):
    repo = JsonlRepository(tmp_path / "evidence.jsonl", EvidenceItem)

    repo.append(make_item("ev-1"))

    assert repo.list_all() == [make_item("ev-1")]


def test_repository_upsert_replaces_same_id(tmp_path: Path):
    repo = JsonlRepository(tmp_path / "evidence.jsonl", EvidenceItem, id_field="evidence_id")
    repo.upsert(make_item("ev-1", label="unknown"))
    repo.upsert(make_item("ev-1", label="teacher_original"))

    items = repo.list_all()

    assert len(items) == 1
    assert items[0].source_label.label == "teacher_original"


def test_repository_find_filters_items(tmp_path: Path):
    repo = JsonlRepository(tmp_path / "evidence.jsonl", EvidenceItem, id_field="evidence_id")
    repo.append(make_item("ev-1", label="teacher_original"))
    repo.append(make_item("ev-2", label="research_report"))

    matched = repo.find(lambda item: item.source_label.label == "teacher_original")

    assert [item.evidence_id for item in matched] == ["ev-1"]
