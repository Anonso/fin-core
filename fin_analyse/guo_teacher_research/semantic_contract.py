"""Typed semantic command and deterministic research contract resolution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from fin_analyse.guo_teacher_research.investment_memory import (
    InvestmentMemoryRecall,
)
from fin_analyse.guo_teacher_research.principal_binding import PrincipalBinding

_ERROR_ID_PATTERN = re.compile(r"err_[0-9a-f]{32}")


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


_DATETIME_ADAPTER = TypeAdapter(datetime)
_RFC3339_DATETIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


def _accept_json_array(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _accept_json_datetime(value: object) -> object:
    if isinstance(value, str):
        if _RFC3339_DATETIME_PATTERN.fullmatch(value) is None:
            raise ValueError("datetime must use RFC 3339 date-time syntax")
        value = _DATETIME_ADAPTER.validate_json(json.dumps(value), strict=True)
    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must be timezone-aware")
    return value


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class _GuidanceContextBase(_FrozenModel):
    topic: str | None = None
    horizon: str | None = None
    user_notes: str | None = None

    @field_validator("topic", "horizon")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_text(value)
        return normalized or None

    @field_validator("user_notes")
    @classmethod
    def _normalize_user_notes_preserving_newlines(cls, value: str | None) -> str | None:
        """user_notes 只去首尾空白，保留内部换行。

        个人策略投影是多行 bounded user context：若像 topic/horizon 一样把
        全部空白折叠成单行，option_id（按原始多行 dump 哈希）与 JSON 往返
        重载后的归一化文本不再一致，``load_input_snapshot`` 的
        ``_option_id_matches_payload`` 校验失败，真实 runner 会以
        ``semantic_invocation_invalid`` 拒绝所有携带策略的咨询（D2 复现的
        公共路径缺陷）。保留换行使往返幂等，且不损失 Agent 可读性。
        """
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class GeneralContext(_GuidanceContextBase):
    kind: Literal["general"] = "general"


class InstrumentRef(_FrozenModel):
    ticker: str | None = None
    name: str | None = None

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_text(value).upper()
        return normalized or None

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_text(value)
        return normalized or None

    @model_validator(mode="after")
    def _has_identity(self) -> InstrumentRef:
        if self.ticker is None and self.name is None:
            raise ValueError("instrument requires ticker or name")
        return self

    def identity_key(self) -> tuple[str, str]:
        if self.ticker is not None:
            return ("ticker", self.ticker)
        return ("name", (self.name or "").casefold())


class SingleAssetContext(_GuidanceContextBase):
    kind: Literal["single_asset"] = "single_asset"
    target: InstrumentRef


class MultiAssetContext(_GuidanceContextBase):
    kind: Literal["multi_asset"] = "multi_asset"
    targets: tuple[InstrumentRef, ...]

    @field_validator("targets", mode="before")
    @classmethod
    def _accept_json_targets(cls, value: object) -> object:
        return _accept_json_array(value)

    @field_validator("targets")
    @classmethod
    def _deduplicate_targets(cls, value: tuple[InstrumentRef, ...]) -> tuple[InstrumentRef, ...]:
        distinct: dict[tuple[str, str], InstrumentRef] = {}
        for target in value:
            distinct.setdefault(target.identity_key(), target)
        return tuple(distinct.values())

    @model_validator(mode="after")
    def _has_multiple_targets(self) -> MultiAssetContext:
        if len(self.targets) < 2:
            raise ValueError("multi-asset context requires at least two distinct targets")
        return self


class PortfolioPosition(_FrozenModel):
    instrument: InstrumentRef
    quantity: float | None = None
    sellable_quantity: float | None = None
    weight: float | None = None
    average_cost: float | None = None
    reference_price: float | None = None
    market_value: float | None = None
    user_notes: str | None = None

    @field_validator("user_notes")
    @classmethod
    def _normalize_user_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_text(value)
        return normalized or None


class PortfolioContext(_GuidanceContextBase):
    kind: Literal["portfolio"] = "portfolio"
    account_mode: Literal["UNSPECIFIED", "PAPER", "ADVISORY_REAL"] = "UNSPECIFIED"
    positions: tuple[PortfolioPosition, ...]
    focus_targets: tuple[InstrumentRef, ...] = ()
    as_of: datetime | None = None
    net_assets: float | None = None
    available_cash: float | None = None
    margin_debt: float | None = None
    account_snapshot_ref: str | None = None
    account_status: Literal["READY", "PARTIAL", "UNKNOWN"] | None = None
    valid_until: datetime | None = None
    source_kind: (
        Literal[
            "USER_CONFIRMED_BROKER_SCREENSHOT",
            "USER_CONFIRMED_MANUAL",
        ]
        | None
    ) = None
    data_gaps: tuple[str, ...] = ()

    @field_validator("positions", "focus_targets", mode="before")
    @classmethod
    def _accept_json_collections(cls, value: object) -> object:
        return _accept_json_array(value)

    @field_validator("as_of", "valid_until", mode="before")
    @classmethod
    def _accept_json_as_of(cls, value: object) -> object:
        return _accept_json_datetime(value)

    @field_validator("data_gaps", mode="before")
    @classmethod
    def _accept_json_data_gaps(cls, value: object) -> object:
        return _accept_json_array(value)

    @model_validator(mode="after")
    def _empty_positions_require_bound_actual_account(self) -> PortfolioContext:
        if self.positions:
            return self
        if (
            self.account_mode != "ADVISORY_REAL"
            or self.account_snapshot_ref is None
            or self.account_status is None
            or self.as_of is None
            or self.source_kind is None
            or self.focus_targets
        ):
            raise ValueError("empty positions require a bound ADVISORY_REAL account")
        return self


GuidanceContext = Annotated[
    GeneralContext | SingleAssetContext | MultiAssetContext | PortfolioContext,
    Field(discriminator="kind"),
]


def guidance_context_option_id(payload: Mapping[str, object]) -> str:
    """Return the canonical identity of one FIN-owned consultation option."""

    return _canonical_hash(payload)


USER_CONTEXT_CONTRIBUTION_MAX_CHARS = 6_000
_USER_CONTEXT_USAGE_BOUNDARIES = {
    "PERSONAL_STRATEGY": "context_only_not_investment_evidence",
    "USER_WATCHLIST": "watchlist_scope_only_not_holdings_not_evidence",
    "PUBLISHED_BET_LEDGER": "published_bet_reference_only_not_current_fact",
}


class UserContextContribution(_FrozenModel):
    """One bounded, source-owned user-context contribution mounted on an option.

    The closed field set keeps sources from declaring their own trust or
    boundary: ``trust`` is fixed ``non_g``, ``usage_boundary`` is fixed by
    ``source_kind``, and ``policy_strength`` is fixed per kind (personal
    strategy may be ``HARD_BOUNDARY``/``ADAPTIVE_DEFAULT``, everything else
    is ``CONTEXT_ONLY``).
    """

    source_kind: Literal["PERSONAL_STRATEGY", "USER_WATCHLIST", "PUBLISHED_BET_LEDGER"]
    source_status: Literal["READY"] = "READY"
    selection_status: Literal[
        "RELEVANCE_SELECTED",
        "EXPLICIT_SCOPE_SELECTED",
        "CONSERVATIVE_FALLBACK",
    ]
    policy_strength: Literal["HARD_BOUNDARY", "ADAPTIVE_DEFAULT", "CONTEXT_ONLY"]
    trust: Literal["non_g"] = "non_g"
    usage_boundary: str
    content: str

    @field_validator("content")
    @classmethod
    def _content_is_bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user context contribution content must be non-empty")
        if len(value) > USER_CONTEXT_CONTRIBUTION_MAX_CHARS:
            raise ValueError("user context contribution content is over budget")
        return value

    @model_validator(mode="after")
    def _source_owned_fields_are_fixed(self):
        if self.usage_boundary != _USER_CONTEXT_USAGE_BOUNDARIES[self.source_kind]:
            raise ValueError("usage boundary must match the source kind")
        if self.source_kind == "PERSONAL_STRATEGY":
            if self.policy_strength not in {"HARD_BOUNDARY", "ADAPTIVE_DEFAULT"}:
                raise ValueError("personal strategy strength is not hard/adaptive")
        elif self.policy_strength != "CONTEXT_ONLY":
            raise ValueError("non-personal contribution must be context only")
        return self

    @classmethod
    def personal_strategy(
        cls,
        *,
        content: str,
        selection_status: Literal[
            "RELEVANCE_SELECTED",
            "EXPLICIT_SCOPE_SELECTED",
            "CONSERVATIVE_FALLBACK",
        ],
        policy_strength: Literal["HARD_BOUNDARY", "ADAPTIVE_DEFAULT"],
    ) -> UserContextContribution:
        return cls(
            source_kind="PERSONAL_STRATEGY",
            selection_status=selection_status,
            policy_strength=policy_strength,
            usage_boundary=_USER_CONTEXT_USAGE_BOUNDARIES["PERSONAL_STRATEGY"],
            content=content,
        )

    @classmethod
    def user_watchlist(
        cls,
        *,
        content: str,
        selection_status: Literal[
            "RELEVANCE_SELECTED",
            "EXPLICIT_SCOPE_SELECTED",
            "CONSERVATIVE_FALLBACK",
        ],
    ) -> UserContextContribution:
        return cls(
            source_kind="USER_WATCHLIST",
            selection_status=selection_status,
            policy_strength="CONTEXT_ONLY",
            usage_boundary=_USER_CONTEXT_USAGE_BOUNDARIES["USER_WATCHLIST"],
            content=content,
        )

    @classmethod
    def published_bet_ledger(
        cls,
        *,
        content: str,
        selection_status: Literal[
            "RELEVANCE_SELECTED",
            "EXPLICIT_SCOPE_SELECTED",
            "CONSERVATIVE_FALLBACK",
        ],
    ) -> UserContextContribution:
        return cls(
            source_kind="PUBLISHED_BET_LEDGER",
            selection_status=selection_status,
            policy_strength="CONTEXT_ONLY",
            usage_boundary=_USER_CONTEXT_USAGE_BOUNDARIES["PUBLISHED_BET_LEDGER"],
            content=content,
        )


class _GuidanceContextOptionBase(_FrozenModel):
    option_id: str = Field(pattern="^[0-9a-f]{64}$")
    data_gaps: tuple[str, ...] = ()
    # 上下文预注入治理：typed user contributions 随 option 身份冻结。
    # 空 contribution 序列化省略，旧 payload 身份逐字不漂移（B3）。
    user_context_contributions: tuple[UserContextContribution, ...] = ()

    @field_validator("data_gaps", mode="before")
    @classmethod
    def _accept_json_data_gaps(cls, value: object) -> object:
        return _accept_json_array(value)

    @field_validator("user_context_contributions", mode="before")
    @classmethod
    def _accept_json_contributions(cls, value: object) -> object:
        return _accept_json_array(value)

    @model_serializer(mode="wrap")
    def _serialize_omitting_empty_contributions(self, handler):
        result = handler(self)
        if isinstance(result, dict) and not result.get("user_context_contributions"):
            result.pop("user_context_contributions", None)
        return result

    @model_validator(mode="after")
    def _option_id_matches_payload(self):
        projection = self.model_dump(mode="json", exclude={"option_id"})
        if self.option_id != guidance_context_option_id(projection):
            raise ValueError("guidance context option identity mismatch")
        return self


class RequestGuidanceContextOption(_GuidanceContextOptionBase):
    """The typed context explicitly supplied by the public FIN request."""

    owner: Literal["REQUEST_CONTEXT"] = "REQUEST_CONTEXT"
    context: GeneralContext | SingleAssetContext | MultiAssetContext


class AdvisoryRealGuidanceContextOption(_GuidanceContextOptionBase):
    """One immutable user-confirmed actual-portfolio owner read."""

    owner: Literal["ADVISORY_REAL"] = "ADVISORY_REAL"
    context: PortfolioContext
    snapshot_ref: str
    revision: str
    status: Literal["READY", "PARTIAL", "UNKNOWN"]
    valid_until: datetime | None = None
    source_kind: Literal[
        "USER_CONFIRMED_BROKER_SCREENSHOT",
        "USER_CONFIRMED_MANUAL",
    ]

    @field_validator("valid_until", mode="before")
    @classmethod
    def _accept_json_valid_until(cls, value: object) -> object:
        return _accept_json_datetime(value)

    @model_validator(mode="after")
    def _matches_portfolio_owner(self):
        if (
            self.context.account_mode != "ADVISORY_REAL"
            or self.context.account_snapshot_ref != self.snapshot_ref
            or self.context.account_status != self.status
            or self.context.valid_until != self.valid_until
            or self.context.source_kind != self.source_kind
        ):
            raise ValueError("actual advisory option does not match its owner")
        return self


class PaperGuidanceContextOption(_GuidanceContextOptionBase):
    """One immutable PAPER account context.

    The technical fields remain only so historical snapshots can decode during
    the R1→R4 clean break.  New options are account-only and Agent projections
    always treat them as ``NOT_REQUIRED``.
    """

    owner: Literal["PAPER"] = "PAPER"
    context: PortfolioContext
    generation_id: str
    revision: int = Field(ge=0)
    status: Literal["READY", "UNKNOWN"]
    technical_status: Literal["READY", "UNKNOWN", "NOT_REQUIRED"]
    technical_product_id: str | None = None
    technical_product_hash: str | None = Field(
        default=None,
        pattern="^[0-9a-f]{64}$",
    )
    technical_product: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _matches_paper_owner(self):
        expected_snapshot_ref = f"paper:{self.generation_id}:{self.revision}"
        if (
            self.context.account_mode != "PAPER"
            or self.context.account_snapshot_ref != expected_snapshot_ref
            or self.context.account_status != self.status
            or self.context.valid_until is not None
        ):
            raise ValueError("paper option does not match its owner")
        if self.technical_status == "READY":
            if (
                self.technical_product_id is None
                or self.technical_product_hash is None
                or self.technical_product is None
            ):
                raise ValueError("ready paper option requires frozen technical identity")
        elif (
            self.technical_product_id is not None
            or self.technical_product_hash is not None
            or self.technical_product is not None
        ):
            raise ValueError("unready paper option cannot carry a technical product")
        return self


GuidanceContextOption = Annotated[
    RequestGuidanceContextOption | AdvisoryRealGuidanceContextOption | PaperGuidanceContextOption,
    Field(discriminator="owner"),
]


def request_guidance_context_option(
    context: GeneralContext | SingleAssetContext | MultiAssetContext,
    *,
    data_gaps: tuple[str, ...] = (),
    user_context_contributions: tuple[UserContextContribution, ...] = (),
) -> RequestGuidanceContextOption:
    """Bind one request-owned semantic context to its canonical option identity."""

    payload: dict[str, object] = {
        "owner": "REQUEST_CONTEXT",
        "context": context.model_dump(mode="json"),
        "data_gaps": list(data_gaps),
    }
    if user_context_contributions:
        payload["user_context_contributions"] = [
            contribution.model_dump(mode="json")
            for contribution in user_context_contributions
        ]
    return RequestGuidanceContextOption(
        option_id=guidance_context_option_id(payload),
        context=context,
        data_gaps=data_gaps,
        user_context_contributions=user_context_contributions,
    )


class AskCommand(_FrozenModel):
    action: Literal["ask"] = "ask"
    question: str
    outcome_mode: Literal["answer", "research"] = "answer"
    context: GuidanceContext = Field(default_factory=GeneralContext)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("question")
    @classmethod
    def _question_is_non_empty(cls, value: str) -> str:
        if not _normalize_text(value):
            raise ValueError("question must be non-empty")
        return value


class ReadCommand(_FrozenModel):
    action: Literal["read"] = "read"
    continuation_token: str = Field(min_length=1, max_length=512)


class ContinueCommand(_FrozenModel):
    action: Literal["continue"] = "continue"
    continuation_token: str = Field(min_length=1, max_length=512)
    question: str
    outcome_mode: Literal["answer", "research"] = "answer"
    context: GuidanceContext | None = None
    # 引导者 non-evidence 过程段（长度上限与 guide_provider.GUIDE_MAX_CHARS
    # 一致，避免跨模块导入）；None=本轮无引导（未配置/失败/STOP），语义与
    # 现状追问完全一致。用户问题原文保持逐字不变。
    process_guidance: str | None = Field(default=None, max_length=1200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("question")
    @classmethod
    def _question_is_non_empty(cls, value: str) -> str:
        if not _normalize_text(value):
            raise ValueError("question must be non-empty")
        return value


class CloseCommand(_FrozenModel):
    action: Literal["close"] = "close"
    continuation_token: str = Field(min_length=1, max_length=512)


class FeedbackCommand(_FrozenModel):
    action: Literal["feedback"] = "feedback"
    continuation_token: str = Field(min_length=1, max_length=512)
    product_version: int = Field(ge=1)
    item_id: str = Field(min_length=1, max_length=256)
    disposition: Literal["useful", "wrong_direction", "handled", "skip"]
    note: str | None = None


DecisionGuidanceCommand = Annotated[
    AskCommand | ReadCommand | ContinueCommand | CloseCommand | FeedbackCommand,
    Field(discriminator="action"),
]


class PublicProblem(_FrozenModel):
    code: str
    category: Literal[
        "auth",
        "request",
        "conflict",
        "state",
        "runtime",
        "contract",
        "safety",
        "service_unavailable",
    ]
    retryable: bool
    display_message: str
    # A1: 内部技术故障附 sanitized 128-bit id(err_ + 32 hex);预期输入/
    # 权限问题与技术故障外问题不生成。同一个故障在层间只复制,不重生成。
    # 严格格式闭集: 自由文本/prompt/token/身份不得进入公开结果。
    error_id: str | None = None

    @field_validator("error_id")
    @classmethod
    def _error_id_is_sanitized(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str) or _ERROR_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("error id must be a sanitized 128-bit id")
        return value


def _problem(
    code: str,
    category: Literal[
        "auth",
        "request",
        "conflict",
        "state",
        "runtime",
        "contract",
        "safety",
        "service_unavailable",
    ],
    retryable: bool,
    message: str,
) -> PublicProblem:
    return PublicProblem(
        code=code,
        category=category,
        retryable=retryable,
        display_message=message,
    )


PUBLIC_PROBLEM_REGISTRY: Mapping[str, PublicProblem] = MappingProxyType(
    {
        "authentication_required": _problem(
            "authentication_required", "auth", False, "Authentication is required."
        ),
        "invalid_request": _problem("invalid_request", "request", False, "The request is invalid."),
        "idempotency_conflict": _problem(
            "idempotency_conflict",
            "conflict",
            False,
            "The idempotency key was already used for a different request.",
        ),
        # A5L-3 (O3): 同 lane 并发明确 busy——不排队不等待，立即可重试。
        "lane_busy": _problem(
            "lane_busy",
            "conflict",
            True,
            "The conversation lane is busy; retry shortly.",
        ),
        "continuation_not_accessible": _problem(
            "continuation_not_accessible",
            "auth",
            False,
            "The continuation is not accessible.",
        ),
        "continuation_conflict": _problem(
            "continuation_conflict",
            "conflict",
            True,
            "The continuation advanced while this request was running.",
        ),
        "continuation_epoch_unsupported": _problem(
            "continuation_epoch_unsupported",
            "state",
            False,
            "The continuation belongs to an unsupported runtime epoch.",
        ),
        "research_in_progress": _problem(
            "research_in_progress", "state", True, "Research is already in progress."
        ),
        "chain_closed": _problem("chain_closed", "state", False, "The guidance chain is closed."),
        "product_version_not_found": _problem(
            "product_version_not_found",
            "state",
            False,
            "The requested product version is not available.",
        ),
        "research_state_schema_unsupported": _problem(
            "research_state_schema_unsupported",
            "state",
            False,
            "The stored research state is not supported by this runtime.",
        ),
        "runtime_unavailable": _problem(
            "runtime_unavailable",
            "runtime",
            True,
            "The research runtime is currently unavailable.",
        ),
        "runtime_timeout": _problem(
            "runtime_timeout",
            "runtime",
            True,
            "The research runtime did not finish within its budget.",
        ),
        "product_contract_invalid": _problem(
            "product_contract_invalid",
            "contract",
            True,
            "The candidate product did not meet the required contract.",
        ),
        # 0.2 v3：subject admission 的 typed fatal code（narrow mapping）。
        "consultation_subject_cap_exceeded": _problem(
            "consultation_subject_cap_exceeded",
            "contract",
            True,
            "Too many subjects were requested for one consultation.",
        ),
        "consultation_subject_generation_mixed": _problem(
            "consultation_subject_generation_mixed",
            "contract",
            False,
            "Subject evidence from different generations was mixed in one consultation.",
        ),
        # B: Agent 提议动作但后置 readiness/subject 失败(不发布矛盾结论)。
        "consultation_action_readiness_unavailable": _problem(
            "consultation_action_readiness_unavailable",
            "contract",
            True,
            "The account or market readiness for the proposed action is not confirmed.",
        ),
        "source_boundary_invalid": _problem(
            "source_boundary_invalid",
            "safety",
            False,
            "The candidate product crossed a protected source boundary.",
        ),
    }
)


def public_problem(code: str) -> PublicProblem:
    """Return a safe public problem projection for a registered machine code."""

    return PUBLIC_PROBLEM_REGISTRY[code]


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    total_seconds: int
    runtime_seconds: int
    context_chars: int
    max_capability_calls: int


@dataclass(frozen=True, slots=True)
class ResearchPolicyCatalog:
    policy_version: str
    answer_budget: RuntimeBudget
    research_budget: RuntimeBudget
    common_capabilities: tuple[str, ...] = (
        "fin.read_g_context",
        "fin.read_teacher_cognition",
        "fin.read_ready_evidence",
        "fin.independent_deliberation",
    )
    asset_scope_capabilities: tuple[str, ...] = (
        "fin.read_market_snapshot",
        "fin.read_cached_external_research",
    )
    portfolio_scope_capabilities: tuple[str, ...] = (
        "fin.inspect_portfolio_snapshot",
        "fin.read_market_snapshot",
        "fin.read_cached_external_research",
    )
    general_scope_capabilities: tuple[str, ...] = ()

    @classmethod
    def m4_v1(cls) -> ResearchPolicyCatalog:
        return cls(
            policy_version="semantic-research-v1",
            answer_budget=RuntimeBudget(180, 120, 32_000, 4),
            research_budget=RuntimeBudget(1_800, 300, 32_000, 12),
        )

    @classmethod
    def consultation_v1(cls) -> ResearchPolicyCatalog:
        """Own the single-Agent consultation contract and bounded read toolbelt."""

        return cls(
            policy_version="semantic-consultation-v1",
            # This is an emergency resource circuit breaker, not an Agent-facing
            # semantic quota.  The Agent may decide to use no FIN read, retry a
            # read, or follow a new lead; deadline/bytes/concurrency/stall remain
            # the primary resource controls.
            answer_budget=RuntimeBudget(3_600, 3_595, 40_000, 64),
            research_budget=RuntimeBudget(1_800, 300, 40_000, 64),
            common_capabilities=(
                "fin.read_actual_portfolio",
                "fin.read_g_context",
                "fin.read_teacher_cognition",
                "fin.read_market_overview",
            ),
            asset_scope_capabilities=(
                "fin.read_market_snapshot",
                "fin.read_margin_evidence",
                "fin.read_external_evidence",
            ),
            portfolio_scope_capabilities=(
                "fin.read_market_snapshot",
                "fin.read_margin_evidence",
                "fin.read_external_evidence",
            ),
            general_scope_capabilities=(
                "fin.read_market_snapshot",
                "fin.read_margin_evidence",
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedOutcome:
    mode: Literal["answer", "research"]


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    kind: Literal["general", "single_asset", "multi_asset", "portfolio", "consultation"]


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    mode: Literal["inline", "queued"]
    queue_allowed: bool


@dataclass(frozen=True, slots=True)
class ResearchGoal:
    kind: Literal["decision_support"] = "decision_support"
    requested_profile: Literal["AUTO", "EXPLAIN", "DECIDE_NOW"] = "AUTO"
    requested_tactical_need: Literal[
        "AUTO",
        "NOT_REQUESTED",
        "EXPLAIN_METHOD",
        "CURRENT_ASSESSMENT",
    ] = "AUTO"


@dataclass(frozen=True, slots=True)
class ResearchInputSnapshotRef:
    schema_version: Literal["fin.research-input/v1", "fin.consultation-input/v1"]
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    ref: str
    max_chars: int
    user_context_classification: Literal["user_context_not_evidence"]


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    ref: str
    allowed_source_kinds: tuple[str, ...]
    teacher_cognition_is_authoritative: bool
    user_context_is_evidence: bool


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    ref: str
    allowed_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    max_calls: int


SemanticEvidenceRequirement = Literal["NONE", "G_REQUIRED", "CURRENT_BROAD_MARKET"]
_TEACHER_MAINLINE_QUERY_TOKENS = (
    "认知主线",
    "当前主线",
    "最近主线",
    "关注的主线",
)
_BROAD_MARKET_ANCHORS = (
    "a股",
    "大a",
    "大盘",
    "全市场",
    "沪深市场",
    "沪深两市",
)
_CURRENT_MARKET_STATE_TOKENS = (
    "当前",
    "当下",
    "现在",
    "目前",
    "今日",
    "今天",
    "最新",
    "开盘",
    "盘中",
    "收盘",
    "现状",
)
_MARKET_STATE_QUESTION_TOKENS = (
    "现状",
    "什么情况",
    "怎么看",
    "如何看待",
    "态势",
    "格局",
    "强弱",
    "阶段",
    "盘面",
    "行情",
)
_MARKET_METHOD_TOKENS = (
    "通常",
    "一般",
    "方法",
    "框架",
    "原理",
    "如何分析",
    "怎么分析",
)
_NEGATED_BROAD_MARKET_CLAUSE = re.compile(
    r"(?:不用|无需|不需要|不必|请勿|别|不要|不看|不谈|不分析)"
    r"[^。！？；，,\n]{0,24}(?:a股|大a|大盘|全市场|沪深市场|沪深两市|市场|盘面|行情)"
)
_WHOLE_MARKET_STATE_PREFIX = re.compile(
    r"(?:的)?"
    r"(?:(?:当前|当下|现在|目前|今日|今天|最新|开盘|盘中|收盘)(?:的)?)?"
    r"(?:整体|市场)?"
    r"(?:是|是个|处于|呈现|表现为)?"
)


def is_combined_teacher_current_market_query(question: str) -> bool:
    """Identify one complete teacher-mainline plus current whole-market ask."""

    normalized = "".join(question.casefold().split())
    if (
        "锅老师" not in normalized
        or not any(token in normalized for token in _TEACHER_MAINLINE_QUERY_TOKENS)
        or _NEGATED_BROAD_MARKET_CLAUSE.search(normalized) is not None
        or any(token in normalized for token in _MARKET_METHOD_TOKENS)
    ):
        return False
    for anchor in _BROAD_MARKET_ANCHORS:
        start = normalized.find(anchor)
        while start >= 0:
            after_anchor = normalized[start + len(anchor) : start + len(anchor) + 24]
            window = normalized[max(0, start - 16) : start + len(anchor)] + after_anchor
            if any(token in window for token in _CURRENT_MARKET_STATE_TOKENS):
                for token in _MARKET_STATE_QUESTION_TOKENS:
                    token_start = after_anchor.find(token)
                    while token_start >= 0:
                        if _WHOLE_MARKET_STATE_PREFIX.fullmatch(after_anchor[:token_start]):
                            return True
                        token_start = after_anchor.find(token, token_start + len(token))
            start = normalized.find(anchor, start + len(anchor))
    return False


ContinuationAudience = Literal[
    "guo.decision_guidance",
    "consultation.decision_support",
]
_CONTINUATION_AUDIENCES = frozenset(
    {
        "guo.decision_guidance",
        "consultation.decision_support",
    }
)


@dataclass(frozen=True, slots=True)
class DeliberationPolicy:
    ref: str
    mode: Literal["disabled", "allowed", "required"]
    max_reference_agents: int
    max_aggregation_agents: int


@dataclass(frozen=True, slots=True)
class ProductContractRef:
    ref: str


@dataclass(frozen=True, slots=True)
class FallbackPolicy:
    ref: str
    partial_allowed: bool
    automatic_research_allowed: bool
    second_provider_allowed: bool


@dataclass(frozen=True, slots=True)
class ContinuationPolicy:
    ref: str
    principal_scoped: bool
    inherit_context_when_omitted: bool
    context_replacement_only: bool
    continuation_audience: ContinuationAudience


@dataclass(frozen=True, slots=True)
class EffectPolicy:
    ref: str
    advisory_only: bool
    execution_allowed: bool
    state_writes_allowed: bool


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    ref: str
    g_z_isolated: bool
    source_cognition_isolated: bool
    human_confirmation_required: bool


@dataclass(frozen=True, slots=True)
class GuidanceSnapshot:
    """Validated prior chain projection supplied by the application service."""

    contract_id: str
    context: GuidanceContext | None = None
    context_options: tuple[GuidanceContextOption, ...] = ()
    goal: ResearchGoal = ResearchGoal()

    def __post_init__(self) -> None:
        if len(self.contract_id) != 64 or any(
            character not in "0123456789abcdef" for character in self.contract_id
        ):
            raise ValueError("prior contract id must be a SHA-256 hex digest")
        if (self.context is None) == (not self.context_options):
            raise ValueError("prior snapshot requires exactly one context representation")


@dataclass(frozen=True, slots=True)
class ResearchInputSnapshot:
    schema_version: Literal["fin.research-input/v1"]
    snapshot_hash: str
    question: str
    context: GuidanceContext
    context_classification: Literal["user_context_not_evidence"]
    principal_namespace: str
    parent_contract_id: str | None


@dataclass(frozen=True, slots=True)
class ConsultationMainlineProjection:
    """滚动咨询主线投影（Phase 2，consultation-session-continuity）。

    由 FIN 从上一版已发布 product 确定性推导（当前最小切片不依赖 Agent
    生成），随 follow_up 注入 ContextPack 帮助 Agent 延续同一决策主线。
    classification 固定为 ``prior_consultation_context_not_evidence``——
    它帮助理解省略指代与用户目标，不提升任何 G/Z/市场事实的来源等级。
    """

    schema_version: Literal["fin.consultation-mainline/v1"]
    focus: str
    open_questions: tuple[str, ...]
    last_turn_summary: str
    as_of: datetime
    source_product_version: int
    source_artifact_hash: str
    classification: Literal["prior_consultation_context_not_evidence"] = (
        "prior_consultation_context_not_evidence"
    )

    def __post_init__(self) -> None:
        if self.schema_version != "fin.consultation-mainline/v1":
            raise ValueError("mainline schema version is not canonical")
        if self.classification != "prior_consultation_context_not_evidence":
            raise ValueError("mainline classification must be non-evidence")
        if re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", self.source_artifact_hash) is None:
            raise ValueError("mainline source artifact hash must be a SHA-256 digest")
        if not _normalize_text(self.focus):
            raise ValueError("mainline focus must be non-empty")
        if len(self.focus) > 2000:
            raise ValueError("mainline focus exceeds 2000 characters")
        if len(self.open_questions) > 5:
            raise ValueError("mainline open questions exceed 5 items")
        if any(not _normalize_text(question) for question in self.open_questions):
            raise ValueError("mainline open questions must be non-empty")
        if len(self.last_turn_summary) > 2000:
            raise ValueError("mainline last turn summary exceeds 2000 characters")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("mainline as_of must be timezone-aware")
        if self.source_product_version < 1:
            raise ValueError("mainline source product version must be positive")


@dataclass(frozen=True, slots=True)
class DailyWorkspaceContextSource:
    """Identity of one immutable workspace version used only as context."""

    trading_day_id: str
    checkpoint: str
    product_version: int
    artifact_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.trading_day_id, str) or not _normalize_text(self.trading_day_id):
            raise ValueError("daily workspace source trading day must be non-empty")
        if not isinstance(self.checkpoint, str) or not _normalize_text(self.checkpoint):
            raise ValueError("daily workspace source checkpoint must be non-empty")
        if not isinstance(self.product_version, int) or self.product_version < 1:
            raise ValueError("daily workspace source version must be positive")
        if (
            not isinstance(self.artifact_hash, str)
            or re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", self.artifact_hash) is None
        ):
            raise ValueError("daily workspace source artifact hash must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class DailyWorkspaceCarryOverProjection:
    """The bounded prior Agent answer available to the next checkpoint."""

    answer_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.answer_text, str) or len(self.answer_text) > 6_500:
            raise ValueError("daily workspace carry-over answer exceeds 6500 characters")


@dataclass(frozen=True, slots=True)
class DailyWorkspaceContextProjection:
    """FIN-owned prior workspace context; it is never a new evidence source."""

    schema_version: Literal["fin.daily-workspace-context/v1"]
    classification: Literal["prior_daily_workspace_context_not_evidence"]
    relationship: Literal[
        "same_trading_day_parent",
        "previous_trading_day",
        "unavailable",
    ]
    source: DailyWorkspaceContextSource | None
    carry_over: DailyWorkspaceCarryOverProjection
    data_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "fin.daily-workspace-context/v1":
            raise ValueError("daily workspace context schema version is not canonical")
        if self.classification != "prior_daily_workspace_context_not_evidence":
            raise ValueError("daily workspace context must be non-evidence")
        if self.relationship not in {
            "same_trading_day_parent",
            "previous_trading_day",
            "unavailable",
        }:
            raise ValueError("daily workspace context relationship is invalid")
        if self.source is not None and not isinstance(self.source, DailyWorkspaceContextSource):
            raise ValueError("daily workspace context source is invalid")
        if not isinstance(self.carry_over, DailyWorkspaceCarryOverProjection):
            raise ValueError("daily workspace carry-over is invalid")
        if (
            not isinstance(self.data_gaps, tuple)
            or any(not isinstance(gap, str) or not _normalize_text(gap) for gap in self.data_gaps)
            or len(set(self.data_gaps)) != len(self.data_gaps)
        ):
            raise ValueError("daily workspace context gaps must be unique non-empty strings")


def load_daily_workspace_context_projection(
    payload: Mapping[str, object],
) -> DailyWorkspaceContextProjection:
    """Decode one persisted daily workspace context projection fail-closed."""

    raw = dict(payload)
    source_raw = raw.get("source")
    source: DailyWorkspaceContextSource | None = None
    if source_raw is not None:
        if not isinstance(source_raw, Mapping):
            raise ValueError("daily workspace context source must be an object")
        source = DailyWorkspaceContextSource(
            trading_day_id=source_raw["trading_day_id"],
            checkpoint=source_raw["checkpoint"],
            product_version=source_raw["product_version"],
            artifact_hash=source_raw["artifact_hash"],
        )
    carry_raw = raw.get("carry_over")
    if not isinstance(carry_raw, Mapping):
        raise ValueError("daily workspace carry-over must be an object")

    data_gaps = raw.get("data_gaps", ())
    if not isinstance(data_gaps, (list, tuple)):
        raise ValueError("daily workspace context gaps are invalid")
    return DailyWorkspaceContextProjection(
        schema_version=cast(Literal["fin.daily-workspace-context/v1"], raw["schema_version"]),
        classification=cast(
            Literal["prior_daily_workspace_context_not_evidence"], raw["classification"]
        ),
        relationship=cast(
            Literal[
                "same_trading_day_parent",
                "previous_trading_day",
                "unavailable",
            ],
            raw["relationship"],
        ),
        source=source,
        carry_over=DailyWorkspaceCarryOverProjection(
            answer_text=carry_raw["answer_text"],
        ),
        data_gaps=tuple(data_gaps),
    )


@dataclass(frozen=True, slots=True)
class ConsultationInputSnapshot:
    """Immutable FIN-owned context choices exposed to one consultation Agent."""

    schema_version: Literal["fin.consultation-input/v1"]
    snapshot_hash: str
    question: str
    context_options: tuple[GuidanceContextOption, ...]
    context_data_gaps: tuple[str, ...]
    context_classification: Literal["user_context_not_evidence"]
    principal_namespace: str
    parent_contract_id: str | None
    prior_consultation_context: ConsultationMainlineProjection | None = None
    daily_workspace_context: DailyWorkspaceContextProjection | None = None
    # Runtime-only bounded recall.  It is deliberately excluded from the
    # persisted input projection so readable user facts remain journal-owned.
    prior_investment_memory: InvestmentMemoryRecall | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.context_options) <= 3:
            raise ValueError("consultation input requires one to three context options")
        owners = tuple(option.owner for option in self.context_options)
        if len(set(owners)) != len(owners):
            raise ValueError("consultation context owners must be unique")
        order = {"REQUEST_CONTEXT": 0, "ADVISORY_REAL": 1, "PAPER": 2}
        if owners != tuple(sorted(owners, key=order.__getitem__)):
            raise ValueError("consultation context options are not canonically ordered")
        if any(not gap or not gap.strip() for gap in self.context_data_gaps):
            raise ValueError("consultation context gaps must be non-empty")
        if len(set(self.context_data_gaps)) != len(self.context_data_gaps):
            raise ValueError("consultation context gaps must be unique")


SemanticInputSnapshot = ResearchInputSnapshot | ConsultationInputSnapshot


@dataclass(frozen=True, slots=True)
class GuidanceContextProof:
    """Immediate selected-owner proof used before semantic publication."""

    status: Literal["CURRENT", "DRIFTED", "UNAVAILABLE"]
    option_id: str
    owner: Literal["REQUEST_CONTEXT", "ADVISORY_REAL", "PAPER"]
    verified_at: datetime
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedResearchContract:
    schema_version: Literal["fin.research-contract/v1"]
    policy_version: str
    contract_id: str
    use_case_ref: Literal["decision_guidance"]
    goal: ResearchGoal
    input_snapshot_ref: ResearchInputSnapshotRef
    outcome: ResolvedOutcome
    scope: ResolvedScope
    delivery: DeliveryPolicy
    context_policy: ContextPolicy
    source_policy: SourcePolicy
    tool_policy: ToolPolicy
    runtime_budget: RuntimeBudget
    deliberation_policy: DeliberationPolicy
    product_contracts: tuple[ProductContractRef, ...]
    fallback_policy: FallbackPolicy
    continuation_policy: ContinuationPolicy
    effect_policy: EffectPolicy
    safety_policy: SafetyPolicy


@dataclass(frozen=True, slots=True)
class ResolvedResearchInvocation:
    contract: ResolvedResearchContract
    input_snapshot: SemanticInputSnapshot
    principal_binding: PrincipalBinding
    idempotency_key_hash: str | None


class ContractResolutionError(ValueError):
    """Safe deterministic resolver failure."""

    problem_code = "invalid_request"


class ResearchContractResolver:
    """Resolve semantic commands without I/O, model calls or mutable state."""

    def resolve(
        self,
        command: AskCommand | ContinueCommand,
        *,
        principal: PrincipalBinding,
        prior_snapshot: GuidanceSnapshot | None,
        policy_catalog: ResearchPolicyCatalog,
        goal: ResearchGoal | None = None,
        context_options: tuple[GuidanceContextOption, ...] | None = None,
        context_data_gaps: tuple[str, ...] = (),
        evidence_requirement: SemanticEvidenceRequirement = "NONE",
        continuation_audience: ContinuationAudience = "guo.decision_guidance",
        prior_consultation_context: ConsultationMainlineProjection | None = None,
        daily_workspace_context: DailyWorkspaceContextProjection | None = None,
        prior_investment_memory: InvestmentMemoryRecall | None = None,
    ) -> ResolvedResearchInvocation:
        if continuation_audience not in _CONTINUATION_AUDIENCES:
            raise ContractResolutionError("continuation audience is invalid")
        if daily_workspace_context is not None and not isinstance(
            daily_workspace_context, DailyWorkspaceContextProjection
        ):
            raise ContractResolutionError("daily workspace context must be a typed projection")
        consultation = policy_catalog.policy_version == "semantic-consultation-v1"
        if evidence_requirement != "NONE" and not consultation:
            raise ContractResolutionError("evidence requirement is only valid for consultation")
        if prior_investment_memory is not None and (
            not consultation or not isinstance(command, AskCommand) or prior_snapshot is not None
        ):
            raise ContractResolutionError(
                "investment memory is only valid for a fresh consultation ask"
            )
        if daily_workspace_context is not None and (
            not consultation or not isinstance(command, AskCommand) or prior_snapshot is not None
        ):
            raise ContractResolutionError(
                "daily workspace context is only valid for a fresh consultation ask"
            )
        if consultation:
            if command.outcome_mode != "answer":
                raise ContractResolutionError("consultation only supports inline answers")
            if isinstance(command, ContinueCommand):
                if (
                    prior_snapshot is None
                    or not prior_snapshot.context_options
                    or command.context is not None
                    or context_options is not None
                    or context_data_gaps
                ):
                    raise ContractResolutionError(
                        "consultation continuation requires its selected context option"
                    )
                resolved_options = prior_snapshot.context_options
                resolved_context_gaps: tuple[str, ...] = ()
                parent_contract_id = prior_snapshot.contract_id
                resolved_goal = goal or prior_snapshot.goal
            else:
                if prior_snapshot is not None or context_options is None:
                    raise ContractResolutionError(
                        "consultation ask requires FIN-owned context options"
                    )
                resolved_options = context_options
                resolved_context_gaps = tuple(dict.fromkeys(context_data_gaps))
                parent_contract_id = None
                resolved_goal = goal or ResearchGoal()
            question = _normalize_text(command.question)
            snapshot_payload: dict[str, Any] = {
                "schema_version": "fin.consultation-input/v1",
                "question": question,
                "context_options": [option.model_dump(mode="json") for option in resolved_options],
                "context_data_gaps": list(resolved_context_gaps),
                "context_classification": "user_context_not_evidence",
                "principal_namespace": principal.namespace,
                "parent_contract_id": parent_contract_id,
            }
            if prior_consultation_context is not None:
                prior_dict = asdict(prior_consultation_context)
                prior_dict["as_of"] = prior_consultation_context.as_of.isoformat()
                snapshot_payload["prior_consultation_context"] = prior_dict
            if daily_workspace_context is not None:
                snapshot_payload["daily_workspace_context"] = asdict(daily_workspace_context)
            snapshot_hash = _canonical_hash(snapshot_payload)
            snapshot: SemanticInputSnapshot = ConsultationInputSnapshot(
                schema_version="fin.consultation-input/v1",
                snapshot_hash=snapshot_hash,
                question=question,
                context_options=resolved_options,
                context_data_gaps=resolved_context_gaps,
                context_classification="user_context_not_evidence",
                principal_namespace=principal.namespace,
                parent_contract_id=parent_contract_id,
                prior_consultation_context=prior_consultation_context,
                daily_workspace_context=daily_workspace_context,
                prior_investment_memory=prior_investment_memory,
            )
            scope: Literal[
                "general", "single_asset", "multi_asset", "portfolio", "consultation"
            ] = "consultation"
        else:
            if context_options is not None or context_data_gaps:
                raise ContractResolutionError(
                    "generic research cannot receive consultation context options"
                )
            if isinstance(command, ContinueCommand):
                if prior_snapshot is None or prior_snapshot.context is None:
                    raise ContractResolutionError("continue requires a validated prior snapshot")
                context_model = command.context or prior_snapshot.context
                parent_contract_id = prior_snapshot.contract_id
                resolved_goal = goal or prior_snapshot.goal
            else:
                if prior_snapshot is not None:
                    raise ContractResolutionError("ask cannot resolve against a prior snapshot")
                context_model = command.context
                parent_contract_id = None
                resolved_goal = goal or ResearchGoal()
            question = _normalize_text(command.question)
            context_projection = context_model.model_dump(mode="json")
            snapshot_payload = {
                "schema_version": "fin.research-input/v1",
                "question": question,
                "context": context_projection,
                "context_classification": "user_context_not_evidence",
                "principal_namespace": principal.namespace,
                "parent_contract_id": parent_contract_id,
            }
            snapshot_hash = _canonical_hash(snapshot_payload)
            snapshot = ResearchInputSnapshot(
                schema_version="fin.research-input/v1",
                snapshot_hash=snapshot_hash,
                question=question,
                context=context_model,
                context_classification="user_context_not_evidence",
                principal_namespace=principal.namespace,
                parent_contract_id=parent_contract_id,
            )
            scope = context_model.kind
        is_research = command.outcome_mode == "research"
        runtime_budget = (
            policy_catalog.research_budget if is_research else policy_catalog.answer_budget
        )
        capabilities = _capabilities_for_scope(
            scope,
            common_capabilities=policy_catalog.common_capabilities,
            asset_scope_capabilities=policy_catalog.asset_scope_capabilities,
            portfolio_scope_capabilities=policy_catalog.portfolio_scope_capabilities,
            general_scope_capabilities=policy_catalog.general_scope_capabilities,
        )
        required_capabilities = _required_capabilities_for_invocation(
            policy_version=policy_catalog.policy_version,
            scope=scope,
            evidence_requirement=evidence_requirement,
        )
        consultation_policy = policy_catalog.policy_version == "semantic-consultation-v1"
        contract = ResolvedResearchContract(
            schema_version="fin.research-contract/v1",
            policy_version=policy_catalog.policy_version,
            contract_id="",
            use_case_ref="decision_guidance",
            goal=resolved_goal,
            input_snapshot_ref=ResearchInputSnapshotRef(
                schema_version=snapshot.schema_version,
                snapshot_hash=snapshot_hash,
            ),
            outcome=ResolvedOutcome(command.outcome_mode),
            scope=ResolvedScope(scope),
            delivery=DeliveryPolicy(
                mode="queued" if is_research else "inline",
                queue_allowed=is_research,
            ),
            context_policy=ContextPolicy(
                ref=f"{policy_catalog.policy_version}/context",
                max_chars=runtime_budget.context_chars,
                user_context_classification="user_context_not_evidence",
            ),
            source_policy=SourcePolicy(
                ref=f"{policy_catalog.policy_version}/sources",
                allowed_source_kinds=(
                    "teacher_cognition",
                    "g_context",
                    "fin_ready_evidence",
                    "market_data",
                    "user_context_not_evidence",
                ),
                teacher_cognition_is_authoritative=True,
                user_context_is_evidence=False,
            ),
            tool_policy=ToolPolicy(
                ref=f"{policy_catalog.policy_version}/tools/{scope}",
                allowed_capabilities=capabilities,
                required_capabilities=required_capabilities,
                max_calls=runtime_budget.max_capability_calls,
            ),
            runtime_budget=runtime_budget,
            deliberation_policy=DeliberationPolicy(
                ref=f"{policy_catalog.policy_version}/deliberation/{command.outcome_mode}",
                mode="disabled" if consultation_policy else "allowed",
                max_reference_agents=0 if consultation_policy else 3 if is_research else 2,
                max_aggregation_agents=0 if consultation_policy else 1,
            ),
            product_contracts=(
                (ProductContractRef("consultation_product/v1"),)
                if policy_catalog.policy_version == "semantic-consultation-v1"
                else (
                    ProductContractRef("decision-guidance-product/v1"),
                    ProductContractRef("source-cognition-boundary/v1"),
                )
            ),
            fallback_policy=FallbackPolicy(
                ref=f"{policy_catalog.policy_version}/fallback",
                partial_allowed=True,
                automatic_research_allowed=False,
                second_provider_allowed=False,
            ),
            continuation_policy=ContinuationPolicy(
                ref=f"{policy_catalog.policy_version}/continuation",
                principal_scoped=True,
                inherit_context_when_omitted=True,
                context_replacement_only=True,
                continuation_audience=continuation_audience,
            ),
            effect_policy=EffectPolicy(
                ref=f"{policy_catalog.policy_version}/effects",
                advisory_only=True,
                execution_allowed=False,
                state_writes_allowed=True,
            ),
            safety_policy=SafetyPolicy(
                ref=f"{policy_catalog.policy_version}/safety",
                g_z_isolated=True,
                source_cognition_isolated=True,
                human_confirmation_required=True,
            ),
        )
        contract_payload = _canonical_contract_projection(contract)
        contract_payload.pop("contract_id")
        contract = replace(contract, contract_id=_canonical_hash(contract_payload))
        idempotency_key_hash = None
        if command.idempotency_key is not None:
            idempotency_key_hash = _canonical_hash(
                {
                    "namespace": principal.namespace,
                    "principal_id": principal.principal_id,
                    "key": command.idempotency_key,
                }
            )
        return ResolvedResearchInvocation(
            contract=contract,
            input_snapshot=snapshot,
            principal_binding=principal,
            idempotency_key_hash=idempotency_key_hash,
        )


def resolved_contract_projection(contract: ResolvedResearchContract) -> dict[str, object]:
    """Return the canonical JSON-ready projection stored for a research job."""

    return cast(dict[str, object], _canonical_contract_projection(contract))


def input_snapshot_projection(snapshot: SemanticInputSnapshot) -> dict[str, object]:
    """Return the canonical JSON-ready projection stored for a research job."""

    if isinstance(snapshot, ConsultationInputSnapshot):
        projection: dict[str, object] = {
            "schema_version": snapshot.schema_version,
            "snapshot_hash": snapshot.snapshot_hash,
            "question": snapshot.question,
            "context_options": [
                option.model_dump(mode="json") for option in snapshot.context_options
            ],
            "context_data_gaps": list(snapshot.context_data_gaps),
            "context_classification": snapshot.context_classification,
            "principal_namespace": snapshot.principal_namespace,
            "parent_contract_id": snapshot.parent_contract_id,
        }
        if snapshot.prior_consultation_context is not None:
            prior = asdict(snapshot.prior_consultation_context)
            prior["as_of"] = snapshot.prior_consultation_context.as_of.isoformat()
            projection["prior_consultation_context"] = prior
        if snapshot.daily_workspace_context is not None:
            projection["daily_workspace_context"] = asdict(snapshot.daily_workspace_context)
        return projection
    return {
        "schema_version": snapshot.schema_version,
        "snapshot_hash": snapshot.snapshot_hash,
        "question": snapshot.question,
        "context": snapshot.context.model_dump(mode="json"),
        "context_classification": snapshot.context_classification,
        "principal_namespace": snapshot.principal_namespace,
        "parent_contract_id": snapshot.parent_contract_id,
    }


def load_resolved_contract(payload: Mapping[str, object]) -> ResolvedResearchContract:
    """Rebuild and hash-check the immutable contract stored at admission."""

    raw = cast(dict[str, Any], dict(payload))
    try:
        contract = ResolvedResearchContract(
            schema_version=raw["schema_version"],
            policy_version=raw["policy_version"],
            contract_id=raw["contract_id"],
            use_case_ref=raw["use_case_ref"],
            goal=ResearchGoal(**raw["goal"]),
            input_snapshot_ref=ResearchInputSnapshotRef(**raw["input_snapshot_ref"]),
            outcome=ResolvedOutcome(**raw["outcome"]),
            scope=ResolvedScope(**raw["scope"]),
            delivery=DeliveryPolicy(**raw["delivery"]),
            context_policy=ContextPolicy(**raw["context_policy"]),
            source_policy=SourcePolicy(
                **{
                    **raw["source_policy"],
                    "allowed_source_kinds": tuple(raw["source_policy"]["allowed_source_kinds"]),
                }
            ),
            tool_policy=ToolPolicy(
                **{
                    **raw["tool_policy"],
                    "allowed_capabilities": tuple(raw["tool_policy"]["allowed_capabilities"]),
                    "required_capabilities": tuple(
                        raw["tool_policy"].get("required_capabilities", ())
                    ),
                }
            ),
            runtime_budget=RuntimeBudget(**raw["runtime_budget"]),
            deliberation_policy=DeliberationPolicy(**raw["deliberation_policy"]),
            product_contracts=tuple(
                ProductContractRef(**item) for item in raw["product_contracts"]
            ),
            fallback_policy=FallbackPolicy(**raw["fallback_policy"]),
            continuation_policy=ContinuationPolicy(**raw["continuation_policy"]),
            effect_policy=EffectPolicy(**raw["effect_policy"]),
            safety_policy=SafetyPolicy(**raw["safety_policy"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ContractResolutionError("stored contract is invalid") from error
    if contract.continuation_policy.continuation_audience not in _CONTINUATION_AUDIENCES:
        raise ContractResolutionError("stored contract is invalid")
    canonical = _canonical_contract_projection(contract)
    claimed_id = canonical.pop("contract_id")
    if claimed_id != _canonical_hash(canonical):
        raise ContractResolutionError("stored contract hash is invalid")
    return contract


def load_input_snapshot(payload: Mapping[str, object]) -> SemanticInputSnapshot:
    """Rebuild and hash-check the immutable input snapshot stored at admission."""

    raw = cast(dict[str, Any], dict(payload))
    try:
        if raw["schema_version"] == "fin.consultation-input/v1":
            options: tuple[GuidanceContextOption, ...] = tuple(
                TypeAdapter(GuidanceContextOption).validate_json(
                    json.dumps(item, ensure_ascii=False, allow_nan=False)
                )
                for item in raw["context_options"]
            )
            prior_raw = raw.get("prior_consultation_context")
            prior: ConsultationMainlineProjection | None = None
            if isinstance(prior_raw, dict):
                prior = ConsultationMainlineProjection(
                    schema_version=prior_raw["schema_version"],
                    focus=prior_raw["focus"],
                    open_questions=tuple(prior_raw["open_questions"]),
                    last_turn_summary=prior_raw["last_turn_summary"],
                    as_of=datetime.fromisoformat(prior_raw["as_of"]),
                    source_product_version=prior_raw["source_product_version"],
                    source_artifact_hash=prior_raw["source_artifact_hash"],
                    classification=prior_raw["classification"],
                )
            workspace_raw = raw.get("daily_workspace_context")
            workspace_context: DailyWorkspaceContextProjection | None = None
            if workspace_raw is not None:
                if not isinstance(workspace_raw, Mapping):
                    raise ValueError("daily workspace context must be an object")
                workspace_context = load_daily_workspace_context_projection(workspace_raw)
            snapshot: SemanticInputSnapshot = ConsultationInputSnapshot(
                schema_version=raw["schema_version"],
                snapshot_hash=raw["snapshot_hash"],
                question=raw["question"],
                context_options=options,
                context_data_gaps=tuple(raw["context_data_gaps"]),
                context_classification=raw["context_classification"],
                principal_namespace=raw["principal_namespace"],
                parent_contract_id=raw["parent_contract_id"],
                prior_consultation_context=prior,
                daily_workspace_context=workspace_context,
                prior_investment_memory=None,
            )
        elif raw["schema_version"] == "fin.research-input/v1":
            context: GuidanceContext = TypeAdapter(GuidanceContext).validate_json(
                json.dumps(raw["context"], ensure_ascii=False, allow_nan=False)
            )
            snapshot = ResearchInputSnapshot(
                schema_version=raw["schema_version"],
                snapshot_hash=raw["snapshot_hash"],
                question=raw["question"],
                context=context,
                context_classification=raw["context_classification"],
                principal_namespace=raw["principal_namespace"],
                parent_contract_id=raw["parent_contract_id"],
            )
        else:
            raise ValueError("unsupported input snapshot schema")
    except (KeyError, TypeError, ValueError) as error:
        raise ContractResolutionError("stored input snapshot is invalid") from error
    canonical = input_snapshot_projection(snapshot)
    claimed_hash = canonical.pop("snapshot_hash")
    if claimed_hash != _canonical_hash(canonical):
        raise ContractResolutionError("stored input snapshot hash is invalid")
    return snapshot


def _capabilities_for_scope(
    scope: Literal["general", "single_asset", "multi_asset", "portfolio", "consultation"],
    *,
    common_capabilities: tuple[str, ...],
    asset_scope_capabilities: tuple[str, ...],
    portfolio_scope_capabilities: tuple[str, ...],
    general_scope_capabilities: tuple[str, ...],
) -> tuple[str, ...]:
    if scope == "consultation":
        return tuple(
            dict.fromkeys(
                (
                    *common_capabilities,
                    *general_scope_capabilities,
                    *asset_scope_capabilities,
                    *portfolio_scope_capabilities,
                )
            )
        )
    if scope in {"single_asset", "multi_asset"}:
        return (*common_capabilities, *asset_scope_capabilities)
    if scope == "portfolio":
        return (*common_capabilities, *portfolio_scope_capabilities)
    return (*common_capabilities, *general_scope_capabilities)


def _required_capabilities_for_invocation(
    *,
    policy_version: str,
    scope: Literal["general", "single_asset", "multi_asset", "portfolio", "consultation"],
    evidence_requirement: SemanticEvidenceRequirement,
) -> tuple[str, ...]:
    # Public consultation keeps G in the allowlist/prefetch path but does not
    # make it an answer gate.  Lower-level callers may still request the
    # stricter G_REQUIRED contract when the product itself must attribute G.
    if policy_version == "semantic-consultation-v1":
        if scope != "consultation":
            raise ContractResolutionError("evidence requirement is incompatible")
        if evidence_requirement == "G_REQUIRED":
            return ("fin.read_g_context",)
        if evidence_requirement in {"NONE", "CURRENT_BROAD_MARKET"}:
            return ()
        raise ContractResolutionError("evidence requirement is incompatible")
    if evidence_requirement == "NONE":
        return ()
    raise ContractResolutionError("evidence requirement is incompatible")


def _canonical_contract_projection(
    contract: ResolvedResearchContract,
) -> dict[str, Any]:
    """Keep empty v1 tool obligations byte-compatible with persisted contracts."""

    projection = asdict(contract)
    tool_policy = projection.get("tool_policy")
    if isinstance(tool_policy, dict) and not contract.tool_policy.required_capabilities:
        tool_policy.pop("required_capabilities", None)
    return projection
