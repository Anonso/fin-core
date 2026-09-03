"""Test CognitiveService orchestration."""

from pathlib import Path

from fin_analyse.cognition.models import (
    EvidenceItem,
    ReasoningTrace,
    SourceLabel,
    TraceVerification,
)
from fin_analyse.cognition.service import CognitiveService


class FakeVerifier:
    def __init__(self):
        self.calls: list[tuple[ReasoningTrace, EvidenceItem]] = []

    def verify(self, trace: ReasoningTrace, evidence: EvidenceItem) -> TraceVerification:
        self.calls.append((trace, evidence))
        return TraceVerification(
            verification_id=f"tv-{trace.trace_id}",
            trace_id=trace.trace_id,
            source_evidence_id=trace.source_evidence_id,
            teacher_id=trace.teacher_id,
            verdict="keep",
            verified_confidence=0.7,
            confidence_adjustment=0.2,
            issues=[],
            suggested_revision={},
            reason="fake ok",
            verifier_backend="fake",
            created_at="2026-06-23T00:00:00+00:00",
        )


def _service_trace(trace_id: str, confidence: float) -> ReasoningTrace:
    return ReasoningTrace(
        trace_id=trace_id,
        teacher_id="guo",
        source_evidence_id="ev-service",
        topic="测试主题",
        companies=["测试公司"],
        premises=["p"],
        observed_variables=["v"],
        inferred_relationships=["r"],
        conclusion="c",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["risk"],
        invalidation_conditions=["invalid"],
        action_implications=["action"],
        extraction_confidence=confidence,
    )


def _service_evidence() -> EvidenceItem:
    return EvidenceItem(
        evidence_id="ev-service",
        source_type="zsxq_article",
        source_id="art-service",
        title="服务层测试文章",
        content="服务层测试正文，包含足够上下文。",
        author="郭老师",
        published_at="2026-06-23",
        collected_at="2026-06-23",
        companies=["测试公司"],
        topics=["测试主题"],
        source_label=SourceLabel("teacher_original", "guo", 0.8, []),
        reliability=0.8,
        metadata={},
    )


