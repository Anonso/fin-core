"""Test CognitiveService orchestration."""

from dataclasses import replace
from pathlib import Path

from fin_analyse.cognition.models import (
    CognitivePattern,
    EvidenceItem,
    PersonaAnalysis,
    ReasoningTrace,
    SourceLabel,
    TeacherPersona,
    TraceVerification,
)
from fin_analyse.cognition.persona import LLMPersonaEngine
from fin_analyse.cognition.service import CognitiveService, _resolve_moa_backends
from fin_analyse.moa.models import MoAReferenceOutput, MoARequest, MoAResult


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


def test_service_labels_extracts_builds_and_analyzes(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    evidence = EvidenceItem(
        evidence_id="ev-1",
        source_type="zsxq_article",
        source_id="doc-1",
        title="政策变化后的行业判断",
        content=(
            "我认为这次政策的关键不在口号，而在利润分配是否真的发生变化。"
            "真正值得观察的是订单和利润率有没有同步改善。如果只是情绪刺激，追高意义不大。"
            "不要因为政策消息而立即行动，需要等待基本面的确认信号。这是我对当前行业的判断。"
        ),
        author="郭老师",
        published_at="2026-06-21",
        collected_at="2026-06-21T00:00:00Z",
        companies=["测试公司"],
        topics=["政策"],
        source_label=SourceLabel("unknown", "guo", 0.0, []),
        reliability=0.8,
        metadata={"column": "星大派锐评"},
    )

    service.save_evidence(evidence)
    label = service.label_evidence("ev-1")
    traces = service.extract_teacher_reasoning("ev-1")
    persona = service.rebuild_persona("guo")
    analysis = service.analyze_with_persona(
        "测试公司怎么看？", teacher_id="guo", company="测试公司", ticker="000001"
    )

    assert label.label == "teacher_original"
    # No LLM → no traces under strict extraction contract.
    # The persona rebuild and analysis still work with empty trace set,
    # but the analysis stance defaults to "unknown" without extracted reasoning.
    assert len(traces) == 0
    assert persona.persona_id == "guo:v0"
    assert analysis.stance in {"watch", "unknown"}


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


def _seed_cognition_service(root: Path) -> CognitiveService:
    service = CognitiveService(root)
    evidence = EvidenceItem(
        evidence_id="ev-teacher-1",
        source_type="teacher_original",
        source_id="article-1",
        title="老师观点",
        content="老师认为要关注但不追高。",
        author="guo",
        published_at="2026-06-23",
        collected_at="2026-06-23T00:00:00+00:00",
        companies=["贵州茅台"],
        topics=["白酒"],
        source_label=SourceLabel(label="teacher_original", teacher_id="guo", confidence=1.0),
        reliability=0.9,
    )
    trace = ReasoningTrace(
        trace_id="trace-1",
        teacher_id="guo",
        source_evidence_id="ev-teacher-1",
        topic="白酒",
        companies=["贵州茅台"],
        premises=["估值不低"],
        observed_variables=["价格"],
        inferred_relationships=["价格过高时不追"],
        conclusion="关注但不追高",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["跌破趋势"],
        invalidation_conditions=["放量跌破支撑"],
        action_implications=["等待回调"],
        extraction_confidence=0.7,
    )
    pattern = CognitivePattern(
        pattern_id="pattern-1",
        teacher_id="guo",
        name="不追高",
        description="不追高",
        trigger_conditions=[],
        typical_variables=[],
        typical_reasoning_shape="",
        supporting_trace_ids=["trace-1"],
        counterexamples=[],
        confidence=0.7,
        updated_at="2026-06-23T00:00:00+00:00",
    )
    persona = TeacherPersona(
        persona_id="persona-guo",
        teacher_id="guo",
        display_name="郭老师",
        active_version="v0",
        style_summary="风险优先",
        core_pattern_ids=[],
        explicit_rules=["不追高"],
        known_blind_spots=[],
        evidence_policy={"teacher_original_only_for_cognition": True},
        last_built_at="2026-06-23T00:00:00+00:00",
    )
    service.evidence_repo.upsert(evidence)
    service.trace_repo.upsert(trace)
    service.pattern_repo.upsert(pattern)
    service.persona_repo.upsert(persona)
    return service


def test_analyze_with_persona_can_force_fresh_analysis_ids(tmp_path):
    service = _seed_cognition_service(tmp_path)

    first = service.analyze_with_persona(
        "怎么看贵州茅台？",
        company="贵州茅台",
        ticker="600519",
        metadata={"request_id": "req-1", "context_type": "conversation"},
        force_new=True,
    )
    second = service.analyze_with_persona(
        "怎么看贵州茅台？",
        company="贵州茅台",
        ticker="600519",
        metadata={"request_id": "req-2", "context_type": "conversation"},
        force_new=True,
    )

    assert first.analysis_id != second.analysis_id
    assert first.metadata["request_id"] == "req-1"
    assert second.metadata["request_id"] == "req-2"
    assert len(service.analysis_repo.list_all()) == 2


def test_analyze_with_persona_force_new_preserves_source_classification(tmp_path):
    service = _seed_cognition_service(tmp_path)

    analysis = service.analyze_with_persona(
        "未知公司怎么看？",
        company="未知公司",
        ticker="000000",
        metadata={"request_id": "req-transfer", "context_type": "conversation"},
        force_new=True,
    )

    assert analysis.metadata["request_id"] == "req-transfer"
    assert analysis.metadata["context_type"] == "conversation"
    assert analysis.metadata["source_classification"]["methodology_transfer"]["available"] is True
    assert analysis.metadata["evidence_gap"]["direct_trace_count"] == 0


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


def test_rebuild_persona_uses_only_traces_with_persona_eligible_evidence(tmp_path: Path):
    service = CognitiveService(runtime_root=tmp_path)
    eligible = EvidenceItem(
        evidence_id="ev-eligible",
        source_type="zsxq_article",
        source_id="article-eligible",
        title="星大派特刊",
        content="关键变量是订单和利润率，风险边界是兑现失败，不追高。",
        author="星大派特刊",
        published_at="2026-06-27",
        collected_at="2026-06-27",
        companies=["测试公司"],
        topics=["星大派"],
        source_label=SourceLabel("teacher_original", "guo", 0.9, []),
        reliability=0.8,
        metadata={
            "column": "星大派特刊",
            "persona_eligible": True,
            "persona_gate": {
                "evidence_id": "ev-eligible",
                "allows_persona": True,
                "category": "star_teacher_original",
                "source_classification": "teacher_original",
                "confidence": 0.9,
                "half_life_class": "medium_logic",
                "reasons": ["fixture"],
            },
        },
    )
    rejected = EvidenceItem(
        evidence_id="ev-rejected",
        source_type="zsxq_article",
        source_id="article-rejected",
        title="研报资料",
        content="券商研报给予买入评级，盈利预测和目标价均上调。",
        author="普通",
        published_at="2026-06-27",
        collected_at="2026-06-27",
        companies=["污染公司"],
        topics=["研报"],
        source_label=SourceLabel("unknown", "guo", 0.5, []),
        reliability=0.8,
        metadata={
            "column": "普通",
            "score": "9.5",
            "persona_eligible": False,
            "persona_gate": {
                "evidence_id": "ev-rejected",
                "allows_persona": False,
                "category": "research_reference_only",
                "source_classification": "research_reference",
                "confidence": 0.95,
                "half_life_class": "medium_logic",
                "reasons": ["fixture"],
            },
        },
    )
    service.evidence_repo.upsert(eligible)
    service.evidence_repo.upsert(rejected)
    service.trace_repo.upsert(
        ReasoningTrace(
            trace_id="trace-eligible",
            teacher_id="guo",
            source_evidence_id="ev-eligible",
            topic="星大派",
            companies=["测试公司"],
            premises=["p"],
            observed_variables=["订单"],
            inferred_relationships=["订单兑现才有意义"],
            conclusion="关注但不追高",
            stance="watch",
            time_horizon="mid",
            risk_boundaries=["追高风险"],
            invalidation_conditions=["订单不兑现"],
            action_implications=["等待验证"],
            extraction_confidence=0.8,
        )
    )
    service.trace_repo.upsert(
        ReasoningTrace(
            trace_id="trace-rejected",
            teacher_id="guo",
            source_evidence_id="ev-rejected",
            topic="研报",
            companies=["污染公司"],
            premises=["p"],
            observed_variables=["目标价"],
            inferred_relationships=["目标价上调就是机会"],
            conclusion="买入",
            stance="bull",
            time_horizon="short",
            risk_boundaries=["无"],
            invalidation_conditions=["无"],
            action_implications=["污染动作"],
            extraction_confidence=0.9,
        )
    )

    persona = service.rebuild_persona("guo")

    assert "等待验证" in persona.explicit_rules
    assert "污染动作" not in persona.explicit_rules
    patterns = service.pattern_repo.list_all()
    supporting_trace_ids = {
        trace_id for pattern in patterns for trace_id in pattern.supporting_trace_ids
    }
    assert supporting_trace_ids == {"trace-eligible"}


class FakeMoAEngine:
    def __init__(self, result: MoAResult) -> None:
        self.result = result
        self.requests: list[MoARequest] = []

    def deliberate(self, request: MoARequest) -> MoAResult:
        self.requests.append(request)
        return self.result


class FakeMoAAnalyzer:
    def __init__(self, analysis: PersonaAnalysis | None) -> None:
        self.analysis = analysis
        self.calls: list[dict] = []

    def to_analysis(self, **kwargs) -> PersonaAnalysis | None:
        self.calls.append(kwargs)
        return self.analysis


class FakeLLMPersonaEngine(LLMPersonaEngine):
    def __init__(self, analysis: PersonaAnalysis | None) -> None:
        self.analysis = analysis
        self.calls: list[dict] = []

    def analyze(self, **kwargs) -> PersonaAnalysis | None:
        self.calls.append(kwargs)
        return self.analysis


def _fake_moa_analysis() -> PersonaAnalysis:
    return PersonaAnalysis(
        analysis_id="pa-moa",
        persona_id="persona-guo",
        question="怎么看贵州茅台？",
        company="贵州茅台",
        ticker="600519",
        activated_trace_ids=["trace-1"],
        activated_pattern_ids=["pattern-1"],
        evidence_ids=["ev-teacher-1"],
        reasoning_steps=["MoA 推理"],
        conclusion="关注但不追高",
        stance="watch",
        confidence=0.62,
        uncertainty=[],
        contradictions=[],
        unsupported_claims=[],
        invalidation_conditions=["跌破趋势"],
        suggested_followups=["等待回调"],
        created_at="2026-06-28T00:00:00+00:00",
        metadata={
            "quality_mode": "moa",
            "source_classification": {
                "direct_knowledge": {
                    "available": True,
                    "trace_ids": ["trace-1"],
                    "evidence_ids": ["ev-teacher-1"],
                },
                "methodology_transfer": {"available": False, "pattern_ids": [], "basis": []},
                "external_observation": {"available": False, "note": "外部上下文仅供参考"},
            },
        },
    )


def test_analyze_with_persona_routes_to_moa_when_quality_mode_moa(tmp_path):
    service = _seed_cognition_service(tmp_path)
    result = MoAResult(
        task_id="persona:600519",
        task_type="persona_analysis",
        status="ok",
        final={"conclusion": "关注但不追高", "stance": "watch", "confidence": 0.62},
        reference_outputs=[
            MoAReferenceOutput(role="direct_trace_reader", backend_name="t0", content="ok", ok=True)
        ],
        consensus=[],
        disagreements=[],
        blind_spots=[],
        confidence=0.62,
        warnings=[],
    )
    engine = FakeMoAEngine(result)
    analyzer = FakeMoAAnalyzer(_fake_moa_analysis())
    service = CognitiveService(
        runtime_root=tmp_path,
        moa_engine=engine,
        moa_analyzer=analyzer,
    )

    analysis = service.analyze_with_persona(
        "怎么看贵州茅台？",
        teacher_id="guo",
        company="贵州茅台",
        ticker="600519",
        quality_mode="moa",
    )

    assert analysis.analysis_id == "pa-moa"
    assert analysis.metadata["quality_mode"] == "moa"
    assert len(engine.requests) == 1
    assert engine.requests[0].task_type == "persona_analysis"
    assert analyzer.calls[0]["result"] is result
    stored = service.analysis_repo.list_all()
    assert len(stored) == 1
    assert stored[0].metadata["quality_mode"] == "moa"


def test_analyze_with_persona_moa_falls_back_to_rule_on_failed_result(tmp_path):
    service = _seed_cognition_service(tmp_path)
    result = MoAResult(
        task_id="persona:fail",
        task_type="persona_analysis",
        status="fallback",
        final={},
        reference_outputs=[],
        consensus=[],
        disagreements=[],
        blind_spots=[],
        confidence=0.0,
        warnings=["no backends"],
        fallback_reason="aggregator unavailable",
    )
    engine = FakeMoAEngine(result)
    analyzer = FakeMoAAnalyzer(None)
    service = CognitiveService(
        runtime_root=tmp_path,
        moa_engine=engine,
        moa_analyzer=analyzer,
    )

    analysis = service.analyze_with_persona(
        "怎么看贵州茅台？",
        teacher_id="guo",
        company="贵州茅台",
        ticker="600519",
        quality_mode="moa",
    )

    assert analysis.stance == "watch"
    assert analysis.metadata.get("quality_mode") != "moa"


def test_analyze_with_persona_routes_to_llm_when_quality_mode_llm(tmp_path):
    service = _seed_cognition_service(tmp_path)
    llm_analysis = _fake_moa_analysis()
    llm_analysis = replace(llm_analysis, analysis_id="pa-llm", metadata={"quality_mode": "llm"})
    llm_engine = FakeLLMPersonaEngine(llm_analysis)
    service = CognitiveService(
        runtime_root=tmp_path,
        llm_persona_engine=llm_engine,
    )

    analysis = service.analyze_with_persona(
        "怎么看贵州茅台？",
        teacher_id="guo",
        company="贵州茅台",
        ticker="600519",
        quality_mode="llm",
    )

    assert analysis.analysis_id == "pa-llm"
    assert analysis.metadata["quality_mode"] == "llm"
    assert len(llm_engine.calls) == 1
    assert llm_engine.calls[0]["company"] == "贵州茅台"


def test_analyze_with_persona_routes_standard_to_llm_when_engine_available(tmp_path):
    service = _seed_cognition_service(tmp_path)
    llm_analysis = _fake_moa_analysis()
    llm_analysis = replace(
        llm_analysis, analysis_id="pa-std", metadata={"quality_mode": "standard"}
    )
    llm_engine = FakeLLMPersonaEngine(llm_analysis)
    service = CognitiveService(
        runtime_root=tmp_path,
        llm_persona_engine=llm_engine,
    )

    analysis = service.analyze_with_persona(
        "怎么看贵州茅台？",
        teacher_id="guo",
        company="贵州茅台",
        ticker="600519",
    )

    assert analysis.analysis_id == "pa-std"
    assert analysis.metadata["quality_mode"] == "standard"
    assert len(llm_engine.calls) == 1
    assert llm_engine.calls[0]["quality_mode"] == "standard"


def test_analyze_with_persona_standard_falls_back_to_rule_without_llm(tmp_path):
    service = _seed_cognition_service(tmp_path)

    analysis = service.analyze_with_persona(
        "怎么看贵州茅台？",
        teacher_id="guo",
        company="贵州茅台",
        ticker="600519",
        quality_mode="standard",
    )

    assert analysis.stance == "watch"
    assert analysis.metadata.get("quality_mode") == "rule"


class _FakeLLM:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    def complete_json(self, prompt: str, *, expected_type: str):
        class _Result:
            ok = True
            data = self.data
            error = None

        return _Result()


def test_llm_persona_engine_emits_required_metadata_and_filters_invalid_trace_ids():
    from fin_analyse.cognition.persona import LLMPersonaEngine

    trace = ReasoningTrace(
        trace_id="trace-valid",
        teacher_id="guo",
        source_evidence_id="ev-1",
        topic="白酒",
        companies=["贵州茅台"],
        premises=["p"],
        observed_variables=["v"],
        inferred_relationships=["r"],
        conclusion="c",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["risk"],
        invalidation_conditions=["invalid"],
        action_implications=["action"],
        extraction_confidence=0.7,
    )
    pattern = CognitivePattern(
        pattern_id="pattern-1",
        teacher_id="guo",
        name="不追高",
        description="d",
        trigger_conditions=[],
        typical_variables=[],
        typical_reasoning_shape="",
        supporting_trace_ids=["trace-valid"],
        counterexamples=[],
        confidence=0.7,
        updated_at="2026-06-28T00:00:00+00:00",
    )
    persona = TeacherPersona(
        persona_id="persona-guo",
        teacher_id="guo",
        display_name="郭老师",
        active_version="v0",
        style_summary="风险优先",
        core_pattern_ids=[],
        explicit_rules=["不追高"],
        known_blind_spots=[],
        evidence_policy={},
        last_built_at="2026-06-28T00:00:00+00:00",
    )
    engine = LLMPersonaEngine(
        _FakeLLM(
            {
                "topic": "白酒",
                "conclusion": "关注但不追高",
                "stance": "watch",
                "confidence": 0.65,
                "reasoning_steps": ["参考历史推理"],
                "risk_boundaries": ["跌破趋势"],
                "invalidation_conditions": ["放量跌破"],
                "unsupported_claims": [],
                "suggested_followups": ["等待回调"],
                "activated_trace_ids": ["trace-valid", "trace-fake"],
            }
        )
    )

    analysis = engine.analyze(
        persona=persona,
        question="怎么看贵州茅台？",
        traces=[trace],
        patterns=[pattern],
        company="贵州茅台",
        ticker="600519",
        quality_mode="standard",
    )

    assert analysis is not None
    assert analysis.activated_trace_ids == ["trace-valid"]
    assert analysis.evidence_ids == ["ev-1"]
    assert analysis.metadata["quality_mode"] == "standard"
    assert analysis.metadata["moa_audit"] is None
    assert analysis.metadata["source_classification"]["direct_knowledge"]["available"] is True
    assert analysis.metadata["evidence_gap"]["direct_trace_count"] == 1
    assert analysis.metadata["confidence_boundary"]["level"] == "low"
    assert analysis.metadata["needs_human_review"] is True


def test_llm_persona_engine_marks_transfer_when_no_direct_traces():
    from fin_analyse.cognition.persona import LLMPersonaEngine

    pattern = CognitivePattern(
        pattern_id="pattern-1",
        teacher_id="guo",
        name="不追高",
        description="d",
        trigger_conditions=[],
        typical_variables=[],
        typical_reasoning_shape="",
        supporting_trace_ids=[],
        counterexamples=[],
        confidence=0.7,
        updated_at="2026-06-28T00:00:00+00:00",
    )
    persona = TeacherPersona(
        persona_id="persona-guo",
        teacher_id="guo",
        display_name="郭老师",
        active_version="v0",
        style_summary="风险优先",
        core_pattern_ids=[],
        explicit_rules=["不追高"],
        known_blind_spots=[],
        evidence_policy={},
        last_built_at="2026-06-28T00:00:00+00:00",
    )
    engine = LLMPersonaEngine(
        _FakeLLM(
            {
                "topic": "白酒",
                "conclusion": "暂不明确",
                "stance": "unknown",
                "confidence": 0.3,
                "reasoning_steps": [],
                "risk_boundaries": [],
                "invalidation_conditions": [],
                "unsupported_claims": [],
                "suggested_followups": [],
                "activated_trace_ids": [],
            }
        )
    )

    analysis = engine.analyze(
        persona=persona,
        question="怎么看未知公司？",
        traces=[],
        patterns=[pattern],
        company="未知公司",
        quality_mode="standard",
    )

    assert analysis is not None
    assert analysis.metadata["source_classification"]["methodology_transfer"]["available"] is True
    assert analysis.metadata["needs_human_review"] is True
    assert analysis.metadata["confidence_boundary"]["level"] == "low"


class _FakeLLMBackend:
    def complete(self, prompt: str) -> str:
        return ""


def test_resolve_moa_backends_glm53_t0_deepseek_t1():
    backends = {
        "glm53": _FakeLLMBackend(),
        "deepseek": _FakeLLMBackend(),
        "qwen": _FakeLLMBackend(),
    }
    t0, t1, name = _resolve_moa_backends(backends)
    assert name == "glm53"
    assert t0.name == "glm53"
    assert t1.name == "deepseek"


def test_resolve_moa_backends_glm53_t0_flash_t1_when_no_deepseek():
    backends = {
        "glm53": _FakeLLMBackend(),
        "glm53_flash": _FakeLLMBackend(),
        "qwen": _FakeLLMBackend(),
    }
    t0, t1, name = _resolve_moa_backends(backends)
    assert name == "glm53"
    assert t0.name == "glm53"
    assert t1.name == "glm53_flash"


def test_resolve_moa_backends_deepseek_t0_flash_t1_when_no_glm53():
    backends = {
        "deepseek": _FakeLLMBackend(),
        "glm53_flash": _FakeLLMBackend(),
        "qwen": _FakeLLMBackend(),
    }
    t0, t1, name = _resolve_moa_backends(backends)
    assert name == "deepseek"
    assert t0.name == "deepseek"
    assert t1.name == "glm53_flash"


def test_resolve_moa_backends_follows_configured_orders():
    backends = {"deepseek": _FakeLLMBackend(), "qwen": _FakeLLMBackend()}
    t0, t1, name = _resolve_moa_backends(
        backends,
        t0_order=("qwen", "deepseek"),
        t1_order=("deepseek", "glm53_flash"),
    )
    assert name == "qwen"
    assert t0.name == "qwen"
    assert t1.name == "deepseek"


def test_resolve_moa_backends_single_backend_falls_back_to_same():
    backends = {"glm53": _FakeLLMBackend()}
    t0, t1, name = _resolve_moa_backends(backends)
    assert name == "glm53"
    assert t0 is t1


def test_resolve_moa_backends_empty_returns_none():
    assert _resolve_moa_backends({}) is None
