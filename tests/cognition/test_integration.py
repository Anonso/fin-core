import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fin_analyse.cognition.conversation import ConversationRequest
from fin_analyse.cognition.integration import (
    BriefingCandidateInput,
    CognitionAnalysisService,
    CognitionSignalImpact,
)
from fin_analyse.cognition.models import PersonaAnalysis
from fin_analyse.context.models import (
    ContextRequestScope,
    ExternalContextBundle,
    ExternalContextRecord,
)
from fin_analyse.portfolio.actual_advisory import ActualAdvisoryPortfolioStore
from fin_analyse.portfolio.user_portfolio import UserPortfolio, UserPosition


@dataclass
class FakeSignal:
    signal_id: str = "sig-1"
    company: str = "贵州茅台"
    symbol: str = "600519"
    composite_score: float = 0.72
    blockers: list[str] = None

    def __post_init__(self):
        if self.blockers is None:
            self.blockers = []


class FakeCognitiveService:
    def __init__(self):
        self.calls = []

    def analyze_with_persona(
        self,
        question,
        *,
        teacher_id="guo",
        company=None,
        ticker=None,
        metadata=None,
        force_new=False,
        **kwargs,
    ):
        self.calls.append(
            {
                "question": question,
                "teacher_id": teacher_id,
                "company": company,
                "ticker": ticker,
                "metadata": metadata,
                "force_new": force_new,
                "quality_mode": kwargs.get("quality_mode"),
            }
        )
        return PersonaAnalysis(
            analysis_id=f"pa-{len(self.calls)}",
            persona_id="persona-guo",
            question=question,
            company=company,
            ticker=ticker,
            activated_trace_ids=["trace-1"],
            activated_pattern_ids=["pattern-1"],
            evidence_ids=["evidence-1"],
            reasoning_steps=["参考历史推理"],
            conclusion="关注但不追高",
            stance="watch",
            confidence=0.62,
            uncertainty=[],
            contradictions=[],
            unsupported_claims=[],
            invalidation_conditions=["跌破趋势"],
            suggested_followups=["等待回调"],
            created_at="2026-06-23T00:00:00+00:00",
            metadata=metadata or {},
        )


def _external_context():
    record = ExternalContextRecord(
        record_id="research:600519:r1",
        source="eastmoney_report",
        category="research",
        ticker="600519",
        title="贵州茅台研报",
        summary="券商维持买入评级",
        occurred_at="2026-06-23",
    )
    return ExternalContextBundle(ticker="600519", records=[record], warnings=["研报仅供参考"])


def test_analyze_conversation_forces_fresh_analysis_and_scope_metadata():
    cognitive = FakeCognitiveService()
    service = CognitionAnalysisService(cognitive)
    request = ConversationRequest(
        scope=ContextRequestScope(
            platform="qq",
            tenant_id="team-a",
            user_id="u1",
            conversation_id="g1",
            visibility="shared",
        ),
        message_id="m1",
        text="帮我看贵州茅台",
        company="贵州茅台",
        ticker="600519",
    )

    response = service.analyze_conversation(request, external_context=_external_context())

    assert response.analysis_id == "pa-1"
    assert response.confidence == 0.62
    assert "外部上下文仅供参考" in cognitive.calls[0]["question"]
    assert cognitive.calls[0]["force_new"] is True
    assert cognitive.calls[0]["metadata"]["platform"] == "qq"
    assert cognitive.calls[0]["metadata"]["message_id"] == "m1"
    assert cognitive.calls[0]["metadata"]["context_type"] == "conversation"
    assert cognitive.calls[0]["quality_mode"] == "standard"
    assert response.research_package is not None
    assert response.research_package["analysis_id"] == "pa-1"
    assert response.research_package["subject"]["company"] == "贵州茅台"
    assert response.research_package["subject"]["source_type"] == "conversation"
    assert response.research_package["advisory_only"] is True
    assert "risk_brake" in response.research_package


def test_analyze_conversation_degrades_on_failure():
    class BrokenService:
        def analyze_with_persona(self, *args, **kwargs):
            raise RuntimeError("llm down")

    service = CognitionAnalysisService(BrokenService())
    response = service.analyze_conversation(ConversationRequest(message_id="m1", text="问题"))

    assert response.analysis_id is None
    assert response.confidence is None
    assert response.needs_human_review is True
    assert "cognition unavailable" in response.warnings[0]


