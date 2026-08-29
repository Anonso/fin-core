"""Teacher persona construction and deterministic v0 analysis."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from hashlib import sha1

from fin_analyse.cognition.models import (
    CognitivePattern,
    PersonaAnalysis,
    ReasoningTrace,
    TeacherPersona,
    TraceVerification,
)

logger = logging.getLogger(__name__)

SOURCE_CLASSIFICATION_KEY = "source_classification"


def _source_classification_metadata(
    *,
    direct_traces: list[ReasoningTrace],
    transfer_patterns: list[CognitivePattern],
    transfer_available: bool,
) -> dict[str, object]:
    evidence_ids = [trace.source_evidence_id for trace in direct_traces]
    return {
        SOURCE_CLASSIFICATION_KEY: {
            "direct_knowledge": {
                "available": bool(direct_traces),
                "trace_ids": [trace.trace_id for trace in direct_traces],
                "evidence_ids": evidence_ids,
                "note": "老师原创 trace 仅作为参考材料，不单独构成高置信证据。",
            },
            "methodology_transfer": {
                "available": transfer_available,
                "pattern_ids": [pattern.pattern_id for pattern in transfer_patterns],
                "basis": [pattern.name for pattern in transfer_patterns],
            },
            "external_observation": {
                "available": False,
                "note": "外部上下文由调用方作为参考材料单独标注，不进入老师认知学习层。",
            },
        },
        "evidence_gap": {
            "direct_trace_count": len(direct_traces),
            "direct_evidence_count": len(evidence_ids),
            "message": (
                "存在老师直接 trace/evidence，但仍需外部事实交叉验证。"
                if direct_traces
                else "当前标的缺少老师直接 trace/evidence，只能按方法论迁移低置信观察。"
            ),
        },
        "confidence_boundary": {
            "level": _confidence_level(direct_traces, transfer_available),
            "reason": "老师原创 trace 与方法论迁移均仅作参考，未经验证不得形成高置信结论。",
        },
    }


def _confidence_level(
    _direct_traces: list[ReasoningTrace],
    _transfer_available: bool,
) -> str:
    # 老师原创 trace 仅作为参考材料，不提升置信度边界。
    return "low"


def _methodology_transfer_confidence(patterns: list[CognitivePattern]) -> float:
    if not patterns:
        return 0.35
    average = sum(pattern.confidence for pattern in patterns) / len(patterns)
    return max(0.42, min(0.58, average * 0.65))


def _methodology_reasoning_steps(patterns: list[CognitivePattern]) -> list[str]:
    steps = ["当前认知库缺少该标的的老师直接 trace/evidence，以下只能作为方法论迁移观察。"]
    for pattern in patterns[:3]:
        variables = (
            "、".join(pattern.typical_variables) if pattern.typical_variables else "关键事实变量"
        )
        triggers = "；".join(pattern.trigger_conditions[:2]) or pattern.description
        steps.append(
            f"迁移{pattern.name}：{pattern.typical_reasoning_shape}"
            f"通常关注{variables}。触发条件：{triggers}。"
        )
    steps.append("需要用行情、财务、公告、新闻或资金流等外部事实交叉验证，验证前不形成高置信结论。")
    return steps


def _supporting_traces_for_patterns(
    patterns: list[CognitivePattern],
    traces: list[ReasoningTrace],
) -> list[ReasoningTrace]:
    supporting_ids = {trace_id for pattern in patterns for trace_id in pattern.supporting_trace_ids}
    return [trace for trace in traces if trace.trace_id in supporting_ids]


def _company_name_parts(company: str) -> list[str]:
    parts: list[str] = [company]
    for suffix in ["科技", "高科", "高新", "股份", "集团", "控股", "有限", "公司"]:
        if company.endswith(suffix) and len(company) > len(suffix) + 1:
            parts.append(company[: -len(suffix)])
    return [p for p in parts if len(p) >= 2]


def _trace_matches_company(trace, company: str) -> bool:
    if company in trace.companies:
        return True
    for tc in trace.companies:
        if tc in company or company in tc:
            return True
    name_parts = _company_name_parts(company)
    for part in name_parts:
        if part in trace.topic:
            return True
        for tc in trace.companies:
            if part in tc:
                return True
    return False


class PersonaEngine:
    def build_persona(
        self,
        teacher_id: str,
        patterns: list[CognitivePattern],
        traces: list[ReasoningTrace],
    ) -> TeacherPersona:
        pattern_ids = [p.pattern_id for p in patterns if p.teacher_id == teacher_id]
        explicit_rules = sorted({rule for trace in traces for rule in trace.action_implications})
        return TeacherPersona(
            persona_id=f"{teacher_id}:v0",
            teacher_id=teacher_id,
            display_name="郭老师" if teacher_id == "guo" else teacher_id,
            active_version="v0",
            style_summary=("先看关键变量是否真实兑现，再判断是否值得行动；重视风险边界，不追高。"),
            core_pattern_ids=pattern_ids,
            explicit_rules=explicit_rules,
            known_blind_spots=["样本不足时只能输出低置信观察"],
            evidence_policy={"teacher_original_only_for_cognition": True},
            last_built_at=datetime.now(UTC).isoformat(),
        )

    def analyze(
        self,
        *,
        persona: TeacherPersona,
        question: str,
        traces: list[ReasoningTrace],
        patterns: list[CognitivePattern],
        company: str | None = None,
        ticker: str | None = None,
        verifications: list[TraceVerification] | None = None,
    ) -> PersonaAnalysis:
        if company:
            relevant_traces = [
                trace
                for trace in traces
                if trace.teacher_id == persona.teacher_id and _trace_matches_company(trace, company)
            ]
        else:
            relevant_traces = [trace for trace in traces if trace.teacher_id == persona.teacher_id]
        relevant_patterns = [p for p in patterns if p.teacher_id == persona.teacher_id]
        analysis_seed = f"{persona.persona_id}:{question}:{company or ''}:{ticker or ''}"
        analysis_id = "pa-" + sha1(analysis_seed.encode("utf-8")).hexdigest()[:12]
        transfer_patterns = relevant_patterns[:3]
        if not relevant_traces and transfer_patterns:
            supporting_traces = _supporting_traces_for_patterns(transfer_patterns, traces)
            metadata = _source_classification_metadata(
                direct_traces=[],
                transfer_patterns=transfer_patterns,
                transfer_available=True,
            )
            return PersonaAnalysis(
                analysis_id=analysis_id,
                persona_id=persona.persona_id,
                question=question,
                company=company,
                ticker=ticker,
                activated_trace_ids=[],
                activated_pattern_ids=[pattern.pattern_id for pattern in transfer_patterns],
                evidence_ids=[],
                reasoning_steps=_methodology_reasoning_steps(transfer_patterns),
                conclusion="缺少该标的的老师直接证据，仅能按已学框架做低置信方法论迁移观察",
                stance="watch",
                confidence=_methodology_transfer_confidence(transfer_patterns),
                uncertainty=["缺少该标的的老师原创历史推理支撑"],
                contradictions=[],
                unsupported_claims=["该结论是方法论迁移观察，不是老师直接观点。"],
                invalidation_conditions=sorted(
                    {
                        condition
                        for trace in supporting_traces
                        for condition in trace.invalidation_conditions
                    }
                ),
                suggested_followups=sorted(
                    {action for trace in supporting_traces for action in trace.action_implications}
                ),
                created_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )
        reasoning_steps = []
        for trace in relevant_traces[:3]:
            if trace.inferred_relationships:
                reasoning_steps.append(
                    f"参考{trace.topic}推理：{trace.inferred_relationships[0]}，"
                    f"结论倾向：{trace.conclusion}"
                )
        if not reasoning_steps:
            reasoning_steps.append("当前认知库缺少直接支撑，只能给低置信观察。")

        if not relevant_traces:
            confidence = 0.35
        else:
            verif_map = {v.trace_id: v.verdict for v in (verifications or [])}
            adjusted_confs = []
            for t in relevant_traces[:3]:
                base = t.extraction_confidence
                verdict = verif_map.get(t.trace_id)
                if verdict == "revise":
                    base *= 0.8
                elif verdict == "reject":
                    base *= 0.5
                adjusted_confs.append(base)
            # 老师直接 trace 仅作参考，不单独构成高置信证据。
            confidence = min(0.4, sum(adjusted_confs) / len(adjusted_confs))
        metadata = _source_classification_metadata(
            direct_traces=relevant_traces[:3],
            transfer_patterns=[],
            transfer_available=False,
        )
        return PersonaAnalysis(
            analysis_id=analysis_id,
            persona_id=persona.persona_id,
            question=question,
            company=company,
            ticker=ticker,
            activated_trace_ids=[trace.trace_id for trace in relevant_traces[:3]],
            activated_pattern_ids=[p.pattern_id for p in relevant_patterns[:3]],
            evidence_ids=[trace.source_evidence_id for trace in relevant_traces[:3]],
            reasoning_steps=reasoning_steps,
            conclusion=("关注但不追高" if relevant_traces else "认知库不足，暂不形成老师视角结论"),
            stance="watch" if relevant_traces else "unknown",
            confidence=confidence,
            uncertainty=([] if relevant_traces else ["缺少老师原创历史推理支撑"]),
            contradictions=[],
            unsupported_claims=(
                ["老师直接 trace 仅作为参考材料，不构成确定性证据。"] if relevant_traces else []
            ),
            invalidation_conditions=sorted(
                {c for trace in relevant_traces for c in trace.invalidation_conditions}
            ),
            suggested_followups=sorted(
                {a for trace in relevant_traces for a in trace.action_implications}
            ),
            created_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )


class LLMPersonaEngine:
    """Generate PersonaAnalysis using LLM grounded in existing traces/patterns."""

    _ANALYZE_PROMPT = (
        "你是一位投资老师的认知代理。基于以下历史推理库，回答用户问题。\n\n"
        "## 老师风格\n{style_summary}\n\n"
        "## 已知规则\n{explicit_rules}\n\n"
        "## 相关历史推理 (traces)\n{traces_text}\n\n"
        "## 问题\n{question}\n\n"
        "返回一个 JSON 对象，包含以下字段：\n"
        "- topic: string (判断主题)\n"
        "- conclusion: string (核心结论，使用中文)\n"
        "- stance: 必须是 bull / bear / watch / unknown 之一\n"
        "- confidence: float 0-1。没有支撑时必须 < 0.4。即使存在老师直接 trace，"
        "它也仅作为参考材料，不单独提升置信度；此时 confidence 不得超过 0.4。\n"
        "- reasoning_steps: list of strings (推理步骤，每步引用一条历史 trace)\n"
        "- risk_boundaries: list of strings\n"
        "- invalidation_conditions: list of strings\n"
        "- unsupported_claims: list of strings (缺少证据支撑的判断，"
        "如直接 trace 只是参考、需要外部验证等)\n"
        "- suggested_followups: list of strings\n"
        "- activated_trace_ids: list of strings (引用的 trace_id 列表，不能编造)\n\n"
        "只能引用上方 given trace_ids 中真实存在的 trace_id。"
        "没有相关 trace 时，confidence 必须 < 0.4，stance 设为 unknown。"
    )

    def __init__(self, llm) -> None:
        self.llm = llm

    def analyze(
        self,
        *,
        persona: TeacherPersona,
        question: str,
        traces: list[ReasoningTrace],
        patterns: list[CognitivePattern],
        company: str | None = None,
        ticker: str | None = None,
        verifications: list[TraceVerification] | None = None,
        quality_mode: str = "standard",
    ) -> PersonaAnalysis | None:
        valid_trace_ids = {t.trace_id for t in traces}
        traces_text = (
            "\n\n".join(
                f"[trace_id={t.trace_id}] topic={t.topic} conclusion={t.conclusion} "
                f"companies={t.companies} stance={t.stance}"
                for t in traces[:10]
            )
            or "（暂无历史推理）"
        )

        prompt = self._ANALYZE_PROMPT.format(
            style_summary=persona.style_summary,
            explicit_rules="; ".join(persona.explicit_rules[:10]),
            traces_text=traces_text,
            question=f"{question} (company={company or '未指定'}, ticker={ticker or '未指定'})",
        )

        result = self.llm.complete_json(prompt, expected_type="persona_analysis")
        if not result.ok or not isinstance(result.data, dict):
            logger.info("LLM persona analysis failed: %s", result.error)
            return None

        data = result.data
        analysis_seed = f"{persona.persona_id}:{question}:{company or ''}:{ticker or ''}:llm"
        analysis_id = "pa-" + sha1(analysis_seed.encode("utf-8")).hexdigest()[:12]

        # Only accept trace_ids that exist
        activated = [tid for tid in data.get("activated_trace_ids", []) if tid in valid_trace_ids]
        evidence_ids = list({t.source_evidence_id for t in traces if t.trace_id in activated})

        raw_confidence = min(max(float(data.get("confidence", 0.35)), 0.0), 1.0)
        verif_map = {v.trace_id: v.verdict for v in (verifications or [])}
        revise_count = sum(1 for tid in activated if verif_map.get(tid) == "revise")
        reject_count = sum(1 for tid in activated if verif_map.get(tid) == "reject")
        adjusted_confidence = raw_confidence * (0.8**revise_count) * (0.5**reject_count)

        direct_traces = [t for t in traces if t.trace_id in activated]
        # 老师直接 trace 仅作参考，不单独提升置信度。
        if direct_traces:
            adjusted_confidence = min(adjusted_confidence, 0.4)
        transfer_patterns: list[CognitivePattern] = []
        transfer_available = False
        if not direct_traces and patterns:
            transfer_patterns = patterns[:3]
            transfer_available = True

        metadata = _source_classification_metadata(
            direct_traces=direct_traces,
            transfer_patterns=transfer_patterns,
            transfer_available=transfer_available,
        )
        metadata["quality_mode"] = quality_mode
        metadata["moa_audit"] = None
        metadata["needs_human_review"] = adjusted_confidence < 0.5 or bool(
            data.get("unsupported_claims")
        )

        return PersonaAnalysis(
            analysis_id=analysis_id,
            persona_id=persona.persona_id,
            question=question,
            company=company,
            ticker=ticker,
            activated_trace_ids=activated,
            activated_pattern_ids=[p.pattern_id for p in patterns[:3]],
            evidence_ids=evidence_ids,
            reasoning_steps=list(data.get("reasoning_steps", [])),
            conclusion=str(data.get("conclusion", "认知库不足，暂不形成老师视角结论")),
            stance=_validate_persona_stance(data.get("stance", "unknown")),
            confidence=adjusted_confidence,
            uncertainty=[],
            contradictions=[],
            unsupported_claims=list(data.get("unsupported_claims", [])),
            invalidation_conditions=list(data.get("invalidation_conditions", [])),
            suggested_followups=list(data.get("suggested_followups", [])),
            created_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# QQ-friendly formatting
# ---------------------------------------------------------------------------


def format_qq_summary(analysis: PersonaAnalysis) -> str:
    """Format a PersonaAnalysis as a QQ-friendly Chinese text summary."""
    stance_map = {"bull": "看好", "bear": "看空", "watch": "关注但不追高", "unknown": "暂不明确"}
    stance_cn = stance_map.get(analysis.stance, analysis.stance)
    lines = [
        "【郭老师视角】",
        f"倾向：{stance_cn}",
        f"结论：{analysis.conclusion}",
        f"置信度：{analysis.confidence:.2f}",
        "",
    ]
    if analysis.reasoning_steps:
        lines.append("核心理由：")
        for i, step in enumerate(analysis.reasoning_steps[:5], 1):
            lines.append(f"{i}. {step}")
        lines.append("")
    if analysis.invalidation_conditions:
        lines.append("失效条件：")
        for ic in analysis.invalidation_conditions[:3]:
            lines.append(f"- {ic}")
        lines.append("")
    if analysis.unsupported_claims:
        lines.append("⚠️ 缺少证据支撑的判断：")
        for uc in analysis.unsupported_claims[:3]:
            lines.append(f"- {uc}")
        lines.append("")
    source = analysis.metadata.get(SOURCE_CLASSIFICATION_KEY, {})
    transfer = source.get("methodology_transfer", {}) if isinstance(source, dict) else {}
    evidence_gap = analysis.metadata.get("evidence_gap", {})
    if isinstance(transfer, dict) and transfer.get("available"):
        lines.append("来源边界：")
        lines.append("- 方法论迁移，不是老师直接观点")
        if isinstance(evidence_gap, dict) and evidence_gap.get("message"):
            lines.append(f"- 直接证据缺口：{evidence_gap['message']}")
        lines.append("")
    if analysis.activated_trace_ids:
        lines.append(f"依据：引用历史推理 {len(analysis.activated_trace_ids)} 条")
    return "\n".join(lines)


def _validate_persona_stance(raw: object) -> str:
    s = str(raw).strip().lower()
    if s in ("bull", "bear", "watch", "unknown"):
        return s
    return "unknown"
