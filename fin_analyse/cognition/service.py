"""Service facade for cognition workflows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from fin_analyse.cognition.evidence_store import JsonlRepository
from fin_analyse.cognition.extractor import (
    HybridReasoningExtractor,
    RuleBasedReasoningExtractor,
    UnavailableReasoningExtractor,
)
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
    CognitivePattern,
    EvidenceItem,
    ReasoningTrace,
    SourceLabel,
    TeacherPersona,
    TraceVerification,
)
from fin_analyse.cognition.pattern_miner import SimplePatternMiner
from fin_analyse.cognition.persona_gate import (
    PersonaGateDecision,
    PersonaIngestionGate,
)
from fin_analyse.cognition.trace_verifier import (
    SelectiveTraceVerifier,
    TraceVerificationError,
    TraceVerificationReport,
)
from fin_analyse.cognition.write_gate import CognitionWriteGateService

logger = logging.getLogger(__name__)


class CognitiveService:
    def __init__(
        self,
        runtime_root: Path | None = None,
        *,
        llm_helper=None,
    ) -> None:
        if runtime_root is None:
            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            runtime_root = default_knowledge_base_root() / "runtime" / "cognition"
        root = runtime_root
        self.runtime_root = root

        # Cognition core JSONL repositories are now owned by the memory store.
        self.memory_store = CognitionMemoryStoreService(root)

        # Compatibility attributes — direct repository access preserved for
        # unmigrated paths (trace verification) and existing tests.
        self.evidence_repo: JsonlRepository[EvidenceItem] = self.memory_store.evidence_repo
        self.trace_repo: JsonlRepository[ReasoningTrace] = self.memory_store.trace_repo
        self.pattern_repo: JsonlRepository[CognitivePattern] = self.memory_store.pattern_repo
        self.persona_repo: JsonlRepository[TeacherPersona] = self.memory_store.persona_repo
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
        self.persona_gate = PersonaIngestionGate()
        self._write_gate_service = CognitionWriteGateService(gate=self.persona_gate)

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