def test_analyze_signal_context_returns_conservative_impact():
    cognitive = FakeCognitiveService()
    service = CognitionAnalysisService(cognitive)

    impact = service.analyze_signal_context(
        FakeSignal(),
        {"entry_price": 100, "reason": "技术共振"},
        scope=ContextRequestScope(platform="system", user_id="phase3", conversation_id="run-1"),
        external_context=_external_context(),
    )

    assert isinstance(impact, CognitionSignalImpact)
    assert impact.analysis_id == "pa-1"
    assert -0.10 <= impact.confidence_delta <= 0.10
    assert -0.03 <= impact.position_delta <= 0.03
    assert "trace-1" in impact.context_sources
    assert any(source.startswith("external_context:") for source in impact.context_sources)


def test_analyze_briefing_candidates_continues_after_item_failure():
    class SometimesBroken(FakeCognitiveService):
        def analyze_with_persona(self, question, **kwargs):
            if kwargs.get("company") == "失败公司":
                raise RuntimeError("boom")
            return super().analyze_with_persona(question, **kwargs)

    service = CognitionAnalysisService(SometimesBroken())
    briefing = service.analyze_briefing_candidates(
        [
            BriefingCandidateInput(
                company="贵州茅台", ticker="600519", reason="top signal", signal_id="sig-1"
            ),
            BriefingCandidateInput(
                company="失败公司", ticker="000000", reason="top signal", signal_id="sig-2"
            ),
        ],
        scope=ContextRequestScope(
            platform="system", user_id="daily_briefing", conversation_id="2026-06-23"
        ),
    )

    assert len(briefing.items) == 2
    assert briefing.items[0].analysis_id == "pa-1"
    assert briefing.items[1].analysis_id is None
    assert briefing.items[1].needs_human_review is True
    assert briefing.items[0].source_type == "paper_signal"
    assert briefing.items[0].research_package is not None
    assert briefing.items[0].research_package["subject"]["source_type"] == "paper_signal"
    assert briefing.items[0].research_package["risk_brake"] == ["跌破趋势"]
    assert briefing.items[1].research_package is None


class TransferFakeCognitiveService(FakeCognitiveService):
    def analyze_with_persona(
        self,
        question,
        *,
        teacher_id="guo",
        company=None,
        ticker=None,
        metadata=None,
        force_new=False,
        **kwargs,
    ):
        self.calls.append(
            {
                "question": question,
                "teacher_id": teacher_id,
                "company": company,
                "ticker": ticker,
                "metadata": metadata,
                "force_new": force_new,
                "quality_mode": kwargs.get("quality_mode"),
            }
        )
        return PersonaAnalysis(
            analysis_id=f"pa-{len(self.calls)}",
            persona_id="persona-guo",
            question=question,
            company=company,
            ticker=ticker,
            activated_trace_ids=[],
            activated_pattern_ids=["pattern-transfer"],
            evidence_ids=[],
            reasoning_steps=[
                "当前认知库缺少该标的的老师直接 trace/evidence，以下只能作为方法论迁移观察。"
            ],
            conclusion="缺少直接证据，仅能按已学框架做低置信方法论迁移观察",
            stance="watch",
            confidence=0.48,
            uncertainty=["缺少该标的的老师原创历史推理支撑"],
            contradictions=[],
            unsupported_claims=["该结论是方法论迁移观察，不是老师直接观点。"],
            invalidation_conditions=["外部验证不支持该框架"],
            suggested_followups=["补充外部事实验证"],
            created_at="2026-06-26T00:00:00+00:00",
            metadata={
                "source_classification": {
                    "direct_knowledge": {"available": False, "trace_ids": [], "evidence_ids": []},
                    "methodology_transfer": {
                        "available": True,
                        "pattern_ids": ["pattern-transfer"],
                        "basis": ["政策兑现判断框架"],
                    },
                    "external_observation": {"available": False, "note": "外部上下文仅供参考"},
                },
                "evidence_gap": {
                    "direct_trace_count": 0,
                    "direct_evidence_count": 0,
                    "message": "当前标的缺少老师直接 trace/evidence，只能按方法论迁移低置信观察。",
                },
                "confidence_boundary": {
                    "level": "low",
                    "reason": "缺少直接证据，置信度不得超过中等。",
                },
            },
        )


