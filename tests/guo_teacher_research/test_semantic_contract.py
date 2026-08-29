import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from fin_analyse.guo_teacher_research.principal_binding import PrincipalBinding
from fin_analyse.guo_teacher_research.semantic_contract import (
    PUBLIC_PROBLEM_REGISTRY,
    AdvisoryRealGuidanceContextOption,
    AskCommand,
    CloseCommand,
    ContinueCommand,
    ContractResolutionError,
    FeedbackCommand,
    GeneralContext,
    GuidanceSnapshot,
    InstrumentRef,
    MultiAssetContext,
    PortfolioContext,
    PortfolioPosition,
    ReadCommand,
    RequestGuidanceContextOption,
    ResearchContractResolver,
    ResearchGoal,
    ResearchPolicyCatalog,
    SingleAssetContext,
    guidance_context_option_id,
    input_snapshot_projection,
    load_input_snapshot,
    load_resolved_contract,
    public_problem,
    request_guidance_context_option,
    resolved_contract_projection,
)


def _request_option(
    context: GeneralContext | SingleAssetContext | MultiAssetContext,
) -> RequestGuidanceContextOption:
    payload = {
        "data_gaps": [],
        "owner": "REQUEST_CONTEXT",
        "context": context.model_dump(mode="json"),
    }
    return RequestGuidanceContextOption(
        option_id=guidance_context_option_id(payload),
        context=context,
    )


def _actual_option(context: PortfolioContext) -> AdvisoryRealGuidanceContextOption:
    bound = context.model_copy(
        update={
            "account_mode": "ADVISORY_REAL",
            "account_snapshot_ref": "actual-snapshot",
            "account_status": "READY",
            "source_kind": "USER_CONFIRMED_MANUAL",
        }
    )
    payload = {
        "data_gaps": [],
        "owner": "ADVISORY_REAL",
        "context": bound.model_dump(mode="json"),
        "snapshot_ref": "actual-snapshot",
        "revision": "revision-1",
        "status": "READY",
        "valid_until": None,
        "source_kind": "USER_CONFIRMED_MANUAL",
    }
    return AdvisoryRealGuidanceContextOption(
        option_id=guidance_context_option_id(payload),
        context=bound,
        snapshot_ref="actual-snapshot",
        revision="revision-1",
        status="READY",
        source_kind="USER_CONFIRMED_MANUAL",
    )


def test_minimal_ask_resolves_to_stable_inline_general_answer() -> None:
    command = AskCommand(question="  What changed in the policy outlook?  ")
    principal = PrincipalBinding(namespace="local", principal_id="principal-1")
    catalog = ResearchPolicyCatalog.m4_v1()
    resolver = ResearchContractResolver()

    first = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=catalog,
    )
    second = resolver.resolve(
        AskCommand(question="What changed in the policy outlook?"),
        principal=principal,
        prior_snapshot=None,
        policy_catalog=catalog,
    )

    assert first.contract.outcome.mode == "answer"
    assert first.contract.scope.kind == "general"
    assert first.contract.delivery.mode == "inline"
    assert first.contract.delivery.queue_allowed is False
    assert first.contract.runtime_budget.total_seconds == 180
    assert first.contract.runtime_budget.runtime_seconds == 120
    assert first.contract.runtime_budget.context_chars == 32_000
    assert first.contract.runtime_budget.max_capability_calls == 4
    assert first.contract.contract_id == second.contract.contract_id
    assert first.input_snapshot.snapshot_hash == second.input_snapshot.snapshot_hash


def test_consultation_policy_uses_the_one_hour_answer_deadline() -> None:
    catalog = ResearchPolicyCatalog.consultation_v1()

    assert catalog.policy_version == "semantic-consultation-v1"
    assert catalog.answer_budget.total_seconds == 3_600
    assert catalog.answer_budget.runtime_seconds == 3_595
    assert catalog.answer_budget.context_chars == 40_000
    assert catalog.answer_budget.max_capability_calls == 64
    assert catalog.research_budget.total_seconds == 1_800
    assert catalog.research_budget.runtime_seconds == 300
    assert catalog.research_budget.context_chars == 40_000
    assert catalog.research_budget.max_capability_calls == 64


