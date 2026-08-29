"""Tests for backfill runner with temporary markdown fixtures."""

from pathlib import Path
from unittest.mock import MagicMock

from fin_analyse.cognition.backfill import (
    BackfillReport,
    CognitionBackfillRunner,
    _markdown_to_evidence,
    _parse_markdown,
)
from fin_analyse.cognition.models import EvidenceItem, SourceLabel

# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------


def test_parse_markdown_with_frontmatter():
    path = Path("/tmp/test.md")
    path.write_text("---\nid: abc123\ndate: 2026-01-01\n---\n# Title\nBody text.")

    result = _parse_markdown(path)
    assert result is not None
    assert result["meta"]["id"] == "abc123"
    assert "Title" in str(result["content"])


def test_parse_markdown_without_frontmatter():
    path = Path("/tmp/test_nofm.md")
    path.write_text("# Just a title\nBody text.")

    result = _parse_markdown(path)
    assert result is not None
    assert result["content"]


def test_parse_markdown_missing_file():
    result = _parse_markdown(Path("/tmp/nonexistent.md"))
    assert result is None


def test_markdown_to_evidence(tmp_path: Path):
    path = tmp_path / "test.md"
    path.write_text(
        "---\nid: test1\ndate: 2026-01-01\ncolumn: 郭老师专栏\ncompanies: [A, B]\ntags: [液冷]\n---\n# 标题\n正文内容需要足够长才能通过回填提取的最小长度校验，这段话至少要有三十个字符。",
        encoding="utf-8",
    )
    data = {
        "meta": {
            "id": "test1",
            "date": "2026-01-01",
            "column": "郭老师专栏",
            "companies": ["A", "B"],
            "tags": ["液冷"],
        },
        "content": "# 标题\n正文内容需要足够长才能通过回填提取的最小长度校验，这段话至少要有三十个字符。",
    }
    ev = _markdown_to_evidence(path, data, teacher_id="guo")
    assert ev is not None
    assert ev.source_type == "zsxq_article"
    assert ev.author == "郭老师专栏"
    assert ev.companies == ["A", "B"]
    assert ev.topics == ["液冷"]


def test_markdown_to_evidence_skips_empty_content():
    data = {"meta": {}, "content": ""}
    ev = _markdown_to_evidence(Path("test.md"), data, teacher_id="guo")
    assert ev is None


# ---------------------------------------------------------------------------
# BackfillReport
# ---------------------------------------------------------------------------


def test_backfill_report_total_processed():
    r = BackfillReport(evidence_saved_count=5, skipped_count=2)
    assert r.total_processed == 7


# ---------------------------------------------------------------------------
# Backfill runner with fake service
# ---------------------------------------------------------------------------


def make_service():
    """Build a lightweight fake CognitiveService for backfill testing."""
    svc = MagicMock()
    svc.llm_available = True
    svc.evidence_repo = MagicMock()
    svc.evidence_repo.find.return_value = []

    def _label(evidence_id):
        return SourceLabel("teacher_original", "guo", 0.85, ["rule matched"])

    svc.label_evidence.side_effect = _label
    svc.labeler.label.return_value = SourceLabel("teacher_original", "guo", 0.85, [])

    svc.extract_teacher_reasoning.return_value = []
    svc.extractor.extract.return_value = []

    # persona_gate_decision returns a mock that allows persona by default,
    # so existing backfill tests continue to exercise trace extraction.
    gate_decision_mock = MagicMock()
    gate_decision_mock.allows_persona = True
    gate_decision_mock.category = "star_teacher_original"
    svc.persona_gate_decision.return_value = gate_decision_mock
    return svc


def test_backfill_dry_run_writes_nothing(tmp_path: Path):
    (tmp_path / "articles").mkdir()
    (tmp_path / "articles" / "a.md").write_text(
        "---\nid: a1\ndate: 2026-01-01\ncolumn: 郭老师专栏\ncompanies: [A]\ntags: [测试]\n---\n# Hello\nSome content for testing the backfill runner with enough characters to pass the minimum body length check.",
        encoding="utf-8",
    )

    svc = make_service()
    runner = CognitionBackfillRunner(tmp_path, svc, dry_run=True, limit=1)
    report = runner.run()

    assert report.scanned_count == 1
    assert report.evidence_saved_count == 1
    assert report.skipped_count == 0
    svc.save_evidence.assert_not_called()


def test_backfill_limit_respected(tmp_path: Path):
    (tmp_path / "articles").mkdir()
    for i in range(5):
        (tmp_path / "articles" / f"{i}.md").write_text(
            f"---\nid: {i}\ndate: 2026-01-01\ncolumn: 郭老师专栏\ncompanies: [A]\ntags: [测试]\n---\n# Art {i}\nContent {i} with additional text to reach the minimum thirty character body length requirement.",
            encoding="utf-8",
        )

    svc = make_service()
    runner = CognitionBackfillRunner(tmp_path, svc, dry_run=True, limit=3)
    report = runner.run()

    assert report.scanned_count == 3
    assert report.total_processed == 3


