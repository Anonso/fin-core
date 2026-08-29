"""Service facade for cognition workflows."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from fin_analyse.cognition.evidence_store import JsonlRepository
from fin_analyse.cognition.extractor import (
    HybridReasoningExtractor,
    RuleBasedReasoningExtractor,
    UnavailableReasoningExtractor,
)
from fin_analyse.cognition.feedback import FeedbackRecorder
from fin_analyse.cognition.labeling import (
    HybridSourceLabeler,
    LLMSourceLabeler,
    SourceLabeler,
)
from fin_analyse.cognition.memory_store import (
    CognitionMemoryRequest,
    CognitionMemoryScope,
    CognitionMemoryStoreService,
)
from fin_analyse.cognition.models import (
    CognitiveFeedback,
    CognitivePattern,
    EvidenceItem,
    PersonaAnalysis,
    ReasoningTrace,
    SourceLabel,
    TeacherPersona,
    TraceVerification,
)
from fin_analyse.cognition.pattern_miner import SimplePatternMiner
from fin_analyse.cognition.persona import LLMPersonaEngine, PersonaEngine
from fin_analyse.cognition.persona_gate import (
    PersonaGateDecision,
    PersonaIngestionGate,
)
from fin_analyse.cognition.persona_moa import MoAPersonaAnalyzer, PersonaMoAAdapter
from fin_analyse.cognition.trace_verifier import (
    SelectiveTraceVerifier,
    TraceVerificationError,
    TraceVerificationReport,
)
from fin_analyse.cognition.write_gate import CognitionWriteGateService
from fin_analyse.moa.engine import MoAEngine
from fin_analyse.utils.ids import stable_id

logger = logging.getLogger(__name__)

# MoA candidate orders (家规规则 6: llm.yaml `priorities` overrides at runtime;
# these tuples are the hardcoded fallback when the section is absent).
_MOA_T0_FALLBACK_ORDER: tuple[str, ...] = ("glm53", "deepseek")
_MOA_T1_FALLBACK_ORDER: tuple[str, ...] = ("glm53_flash", "deepseek", "glm53", "qwen")


class _CognitionLLMCompletionBackend:
    """Wrap a CognitionLLM so it satisfies MoAEngine's TextCompletionBackend."""

    def __init__(self, llm) -> None:
        self.llm = llm
        self.name: str | None = None

    def complete(self, prompt: str) -> str:
        return cast(str, self.llm.complete_text(prompt))


def _resolve_moa_backends(
    backends: dict[str, Any],
    *,
    t0_order: tuple[str, ...] | None = None,
    t1_order: tuple[str, ...] | None = None,
) -> tuple[_CognitionLLMCompletionBackend, _CognitionLLMCompletionBackend, str] | None:
    """Pick T0 (aggregator + primary reference) and T1 (cross-check) backends.

    Candidate orders come from llm.yaml `priorities` (家规规则 6) via the
    caller; the hardcoded fallbacks apply when orders are not provided.

    Selection rules:
    - glm53 available: T0 = glm53 (aggregator), T1 = glm53_flash / DeepSeek / Qwen
    - glm53 unavailable: T0 = DeepSeek, T1 follows T1 order
    - If only one backend: T0 and T1 degrade to same model
    """
    from fin_analyse.cognition.llm import CognitionLLM

    def _wrap(name: str, backend: Any) -> _CognitionLLMCompletionBackend:
        wrapper = _CognitionLLMCompletionBackend(CognitionLLM(backend=backend))
        wrapper.name = name
        return wrapper

    t0_name: str | None = None
    t0_backend: _CognitionLLMCompletionBackend | None = None
    # glm53 is the preferred T0 aggregator; DeepSeek is the T0 fallback.
    for name in t0_order or _MOA_T0_FALLBACK_ORDER:
        backend = backends.get(name)
        if backend is not None:
            t0_backend = _wrap(name, backend)
            t0_name = name
            break
    if t0_backend is None:
        for name, backend in backends.items():
            t0_backend = _wrap(name, backend)
            t0_name = name
            break
    if t0_backend is None or t0_name is None:
        return None

    # Secondary reference slot follows the T1 order, excluding T0.
    t1_candidates = tuple(
        name
        for name in (t1_order or _MOA_T1_FALLBACK_ORDER)
        if name != t0_name
    )

    t1_backend: _CognitionLLMCompletionBackend | None = None
    for name in t1_candidates:
        if name == t0_name:
            continue
        backend = backends.get(name)
        if backend is not None:
            t1_backend = _wrap(name, backend)
            break
    if t1_backend is None:
        t1_backend = t0_backend

    return t0_backend, t1_backend, t0_name