@pytest.mark.parametrize(
    ("goal", "evidence_requirement", "required_capabilities"),
    (
        (None, "NONE", ()),
        (
            ResearchGoal(
                requested_profile="DECIDE_NOW",
                requested_tactical_need="CURRENT_ASSESSMENT",
            ),
            "NONE",
            (),
        ),
        (None, "G_REQUIRED", ("fin.read_g_context",)),
        (None, "CURRENT_BROAD_MARKET", ()),
    ),
)
def test_consultation_contract_requires_g_when_the_public_route_marks_it_required(
    goal: ResearchGoal | None,
    evidence_requirement: str,
    required_capabilities: tuple[str, ...],
) -> None:
    """The public route opts in; lower-level integrations remain explicit."""
    invocation = ResearchContractResolver().resolve(
        AskCommand(question="今天市场、老师观点和持仓要怎么一起看？"),
        principal=PrincipalBinding(namespace="local", principal_id="principal-1"),
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
        goal=goal,
        evidence_requirement=evidence_requirement,  # type: ignore[arg-type]
    )

    assert invocation.contract.tool_policy.required_capabilities == required_capabilities
    assert invocation.contract.tool_policy.max_calls >= 64
    assert invocation.contract.runtime_budget.max_capability_calls >= 64
    assert load_resolved_contract(resolved_contract_projection(invocation.contract)) == (
        invocation.contract
    )


def test_consultation_goal_is_contract_bound_without_forcing_dynamic_market_reads() -> None:
    resolver = ResearchContractResolver()
    principal = PrincipalBinding(namespace="local", principal_id="principal-1")
    command = AskCommand(question="分析下紫金矿业")
    catalog = ResearchPolicyCatalog.consultation_v1()

    automatic = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=catalog,
        context_options=(_request_option(GeneralContext()),),
    )
    current = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=catalog,
        context_options=(_request_option(GeneralContext()),),
        goal=ResearchGoal(
            requested_profile="DECIDE_NOW",
            requested_tactical_need="CURRENT_ASSESSMENT",
        ),
    )

    assert automatic.input_snapshot.snapshot_hash == current.input_snapshot.snapshot_hash
    assert automatic.contract.contract_id != current.contract.contract_id
    assert current.contract.tool_policy.required_capabilities == ()
    assert current.contract.tool_policy.max_calls == 64
    assert current.contract.product_contracts[0].ref == "consultation_product/v1"


def test_current_broad_market_consultation_keeps_reads_optional_in_contract() -> None:
    resolver = ResearchContractResolver()
    principal = PrincipalBinding(namespace="local", principal_id="principal-1")
    exact_question = "今天A股市场主线怎么看？大盘态势、板块轮动、资金面与关键信号是什么？"

    current = resolver.resolve(
        AskCommand(question=exact_question, context=GeneralContext(topic="今日市场主线")),
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext(topic="今日市场主线")),),
        evidence_requirement="CURRENT_BROAD_MARKET",
    )
    non_current = resolver.resolve(
        AskCommand(question="A股市场主线通常如何分析？"),
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
    )
    generic = resolver.resolve(
        AskCommand(question=exact_question),
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.m4_v1(),
    )
    target_bound = resolver.resolve(
        AskCommand(
            question=exact_question,
            context=SingleAssetContext(target=InstrumentRef(ticker="000001.SZ")),
        ),
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(
            _request_option(SingleAssetContext(target=InstrumentRef(ticker="000001.SZ"))),
        ),
    )

    assert current.contract.tool_policy.required_capabilities == ()
    assert current.contract.tool_policy.max_calls == 64
    assert (
        load_resolved_contract(resolved_contract_projection(current.contract)) == current.contract
    )
    assert non_current.contract.tool_policy.required_capabilities == ()
    assert generic.contract.tool_policy.required_capabilities == ()
    assert target_bound.contract.tool_policy.required_capabilities == ()
    generic_projection = resolved_contract_projection(generic.contract)
    assert "required_capabilities" not in generic_projection["tool_policy"]
    assert load_resolved_contract(generic_projection) == generic.contract

    with pytest.raises(ContractResolutionError):
        resolver.resolve(
            AskCommand(question=exact_question),
            principal=principal,
            prior_snapshot=None,
            policy_catalog=ResearchPolicyCatalog.m4_v1(),
            evidence_requirement="CURRENT_BROAD_MARKET",
        )