def test_service_verify_low_confidence_traces_writes_verifications(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    service.save_evidence(_service_evidence())
    service.trace_repo.upsert(_service_trace("trace-low", 0.42))
    service.trace_repo.upsert(_service_trace("trace-high", 0.8))
    fake = FakeVerifier()

    report = service.verify_low_confidence_traces(
        threshold=0.5,
        limit=3,
        resume=True,
        verifier=fake,
    )

    assert report.selected_count == 1
    assert report.verified_count == 1
    assert report.keep_count == 1
    assert report.error_count == 0
    assert report.verification_ids == ["tv-trace-low"]
    stored = service.trace_verification_repo.list_all()
    assert len(stored) == 1
    assert stored[0].trace_id == "trace-low"
    assert [t.trace_id for t in service.trace_repo.list_all()] == ["trace-low", "trace-high"]


def test_service_verify_low_confidence_traces_resume_skips_existing(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    service.save_evidence(_service_evidence())
    service.trace_repo.upsert(_service_trace("trace-low", 0.42))
    existing = TraceVerification(
        verification_id="tv-existing",
        trace_id="trace-low",
        source_evidence_id="ev-service",
        teacher_id="guo",
        verdict="keep",
        verified_confidence=0.7,
        confidence_adjustment=0.2,
        issues=[],
        suggested_revision={},
        reason="already verified",
        verifier_backend="fake",
        created_at="2026-06-23T00:00:00+00:00",
    )
    service.trace_verification_repo.upsert(existing)
    fake = FakeVerifier()

    report = service.verify_low_confidence_traces(
        threshold=0.5,
        limit=3,
        resume=True,
        verifier=fake,
    )

    assert report.selected_count == 1
    assert report.skipped_count == 1
    assert report.verified_count == 0
    assert fake.calls == []
    assert service.trace_verification_repo.list_all() == [existing]


def test_service_verify_low_confidence_traces_null_teacher_id_covers_all_teachers(
    tmp_path: Path,
):
    """When teacher_id=None is passed explicitly, low-confidence traces from all
    teachers are selected and verified, and resume skip compares against all
    trace verifications."""
    service = CognitiveService(runtime_root=tmp_path)

    # Seed evidence for both teachers
    guo_evidence = EvidenceItem(
        evidence_id="ev-guo",
        source_type="zsxq_article",
        source_id="art-guo",
        title="郭老师文章",
        content="郭老师的测试内容。",
        author="郭老师",
        published_at="2026-06-23",
        collected_at="2026-06-23",
        companies=["测试公司"],
        topics=["测试"],
        source_label=SourceLabel("teacher_original", "guo", 0.8, []),
        reliability=0.8,
        metadata={},
    )
    other_evidence = EvidenceItem(
        evidence_id="ev-other",
        source_type="zsxq_article",
        source_id="art-other",
        title="其他老师文章",
        content="其他老师的测试内容。",
        author="Other",
        published_at="2026-06-23",
        collected_at="2026-06-23",
        companies=["测试公司"],
        topics=["测试"],
        source_label=SourceLabel("teacher_original", "other_teacher", 0.8, []),
        reliability=0.8,
        metadata={},
    )
    service.save_evidence(guo_evidence)
    service.save_evidence(other_evidence)

    # Create low-confidence traces for both teachers
    guo_trace = ReasoningTrace(
        trace_id="trace-guo-low",
        teacher_id="guo",
        source_evidence_id="ev-guo",
        topic="测试",
        companies=["测试公司"],
        premises=["p"],
        observed_variables=["v"],
        inferred_relationships=["r"],
        conclusion="c",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["risk"],
        invalidation_conditions=["invalid"],
        action_implications=["action"],
        extraction_confidence=0.3,
    )
    other_trace = ReasoningTrace(
        trace_id="trace-other-low",
        teacher_id="other_teacher",
        source_evidence_id="ev-other",
        topic="测试",
        companies=["测试公司"],
        premises=["p"],
        observed_variables=["v"],
        inferred_relationships=["r"],
        conclusion="c",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["risk"],
        invalidation_conditions=["invalid"],
        action_implications=["action"],
        extraction_confidence=0.4,
    )
    service.trace_repo.upsert(guo_trace)
    service.trace_repo.upsert(other_trace)

    fake = FakeVerifier()

    report = service.verify_low_confidence_traces(
        threshold=0.5,
        limit=5,
        resume=True,
        teacher_id=None,  # explicit None → all teachers
        verifier=fake,
    )

    assert report.selected_count == 2
    assert report.verified_count == 2
    assert report.error_count == 0
    stored = service.trace_verification_repo.list_all()
    stored_trace_ids = {v.trace_id for v in stored}
    assert "trace-guo-low" in stored_trace_ids
    assert "trace-other-low" in stored_trace_ids


def test_service_verify_trace_by_id(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    service.save_evidence(_service_evidence())
    service.trace_repo.upsert(_service_trace("trace-one", 0.42))
    fake = FakeVerifier()

    verification = service.verify_trace("trace-one", verifier=fake)

    assert verification.trace_id == "trace-one"
    assert service.trace_verification_repo.list_all()[0].trace_id == "trace-one"




def test_extract_teacher_reasoning_ignores_external_context_evidence(tmp_path):
    service = CognitiveService(tmp_path)
    evidence = EvidenceItem(
        evidence_id="ev-external-1",
        source_type="external_context",
        source_id="external-context:dragon_tiger:600519",
        title="龙虎榜",
        content="外部市场观察，仅供学徒认知参考。",
        author=None,
        published_at="2026-06-23",
        collected_at="2026-06-23T00:00:00+00:00",
        companies=["贵州茅台"],
        topics=["龙虎榜"],
        source_label=SourceLabel(label="external_context", teacher_id=None, confidence=1.0),
        reliability=0.5,
        metadata={"evidence_type": "external_context", "is_decision_factor": False},
    )
    service.evidence_repo.upsert(evidence)

    traces = service.extract_teacher_reasoning("ev-external-1")

    assert traces == []
    assert service.trace_repo.list_all() == []


def test_label_evidence_persists_persona_gate_and_upgrades_star_source(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    evidence = EvidenceItem(
        evidence_id="ev-star",
        source_type="zsxq_article",
        source_id="article-star",
        title="星大派锐评",
        content="关键不在情绪，而在订单、价格和利润率是否兑现；需要观察风险边界。",
        author="星大派锐评",
        published_at="2026-06-27",
        collected_at="2026-06-27",
        companies=["测试公司"],
        topics=["星大派"],
        source_label=SourceLabel("unknown", "guo", 0.0, []),
        reliability=0.8,
        metadata={"column": "星大派锐评"},
    )

    service.save_evidence(evidence)
    label = service.label_evidence("ev-star")
    stored = service.evidence_repo.find(lambda item: item.evidence_id == "ev-star")[-1]

    assert label.label == "teacher_original"
    assert stored.source_label.label == "teacher_original"
    assert stored.metadata["persona_eligible"] is True
    assert stored.metadata["persona_gate"]["category"] == "star_teacher_original"


def test_extract_teacher_reasoning_rejects_report_mislabeled_as_teacher_original(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    evidence = EvidenceItem(
        evidence_id="ev-report",
        source_type="zsxq_article",
        source_id="article-report",
        title="9分研报资料",
        content="券商研报给予买入评级，盈利预测和目标价均上调。订单和价格值得观察。",
        author="普通",
        published_at="2026-06-27",
        collected_at="2026-06-27",
        companies=["测试公司"],
        topics=["研报"],
        source_label=SourceLabel("teacher_original", "guo", 0.9, ["bad fixture label"]),
        reliability=0.8,
        metadata={"column": "普通", "score": "9.4"},
    )

    service.save_evidence(evidence)
    traces = service.extract_teacher_reasoning("ev-report")
    stored = service.evidence_repo.find(lambda item: item.evidence_id == "ev-report")[-1]

    assert traces == []
    assert stored.metadata["persona_eligible"] is False
    assert stored.metadata["source_classification"] == "research_reference"
    assert stored.source_label.label == "unknown"