def test_backfill_resume_skips_existing(tmp_path: Path):
    (tmp_path / "articles").mkdir()
    (tmp_path / "articles" / "existing.md").write_text(
        "---\nid: exist1\ndate: 2026-01-01\ncolumn: 郭老师专栏\ncompanies: [A]\ntags: [测试]\n---\n# Existing\nContent with enough text to satisfy the minimum body length requirement for evidence extraction.",
        encoding="utf-8",
    )
    (tmp_path / "articles" / "new.md").write_text(
        "---\nid: new1\ndate: 2026-01-01\ncolumn: 郭老师专栏\ncompanies: [A]\ntags: [测试]\n---\n# New Article\nWith more content that is sufficiently long to pass the body validation check for evidence extraction.",
        encoding="utf-8",
    )

    svc = make_service()
    # first article already exists
    svc.evidence_repo.find.side_effect = [
        [MagicMock()],  # existing
        [],  # new
    ]

    runner = CognitionBackfillRunner(tmp_path, svc, resume=True, dry_run=True)
    report = runner.run()

    assert report.scanned_count == 2
    assert report.skipped_count == 1
    assert report.evidence_saved_count == 1


def test_backfill_empty_article_dir(tmp_path: Path):
    (tmp_path / "articles").mkdir()
    svc = make_service()
    runner = CognitionBackfillRunner(tmp_path, svc)
    report = runner.run()

    assert report.scanned_count == 0
    assert report.evidence_saved_count == 0


def test_backfill_missing_article_dir(tmp_path: Path):
    svc = make_service()
    runner = CognitionBackfillRunner(tmp_path, svc)
    report = runner.run()

    assert report.scanned_count == 0


def test_markdown_to_evidence_preserves_column_metadata(tmp_path: Path):
    path = tmp_path / "star.md"
    data = {
        "meta": {
            "id": "star1",
            "date": "2026-06-27",
            "column": "星大派锐评",
            "score": "9.1",
            "is_qa": False,
        },
        "content": "# 星大派锐评\n关键变量、产业链逻辑和风险边界都写在正文里，足够长的正文内容以通过最小长度校验。",
    }
    evidence = _markdown_to_evidence(path, data, teacher_id="guo")
    assert evidence is not None
    assert evidence.author == "星大派锐评"
    assert evidence.metadata["column"] == "星大派锐评"
    assert evidence.metadata["score"] == "9.1"


def test_backfill_report_counts_persona_gate_decisions(tmp_path: Path):
    (tmp_path / "articles").mkdir()
    (tmp_path / "articles" / "star.md").write_text(
        "---\nid: star1\ndate: 2026-06-27\ncolumn: 星大派锐评\n---\n"
        "# 星大派锐评\n关键不在情绪，而在订单、价格和利润率是否兑现；需要观察风险边界。",
        encoding="utf-8",
    )
    (tmp_path / "articles" / "report.md").write_text(
        "---\nid: report1\ndate: 2026-06-27\ncolumn: 普通\nscore: 9.3\n---\n"
        "# 9分研报\n券商研报给予买入评级，盈利预测和目标价均上调，正文足够长。",
        encoding="utf-8",
    )

    svc = make_service()
    from fin_analyse.cognition.persona_gate import PersonaIngestionGate, apply_persona_gate

    gate = PersonaIngestionGate()
    saved: dict[str, EvidenceItem] = {}

    def _save(evidence: EvidenceItem):
        saved[evidence.evidence_id] = evidence

    def _label(evidence_id: str):
        evidence = saved[evidence_id]
        label = SourceLabel("unknown", "guo", 0.5, ["fixture"])
        if evidence.metadata.get("score") == "9.3":
            label = SourceLabel("research_report", "guo", 0.9, ["fixture report"])
        updated_dict = {**evidence.to_dict(), "source_label": label.to_dict()}
        gated = apply_persona_gate(
            EvidenceItem.from_dict(updated_dict),
            gate.evaluate(EvidenceItem.from_dict(updated_dict)),
        )
        saved[evidence_id] = gated
        return gated.source_label

    def _decision(evidence_id: str):
        return saved[evidence_id].metadata["persona_gate"]

    svc.save_evidence.side_effect = _save
    svc.label_evidence.side_effect = _label
    svc.persona_gate_decision.side_effect = lambda eid: gate.evaluate(saved[eid])
    svc.extract_teacher_reasoning.return_value = []

    runner = CognitionBackfillRunner(tmp_path, svc, dry_run=False)
    report = runner.run()

    assert report.evidence_saved_count == 2
    assert report.persona_eligible_count == 1
    assert report.persona_rejected_count == 1
    assert report.persona_gate_unknown_count == 0