def test_explicit_research_and_typed_contexts_resolve_outcome_and_scope() -> None:
    resolver = ResearchContractResolver()
    principal = PrincipalBinding(namespace="local", principal_id="principal-1")
    catalog = ResearchPolicyCatalog.m4_v1()
    contexts = (
        SingleAssetContext(target=InstrumentRef(ticker="000001.sz", name=" Ping An ")),
        MultiAssetContext(
            targets=(
                InstrumentRef(ticker="000001.SZ"),
                InstrumentRef(ticker="600519.SH"),
            )
        ),
        PortfolioContext(
            positions=(PortfolioPosition(instrument=InstrumentRef(ticker="600519.sh")),)
        ),
    )

    invocations = [
        resolver.resolve(
            AskCommand(question="Compare the risks", outcome_mode="research", context=context),
            principal=principal,
            prior_snapshot=None,
            policy_catalog=catalog,
        )
        for context in contexts
    ]

    assert [invocation.contract.scope.kind for invocation in invocations] == [
        "single_asset",
        "multi_asset",
        "portfolio",
    ]
    for invocation in invocations:
        assert invocation.contract.outcome.mode == "research"
        assert invocation.contract.delivery.mode == "queued"
        assert invocation.contract.delivery.queue_allowed is True
        assert invocation.contract.runtime_budget.total_seconds == 1_800
        assert invocation.contract.runtime_budget.runtime_seconds == 300
        assert invocation.contract.runtime_budget.max_capability_calls == 12


def test_generic_portfolio_context_does_not_claim_an_account_truth_mode() -> None:
    context = PortfolioContext(
        positions=(PortfolioPosition(instrument=InstrumentRef(ticker="600519.SH")),)
    )

    assert context.account_mode == "UNSPECIFIED"


def test_only_bound_actual_account_may_represent_an_empty_portfolio() -> None:
    actual = PortfolioContext(
        account_mode="ADVISORY_REAL",
        positions=(),
        as_of=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
        account_snapshot_ref="actual-advisory-snapshot-1234567890abcdef",
        account_status="READY",
        valid_until=datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
        source_kind="USER_CONFIRMED_MANUAL",
    )

    assert actual.positions == ()
    for account_mode in ("UNSPECIFIED", "PAPER"):
        with pytest.raises(ValidationError, match="bound ADVISORY_REAL"):
            PortfolioContext(account_mode=account_mode, positions=())


def test_json_native_context_fields_preserve_strict_domain_types() -> None:
    multi_asset = MultiAssetContext.model_validate(
        {
            "targets": [
                {"ticker": "000001.SZ"},
                {"ticker": "600519.SH"},
            ]
        }
    )
    portfolio = PortfolioContext.model_validate(
        {
            "positions": [
                {
                    "instrument": {"ticker": "002409.SZ"},
                    "quantity": 100,
                }
            ],
            "focus_targets": [{"ticker": "002409.SZ"}],
            "as_of": "2026-07-22T09:54:00+08:00",
        }
    )

    assert isinstance(multi_asset.targets, tuple)
    assert [target.ticker for target in multi_asset.targets] == [
        "000001.SZ",
        "600519.SH",
    ]
    assert isinstance(portfolio.positions, tuple)
    assert isinstance(portfolio.focus_targets, tuple)
    assert portfolio.as_of == datetime.fromisoformat("2026-07-22T09:54:00+08:00")

    for invalid_as_of in ("2026-07-22", "1720000000"):
        with pytest.raises(ValidationError, match="RFC 3339"):
            PortfolioContext.model_validate(
                {
                    "positions": [{"instrument": {"ticker": "002409.SZ"}}],
                    "as_of": invalid_as_of,
                }
            )

    with pytest.raises(ValidationError, match="timezone-aware"):
        PortfolioContext(
            positions=(PortfolioPosition(instrument=InstrumentRef(ticker="002409.SZ")),),
            as_of=datetime(2026, 7, 22, 9, 54),
        )