def test_analyze_conversation_preserves_methodology_transfer_metadata():
    cognitive = TransferFakeCognitiveService()
    service = CognitionAnalysisService(cognitive)
    request = ConversationRequest(
        scope=ContextRequestScope(
            platform="qq", tenant_id="team-a", user_id="u1", conversation_id="g1"
        ),
        message_id="m-transfer",
        text="帮我看广晟有色",
        company="广晟有色",
        ticker="600259",
    )

    response = service.analyze_conversation(request)

    assert response.analysis_id == "pa-1"
    assert response.needs_human_review is True
    assert "方法论迁移" in response.text
    assert "不是老师直接观点" in response.text
    assert (
        response.analysis["metadata"]["source_classification"]["methodology_transfer"]["available"]
        is True
    )
    assert response.research_package is not None
    assert (
        response.research_package["source_classification"]["methodology_transfer"]["available"]
        is True
    )
    assert response.research_package["confidence_boundary"]["level"] == "low"
    assert response.research_package["needs_human_review"] is True


def test_analyze_user_holdings_builds_real_holding_packages():
    cognitive = FakeCognitiveService()
    service = CognitionAnalysisService(cognitive)
    portfolio = UserPortfolio(
        user_id="ypk",
        positions=[
            UserPosition(ticker="600259", company="广晟有色", shares=200, avg_cost=102.87),
            UserPosition(ticker="002015", company="协鑫能科", shares=400, avg_cost=25.22),
        ],
    )

    briefing = service.analyze_user_holdings(
        portfolio,
        teacher_id="guo",
        scope=ContextRequestScope(platform="system", user_id="ypk", conversation_id="2026-06-27"),
    )

    assert len(briefing.items) == 2
    assert briefing.items[0].company == "广晟有色"
    assert briefing.items[0].source_type == "real_holding"
    assert briefing.items[0].research_package["subject"]["source_type"] == "real_holding"
    assert briefing.items[0].research_package["subject"]["ticker"] == "600259"
    assert cognitive.calls[0]["metadata"]["user_id"] == "ypk"
    assert cognitive.calls[0]["quality_mode"] == "moa"
    assert "真实持仓" in cognitive.calls[0]["question"]