class CognitiveService:
    def __init__(
        self,
        runtime_root: Path | None = None,
        *,
        llm_helper=None,
        llm_persona_engine: LLMPersonaEngine | None = None,
        moa_engine: MoAEngine | None = None,
        moa_analyzer: MoAPersonaAnalyzer | None = None,
    ) -> None:
        if runtime_root is None:
            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            runtime_root = default_knowledge_base_root() / "runtime" / "cognition"
        root = runtime_root
        self.runtime_root = root

        # Cognition core JSONL repositories are now owned by the memory store.
        self.memory_store = CognitionMemoryStoreService(root)

        # Compatibility attributes — direct repository access preserved for
        # unmigrated paths (feedback, trace verification) and existing tests.
        self.evidence_repo: JsonlRepository[EvidenceItem] = self.memory_store.evidence_repo
        self.trace_repo: JsonlRepository[ReasoningTrace] = self.memory_store.trace_repo
        self.pattern_repo: JsonlRepository[CognitivePattern] = self.memory_store.pattern_repo
        self.persona_repo: JsonlRepository[TeacherPersona] = self.memory_store.persona_repo
        self.analysis_repo: JsonlRepository[PersonaAnalysis] = self.memory_store.analysis_repo
        self.feedback_repo: JsonlRepository[CognitiveFeedback] = self.memory_store.feedback_repo
        self.trace_verification_repo: JsonlRepository[TraceVerification] = (
            self.memory_store.trace_verification_repo
        )

        rule_labeler = SourceLabeler(default_teacher_id="guo")
        rule_extractor = RuleBasedReasoningExtractor()

        self.labeler: SourceLabeler | HybridSourceLabeler
        self.extractor: (
            UnavailableReasoningExtractor
            | RuleBasedReasoningExtractor
            | HybridReasoningExtractor
        )

        if llm_helper is not None:
            llm_labeler = LLMSourceLabeler(llm_helper, default_teacher_id="guo")
            self.labeler = HybridSourceLabeler(rule_labeler, llm_labeler)

            # Strict consensus requires >=2 independent backends.
            # Fewer than 2 → refuse trace writes; NO single-LLM fallback.
            from fin_analyse.cognition.llm import CognitionLLM

            multi_llms = CognitionLLM.from_config_multi(count=3)
            if len(multi_llms) >= 2:
                from fin_analyse.cognition.extractor import ConsensusReasoningExtractor

                # Only an independent third backend may act as aggregator.
                # With exactly two backends the aggregator is None — divergent
                # references then surface a stable data gap instead of reusing
                # a reference as aggregator.
                consensus = ConsensusReasoningExtractor(
                    primary_llm=multi_llms[0],
                    secondary_llm=multi_llms[1],
                    aggregator_llm=multi_llms[2] if len(multi_llms) >= 3 else None,
                )
                self.extractor = HybridReasoningExtractor(consensus, rule_extractor)
                self.llm_available = True
                logger.info(
                    "CognitiveService: dual-LLM consensus extraction (%d backends)",
                    len(multi_llms),
                )
            else:
                # < 2 independent backends → strict contract forbids any
                # ReasoningTrace write. Expose insufficient-backends data gap.
                self.extractor = UnavailableReasoningExtractor(
                    data_gaps=("reasoning_trace_insufficient_backends",)
                )
                self.llm_available = False
                logger.info(
                    "CognitiveService: insufficient backends (%d) for consensus "
                    "extraction — no ReasoningTrace writes",
                    len(multi_llms),
                )
        else:
            self.labeler = rule_labeler
            self.extractor = UnavailableReasoningExtractor()
            self.llm_available = False

        self.pattern_miner = SimplePatternMiner()
        self.persona_engine = PersonaEngine()
        self.persona_gate = PersonaIngestionGate()
        self._write_gate_service = CognitionWriteGateService(gate=self.persona_gate)
        self.feedback_recorder = FeedbackRecorder(self.feedback_repo)

        self.llm_persona_engine = llm_persona_engine
        if self.llm_persona_engine is None and llm_helper is not None:
            self.llm_persona_engine = LLMPersonaEngine(llm_helper)
        self.moa_engine = moa_engine
        self.moa_analyzer = moa_analyzer or MoAPersonaAnalyzer()

    def _teacher_scope(self, teacher_id: str) -> CognitionMemoryScope:
        return CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id=teacher_id)

    def _scope_for_evidence(self, evidence: EvidenceItem) -> CognitionMemoryScope:
        teacher_id = evidence.source_label.teacher_id or ""
        if teacher_id:
            return self._teacher_scope(teacher_id)
        return CognitionMemoryScope(memory_kind="external_evidence")

    def save_evidence(self, evidence: EvidenceItem) -> None:
        self.memory_store.handle(
            CognitionMemoryRequest(
                operation="save_evidence",
                scope=self._scope_for_evidence(evidence),
                evidence=evidence,
            )
        )

    def label_evidence(self, evidence_id: str) -> SourceLabel:
        evidence = self._get_evidence(evidence_id)
        label = self.labeler.label(
            title=evidence.title,
            content=evidence.content,
            author=evidence.author,
        )
        labeled = EvidenceItem.from_dict(
            {
                **evidence.to_dict(),
                "source_label": label.to_dict(),
            }
        )
        result = self._write_gate_service.evaluate_evidence(labeled)
        self.memory_store.handle(
            CognitionMemoryRequest(
                operation="save_evidence",
                scope=self._scope_for_evidence(result.gated_evidence),
                evidence=result.gated_evidence,
            )
        )
        return result.gated_evidence.source_label

    def persona_gate_decision(self, evidence_id: str) -> PersonaGateDecision:
        evidence = self._get_evidence(evidence_id)
        result = self._write_gate_service.evaluate_evidence(evidence)
        self.memory_store.handle(
            CognitionMemoryRequest(
                operation="save_evidence",
                scope=self._scope_for_evidence(result.gated_evidence),
                evidence=result.gated_evidence,
            )
        )
        return result.gate_decision

    @property
    def last_extraction_data_gaps(self) -> tuple[str, ...]:
        """Stable data gap identifiers from the most recent extraction attempt.

        Exposes why no ReasoningTrace was written (e.g. insufficient backends,
        aggregator unavailable) so callers can report a data gap instead of
        silently degrading.
        """
        return tuple(getattr(self.extractor, "last_data_gaps", ()))

    def extract_teacher_reasoning(self, evidence_id: str) -> list[ReasoningTrace]:
        evidence = self._get_evidence(evidence_id)
        result = self._write_gate_service.evaluate_evidence(evidence)
        if result.gated_evidence != evidence:
            self.memory_store.handle(
                CognitionMemoryRequest(
                    operation="save_evidence",
                    scope=self._scope_for_evidence(result.gated_evidence),
                    evidence=result.gated_evidence,
                )
            )
        if not result.is_teacher_cognition:
            logger.info("skip non-persona-eligible cognition evidence: %s", evidence_id)
            return []
        traces = self.extractor.extract(result.gated_evidence)
        for trace in traces:
            store_result = self.memory_store.handle(
                CognitionMemoryRequest(
                    operation="upsert_trace",
                    scope=self._teacher_scope(trace.teacher_id),
                    trace=trace,
                )
            )
            if store_result.status == "error":
                logger.warning(
                    "memory store rejected trace %s: %s",
                    trace.trace_id,
                    store_result.payload,
                )
        return traces

    def verify_trace(
        self,
        trace_id: str,
        *,
        verifier: SelectiveTraceVerifier | None = None,
    ) -> TraceVerification:
        matches = self.trace_repo.find(lambda item: item.trace_id == trace_id)
        if not matches:
            raise KeyError(f"Trace not found: {trace_id}")
        trace = matches[-1]
        evidence = self._get_evidence(trace.source_evidence_id)
        actual_verifier = verifier or self._default_trace_verifier()
        verification = actual_verifier.verify(trace, evidence)
        self.memory_store.handle(
            CognitionMemoryRequest(
                operation="upsert_trace_verification",
                scope=self._teacher_scope(trace.teacher_id),
                trace_verification=verification,
            )
        )
        return verification

    def verify_low_confidence_traces(
        self,
        *,
        threshold: float = 0.5,
        limit: int = 3,
        resume: bool = True,
        teacher_id: str | None = "guo",
        verifier: SelectiveTraceVerifier | None = None,
    ) -> TraceVerificationReport:
        report = TraceVerificationReport()
        # teacher_id=None means "all teachers" — use "*" wildcard scope.
        scope = self._teacher_scope("*" if teacher_id is None else teacher_id)
        select_result = self.memory_store.handle(
            CognitionMemoryRequest(
                operation="select_low_confidence_traces",
                scope=scope,
                threshold=threshold,
                limit=limit,
            )
        )
        traces: list[ReasoningTrace] = select_result.payload.get("traces", [])
        report.selected_count = len(traces)

        list_result = self.memory_store.handle(
            CognitionMemoryRequest(
                operation="list_trace_verifications",
                scope=scope,
            )
        )
        existing_trace_ids = {
            verification.trace_id for verification in list_result.payload.get("verifications", [])
        }
        actual_verifier = verifier or self._default_trace_verifier()

        for trace in traces:
            if resume and trace.trace_id in existing_trace_ids:
                report.skipped_count += 1
                continue
            try:
                evidence = self._get_evidence(trace.source_evidence_id)
                verification = actual_verifier.verify(trace, evidence)
                # Use the trace's own teacher_id for writes — wildcard "*"
                # is only valid for read/select operations.
                write_scope = self._teacher_scope(trace.teacher_id)
                self.memory_store.handle(
                    CognitionMemoryRequest(
                        operation="upsert_trace_verification",
                        scope=write_scope,
                        trace_verification=verification,
                    )
                )
                report.verified_count += 1
                report.verification_ids.append(verification.verification_id)
                if verification.verdict == "keep":
                    report.keep_count += 1
                elif verification.verdict == "revise":
                    report.revise_count += 1
                elif verification.verdict == "reject":
                    report.reject_count += 1
            except (KeyError, TraceVerificationError, ValueError) as exc:
                report.error_count += 1
                if len(report.errors_sample) < 5:
                    report.errors_sample.append(f"{trace.trace_id}: {exc}")
        return report

    def _default_trace_verifier(self) -> SelectiveTraceVerifier:
        from fin_analyse.cognition.llm import CognitionLLM

        llm = CognitionLLM.from_config(preferred=("glm53", "deepseek", "qwen", "claude"))
        backend = llm.backend
        if backend is None:
            backend_name = "unknown"
        else:
            backend_name = getattr(backend, "name", None) or backend.__class__.__name__
        return SelectiveTraceVerifier(llm, verifier_backend=str(backend_name))

    def _eligible_persona_traces(self, teacher_id: str) -> list[ReasoningTrace]:
        list_result = self.memory_store.handle(
            CognitionMemoryRequest(
                operation="list_traces",
                scope=self._teacher_scope(teacher_id),
            )
        )
        all_traces: list[ReasoningTrace] = list_result.payload.get("traces", [])
        traces: list[ReasoningTrace] = []
        evidence_lookup: dict[str, EvidenceItem] = {}
        for trace in all_traces:
            try:
                evidence = self._get_evidence(trace.source_evidence_id)
            except KeyError:
                logger.info(
                    "skip trace with missing evidence during Persona rebuild: %s", trace.trace_id
                )
                continue
            result = self._write_gate_service.prepare_evidence(evidence)
            if result.gated_evidence != evidence:
                self.memory_store.handle(
                    CognitionMemoryRequest(
                        operation="save_evidence",
                        scope=self._scope_for_evidence(result.gated_evidence),
                        evidence=result.gated_evidence,
                    )
                )
            traces.append(trace)
            evidence_lookup[trace.source_evidence_id] = result.gated_evidence
        return self._write_gate_service.select_traces(
            traces,
            evidence_lookup,
            teacher_id=teacher_id,
        )

    def rebuild_persona(self, teacher_id: str) -> TeacherPersona:
        traces = self._eligible_persona_traces(teacher_id)
        patterns = self.pattern_miner.mine(traces)
        scope = self._teacher_scope(teacher_id)
        for pattern in patterns:
            self.memory_store.handle(
                CognitionMemoryRequest(
                    operation="upsert_pattern",
                    scope=scope,
                    pattern=pattern,
                )
            )
        persona = self.persona_engine.build_persona(teacher_id, patterns, traces)
        self.memory_store.handle(
            CognitionMemoryRequest(
                operation="upsert_persona",
                scope=scope,
                persona=persona,
            )
        )
        return persona

    def analyze_with_persona(
        self,
        question: str,
        *,
        teacher_id: str = "guo",
        company: str | None = None,
        ticker: str | None = None,
        metadata: dict | None = None,
        force_new: bool = False,
        quality_mode: str | None = None,
    ) -> PersonaAnalysis:
        scope = self._teacher_scope(teacher_id)

        personas_result = self.memory_store.handle(
            CognitionMemoryRequest(operation="list_personas", scope=scope)
        )
        personas: list[TeacherPersona] = personas_result.payload.get("personas", [])
        persona = personas[-1] if personas else self.rebuild_persona(teacher_id)

        traces_result = self.memory_store.handle(
            CognitionMemoryRequest(operation="list_traces", scope=scope)
        )
        traces: list[ReasoningTrace] = traces_result.payload.get("traces", [])

        patterns_result = self.memory_store.handle(
            CognitionMemoryRequest(operation="list_patterns", scope=scope)
        )
        patterns: list[CognitivePattern] = patterns_result.payload.get("patterns", [])

        verifications_result = self.memory_store.handle(
            CognitionMemoryRequest(operation="list_trace_verifications", scope=scope)
        )
        verifications: list[TraceVerification] = verifications_result.payload.get(
            "verifications", []
        )

        quality = (quality_mode or (metadata or {}).get("quality_mode") or "standard").lower()
        analysis: PersonaAnalysis | None = None
        if quality == "moa":
            analysis = self._analyze_with_moa(
                persona=persona,
                question=question,
                traces=traces,
                patterns=patterns,
                company=company,
                ticker=ticker,
                verifications=verifications,
            )
            if analysis is None and self.llm_persona_engine is not None:
                analysis = self.llm_persona_engine.analyze(
                    persona=persona,
                    question=question,
                    traces=traces,
                    patterns=patterns,
                    company=company,
                    ticker=ticker,
                    verifications=verifications,
                    quality_mode="moa",
                )
        elif quality in {"standard", "deep", "llm"} and self.llm_persona_engine is not None:
            analysis = self.llm_persona_engine.analyze(
                persona=persona,
                question=question,
                traces=traces,
                patterns=patterns,
                company=company,
                ticker=ticker,
                verifications=verifications,
                quality_mode=quality,
            )

        if analysis is None:
            analysis = self.persona_engine.analyze(
                persona=persona,
                question=question,
                traces=traces,
                patterns=patterns,
                company=company,
                ticker=ticker,
                verifications=verifications,
            )
            if analysis.metadata.get("quality_mode") is None:
                analysis = replace(
                    analysis,
                    metadata={
                        **analysis.metadata,
                        "quality_mode": "quick" if quality == "quick" else "rule",
                    },
                )

        audit_metadata = dict(metadata or {})
        if force_new:
            request_id = str(audit_metadata.get("request_id") or analysis.created_at)
            merged_metadata = {**audit_metadata, **analysis.metadata}
            analysis = replace(
                analysis,
                analysis_id=stable_id(analysis.analysis_id, request_id, prefix="pa-"),
                metadata=merged_metadata,
            )
        elif audit_metadata:
            analysis = replace(analysis, metadata={**audit_metadata, **analysis.metadata})
        self.memory_store.handle(
            CognitionMemoryRequest(
                operation="upsert_analysis",
                scope=scope,
                analysis=analysis,
            )
        )
        return analysis

    def _analyze_with_moa(
        self,
        *,
        persona: TeacherPersona,
        question: str,
        traces: list[ReasoningTrace],
        patterns: list[CognitivePattern],
        company: str | None = None,
        ticker: str | None = None,
        verifications: list[TraceVerification] | None = None,
    ) -> PersonaAnalysis | None:
        engine = self.moa_engine
        if engine is None:
            engine = self._default_moa_engine()
            self.moa_engine = engine
        if engine is None:
            logger.info("MoA engine unavailable, falling back for quality=moa")
            return None
        request = PersonaMoAAdapter.build_request(
            persona=persona,
            question=question,
            traces=traces,
            patterns=patterns,
            company=company,
            ticker=ticker,
        )
        result = engine.deliberate(request)
        return self.moa_analyzer.to_analysis(
            result=result,
            persona=persona,
            question=question,
            traces=traces,
            patterns=patterns,
            company=company,
            ticker=ticker,
            verifications=verifications,
        )

    def _default_moa_engine(self) -> MoAEngine | None:
        from fin_analyse.claims.config_loader import (
            configured_backend_order,
            create_backends_from_config,
        )

        backends = create_backends_from_config()
        resolved = _resolve_moa_backends(
            backends,
            t0_order=configured_backend_order("t0", _MOA_T0_FALLBACK_ORDER),
            t1_order=configured_backend_order("t1", _MOA_T1_FALLBACK_ORDER),
        )
        if resolved is None:
            return None
        t0_backend, t1_backend, aggregator_name = resolved
        return MoAEngine(
            aggregator_backend=t0_backend,
            aggregator_backend_name=aggregator_name,
            reference_backends={"t0": t0_backend, "t1": t1_backend},
        )

    def record_feedback(
        self,
        *,
        target_type: str,
        target_id: str,
        feedback_type: str,
        note: str | None = None,
    ) -> CognitiveFeedback:
        from datetime import UTC, datetime
        from hashlib import sha1

        created_at = datetime.now(UTC).isoformat()
        seed = f"{target_type}:{target_id}:{feedback_type}:{note or ''}:{created_at}"
        feedback = CognitiveFeedback(
            feedback_id="fb-" + sha1(seed.encode("utf-8")).hexdigest()[:12],
            target_type=target_type,
            target_id=target_id,
            feedback_type=feedback_type,
            note=note,
            created_at=created_at,
        )
        result = self.memory_store.handle(
            CognitionMemoryRequest(
                operation="record_feedback",
                scope=CognitionMemoryScope(memory_kind="teacher_cognition", teacher_id="guo"),
                feedback=feedback,
            )
        )
        stored = result.payload.get("feedback")
        if stored is None:
            raise RuntimeError(f"Memory store failed to record feedback: {result.payload}")
        return cast(CognitiveFeedback, stored)

    def _get_evidence(self, evidence_id: str) -> EvidenceItem:
        result = self.memory_store.handle(
            CognitionMemoryRequest(
                operation="get_evidence",
                scope=self._teacher_scope(""),
                evidence_id=evidence_id,
            )
        )
        if result.payload.get("not_found") or result.status == "error":
            raise KeyError(f"Evidence not found: {evidence_id}")
        return cast(EvidenceItem, result.payload["evidence"])