def test_multiline_user_notes_survive_consultation_snapshot_round_trip() -> None:
    """D2 复现的公共路径缺陷回归：多行 user_notes（个人策略投影）往返幂等。

    user_notes 只能去首尾空白、保留内部换行；否则 option_id（按原始多行
    dump 哈希）与 JSON 往返重载后的归一化文本不一致，load_input_snapshot
    的身份校验失败，真实 runner 会以 semantic_invocation_invalid 拒绝所有
    携带策略的咨询。
    """
    strategy = (
        "个人投资策略（用户上下文）\n"
        "canonical native Codex Session: 019f6b35-14ce-7200-a82b-947cdc6b553c\n"
        "- 核心原则: 现金和不交易都是有效决策。\n"
        "- 待验证边界: 当前持仓属于动态账户事实。"
    )
    request_context = GeneralContext(topic="t", horizon="h").model_copy(
        update={"user_notes": strategy}
    )
    invocation = ResearchContractResolver().resolve(
        AskCommand(question="空仓是不是也可以？", outcome_mode="answer"),
        principal=PrincipalBinding(namespace="local", principal_id="principal-1"),
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(request_guidance_context_option(request_context),),
        goal=ResearchGoal(requested_profile="EXPLAIN"),
        continuation_audience="consultation.decision_support",
    )
    projected = input_snapshot_projection(invocation.input_snapshot)
    restored = load_input_snapshot(projected)
    assert restored == invocation.input_snapshot
    assert restored.context_options[0].context.user_notes == strategy


def test_timezone_aware_portfolio_context_survives_snapshot_round_trip() -> None:
    invocation = ResearchContractResolver().resolve(
        AskCommand(
            question="检查持仓结构。",
            context=PortfolioContext(
                positions=(PortfolioPosition(instrument=InstrumentRef(ticker="002409.SZ")),),
                as_of=datetime.fromisoformat("2026-07-22T09:54:00+08:00"),
            ),
        ),
        principal=PrincipalBinding(namespace="local", principal_id="principal-1"),
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.m4_v1(),
    )

    assert (
        load_input_snapshot(input_snapshot_projection(invocation.input_snapshot))
        == invocation.input_snapshot
    )


def test_public_commands_are_typed_and_reject_caller_controlled_execution_fields() -> None:
    assert ReadCommand(continuation_token="opaque-token").action == "read"
    assert ContinueCommand(continuation_token="opaque-token", question="Follow up").action == (
        "continue"
    )
    assert CloseCommand(continuation_token="opaque-token").action == "close"
    assert (
        FeedbackCommand(
            continuation_token="opaque-token",
            product_version=1,
            item_id="claim-1",
            disposition="useful",
        ).action
        == "feedback"
    )

    forbidden_fields = {
        "principal": "caller",
        "trust_marker": True,
        "level": "L6",
        "depth": "deep",
        "provider": "other",
        "budget": 9_999,
        "context_json": '{"kind":"general"}',
    }
    for field, value in forbidden_fields.items():
        with pytest.raises(ValidationError):
            AskCommand.model_validate({"question": "Explain", field: value})


def test_resolved_contract_contains_bounded_policy_refs_without_hidden_levels() -> None:
    invocation = ResearchContractResolver().resolve(
        AskCommand(question="Explain the current thesis"),
        principal=PrincipalBinding(namespace="local", principal_id="principal-1"),
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.m4_v1(),
    )

    contract = invocation.contract
    assert contract.goal.kind == "decision_support"
    assert contract.input_snapshot_ref.snapshot_hash == invocation.input_snapshot.snapshot_hash
    assert contract.context_policy.max_chars == 32_000
    assert contract.source_policy.teacher_cognition_is_authoritative is True
    assert contract.tool_policy.max_calls == 4
    assert "fin.read_teacher_cognition" in contract.tool_policy.allowed_capabilities
    assert all(
        capability.startswith("fin.") for capability in contract.tool_policy.allowed_capabilities
    )
    assert contract.deliberation_policy.mode == "allowed"
    assert contract.product_contracts[0].ref == "decision-guidance-product/v1"
    assert contract.fallback_policy.second_provider_allowed is False
    assert contract.continuation_policy.principal_scoped is True
    assert contract.effect_policy.advisory_only is True
    assert contract.effect_policy.execution_allowed is False
    assert contract.safety_policy.human_confirmation_required is True
    assert invocation.input_snapshot.context_classification == "user_context_not_evidence"

    serialized = json.dumps(asdict(contract), sort_keys=True)
    assert '"level"' not in serialized
    assert '"depth"' not in serialized
    assert all(legacy not in serialized for legacy in ("L3", "L4", "L5", "L6"))


