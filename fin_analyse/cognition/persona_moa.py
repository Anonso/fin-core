"""MoA-first PersonaAnalysis adapter for quality='moa' paths."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha1
from typing import Any

from fin_analyse.cognition.models import (
    CognitivePattern,
    PersonaAnalysis,
    ReasoningTrace,
    TeacherPersona,
    TraceVerification,
)
from fin_analyse.moa.models import MoAReferenceRole, MoARequest, MoAResult
from fin_analyse.utils.ids import stable_id

PERSONA_ANALYSIS_AGGREGATOR_PROMPT = """你是一位投资老师的认知代理 MoA 聚合器。
基于以下三个能力槽（capability slots）对老师历史推理的独立审查，综合生成老师视角分析：
- core_reasoning：按原始 trace 判断直接观点覆盖 + 无 direct trace 时做低置信方法论迁移
- cross_view_risk：识别证据缺口、unsupported claims、失效条件、风险边界和反证
- boundary_schema_guard：检查外部上下文污染和置信度越界

硬约束：
- 只能引用 context 中真实存在的 trace_id；不存在的 id 必须剔除。
- 外部上下文不得写成老师观点。
- 没有 direct trace 时只能做低置信方法论迁移，confidence 必须 < 0.5。
- cross_view_risk 发现的所有 unsupported claims 必须写入 unsupported_claims 字段。
- 必须给出 source_classification、evidence_gap、confidence_boundary。
- 输出必须 advisory-only，不得出现买入/加仓/卖出等交易指令。
"""

PERSONA_ANALYSIS_EXPECTED_SCHEMA: dict[str, Any] = {
    "conclusion": "string (核心结论，中文)",
    "stance": "bull | bear | watch | unknown",
    "confidence": "float 0-1",
    "reasoning_steps": ["string"],
    "activated_trace_ids": ["string"],
    "invalidation_conditions": ["string"],
    "suggested_followups": ["string"],
    "source_classification": {
        "direct_knowledge": {
            "available": "bool",
            "trace_ids": ["string"],
            "evidence_ids": ["string"],
        },
        "methodology_transfer": {
            "available": "bool",
            "pattern_ids": ["string"],
            "basis": ["string"],
        },
        "external_observation": {"available": "bool", "note": "string"},
    },
    "evidence_gap": {
        "direct_trace_count": "int",
        "direct_evidence_count": "int",
        "message": "string",
        "severity": "low | medium | high",
    },
    "unsupported_claims": ["string"],
    "confidence_boundary": {"level": "low | medium | high", "reason": "string"},
    "moa_audit": {"roles": ["string"], "verdict": "string"},
    "warnings": ["string"],
    "needs_human_review": "bool",
}


def _role_prompt(role: str, question: str, traces_text: str, instruction: str) -> str:
    return (
        f"问题：{question}\n\n"
        f"相关历史推理：\n{traces_text}\n\n"
        f"你的角色：{role}\n"
        f"任务：{instruction}\n\n"
        "请输出简洁中文分析，重点说明：支持点、反对点、证据缺口、风险边界。"
    )


class PersonaMoAAdapter:
    """Build MoA requests and convert results for PersonaAnalysis."""

    @staticmethod
    def build_request(
        *,
        persona: TeacherPersona,
        question: str,
        traces: list[ReasoningTrace],
        patterns: list[CognitivePattern],
        company: str | None = None,
        ticker: str | None = None,
    ) -> MoARequest:
        traces_text = (
            "\n\n".join(
                f"[trace_id={t.trace_id}] topic={t.topic} conclusion={t.conclusion} "
                f"companies={t.companies} stance={t.stance}"
                for t in traces[:10]
            )
            or "（暂无历史推理）"
        )

        context = {
            "persona_id": persona.persona_id,
            "style_summary": persona.style_summary,
            "explicit_rules": persona.explicit_rules[:10],
            "question": question,
            "company": company,
            "ticker": ticker,
            "trace_ids": [t.trace_id for t in traces],
            "pattern_ids": [p.pattern_id for p in patterns],
        }

        roles = [
            MoAReferenceRole(
                name="core_reasoning",
                backend_name="t0",
                prompt=_role_prompt(
                    "core_reasoning",
                    question,
                    traces_text,
                    "按老师原始 trace 判断直接观点覆盖：对每个 trace 说明是否提供直接观点覆盖，列出具体支持的 trace_id，拒绝无据推理。当没有 direct trace 时，基于老师方法论/pattern 做低置信迁移，必须明确标注「这是迁移观察，不是老师直接观点」。",
                ),
            ),
            MoAReferenceRole(
                name="cross_view_risk",
                backend_name="t1",
                prompt=_role_prompt(
                    "cross_view_risk",
                    question,
                    traces_text,
                    "找证据缺口和 unsupported claims：逐条列出哪些结论缺乏 trace/evidence 支撑。找出失效条件、风险边界、反证和需要验证的关键假设。区分「可容忍风险」和「硬刹车条件」。",
                ),
            ),
            MoAReferenceRole(
                name="boundary_schema_guard",
                backend_name="t1",
                prompt=_role_prompt(
                    "boundary_schema_guard",
                    question,
                    traces_text,
                    "检查是否有外部上下文被错误写成老师观点，或 confidence 超过证据边界。列出越界风险和污染来源。确保输出 schema 合规。",
                ),
            ),
        ]

        return MoARequest(
            task_id=stable_id("persona", question, company or "", ticker or "", prefix="persona:"),
            task_type="persona_analysis",
            context=context,
            aggregator_prompt=PERSONA_ANALYSIS_AGGREGATOR_PROMPT,
            reference_roles=roles,
            expected_schema=PERSONA_ANALYSIS_EXPECTED_SCHEMA,
            min_reference_success=2,
            fallback_policy="rule_fallback",
            metadata={
                "adapter": "persona_moa",
                "persona_id": persona.persona_id,
                "moa_topology": "capability_slots_v1",
                "capability_slots": [
                    {
                        "slot": "core_reasoning",
                        "capability": "老师原始trace审查+方法论迁移",
                        "backend_name": "t0",
                        "output_focus": "trace_coverage_methodology",
                    },
                    {
                        "slot": "cross_view_risk",
                        "capability": "证据缺口/风险边界/反证审查",
                        "backend_name": "t1",
                        "output_focus": "evidence_gaps_risk_boundary",
                    },
                    {
                        "slot": "boundary_schema_guard",
                        "capability": "source边界/置信度越界/schema合规",
                        "backend_name": "t1",
                        "output_focus": "source_boundary_schema",
                    },
                ],
            },
        )


class MoAPersonaAnalyzer:
    """Convert MoA results into PersonaAnalysis with required S-009 metadata."""

    def to_analysis(
        self,
        *,
        result: MoAResult,
        persona: TeacherPersona,
        question: str,
        traces: list[ReasoningTrace],
        patterns: list[CognitivePattern],
        company: str | None = None,
        ticker: str | None = None,
        verifications: list[TraceVerification] | None = None,
    ) -> PersonaAnalysis | None:
        if result.status != "ok":
            return None
        final = result.final
        if not isinstance(final, dict):
            return None

        valid_trace_ids = {t.trace_id for t in traces}
        activated = [tid for tid in final.get("activated_trace_ids", []) if tid in valid_trace_ids]
        evidence_ids = list({t.source_evidence_id for t in traces if t.trace_id in activated})

        raw_confidence = min(max(float(final.get("confidence", 0.35)), 0.0), 1.0)
        verif_map = {v.trace_id: v.verdict for v in (verifications or [])}
        revise_count = sum(1 for tid in activated if verif_map.get(tid) == "revise")
        reject_count = sum(1 for tid in activated if verif_map.get(tid) == "reject")
        adjusted_confidence = raw_confidence * (0.8**revise_count) * (0.5**reject_count)
        # 老师直接 trace 仅作参考，不单独提升置信度。
        if activated:
            adjusted_confidence = min(adjusted_confidence, 0.4)

        analysis_seed = f"{persona.persona_id}:{question}:{company or ''}:{ticker or ''}:moa"
        analysis_id = "pa-" + sha1(analysis_seed.encode("utf-8")).hexdigest()[:12]

        source_classification = final.get("source_classification") or {
            "direct_knowledge": {
                "available": bool(activated),
                "trace_ids": activated,
                "evidence_ids": evidence_ids,
                "note": "老师原创 trace 仅作为参考材料，不单独构成高置信证据。",
            },
            "methodology_transfer": {"available": False, "pattern_ids": [], "basis": []},
            "external_observation": {"available": False, "note": "外部上下文仅供参考"},
        }
        evidence_gap = final.get("evidence_gap") or {
            "direct_trace_count": len(activated),
            "direct_evidence_count": len(evidence_ids),
            "message": (
                "存在老师直接 trace/evidence，但仍需外部事实交叉验证。"
                if activated
                else "缺少老师直接 trace/evidence。"
            ),
        }
        # 无论是否存在 direct trace，老师原创材料只作参考，置信度边界保持 low。
        confidence_boundary = {
            "level": "low",
            "reason": "老师原创 trace 与方法论迁移均仅作参考，未经验证不得形成高置信结论。",
        }
        moa_audit = final.get("moa_audit") or {
            "roles": [output.role for output in result.reference_outputs if output.ok],
            "verdict": "accepted" if result.status == "ok" else "fallback",
        }

        metadata: dict[str, Any] = {
            "source_classification": source_classification,
            "evidence_gap": evidence_gap,
            "confidence_boundary": confidence_boundary,
            "moa_audit": moa_audit,
            "quality_mode": "moa",
            "needs_human_review": True,
        }

        stance = str(final.get("stance", "unknown")).strip().lower()
        if stance not in ("bull", "bear", "watch", "unknown"):
            stance = "unknown"

        return PersonaAnalysis(
            analysis_id=analysis_id,
            persona_id=persona.persona_id,
            question=question,
            company=company,
            ticker=ticker,
            activated_trace_ids=activated,
            activated_pattern_ids=[p.pattern_id for p in patterns[:3]],
            evidence_ids=evidence_ids,
            reasoning_steps=list(final.get("reasoning_steps", [])),
            conclusion=str(final.get("conclusion", "认知库不足，暂不形成老师视角结论")),
            stance=stance,
            confidence=adjusted_confidence,
            uncertainty=[],
            contradictions=[],
            unsupported_claims=(
                list(final.get("unsupported_claims", []))
                if activated
                else ["该结论是方法论迁移观察，不是老师直接观点。"]
            ),
            invalidation_conditions=list(final.get("invalidation_conditions", [])),
            suggested_followups=list(final.get("suggested_followups", [])),
            created_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )
