"""Strict public contracts for FIN advisory consultation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef, PublicProblem

AnalysisProfile = Literal["EXPLAIN", "DECIDE_NOW"]
TacticalAnalysisNeed = Literal[
    "NOT_REQUESTED",
    "EXPLAIN_METHOD",
    "CURRENT_ASSESSMENT",
]
ActionEligibility = Literal["OBSERVE_ONLY", "MANUAL_REVIEW_ELIGIBLE"]
ConsultationStatus = Literal["completed", "partial", "unknown", "unavailable"]


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ConsultationGeneralScope(_FrozenModel):
    kind: Literal["general"] = "general"
    topic: str | None = None
    horizon: str | None = None

    @field_validator("topic", "horizon")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_text(value)
        return normalized or None


class ConsultationSingleAssetScope(_FrozenModel):
    """Explicit target-only scope; it never creates or overrides a position."""

    kind: Literal["single_asset"] = "single_asset"
    target: InstrumentRef
    horizon: str | None = None


class ConsultationMultiAssetScope(_FrozenModel):
    kind: Literal["multi_asset"] = "multi_asset"
    targets: tuple[InstrumentRef, ...]
    horizon: str | None = None

    @field_validator("targets", mode="before")
    @classmethod
    def _accept_json_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _requires_distinct_targets(self) -> ConsultationMultiAssetScope:
        distinct = {target.identity_key() for target in self.targets}
        if len(distinct) != len(self.targets):
            raise ValueError("multi-asset consultation targets must be distinct")
        if len(distinct) < 2:
            raise ValueError("multi-asset consultation requires two distinct targets")
        if len(distinct) > 16:
            raise ValueError("multi-asset consultation supports at most sixteen targets")
        return self


class ConsultationPortfolioScope(_FrozenModel):
    """Portfolio scope backed by FIN-owned PAPER truth or a 正式实际持仓快照."""

    kind: Literal["portfolio"] = "portfolio"
    account_mode: Literal["PAPER", "ADVISORY_REAL"] = Field(
        description=(
            "显式选择 PAPER 正式账户事实或 ADVISORY_REAL 正式实际持仓快照；"
            "caller 不得提交 positions。自然问题通常省略 scope，由 FIN 读取正式上下文。"
        ),
    )
    focus_targets: tuple[InstrumentRef, ...] = Field(
        default=(),
        description=(
            "仅在调用方持有经过校验的显式目标时填写；它只从正式组合快照中选择范围，"
            "问题文本不会创建或覆盖实际持仓。"
        ),
    )
    horizon: str | None = None

    @field_validator("focus_targets", mode="before")
    @classmethod
    def _accept_json_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _requires_distinct_focus_targets(self) -> ConsultationPortfolioScope:
        if len({target.identity_key() for target in self.focus_targets}) != len(self.focus_targets):
            raise ValueError("portfolio focus targets must be distinct")
        if len(self.focus_targets) > 16:
            raise ValueError("portfolio consultation supports at most sixteen focus targets")
        return self


ConsultationScope = Annotated[
    ConsultationGeneralScope
    | ConsultationSingleAssetScope
    | ConsultationMultiAssetScope
    | ConsultationPortfolioScope,
    Field(discriminator="kind"),
]


class ConsultationUnavailableScope(_FrozenModel):
    kind: Literal["unavailable"] = "unavailable"


ConsultationResultScope = Annotated[
    ConsultationGeneralScope
    | ConsultationSingleAssetScope
    | ConsultationMultiAssetScope
    | ConsultationPortfolioScope
    | ConsultationUnavailableScope,
    Field(discriminator="kind"),
]


class InvestmentMemoryEventCommand(_FrozenModel):
    """One explicit user fact for the FIN-owned cross-generation journal.

    This is intentionally opt-in typed sidecar data, never a keyword-derived
    interpretation of ``question``.  It records only what the user explicitly
    declares and does not carry a transcript, account payload, or analysis body.
    """

    kind: Literal[
        "USER_DECISION",
        "USER_REPORTED_EXECUTION",
        "OUTCOME_OBSERVATION",
        "OUTCOME_JUDGMENT",
    ]
    statement: str = Field(min_length=1, max_length=1_000)
    decision: Literal["ACCEPT", "REJECT", "WAIT", "CHANGE_PLAN"] | None = None
    supersedes_event_id: str | None = None
    related_event_ids: tuple[str, ...] = Field(default=(), max_length=3)

    @field_validator("statement")
    @classmethod
    def _normalize_statement(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("investment memory statement must be non-empty")
        return normalized

    @field_validator("related_event_ids", mode="before")
    @classmethod
    def _accept_json_related_event_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("supersedes_event_id")
    @classmethod
    def _validate_supersedes_event_id(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError("supersedes_event_id must be a journal event ID")
        return value

    @field_validator("related_event_ids")
    @classmethod
    def _validate_related_event_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(r"[0-9a-f]{32}", event_id) is None for event_id in value
        ):
            raise ValueError("related_event_ids must contain distinct journal event IDs")
        return value

    @model_validator(mode="after")
    def _requires_exact_decision_shape(self) -> InvestmentMemoryEventCommand:
        if self.kind == "USER_DECISION":
            if self.decision is None:
                raise ValueError("user decision memory requires a decision")
        elif self.decision is not None:
            raise ValueError("only user decision memory may carry a decision")
        if self.kind not in {"OUTCOME_OBSERVATION", "OUTCOME_JUDGMENT"} and self.related_event_ids:
            raise ValueError("only outcome memory may reference prior journal events")
        return self


class ConsultCommand(_FrozenModel):
    action: Literal["consult"] = "consult"
    question: str = Field(min_length=1, max_length=8192)
    scope: ConsultationScope = Field(
        default_factory=ConsultationGeneralScope,
        description=(
            "自然语言咨询应省略 scope，FIN 会绑定可用的正式上下文；仅在调用方已有经过校验的"
            "显式范围时提交 single_asset、multi_asset 或 portfolio。FIN 不从问题文本改写 scope。"
        ),
    )
    requested_profile: Literal["AUTO", "EXPLAIN", "DECIDE_NOW"] = "AUTO"
    requested_tactical_need: Literal[
        "AUTO",
        "NOT_REQUESTED",
        "EXPLAIN_METHOD",
        "CURRENT_ASSESSMENT",
    ] = "AUTO"
    trigger: Literal["on_demand"] = "on_demand"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    memory_event: InvestmentMemoryEventCommand | None = Field(
        default=None,
        description=(
            "仅当用户明确作出接受/拒绝/等待/改计划，或明确报送执行/结果时传入；"
            "不得从问题关键词推断。"
        ),
    )
    memory_operation: Literal["DELETE_ALL"] | None = Field(
        default=None,
        description="用户明确要求删除跨代投资记忆时使用；不会删除原始聊天平台记录。",
    )

    @field_validator("question")
    @classmethod
    def _normalize_question(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("question must be non-empty")
        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def _require_stable_idempotency_key(cls, value: str | None) -> str | None:
        # Reject rather than normalize: distinct opaque keys must not share a dedupe identity.
        if value is not None and any(
            character.isspace() or category(character) == "Cc" for character in value
        ):
            raise ValueError("idempotency_key must not contain whitespace or control characters")
        return value

    @model_validator(mode="after")
    def _has_at_most_one_memory_sidecar(self) -> ConsultCommand:
        if self.memory_event is not None and self.memory_operation is not None:
            raise ValueError("memory event and memory operation are mutually exclusive")
        return self


class FollowUpCommand(_FrozenModel):
    action: Literal["follow_up"] = "follow_up"
    continuation_token: str = Field(min_length=1, max_length=512)
    question: str = Field(min_length=1, max_length=8192)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("question")
    @classmethod
    def _normalize_question(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("question must be non-empty")
        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def _require_stable_idempotency_key(cls, value: str | None) -> str | None:
        # Reject rather than normalize: distinct opaque keys must not share a dedupe identity.
        if value is not None and any(
            character.isspace() or category(character) == "Cc" for character in value
        ):
            raise ValueError("idempotency_key must not contain whitespace or control characters")
        return value


class DailyWorkspaceOpenCommand(_FrozenModel):
    action: Literal["daily_workspace_open"] = "daily_workspace_open"
    trading_day_id: str | None = Field(default=None, min_length=1, max_length=64)
    trigger: Literal["on_demand", "scheduled"] = "on_demand"


class DailyWorkspaceAskCommand(_FrozenModel):
    action: Literal["daily_workspace_ask"] = "daily_workspace_ask"
    question: str = Field(min_length=1, max_length=8192)
    trading_day_id: str | None = Field(default=None, min_length=1, max_length=64)
    trigger: Literal["on_demand", "scheduled"] = "on_demand"

    @field_validator("question")
    @classmethod
    def _normalize_question(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("question must be non-empty")
        return normalized


ConsultationCommand = Annotated[
    ConsultCommand | FollowUpCommand | DailyWorkspaceOpenCommand | DailyWorkspaceAskCommand,
    Field(discriminator="action"),
]


class ConsultationAnswer(_FrozenModel):
    summary: str
    disposition: Literal["OBSERVE", "MANUAL_REVIEW", "NO_ACTION"]
    no_action: bool
    manual_review_targets: tuple[str, ...] = ()
    watch_conditions: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()


class AgentContribution(_FrozenModel):
    role: Literal["GUO_COGNITION", "A_SHARE_TACTICAL"]
    status: Literal["READY", "PARTIAL", "UNKNOWN", "NOT_REQUIRED"]
    summary: str
    product: dict[str, Any] | None = None
    artifact_id: str | None = None
    reused: bool = False
    data_gaps: tuple[str, ...] = ()
    source_boundary: str


class AgentContributions(_FrozenModel):
    guo: AgentContribution
    tactical: AgentContribution | None = Field(default=None, exclude_if=lambda value: value is None)


class ConsultationSourceBoundaries(_FrozenModel):
    teacher_cognition: Literal["G_ONLY"] = "G_ONLY"
    curated_attention: Literal["DIRECTIONAL_LEAD_ONLY"] = "DIRECTIONAL_LEAD_ONLY"
    curated_external_reference: Literal["NON_G_REFERENCE"] = "NON_G_REFERENCE"
    system_evidence: Literal["NON_G_EVIDENCE"] = "NON_G_EVIDENCE"
    tactical: Literal["Z_EVIDENCE_NOT_G"] = "Z_EVIDENCE_NOT_G"
    user_context: Literal["NOT_EVIDENCE"] = "NOT_EVIDENCE"
    paper_account: Literal["PAPER_TRUTH_ONLY"] = "PAPER_TRUTH_ONLY"
    actual_account: Literal["USER_CONFIRMED_CONTEXT_NOT_BROKER_TRUTH"] = (
        "USER_CONFIRMED_CONTEXT_NOT_BROKER_TRUTH"
    )


class GContextReference(_FrozenModel):
    """One exact G generation/source pair consumed by an advisory product."""

    generation: str = Field(min_length=1, max_length=256)
    source_ref: str = Field(min_length=1, max_length=256)


class GContextConsumption(_FrozenModel):
    status: Literal["NOT_REQUIRED", "NOT_CONSUMED", "UNAVAILABLE", "CONSUMED"] = "NOT_REQUIRED"
    generation: str = ""
    source_refs: tuple[str, ...] = ()
    references: tuple[GContextReference, ...] = Field(default=(), max_length=32)
    published_at: tuple[datetime, ...] = ()
    available_at: tuple[datetime, ...] = ()
    freshness_status: Literal["NOT_CHECKED", "UNKNOWN", "READY", "PARTIAL", "STALE", "MISSING"] = (
        "NOT_CHECKED"
    )
    evaluated_at: datetime | None = None
    data_gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _references_match_source_refs(self) -> GContextConsumption:
        if not self.references:
            return self
        pairs = tuple((reference.generation, reference.source_ref) for reference in self.references)
        if len(pairs) != len(set(pairs)) or len({source_ref for _, source_ref in pairs}) != len(
            pairs
        ):
            raise ValueError("G context references must be unique")
        if self.source_refs != tuple(source_ref for _, source_ref in pairs):
            raise ValueError("G context references must match source_refs")
        return self


class ConsultationFreshness(_FrozenModel):
    reused_artifacts: tuple[str, ...] = ()
    refreshed_artifacts: tuple[str, ...] = ()
    invalidated_artifacts: tuple[str, ...] = ()
    g_context: GContextConsumption = Field(
        default_factory=GContextConsumption,
        exclude_if=lambda value: value.status == "NOT_REQUIRED",
    )


class ConsultationNoAccountContext(_FrozenModel):
    mode: Literal["NONE"] = "NONE"
    status: Literal["NOT_REQUIRED"] = "NOT_REQUIRED"
    risk_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"


class ConsultationPaperAccountContext(_FrozenModel):
    mode: Literal["PAPER"] = "PAPER"
    status: Literal["READY", "UNKNOWN"]
    generation_ref: str | None = None
    revision: int | None = None
    position_count: int | None = None
    risk_status: Literal[
        "NOT_EVALUATED",
        "UNKNOWN",
        "EMBEDDED_IN_FROZEN_PAPER_DECISION",
    ] = "NOT_EVALUATED"
    action_readiness: Literal["NOT_EVALUATED", "READY", "NOT_READY"] = "NOT_EVALUATED"


class ConsultationActualAdvisoryAccountContext(_FrozenModel):
    mode: Literal["ADVISORY_REAL"] = "ADVISORY_REAL"
    status: Literal["READY", "PARTIAL", "UNKNOWN"]
    snapshot_ref: str | None = None
    as_of: datetime | None = None
    position_count: int | None = None
    source_kind: (
        Literal[
            "USER_CONFIRMED_BROKER_SCREENSHOT",
            "USER_CONFIRMED_MANUAL",
        ]
        | None
    ) = None
    source_boundary: Literal["USER_CONFIRMED_CONTEXT_NOT_BROKER_TRUTH"] = (
        "USER_CONFIRMED_CONTEXT_NOT_BROKER_TRUTH"
    )
    risk_status: Literal["NOT_EVALUATED", "UNKNOWN"] = "NOT_EVALUATED"
    action_readiness: Literal["NOT_EVALUATED", "READY", "NOT_READY"] = "NOT_EVALUATED"
    valid_until: datetime | None = None
    # P0 下注计分切片：确认快照的净资产（R→元展示参照；None=未提供）。
    # 公开 payload 新增的 null 容忍可选键；不改 mode 判别联合形态。
    net_assets: float | None = None


ConsultationDecisionContext = Annotated[
    ConsultationNoAccountContext
    | ConsultationPaperAccountContext
    | ConsultationActualAdvisoryAccountContext,
    Field(discriminator="mode"),
]


class ConsultationSafety(_FrozenModel):
    advisory_only: Literal[True] = True
    execution_allowed: Literal[False] = False
    human_confirmation_required: Literal[True] = True


class ConsultationResultMeta(_FrozenModel):
    """结论来源与链延续元数据（设计 consultation-session-continuity v1 Phase 1）。

    generation=IDEMPOTENCY_REPLAY 表示同 turn 重试命中原结论，未重新运行
    Agent——展示时必须以原 as_of 标注结论时间，不能伪装成新分析。
    continuity=CONTINUED_CHAIN 表示本轮延续了同一 FIN semantic chain；
    continuity=DEGRADED_FRESH 表示本轮原本要求延续，但 FIN 已知实际转入
    fresh 路径——它是单结果的来源事实，不冒充 resume，也不表示上一轮
    provider session 已恢复。
    """

    generation: Literal["FRESH", "IDEMPOTENCY_REPLAY"] = "FRESH"
    continuity: Literal[
        "NEW_CHAIN",
        "CONTINUED_CHAIN",
        "DEGRADED_FRESH",
    ] = "NEW_CHAIN"
    # 两个事实不能合并：replay/open 没有在本次调用 runtime，但展示的仍是
    # 已验证 Agent 产物；preflight/无产品失败则两者都为 False。
    agent_runtime_invoked: bool = False
    agent_output_used: bool = False
    model_quality: Literal["PINNED", "DEGRADED", "UNKNOWN"] = "UNKNOWN"


class ConsultationGuideMeta(_FrozenModel):
    """切片二引导者元数据：只记有界事实，不记引导全文。"""

    guide_used: bool = False
    guidance_count: int = 0
    guide_error: str | None = None


class OpenRiskLedgerRowProjection(_FrozenModel):
    """台账行的有界公开展示投影：typed 字段 only，UNKNOWN 目标 = 空 tuple。"""

    created_at: datetime
    size_r: float
    horizon: str
    horizon_days: float | None = None
    review_point: str
    subject_tickers: tuple[str, ...] = ()

    @field_validator("subject_tickers", mode="before")
    @classmethod
    def _accept_json_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class ConsultationResult(_FrozenModel):
    schema_version: Literal["fin.consultation/v1"] = "fin.consultation/v1"
    action: Literal[
        "consult",
        "follow_up",
        "daily_workspace_open",
        "daily_workspace_ask",
        "daily_workspace_scheduled",
    ] = "consult"
    origin: Literal["on_demand", "scheduled"] = "on_demand"
    workspace_ref: str | None = Field(default=None, min_length=1, max_length=256)
    status: ConsultationStatus
    analysis_profile: AnalysisProfile
    profile_reason: str
    trigger: Literal["on_demand"] = "on_demand"
    scope: ConsultationResultScope = Field(default_factory=ConsultationGeneralScope)
    as_of: datetime
    deadline_at: datetime | None = None
    answer: ConsultationAnswer
    product: dict[str, Any] | None = None
    served_via_proxy: bool = False
    agent_contributions: AgentContributions
    decision_context: ConsultationDecisionContext
    source_boundaries: ConsultationSourceBoundaries = ConsultationSourceBoundaries()
    freshness: ConsultationFreshness = ConsultationFreshness()
    data_gaps: tuple[str, ...] = ()
    safety: ConsultationSafety = ConsultationSafety()
    continuation_token: str | None = Field(default=None, min_length=1, max_length=512)
    result_meta: ConsultationResultMeta = ConsultationResultMeta()
    # 切片二引导者元数据（null 容忍可选键；旧产品重放兼容）
    guide_meta: ConsultationGuideMeta | None = None
    # P0 已发布下注清单：与注入片段同源同值的展示块文本（公开 payload 新增
    # 的 null 容忍可选键）；None=省略，展示层容忍缺失。
    open_risk_ledger: str | None = None
    # 上下文预注入治理（B6）：typed 行投影（展示层按 final
    # manual_review_targets × subject_tickers 逐行 ANY-intersection 渲染；
    # 旧字符串载体不再是展示回退路径）。
    open_risk_ledger_rows: tuple[OpenRiskLedgerRowProjection, ...] = ()
    open_risk_ledger_scan_capped: bool = False
    # R1-5 LIVE：本轮已写入投研记忆 journal 的决定（供展示层公示；独立
    # typed 字段，不经可被 envelope 污染的 answer 层）。
    recorded_decision_notices: tuple[str, ...] = ()
    # 内部技术故障统一为 unavailable + sanitized PublicProblem(error_id);
    # 正常 epistemic unknown 没有 problem。A1: 公开结果仅给 sanitized error id。
    problem: PublicProblem | None = None