def test_consultation_has_one_bounded_toolbelt_for_every_scope() -> None:
    resolver = ResearchContractResolver()
    principal = PrincipalBinding(namespace="local", principal_id="principal-1")

    def capabilities(
        catalog: ResearchPolicyCatalog,
        context: GeneralContext | SingleAssetContext | MultiAssetContext | PortfolioContext,
    ) -> tuple[str, ...]:
        options = (
            (_actual_option(context),)
            if isinstance(context, PortfolioContext)
            else (_request_option(context),)
        )
        invocation = resolver.resolve(
            AskCommand(question="今天市场主线怎么看？", context=context),
            principal=principal,
            prior_snapshot=None,
            policy_catalog=catalog,
            context_options=(
                options if catalog.policy_version == "semantic-consultation-v1" else None
            ),
        )
        return invocation.contract.tool_policy.allowed_capabilities

    generic_general = capabilities(ResearchPolicyCatalog.m4_v1(), GeneralContext())
    consultation_general = capabilities(ResearchPolicyCatalog.consultation_v1(), GeneralContext())

    assert "fin.read_market_overview" not in generic_general
    assert "fin.read_market_overview" in consultation_general
    assert "fin.read_market_snapshot" in consultation_general
    assert "fin.read_margin_evidence" in consultation_general
    assert "fin.read_external_evidence" in consultation_general
    assert "fin.read_actual_portfolio" in consultation_general
    assert consultation_general[0] == "fin.read_actual_portfolio"
    assert "fin.independent_deliberation" not in consultation_general
    assert "fin.read_shared_knowledge" not in consultation_general

    non_general_contexts = (
        SingleAssetContext(target=InstrumentRef(ticker="000001.SZ")),
        MultiAssetContext(
            targets=(
                InstrumentRef(ticker="000001.SZ"),
                InstrumentRef(ticker="600519.SH"),
            )
        ),
        PortfolioContext(
            positions=(PortfolioPosition(instrument=InstrumentRef(ticker="600519.SH")),)
        ),
    )
    for context in non_general_contexts:
        consultation = capabilities(ResearchPolicyCatalog.consultation_v1(), context)
        assert "fin.read_market_overview" in consultation
        assert "fin.independent_deliberation" not in consultation
        assert "fin.read_market_snapshot" in consultation
        assert "fin.read_margin_evidence" in consultation
        assert "fin.read_external_evidence" in consultation
        assert "fin.read_actual_portfolio" in consultation
        assert "fin.read_cached_external_research" not in consultation
        assert "fin.read_shared_knowledge" not in consultation
        assert "fin.inspect_portfolio_snapshot" not in consultation

    invocation = resolver.resolve(
        AskCommand(question="今天市场主线怎么看？", context=GeneralContext()),
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
    )
    assert invocation.contract.deliberation_policy.mode == "disabled"
    assert invocation.contract.deliberation_policy.max_reference_agents == 0
    assert invocation.contract.deliberation_policy.max_aggregation_agents == 0


def test_public_problem_registry_is_fixed_and_projects_only_safe_fields() -> None:
    assert set(PUBLIC_PROBLEM_REGISTRY) == {
        "authentication_required",
        "invalid_request",
        "idempotency_conflict",
        "lane_busy",
        "continuation_conflict",
        "continuation_not_accessible",
        "continuation_epoch_unsupported",
        "research_in_progress",
        "chain_closed",
        "product_version_not_found",
        "research_state_schema_unsupported",
        "runtime_unavailable",
        "runtime_timeout",
        "product_contract_invalid",
        "source_boundary_invalid",
        # 0.2 v3：subject admission typed fatal code（narrow 扩展，审计 r1 M3 预注册）。
        "consultation_subject_cap_exceeded",
        "consultation_subject_generation_mixed",
        # B: Agent 提议动作但后置 readiness/subject 失败。
        "consultation_action_readiness_unavailable",
    }

    problem = public_problem("runtime_timeout")
    assert problem.model_dump() == {
        "code": "runtime_timeout",
        "category": "runtime",
        "retryable": True,
        "display_message": "The research runtime did not finish within its budget.",
        # A1: registry 模板不携带 error_id;技术故障的 id 由故障 origin 生成。
        "error_id": None,
    }
    with pytest.raises(KeyError):
        public_problem("provider_stack_trace")


