"""Scenario adapters for using cognition in real product flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fin_analyse.cognition.conversation import ConversationRequest, ConversationResponse
from fin_analyse.cognition.models import PersonaAnalysis
from fin_analyse.cognition.persona import format_qq_summary
from fin_analyse.cognition.research_package import ResearchPackageBuilder, ResearchPackageSubject
from fin_analyse.context.models import ContextRequestScope, ExternalContextBundle
from fin_analyse.portfolio.user_portfolio import UserPortfolio
from fin_analyse.utils.ids import stable_id


@dataclass(frozen=True)
class CognitionSignalImpact:
    analysis_id: str | None
    confidence: float | None
    confidence_delta: float
    position_delta: float
    warnings: list[str] = field(default_factory=list)
    supporting_reasons: list[str] = field(default_factory=list)
    opposing_reasons: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    context_sources: list[str] = field(default_factory=list)
    # G-line fields
    stance: str | None = None
    evidence_mode: str = "unknown"
    reasoning: list[str] = field(default_factory=list)
    risk_boundaries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BriefingCandidateInput:
    company: str
    ticker: str | None = None
    reason: str = ""
    signal_id: str | None = None
    plan_id: str | None = None


@dataclass(frozen=True)
class DailyCognitionItem:
    company: str
    ticker: str | None
    analysis_id: str | None
    conclusion: str
    confidence: float | None
    risks: list[str] = field(default_factory=list)
    action_hint: str = ""
    warnings: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    source_type: str = "paper_signal"
    research_package: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "ticker": self.ticker,
            "analysis_id": self.analysis_id,
            "conclusion": self.conclusion,
            "confidence": self.confidence,
            "risks": self.risks,
            "action_hint": self.action_hint,
            "warnings": self.warnings,
            "needs_human_review": self.needs_human_review,
            "source_type": self.source_type,
            "research_package": self.research_package,
        }


@dataclass(frozen=True)
class DailyCognitionBriefing:
    date: str
    teacher_id: str
    generated_at: str
    items: list[DailyCognitionItem]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "teacher_id": self.teacher_id,
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
            "warnings": self.warnings,
        }


class CognitionAnalysisService:
    """Shared adapter for conversation, Phase3, and daily cognition usage."""

    def __init__(self, cognitive_service) -> None:
        self._cognitive = cognitive_service
        self._package_builder = ResearchPackageBuilder()

    def analyze_conversation(
        self,
        request: ConversationRequest,
        *,
        external_context: ExternalContextBundle | None = None,
    ) -> ConversationResponse:
        request_id = stable_id(
            request.scope.platform,
            request.scope.tenant_id,
            request.scope.user_id,
            request.scope.conversation_id,
            request.message_id,
            datetime.now(UTC).isoformat(),
            prefix="car-",
        )
        metadata = request.to_metadata(context_type="conversation", request_id=request_id)
        question = self._with_external_context_note(request.text, external_context)
        try:
            analysis = self._cognitive.analyze_with_persona(
                question,
                teacher_id=request.teacher_id,
                company=request.company,
                ticker=request.ticker,
                metadata=metadata,
                force_new=True,
                quality_mode="standard",
            )
            warnings = list(analysis.uncertainty) + list(analysis.contradictions)
            warnings.extend(self._external_context_warnings(external_context))
            package = self._package_builder.build_from_persona_analysis(
                analysis,
                subject=ResearchPackageSubject(
                    company=request.company,
                    ticker=request.ticker,
                    source_type="conversation",
                ),
                external_context_bundle=external_context,
            ).to_dict()
            return ConversationResponse(
                text=format_qq_summary(analysis),
                analysis_id=analysis.analysis_id,
                confidence=analysis.confidence,
                warnings=warnings,
                needs_human_review=analysis.confidence < 0.5 or bool(analysis.unsupported_claims),
                analysis=analysis.to_dict(),
                research_package=package,
            )
        except Exception:
            return ConversationResponse(
                text="认知分析暂不可用，请稍后重试。",
                analysis_id=None,
                confidence=None,
                warnings=["cognition unavailable (分析服务暂时不可用)"],
                needs_human_review=True,
            )

    def analyze_signal_context(
        self,
        signal,
        plan_context: dict[str, Any],
        *,
        teacher_id: str = "guo",
        scope: ContextRequestScope | None = None,
        external_context: ExternalContextBundle | None = None,
    ) -> CognitionSignalImpact:
        scope = scope or ContextRequestScope(platform="system", user_id="phase3")
        company = getattr(signal, "company", None) or plan_context.get("company")
        ticker = getattr(signal, "symbol", None) or plan_context.get("ticker")
        request_id = stable_id(
            str(getattr(signal, "signal_id", "")), datetime.now(UTC).isoformat(), prefix="csi-"
        )
        metadata = {
            "context_type": "phase3",
            "platform": scope.platform,
            "tenant_id": scope.tenant_id,
            "user_id": scope.user_id,
            "conversation_id": scope.conversation_id,
            "visibility": scope.visibility,
            "message_id": str(getattr(signal, "signal_id", "")),
            "request_id": request_id,
            "company": company,
            "ticker": ticker,
            "teacher_id": teacher_id,
        }
        subject = company or ticker or "该公司"
        question = self._with_external_context_note(
            f"请从老师认知视角分析 {subject}：方向判断、证据来源、风险边界、置信度、是否需要人工复核。"
            "不要基于入场价、止损、止盈或仓位做判断。",
            external_context,
        )
        try:
            analysis = self._cognitive.analyze_with_persona(
                question,
                teacher_id=teacher_id,
                company=company,
                ticker=ticker,
                metadata=metadata,
                force_new=True,
                quality_mode="standard",
            )
            return self._signal_impact_from_analysis(analysis, external_context)
        except Exception:
            return CognitionSignalImpact(
                analysis_id=None,
                confidence=None,
                confidence_delta=0.0,
                position_delta=0.0,
                warnings=["cognition unavailable (分析服务暂时不可用)"],
                needs_human_review=True,
            )

    def analyze_briefing_candidates(
        self,
        candidates: list[BriefingCandidateInput],
        *,
        teacher_id: str = "guo",
        scope: ContextRequestScope | None = None,
        external_context_by_ticker: dict[str, ExternalContextBundle] | None = None,
    ) -> DailyCognitionBriefing:
        scope = scope or ContextRequestScope(platform="system", user_id="daily_briefing")
        date = scope.conversation_id or datetime.now(UTC).strftime("%Y-%m-%d")
        items: list[DailyCognitionItem] = []
        warnings: list[str] = []
        for candidate in candidates:
            bundle = None
            if candidate.ticker and external_context_by_ticker:
                bundle = external_context_by_ticker.get(candidate.ticker)
            request_id = stable_id(date, candidate.company, candidate.ticker or "", prefix="dci-")
            metadata = {
                "context_type": "daily_briefing",
                "platform": scope.platform,
                "tenant_id": scope.tenant_id,
                "user_id": scope.user_id,
                "conversation_id": scope.conversation_id,
                "visibility": scope.visibility,
                "message_id": candidate.signal_id or candidate.plan_id or candidate.company,
                "request_id": request_id,
                "company": candidate.company,
                "ticker": candidate.ticker,
                "teacher_id": teacher_id,
            }
            question = self._with_external_context_note(
                f"每日认知简报候选：{candidate.company}，入选原因：{candidate.reason}。请总结结论、风险和行动提示。",
                bundle,
            )
            try:
                analysis = self._cognitive.analyze_with_persona(
                    question,
                    teacher_id=teacher_id,
                    company=candidate.company,
                    ticker=candidate.ticker,
                    metadata=metadata,
                    force_new=True,
                    quality_mode="standard",
                )
                items.append(self._daily_item_from_analysis(candidate, analysis, bundle))
            except Exception:
                warning = f"{candidate.company} cognition unavailable (分析服务暂时不可用)"
                warnings.append(warning)
                items.append(
                    DailyCognitionItem(
                        company=candidate.company,
                        ticker=candidate.ticker,
                        analysis_id=None,
                        conclusion="认知分析暂不可用",
                        confidence=None,
                        warnings=[warning],
                        needs_human_review=True,
                        source_type="paper_signal"
                        if candidate.signal_id or candidate.plan_id
                        else "daily_briefing",
                        research_package=None,
                    )
                )
        return DailyCognitionBriefing(
            date=date,
            teacher_id=teacher_id,
            generated_at=datetime.now(UTC).isoformat(),
            items=items,
            warnings=warnings,
        )

    def analyze_user_holdings(
        self,
        portfolio: UserPortfolio,
        *,
        teacher_id: str = "guo",
        scope: ContextRequestScope | None = None,
        external_context_by_ticker: dict[str, ExternalContextBundle] | None = None,
    ) -> DailyCognitionBriefing:
        scope = scope or ContextRequestScope(platform="system", user_id=portfolio.user_id)
        date = scope.conversation_id or datetime.now(UTC).strftime("%Y-%m-%d")
        items: list[DailyCognitionItem] = []
        warnings: list[str] = []
        for position in portfolio.positions:
            bundle = None
            if external_context_by_ticker:
                bundle = external_context_by_ticker.get(position.ticker)
            request_id = stable_id(date, portfolio.user_id, position.ticker, prefix="dci-")
            metadata = {
                "context_type": "real_holding",
                "platform": scope.platform,
                "tenant_id": scope.tenant_id,
                "user_id": scope.user_id or portfolio.user_id,
                "conversation_id": scope.conversation_id,
                "visibility": scope.visibility,
                "message_id": position.ticker,
                "request_id": request_id,
                "company": position.company,
                "ticker": position.ticker,
                "teacher_id": teacher_id,
            }
            question = self._with_external_context_note(
                f"当前真实持仓：{position.company}（{position.ticker}），持仓数量={position.shares}，成本={position.avg_cost}。请给出老师视角的研究判断包、风险刹车和下一步验证动作。",
                bundle,
            )
            try:
                analysis = self._cognitive.analyze_with_persona(
                    question,
                    teacher_id=teacher_id,
                    company=position.company,
                    ticker=position.ticker,
                    metadata=metadata,
                    force_new=True,
                    quality_mode="moa",
                )
                warnings_for_item = list(analysis.uncertainty) + self._external_context_warnings(
                    bundle
                )
                package = self._package_builder.build_from_persona_analysis(
                    analysis,
                    subject=ResearchPackageSubject(
                        company=position.company,
                        ticker=position.ticker,
                        source_type="real_holding",
                    ),
                    external_context_bundle=bundle,
                ).to_dict()
                items.append(
                    DailyCognitionItem(
                        company=position.company,
                        ticker=position.ticker,
                        analysis_id=analysis.analysis_id,
                        conclusion=analysis.conclusion,
                        confidence=analysis.confidence,
                        risks=list(analysis.invalidation_conditions[:3]),
                        action_hint="；".join(analysis.suggested_followups[:2]),
                        warnings=warnings_for_item,
                        needs_human_review=analysis.confidence < 0.5
                        or bool(analysis.unsupported_claims),
                        source_type="real_holding",
                        research_package=package,
                    )
                )
            except Exception:
                warning = f"{position.company} cognition unavailable (分析服务暂时不可用)"
                warnings.append(warning)
                items.append(
                    DailyCognitionItem(
                        company=position.company,
                        ticker=position.ticker,
                        analysis_id=None,
                        conclusion="认知分析暂不可用",
                        confidence=None,
                        warnings=[warning],
                        needs_human_review=True,
                        source_type="real_holding",
                        research_package=None,
                    )
                )
        return DailyCognitionBriefing(
            date=date,
            teacher_id=teacher_id,
            generated_at=datetime.now(UTC).isoformat(),
            items=items,
            warnings=warnings,
        )

    def _with_external_context_note(
        self, question: str, bundle: ExternalContextBundle | None
    ) -> str:
        if bundle is None or not bundle.records:
            return question
        lines = [question, "", "外部上下文仅供参考，不代表老师认知，也不构成交易决定："]
        for record in bundle.records[:5]:
            lines.append(f"- [{record.category}] {record.title}: {record.summary}")
        return "\n".join(lines)

    def _external_context_warnings(self, bundle: ExternalContextBundle | None) -> list[str]:
        if bundle is None:
            return []
        return ["外部上下文仅供参考"] + list(bundle.warnings)

    def _signal_impact_from_analysis(
        self,
        analysis: PersonaAnalysis,
        bundle: ExternalContextBundle | None,
    ) -> CognitionSignalImpact:
        if analysis.confidence < 0.45:
            confidence_delta = -0.08
            position_delta = -0.02
        elif (
            analysis.confidence >= 0.70
            and analysis.stance in {"bull", "watch"}
            and not analysis.unsupported_claims
        ):
            confidence_delta = 0.05
            position_delta = 0.0
        else:
            confidence_delta = 0.0
            position_delta = 0.0
        context_sources = list(analysis.activated_trace_ids)
        if bundle is not None:
            context_sources.extend(
                f"external_context:{record.category}" for record in bundle.records[:5]
            )
        # Resolve evidence_mode
        if analysis.activated_trace_ids or analysis.evidence_ids:
            evidence_mode = "direct_trace"
        elif analysis.activated_pattern_ids:
            evidence_mode = "methodology_transfer"
        else:
            evidence_mode = "insufficient"
        return CognitionSignalImpact(
            analysis_id=analysis.analysis_id,
            confidence=analysis.confidence,
            confidence_delta=max(-0.10, min(0.10, confidence_delta)),
            position_delta=max(-0.03, min(0.03, position_delta)),
            warnings=list(analysis.uncertainty)
            + list(analysis.contradictions)
            + self._external_context_warnings(bundle),
            supporting_reasons=list(analysis.reasoning_steps[:3]),
            opposing_reasons=list(analysis.unsupported_claims[:3]),
            needs_human_review=analysis.confidence < 0.5 or bool(analysis.unsupported_claims),
            context_sources=context_sources,
            stance=analysis.stance,
            evidence_mode=evidence_mode,
            reasoning=list(analysis.reasoning_steps[:3]),
            risk_boundaries=list(analysis.invalidation_conditions[:3]),
        )

    def _daily_item_from_analysis(
        self,
        candidate: BriefingCandidateInput,
        analysis: PersonaAnalysis,
        bundle: ExternalContextBundle | None,
    ) -> DailyCognitionItem:
        warnings = list(analysis.uncertainty) + self._external_context_warnings(bundle)
        source_type = (
            "paper_signal" if candidate.signal_id or candidate.plan_id else "daily_briefing"
        )
        package = self._package_builder.build_from_persona_analysis(
            analysis,
            subject=ResearchPackageSubject(
                company=candidate.company,
                ticker=candidate.ticker,
                source_type=source_type,
            ),
            external_context_bundle=bundle,
        ).to_dict()
        return DailyCognitionItem(
            company=candidate.company,
            ticker=candidate.ticker,
            analysis_id=analysis.analysis_id,
            conclusion=analysis.conclusion,
            confidence=analysis.confidence,
            risks=list(analysis.invalidation_conditions[:3]),
            action_hint="；".join(analysis.suggested_followups[:2]),
            warnings=warnings,
            needs_human_review=analysis.confidence < 0.5 or bool(analysis.unsupported_claims),
            source_type=source_type,
            research_package=package,
        )