class MoAFakeCognitiveService(FakeCognitiveService):
    def analyze_with_persona(
        self,
        question,
        *,
        teacher_id="guo",
        company=None,
        ticker=None,
        metadata=None,
        force_new=False,
        **kwargs,
    ):
        analysis = super().analyze_with_persona(
            question,
            teacher_id=teacher_id,
            company=company,
            ticker=ticker,
            metadata=metadata,
            force_new=force_new,
            **kwargs,
        )
        return analysis.__class__(  # type: ignore[call-arg]
            **{
                **analysis.__dict__,
                "confidence": 0.35,
                "metadata": {
                    "quality_mode": "moa",
                    "moa_audit": {"consensus": "ok"},
                    "source_classification": {
                        "direct_knowledge": {
                            "available": False,
                            "trace_ids": [],
                            "evidence_ids": [],
                        },
                        "methodology_transfer": {
                            "available": True,
                            "pattern_ids": ["pattern-1"],
                            "basis": ["老师框架迁移"],
                        },
                        "external_observation": {"available": False, "note": "外部上下文仅供参考"},
                    },
                    "evidence_gap": {
                        "direct_trace_count": 0,
                        "direct_evidence_count": 0,
                        "message": "缺少老师直接 trace/evidence，只能按方法论迁移低置信观察。",
                    },
                    "confidence_boundary": {
                        "level": "low",
                        "reason": "缺少直接证据，置信度不得超过中等。",
                    },
                },
            }
        )


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.slow
def test_s009_real_holdings_acceptance_from_canonical_file(tmp_path: Path):
    payload = {
        "schema_version": "actual-advisory-portfolio.v1",
        "confirmation": "USER_CONFIRMED",
        "source_kind": "USER_CONFIRMED_MANUAL",
        "positions_complete": True,
        "account_alias": "示例账户",
        "as_of": "2026-08-26T09:50:00+08:00",
        "net_assets": "10000.00",
        "available_cash": "5000.00",
        "margin_debt": None,
        "positions": [
            {
                "symbol": "600000.SH",
                "name": "示例银行甲",
                "total_shares": 100,
                "sellable_shares": 100,
                "average_cost": "24.500",
                "snapshot_price": "25.000",
                "market_value": "2500.00",
            },
            {
                "symbol": "000001.SZ",
                "name": "示例银行乙",
                "total_shares": 200,
                "sellable_shares": None,
                "average_cost": "12.000",
                "snapshot_price": "12.500",
                "market_value": "2500.00",
            },
        ],
    }
    config_home = tmp_path / "config"
    target = config_home / "fin-analyse" / "actual-advisory-portfolio.v1.json"
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)

    read = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)},
        clock=lambda: datetime(2026, 8, 26, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    ).read()
    assert read.snapshot is not None, (
        f"canonical snapshot missing: {[reason.value for reason in read.reason_codes]}"
    )
    portfolio = UserPortfolio(
        user_id="ypk",
        positions=[
            UserPosition(
                ticker=position.symbol,
                company=position.name,
                shares=position.total_shares,
                avg_cost=(
                    float(position.average_cost)
                    if position.average_cost is not None
                    else None
                ),
            )
            for position in read.snapshot.positions
        ],
    )

    assert portfolio.user_id == "ypk"
    position_count = len(portfolio.positions)
    assert position_count >= 1, "ypk should have at least one position"
    tickers = {position.ticker for position in portfolio.positions}
    assert len(tickers) == position_count

    cognitive = MoAFakeCognitiveService()
    service = CognitionAnalysisService(cognitive)
    briefing = service.analyze_user_holdings(portfolio, teacher_id="guo")

    assert len(briefing.items) == position_count
    required_fields = [
        "topic_priority",
        "industry_chain_position",
        "expectation_gap",
        "realization_tempo",
        "risk_brake",
        "next_verification_actions",
        "review_hooks",
        "source_classification",
        "evidence_gap",
        "confidence_boundary",
        "reference_context_used",
        "quality_mode",
        "advisory_only",
        "needs_human_review",
    ]
    for item in briefing.items:
        package = item.research_package
        assert package is not None, f"{item.company} missing research package"
        assert package["quality_mode"] == "moa", f"{item.company} quality_mode mismatch"
        assert package["advisory_only"] is True, f"{item.company} must be advisory only"
        assert "direct_knowledge" in package["source_classification"]
        assert "methodology_transfer" in package["source_classification"]
        assert package["confidence_boundary"]["level"] in {"low", "medium", "high"}
        assert package["moa_audit"] is not None
        assert package["needs_human_review"] is True
        for field in required_fields:
            assert field in package, f"{item.company} missing field {field}"

    quality_calls = [call["quality_mode"] for call in cognitive.calls]
    assert all(mode == "moa" for mode in quality_calls)


def test_analyze_signal_context_does_not_include_plan_execution_numbers():
    cognitive = FakeCognitiveService()
    service = CognitionAnalysisService(cognitive)

    service.analyze_signal_context(
        FakeSignal(company="雅克科技", symbol="002409"),
        {
            "entry_price": 101.23,
            "stop_loss": 95.67,
            "take_profit_1": 120.89,
            "position_pct": 0.08,
        },
        scope=ContextRequestScope(platform="system", user_id="phase3"),
    )

    question = cognitive.calls[0]["question"]
    assert "雅克科技" in question
    assert "entry_price" not in question
    assert "stop_loss" not in question
    assert "take_profit" not in question
    assert "position_pct" not in question
    assert "101.23" not in question
    assert "95.67" not in question
    assert "120.89" not in question


def test_analyze_signal_context_returns_cognition_line_fields():
    cognitive = FakeCognitiveService()
    service = CognitionAnalysisService(cognitive)

    impact = service.analyze_signal_context(FakeSignal(), {}, scope=ContextRequestScope())

    assert impact.stance == "watch"
    assert impact.reasoning == ["参考历史推理"]
    assert impact.risk_boundaries == ["跌破趋势"]
    assert impact.evidence_mode == "direct_trace"