def test_canonical_identity_normalizes_input_and_is_principal_scoped() -> None:
    resolver = ResearchContractResolver()
    catalog = ResearchPolicyCatalog.m4_v1()
    first_principal = PrincipalBinding(namespace="installation-a", principal_id="principal-1")
    equivalent_commands = (
        AskCommand(
            question="  Explain   the thesis ",
            context=SingleAssetContext(
                target=InstrumentRef(ticker=" 000001.sz ", name=" Ping   An "),
                topic="  policy   exposure ",
                horizon="  12   months ",
                # user_notes 只去首尾空白、保留内部空白/换行（结构化多行
                # 用户上下文，如个人策略投影；折叠会破坏 JSON 往返幂等）。
                user_notes="  my working note  ",
            ),
            idempotency_key="request-1",
        ),
        AskCommand(
            question="Explain the thesis",
            context=SingleAssetContext(
                target=InstrumentRef(ticker="000001.SZ", name="Ping An"),
                topic="policy exposure",
                horizon="12 months",
                user_notes="my working note",
            ),
            idempotency_key="request-1",
        ),
    )

    first, second = (
        resolver.resolve(
            command,
            principal=first_principal,
            prior_snapshot=None,
            policy_catalog=catalog,
        )
        for command in equivalent_commands
    )
    foreign = resolver.resolve(
        equivalent_commands[1],
        principal=PrincipalBinding(namespace="installation-b", principal_id="principal-2"),
        prior_snapshot=None,
        policy_catalog=catalog,
    )

    assert first.input_snapshot.snapshot_hash == second.input_snapshot.snapshot_hash
    assert first.contract.contract_id == second.contract.contract_id
    assert first.idempotency_key_hash == second.idempotency_key_hash
    assert foreign.input_snapshot.snapshot_hash != first.input_snapshot.snapshot_hash
    assert foreign.contract.contract_id != first.contract.contract_id
    assert foreign.idempotency_key_hash != first.idempotency_key_hash

    payload = resolved_contract_projection(first.contract)
    contract_id = payload.pop("contract_id")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert contract_id == hashlib.sha256(canonical).hexdigest()


def test_continue_inherits_or_replaces_the_prior_typed_context() -> None:
    resolver = ResearchContractResolver()
    principal = PrincipalBinding(namespace="local", principal_id="principal-1")
    catalog = ResearchPolicyCatalog.m4_v1()
    prior = GuidanceSnapshot(
        contract_id="a" * 64,
        context=SingleAssetContext(target=InstrumentRef(ticker="000001.SZ")),
    )

    inherited = resolver.resolve(
        ContinueCommand(continuation_token="transport-only-a", question="What changed?"),
        principal=principal,
        prior_snapshot=prior,
        policy_catalog=catalog,
    )
    replaced = resolver.resolve(
        ContinueCommand(
            continuation_token="transport-only-b",
            question="What changed?",
            context=GeneralContext(topic="macro"),
        ),
        principal=principal,
        prior_snapshot=prior,
        policy_catalog=catalog,
    )

    assert inherited.contract.scope.kind == "single_asset"
    assert inherited.input_snapshot.parent_contract_id == prior.contract_id
    assert isinstance(inherited.input_snapshot.context, SingleAssetContext)
    assert inherited.input_snapshot.context.target.ticker == "000001.SZ"
    assert replaced.contract.scope.kind == "general"
    assert isinstance(replaced.input_snapshot.context, GeneralContext)
    assert replaced.input_snapshot.context.topic == "macro"

    with pytest.raises(ContractResolutionError) as error:
        resolver.resolve(
            ContinueCommand(continuation_token="opaque", question="No prior"),
            principal=principal,
            prior_snapshot=None,
            policy_catalog=catalog,
        )
    assert error.value.problem_code == "invalid_request"


# ── Phase 2：ConsultationMainlineProjection（consultation-session-continuity）──


