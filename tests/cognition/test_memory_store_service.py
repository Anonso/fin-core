"""Test CognitionMemoryStoreService scope isolation and CRUD operations."""

from pathlib import Path

import pytest

from fin_analyse.cognition.models import (
    CognitivePattern,
    EvidenceItem,
    ReasoningTrace,
    SourceLabel,
    TeacherPersona,
    TraceVerification,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _sample_evidence(
    evidence_id: str = "ev-001",
    author: str = "郭老师",
    source_label: SourceLabel | None = None,
) -> EvidenceItem:
    if source_label is None:
        source_label = SourceLabel("teacher_original", "guo", 1.0, [])
    return EvidenceItem(
        evidence_id=evidence_id,
        source_type="zsxq_article",
        source_id="article-001",
        title="星大派观察",
        content="关键是订单和利润率是否兑现。",
        author=author,
        published_at="2026-07-07",
        collected_at="2026-07-07T00:00:00+00:00",
        companies=["测试公司"],
        topics=["订单"],
        source_label=source_label,
        reliability=0.9,
        metadata={},
    )


def _sample_trace(
    trace_id: str = "tr-001",
    teacher_id: str = "guo",
    source_evidence_id: str = "ev-001",
) -> ReasoningTrace:
    return ReasoningTrace(
        trace_id=trace_id,
        teacher_id=teacher_id,
        source_evidence_id=source_evidence_id,
        topic="测试主题",
        companies=["测试公司"],
        premises=["premise"],
        observed_variables=["var"],
        inferred_relationships=["rel"],
        conclusion="结论",
        stance="看多",
        time_horizon="1个月",
        risk_boundaries=["risk"],
        invalidation_conditions=["cond"],
        action_implications=["action"],
        extraction_confidence=0.8,
    )


def _sample_pattern(pattern_id: str = "pat-001", teacher_id: str = "guo") -> CognitivePattern:
    return CognitivePattern(
        pattern_id=pattern_id,
        teacher_id=teacher_id,
        name="测试模式",
        description="描述",
        trigger_conditions=["trigger"],
        typical_variables=["var"],
        typical_reasoning_shape="shape",
        supporting_trace_ids=[],
        counterexamples=[],
        confidence=0.8,
        updated_at="2026-07-07T00:00:00+00:00",
        metadata={},
    )


def _sample_persona(persona_id: str = "per-001", teacher_id: str = "guo") -> TeacherPersona:
    return TeacherPersona(
        persona_id=persona_id,
        teacher_id=teacher_id,
        display_name="郭老师 Persona",
        active_version="v1",
        style_summary="风格",
        core_pattern_ids=[],
        explicit_rules=["规则"],
        known_blind_spots=["盲点"],
        evidence_policy={},
        last_built_at="2026-07-07T00:00:00+00:00",
    )



def _owner_only_store_with_persona(root: Path):
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(root)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")
    result = store.handle(
        CognitionMemoryRequest(
            operation="upsert_persona",
            scope=scope,
            persona=_sample_persona(),
        )
    )
    assert result.status == "success"
    root.chmod(0o700)
    (root / "teacher_personas.jsonl").chmod(0o600)
    return scope


def test_existing_owner_only_read_view_reads_without_mutating_store(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryStoreService,
    )

    root = tmp_path / "cognition"
    scope = _owner_only_store_with_persona(root)
    target = root / "teacher_personas.jsonl"
    before = (target.read_bytes(), target.stat().st_mtime_ns)

    reader = CognitionMemoryStoreService.open_existing_owner_only_read(root)
    listed = reader.handle(CognitionMemoryRequest(operation="list_personas", scope=scope))
    refused = reader.handle(
        CognitionMemoryRequest(
            operation="upsert_persona",
            scope=scope,
            persona=_sample_persona("per-002"),
        )
    )

    assert [item.persona_id for item in listed.payload["personas"]] == ["per-001"]
    assert refused.status == "error"
    assert refused.data_gaps == ["cognition_memory_read_only"]
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_existing_owner_only_read_view_rejects_symlink_root(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import CognitionMemoryStoreService

    real_root = tmp_path / "real-cognition"
    real_root.mkdir(mode=0o700)
    linked_root = tmp_path / "linked-cognition"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="owner_only_jsonl_root_invalid"):
        CognitionMemoryStoreService.open_existing_owner_only_read(linked_root)


@pytest.mark.parametrize("unsafe_kind", ("mode", "symlink", "hardlink"))
def test_existing_owner_only_read_view_rejects_unsafe_jsonl(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryStoreService,
    )

    root = tmp_path / unsafe_kind / "cognition"
    scope = _owner_only_store_with_persona(root)
    target = root / "teacher_personas.jsonl"
    if unsafe_kind == "mode":
        target.chmod(0o644)
    elif unsafe_kind == "symlink":
        victim = tmp_path / unsafe_kind / "victim.jsonl"
        target.replace(victim)
        target.symlink_to(victim)
    else:
        (root / "persona-hardlink.jsonl").hardlink_to(target)

    reader = CognitionMemoryStoreService.open_existing_owner_only_read(root)
    with pytest.raises(ValueError, match="owner_only_jsonl_file_invalid"):
        reader.handle(CognitionMemoryRequest(operation="list_personas", scope=scope))


# ── Step 1: save / get evidence with scope boundary ────────────────────


def test_store_saves_and_gets_teacher_evidence_with_scope_boundary(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")
    evidence = _sample_evidence("ev-smoke")

    result = store.handle(
        CognitionMemoryRequest(operation="save_evidence", scope=scope, evidence=evidence)
    )
    assert result.status == "success"
    assert result.write_effect == "evidence_saved"
    assert result.source_boundary == "teacher_cognition"

    fetched = store.handle(
        CognitionMemoryRequest(operation="get_evidence", scope=scope, evidence_id="ev-smoke")
    )
    assert fetched.status == "success"
    assert fetched.source_boundary == "teacher_cognition"
    assert fetched.payload["evidence"].evidence_id == "ev-smoke"
    assert fetched.payload["evidence"].author == "郭老师"


# ── Step 2: trace isolation by teacher_id ──────────────────────────────


def test_teacher_trace_list_is_isolated_by_teacher_id(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    guo_scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")
    other_scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="other")

    # Save evidence for both teachers
    guo_ev = _sample_evidence("ev-guo", author="郭老师")
    other_ev = _sample_evidence(
        "ev-other",
        author="其他老师",
        source_label=SourceLabel("teacher_original", "other", 1.0, []),
    )
    store.handle(
        CognitionMemoryRequest(operation="save_evidence", scope=guo_scope, evidence=guo_ev)
    )
    store.handle(
        CognitionMemoryRequest(operation="save_evidence", scope=other_scope, evidence=other_ev)
    )

    # Upsert guo trace through guo scope
    guo_trace = _sample_trace("tr-guo", teacher_id="guo", source_evidence_id="ev-guo")
    r1 = store.handle(
        CognitionMemoryRequest(operation="upsert_trace", scope=guo_scope, trace=guo_trace)
    )
    assert r1.status == "success"

    # Upsert other trace through other scope
    other_trace = _sample_trace("tr-other", teacher_id="other", source_evidence_id="ev-other")
    r2 = store.handle(
        CognitionMemoryRequest(operation="upsert_trace", scope=other_scope, trace=other_trace)
    )
    assert r2.status == "success"

    # list_traces with guo scope returns only guo trace
    list_result = store.handle(CognitionMemoryRequest(operation="list_traces", scope=guo_scope))
    assert list_result.status == "success"
    trace_ids = [t.trace_id for t in list_result.payload["traces"]]
    assert "tr-guo" in trace_ids
    assert "tr-other" not in trace_ids

    # Attempt to upsert other trace through guo scope → error
    bad_trace = _sample_trace("tr-bad", teacher_id="other", source_evidence_id="ev-other")
    reject = store.handle(
        CognitionMemoryRequest(operation="upsert_trace", scope=guo_scope, trace=bad_trace)
    )
    assert reject.status == "error"
    assert reject.write_effect == ""


def test_teacher_cognition_scope_requires_teacher_id_and_does_not_list_all(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    guo_scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")
    empty_scope = CognitionMemoryScope(memory_kind="teacher_cognition")

    store.handle(
        CognitionMemoryRequest(
            operation="save_evidence",
            scope=guo_scope,
            evidence=_sample_evidence("ev-guo"),
        )
    )
    store.handle(
        CognitionMemoryRequest(
            operation="upsert_trace",
            scope=guo_scope,
            trace=_sample_trace("tr-guo", teacher_id="guo", source_evidence_id="ev-guo"),
        )
    )

    listed = store.handle(CognitionMemoryRequest(operation="list_traces", scope=empty_scope))
    assert listed.status == "error"
    assert listed.payload["error_code"] == "MISSING_TEACHER_ID"

    write = store.handle(
        CognitionMemoryRequest(
            operation="upsert_trace",
            scope=empty_scope,
            trace=_sample_trace("tr-empty", teacher_id="guo"),
        )
    )
    assert write.status == "error"
    assert write.payload["error_code"] == "MISSING_TEACHER_ID"


def test_unknown_memory_kind_cannot_read_teacher_cognition(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    teacher_scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")
    store.handle(
        CognitionMemoryRequest(
            operation="upsert_persona",
            scope=teacher_scope,
            persona=_sample_persona("private-persona"),
        )
    )

    result = store.handle(
        CognitionMemoryRequest(
            operation="list_personas",
            scope=CognitionMemoryScope(memory_kind="unknown"),
        )
    )

    assert result.status == "error"
    assert result.payload["error_code"] == "UNKNOWN_MEMORY_KIND"
    assert result.write_effect == "none"


def test_unknown_memory_kind_cannot_write_teacher_cognition(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    result = store.handle(
        CognitionMemoryRequest(
            operation="upsert_persona",
            scope=CognitionMemoryScope(memory_kind="unknown"),
            persona=_sample_persona("private-persona"),
        )
    )
    listed = store.handle(
        CognitionMemoryRequest(
            operation="list_personas",
            scope=CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo"),
        )
    )

    assert result.status == "error"
    assert result.payload["error_code"] == "UNKNOWN_MEMORY_KIND"
    assert result.write_effect == "none"
    assert listed.payload["personas"] == []


# ── Step 3: external scope cannot write teacher cognition ───────────────


def test_external_scope_cannot_write_teacher_cognition(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    ext_scope = CognitionMemoryScope(memory_kind="external_evidence")

    # save_evidence through external scope is allowed
    ext_evidence = _sample_evidence(
        "ev-ext",
        author="外部作者",
        source_label=SourceLabel("external_context", None, 0.5, []),
    )
    r_save = store.handle(
        CognitionMemoryRequest(operation="save_evidence", scope=ext_scope, evidence=ext_evidence)
    )
    assert r_save.status == "success"
    assert r_save.source_boundary == "external_evidence"

    # get_evidence works
    r_get = store.handle(
        CognitionMemoryRequest(operation="get_evidence", scope=ext_scope, evidence_id="ev-ext")
    )
    assert r_get.status == "success"

    # upsert_trace through external scope → error
    ext_trace = _sample_trace("tr-ext", teacher_id="guo")
    r_trace = store.handle(
        CognitionMemoryRequest(operation="upsert_trace", scope=ext_scope, trace=ext_trace)
    )
    assert r_trace.status == "error"
    assert "EXTERNAL_SCOPE_CANNOT_WRITE_COGNITION" in str(r_trace.payload)

    # upsert_persona through external scope → error
    ext_persona = _sample_persona("per-ext")
    r_persona = store.handle(
        CognitionMemoryRequest(operation="upsert_persona", scope=ext_scope, persona=ext_persona)
    )
    assert r_persona.status == "error"
    assert "EXTERNAL_SCOPE_CANNOT_WRITE_COGNITION" in str(r_persona.payload)


# ── Step 4: agent_private scope checks ──────────────────────────────────


def test_agent_private_scope_requires_agent_id_and_cannot_write_persona(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)

    # Missing agent_id
    bad_scope = CognitionMemoryScope(memory_kind="agent_private")
    r_missing = store.handle(
        CognitionMemoryRequest(
            operation="save_evidence",
            scope=bad_scope,
            evidence=_sample_evidence("ev-ap"),
        )
    )
    assert r_missing.status == "error"
    assert "MISSING_AGENT_ID" in str(r_missing.payload)

    # With agent_id, save_evidence works
    ap_scope = CognitionMemoryScope(memory_kind="agent_private", agent_id="agent-42")
    r_save = store.handle(
        CognitionMemoryRequest(
            operation="save_evidence",
            scope=ap_scope,
            evidence=_sample_evidence(
                "ev-ap-ok",
                source_label=SourceLabel("external_context", None, 0.5, []),
            ),
        )
    )
    assert r_save.status == "success"

    # agent_private scope cannot upsert_persona
    r_persona = store.handle(
        CognitionMemoryRequest(
            operation="upsert_persona",
            scope=ap_scope,
            persona=_sample_persona("per-ap"),
        )
    )
    assert r_persona.status == "error"
    assert "AGENT_PRIVATE_CANNOT_WRITE_TEACHER_PERSONA" in str(r_persona.payload)

    # agent_private scope cannot upsert_pattern
    r_pattern = store.handle(
        CognitionMemoryRequest(
            operation="upsert_pattern",
            scope=ap_scope,
            pattern=_sample_pattern("pat-ap"),
        )
    )
    assert r_pattern.status == "error"
    assert "AGENT_PRIVATE_CANNOT_WRITE_TEACHER_PERSONA" in str(r_pattern.payload)


# ── shared_reference scope ──────────────────────────────────────────────


def test_shared_reference_scope_cannot_write_cognition(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="shared_reference")

    r = store.handle(
        CognitionMemoryRequest(
            operation="upsert_trace",
            scope=scope,
            trace=_sample_trace("tr-shared"),
        )
    )
    assert r.status == "error"
    assert "SHARED_REFERENCE_NOT_COGNITION_MEMORY" in str(r.payload)


# ── pattern / persona / analysis CRUD through teacher scope ────────────


def test_store_upserts_and_lists_patterns(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")

    pattern = _sample_pattern("pat-001", teacher_id="guo")
    r = store.handle(
        CognitionMemoryRequest(operation="upsert_pattern", scope=scope, pattern=pattern)
    )
    assert r.status == "success"
    assert r.write_effect == "pattern_upserted"

    listed = store.handle(CognitionMemoryRequest(operation="list_patterns", scope=scope))
    assert listed.status == "success"
    assert any(p.pattern_id == "pat-001" for p in listed.payload["patterns"])


def test_store_upserts_and_lists_personas(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")

    persona = _sample_persona("per-001", teacher_id="guo")
    r = store.handle(
        CognitionMemoryRequest(operation="upsert_persona", scope=scope, persona=persona)
    )
    assert r.status == "success"
    assert r.write_effect == "persona_upserted"

    listed = store.handle(CognitionMemoryRequest(operation="list_personas", scope=scope))
    assert listed.status == "success"
    assert any(p.persona_id == "per-001" for p in listed.payload["personas"])



def test_unknown_operation_returns_error(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")

    r = store.handle(CognitionMemoryRequest(operation="do_something_weird", scope=scope))
    assert r.status == "error"
    assert "UNKNOWN_OPERATION" in str(r.payload)


# ── migration guard tests ────────────────────────────────────────────────



def _sample_verification(
    verification_id: str = "tv-001",
    trace_id: str = "tr-001",
    teacher_id: str = "guo",
) -> TraceVerification:
    return TraceVerification(
        verification_id=verification_id,
        trace_id=trace_id,
        source_evidence_id="ev-001",
        teacher_id=teacher_id,
        verdict="keep",
        verified_confidence=0.85,
        confidence_adjustment=0.1,
        issues=[],
        suggested_revision={},
        reason="ok",
        verifier_backend="fake",
        created_at="2026-07-07T00:00:00+00:00",
    )


# ── feedback scope tests ─────────────────────────────────────────────────






def test_memory_store_upserts_and_lists_trace_verifications_by_teacher_scope(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")

    verification = _sample_verification("tv-001", trace_id="tr-001", teacher_id="guo")
    result = store.handle(
        CognitionMemoryRequest(
            operation="upsert_trace_verification",
            scope=scope,
            trace_verification=verification,
        )
    )
    assert result.status == "success"
    assert result.write_effect == "trace_verification_upserted"
    assert result.source_boundary == "teacher_cognition"

    # list verifications for teacher scope
    list_result = store.handle(
        CognitionMemoryRequest(operation="list_trace_verifications", scope=scope)
    )
    assert list_result.status == "success"
    verifications = list_result.payload.get("verifications", [])
    assert any(v.verification_id == "tv-001" for v in verifications)


def test_memory_store_selects_low_confidence_traces_by_teacher_scope(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo")

    # seed traces with different confidence levels
    store.trace_repo.upsert(
        _sample_trace("tr-low-1", teacher_id="guo", source_evidence_id="ev-001")
    )
    # Patch extraction_confidence after creation (frozen dataclass needs replace)
    from dataclasses import replace

    low_trace = replace(
        _sample_trace("tr-low-2", teacher_id="guo", source_evidence_id="ev-001"),
        extraction_confidence=0.3,
    )
    high_trace = replace(
        _sample_trace("tr-high", teacher_id="guo", source_evidence_id="ev-001"),
        extraction_confidence=0.9,
    )
    other_trace = _sample_trace("tr-other", teacher_id="other", source_evidence_id="ev-001")
    store.trace_repo.upsert(low_trace)
    store.trace_repo.upsert(high_trace)
    store.trace_repo.upsert(other_trace)

    result = store.handle(
        CognitionMemoryRequest(
            operation="select_low_confidence_traces",
            scope=scope,
            threshold=0.5,
            limit=3,
        )
    )
    assert result.status == "success"
    traces = result.payload.get("traces", [])
    trace_ids = [t.trace_id for t in traces]
    # tr-low-2 (0.3), tr-low-1 (0.8) — but tr-low-1 has 0.8 > 0.5 threshold
    assert "tr-low-2" in trace_ids
    assert "tr-high" not in trace_ids  # 0.9 > 0.5
    assert "tr-other" not in trace_ids  # different teacher

    # verify ordering: lowest confidence first
    assert trace_ids[0] == "tr-low-2"


def test_external_scope_cannot_write_trace_verification(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    ext_scope = CognitionMemoryScope(memory_kind="external_evidence")

    verification = _sample_verification("tv-ext")
    result = store.handle(
        CognitionMemoryRequest(
            operation="upsert_trace_verification",
            scope=ext_scope,
            trace_verification=verification,
        )
    )
    assert result.status == "error"
    assert "EXTERNAL_SCOPE_CANNOT_WRITE" in str(result.payload)


def test_shared_reference_scope_cannot_write_trace_verification(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import (
        CognitionMemoryRequest,
        CognitionMemoryScope,
        CognitionMemoryStoreService,
    )

    store = CognitionMemoryStoreService(runtime_root=tmp_path)
    scope = CognitionMemoryScope(memory_kind="shared_reference")

    verification = _sample_verification("tv-shared")
    result = store.handle(
        CognitionMemoryRequest(
            operation="upsert_trace_verification",
            scope=scope,
            trace_verification=verification,
        )
    )
    assert result.status == "error"
    assert "CANNOT_WRITE" in str(result.payload) or "NOT_COGNITION_MEMORY" in str(result.payload)


def test_cognitive_service_has_memory_store_attribute(tmp_path: Path) -> None:
    from fin_analyse.cognition.memory_store import CognitionMemoryStoreService
    from fin_analyse.cognition.service import CognitiveService

    service = CognitiveService(runtime_root=tmp_path)
    assert hasattr(service, "memory_store")
    assert isinstance(service.memory_store, CognitionMemoryStoreService)

    # Compatibility attributes should point to store repos
    assert service.evidence_repo is service.memory_store.evidence_repo
    assert service.trace_repo is service.memory_store.trace_repo
    assert service.pattern_repo is service.memory_store.pattern_repo
    assert service.persona_repo is service.memory_store.persona_repo
    assert service.trace_verification_repo is service.memory_store.trace_verification_repo


def test_cognitive_service_uses_external_scope_for_external_evidence(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.memory_store import CognitionMemoryRequest
    from fin_analyse.cognition.service import CognitiveService

    service = CognitiveService(runtime_root=tmp_path)
    ext_evidence = EvidenceItem(
        evidence_id="ev-ext-scope",
        source_type="external_context",
        source_id="external-context:news:000001",
        title="公告",
        content="外部公告仅作参考。",
        author=None,
        published_at="2026-07-07",
        collected_at="2026-07-07T00:00:00+00:00",
        companies=["测试公司"],
        topics=["公告"],
        source_label=SourceLabel("external_context", None, 0.5, []),
        reliability=0.4,
        metadata={"evidence_type": "external_context"},
    )

    scope = service._scope_for_evidence(ext_evidence)
    assert scope.memory_kind == "external_evidence"
    assert scope.teacher_id == ""

    service.save_evidence(ext_evidence)
    fetched = service.memory_store.handle(
        CognitionMemoryRequest(
            operation="get_evidence",
            scope=scope,
            evidence_id="ev-ext-scope",
        )
    )
    assert fetched.status == "success"
    assert fetched.source_boundary == "external_evidence"
    assert fetched.payload["evidence"].evidence_id == "ev-ext-scope"



def test_external_evidence_persists_but_does_not_create_traces(
    tmp_path: Path,
) -> None:
    from fin_analyse.cognition.service import CognitiveService

    service = CognitiveService(runtime_root=tmp_path)
    ext_evidence = EvidenceItem(
        evidence_id="ev-ext-persist",
        source_type="external_context",
        source_id="external-context:dragon_tiger:000001",
        title="龙虎榜数据",
        content="今日资金大幅流出，主力减仓明显。",
        author=None,
        published_at="2026-07-07",
        collected_at="2026-07-07T00:00:00+00:00",
        companies=["测试公司"],
        topics=["龙虎榜"],
        source_label=SourceLabel("external_context", None, 0.5, []),
        reliability=0.4,
        metadata={"evidence_type": "external_context", "is_decision_factor": False},
    )

    # Save through service
    service.save_evidence(ext_evidence)

    # Evidence is persisted
    stored = service.evidence_repo.find(lambda item: item.evidence_id == "ev-ext-persist")
    assert len(stored) == 1
    assert stored[0].source_label.label == "external_context"

    # extract_teacher_reasoning should return empty (external evidence)
    traces = service.extract_teacher_reasoning("ev-ext-persist")
    assert traces == []

    # No traces should have been written
    assert service.trace_repo.list_all() == []