def test_mainline_projection_validates_bounds_and_classification() -> None:
    from datetime import UTC, datetime

    from fin_analyse.guo_teacher_research.semantic_contract import (
        ConsultationMainlineProjection,
    )

    projection = ConsultationMainlineProjection(
        schema_version="fin.consultation-mainline/v1",
        focus="市场主线 AI 应用扩散",
        open_questions=("持续性如何验证",),
        last_turn_summary="科技成长占优，成交 2.54 万亿",
        as_of=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        source_product_version=1,
        source_artifact_hash="a" * 64,
    )
    assert projection.classification == "prior_consultation_context_not_evidence"

    with pytest.raises(ValueError):
        ConsultationMainlineProjection(
            schema_version="fin.consultation-mainline/v1",
            focus="",
            open_questions=(),
            last_turn_summary="",
            as_of=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            source_product_version=1,
            source_artifact_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        ConsultationMainlineProjection(
            schema_version="fin.consultation-mainline/v1",
            focus="x" * 2001,
            open_questions=(),
            last_turn_summary="",
            as_of=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            source_product_version=1,
            source_artifact_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        ConsultationMainlineProjection(
            schema_version="fin.consultation-mainline/v1",
            focus="f",
            open_questions=tuple(f"q{i}" for i in range(6)),
            last_turn_summary="",
            as_of=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            source_product_version=1,
            source_artifact_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        ConsultationMainlineProjection(
            schema_version="fin.consultation-mainline/v1",
            focus="f",
            open_questions=(),
            last_turn_summary="",
            as_of=datetime(2026, 8, 2, 12, 0),  # naive
            source_product_version=1,
            source_artifact_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        ConsultationMainlineProjection(
            schema_version="fin.consultation-mainline/v1",
            focus="f",
            open_questions=(),
            last_turn_summary="",
            as_of=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            source_product_version=0,
            source_artifact_hash="a" * 64,
        )


def test_resolver_includes_prior_context_in_snapshot_hash_and_roundtrip() -> None:
    from datetime import UTC, datetime

    from fin_analyse.guo_teacher_research.principal_binding import PrincipalBinding
    from fin_analyse.guo_teacher_research.semantic_contract import (
        AskCommand,
        ConsultationInputSnapshot,
        ConsultationMainlineProjection,
        GeneralContext,
        load_input_snapshot,
    )

    principal = PrincipalBinding(namespace="test", principal_id="p-test")
    prior = ConsultationMainlineProjection(
        schema_version="fin.consultation-mainline/v1",
        focus="市场主线 AI 应用扩散",
        open_questions=(),
        last_turn_summary="科技成长占优",
        as_of=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        source_product_version=1,
        source_artifact_hash="b" * 64,
    )
    command = AskCommand(question="它最大风险是什么", outcome_mode="answer")
    resolver = ResearchContractResolver()

    base = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
    )
    with_prior = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
        prior_consultation_context=prior,
    )

    base_snapshot = cast(ConsultationInputSnapshot, base.input_snapshot)
    prior_snapshot = cast(ConsultationInputSnapshot, with_prior.input_snapshot)
    assert base_snapshot.prior_consultation_context is None
    assert prior_snapshot.prior_consultation_context == prior
    assert base_snapshot.snapshot_hash != prior_snapshot.snapshot_hash

    # 反序列化 roundtrip：投影（含 prior）→ load_input_snapshot 恢复
    projected = input_snapshot_projection(prior_snapshot)
    restored = load_input_snapshot(projected)
    assert isinstance(restored, ConsultationInputSnapshot)
    assert restored.prior_consultation_context == prior
    assert restored.snapshot_hash == prior_snapshot.snapshot_hash


def test_resolver_persists_daily_workspace_context_in_snapshot_roundtrip() -> None:
    from fin_analyse.guo_teacher_research.semantic_contract import (
        ConsultationInputSnapshot,
        DailyWorkspaceCarryOverProjection,
        DailyWorkspaceContextProjection,
        DailyWorkspaceContextSource,
    )

    principal = PrincipalBinding(namespace="test", principal_id="p-test")
    command = AskCommand(question="10:00 后有什么变化？", outcome_mode="answer")
    workspace_context = DailyWorkspaceContextProjection(
        schema_version="fin.daily-workspace-context/v1",
        classification="prior_daily_workspace_context_not_evidence",
        relationship="same_trading_day_parent",
        source=DailyWorkspaceContextSource(
            trading_day_id="2026-08-03",
            checkpoint="premarket",
            product_version=1,
            artifact_hash="a" * 64,
        ),
        carry_over=DailyWorkspaceCarryOverProjection(
            answer_text="开盘先观察主线 A。量能仍未确认。",
        ),
    )
    resolver = ResearchContractResolver()
    base = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
    )
    with_workspace = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
        daily_workspace_context=workspace_context,
    )

    snapshot = cast(ConsultationInputSnapshot, with_workspace.input_snapshot)
    assert snapshot.daily_workspace_context == workspace_context
    assert base.input_snapshot.snapshot_hash != snapshot.snapshot_hash
    restored = load_input_snapshot(input_snapshot_projection(snapshot))
    assert isinstance(restored, ConsultationInputSnapshot)
    assert restored.daily_workspace_context == workspace_context
    assert restored.snapshot_hash == snapshot.snapshot_hash


def test_resolver_rejects_non_typed_daily_workspace_context() -> None:
    resolver = ResearchContractResolver()

    with pytest.raises(ContractResolutionError, match="typed projection"):
        resolver.resolve(
            AskCommand(question="当前如何处理？"),
            principal=PrincipalBinding(namespace="test", principal_id="p-test"),
            prior_snapshot=None,
            policy_catalog=ResearchPolicyCatalog.consultation_v1(),
            context_options=(_request_option(GeneralContext()),),
            daily_workspace_context={"raw": "context"},  # type: ignore[arg-type]
        )


def test_resolver_binds_bounded_investment_memory_only_to_fresh_runtime() -> None:
    """Cross-generation memory is typed non-evidence, never persisted as a copy."""

    from fin_analyse.guo_teacher_research.investment_memory import (
        AccountReference,
        AnalysisReference,
        InvestmentMemoryEvent,
        InvestmentMemoryRecall,
    )
    from fin_analyse.guo_teacher_research.semantic_contract import (
        ConsultationInputSnapshot,
    )

    principal = PrincipalBinding(namespace="test", principal_id="p-test")
    analysis = AnalysisReference(
        chain_id="a" * 64,
        product_version=2,
        artifact_hash="sha256:" + "a" * 64,
    )
    account = AccountReference(
        snapshot_ref="actual-advisory-snapshot-aaaaaaaaaaaaaaaa",
        revision="sha256:" + "b" * 64,
        as_of=1_786_081_200.0,
    )
    memory = InvestmentMemoryRecall(
        schema_version="fin.investment-memory-recall/v1",
        classification="investment_memory_not_evidence",
        unresolved_decisions=(
            InvestmentMemoryEvent(
                event_id="c" * 32,
                kind="USER_DECISION",
                statement="暂缓加仓，等待量价确认。",
                decision="WAIT",
                analysis_ref=analysis,
                account_ref=account,
                created_at=1_786_081_200.0,
            ),
        ),
        account_refs=(account,),
        recent_analyses=(analysis,),
    )
    command = AskCommand(question="现在应重点验证什么？", outcome_mode="answer")
    resolver = ResearchContractResolver()

    base = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
    )
    with_memory = resolver.resolve(
        command,
        principal=principal,
        prior_snapshot=None,
        policy_catalog=ResearchPolicyCatalog.consultation_v1(),
        context_options=(_request_option(GeneralContext()),),
        prior_investment_memory=memory,
    )

    base_snapshot = cast(ConsultationInputSnapshot, base.input_snapshot)
    memory_snapshot = cast(ConsultationInputSnapshot, with_memory.input_snapshot)
    assert base_snapshot.prior_investment_memory is None
    assert memory_snapshot.prior_investment_memory == memory
    assert memory_snapshot.snapshot_hash == base_snapshot.snapshot_hash

    projected = input_snapshot_projection(memory_snapshot)
    assert "prior_investment_memory" not in projected
    assert "暂缓加仓，等待量价确认。" not in json.dumps(projected, ensure_ascii=False)
    restored = load_input_snapshot(projected)
    assert isinstance(restored, ConsultationInputSnapshot)
    assert restored.prior_investment_memory is None
    assert restored.snapshot_hash == memory_snapshot.snapshot_hash
