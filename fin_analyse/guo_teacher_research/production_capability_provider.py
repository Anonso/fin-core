"""Bounded production readers for the semantic FIN capability registry.

The adapter deliberately owns no cache, identity, registry, or mutable runtime
state. It reads through already-owned FIN services. Generic research evidence
remains cache-only, while source-native market, margin, and official-company
evidence retain their own readers and state owners. The consultation composition
may inject FIN's bounded on-demand readers plus exact A-share identity resolver
for typed or Agent-selected research targets. Every unavailable source returns
typed gaps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Any, Protocol, TypeVar

from fin_analyse.cognition.memory_store import (
    CognitionMemoryRequest,
    CognitionMemoryResult,
    CognitionMemoryScope,
)
from fin_analyse.cognition.models import CognitivePattern, ReasoningTrace, TeacherPersona
from fin_analyse.consultation.instrument_identity import (
    INSTRUMENT_IDENTITY_UNRESOLVED,
    ConsultationInstrumentIdentityResolver,
)
from fin_analyse.external_evidence import ExternalEvidenceReader
from fin_analyse.guo_teacher_research.macro_brain import (
    load_shared_brain_cards,
    macro_search_signal,
    match_shared_brain_cards,
    suggested_queries,
)
from fin_analyse.guo_teacher_research.ready_evidence import (
    RecentReferenceReadyEvidenceReader,
)
from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextProvider,
    AgentRuntimeContextRequest,
    AgentRuntimeContextResult,
)
from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.margin.evidence import (
    MarginEvidence,
    MarginEvidenceReader,
    MarginEvidenceRequest,
)
from fin_analyse.market.current_overview import (
    AshareMarketOverviewRequest,
    AshareMarketOverviewResult,
)
from fin_analyse.market.instrument_directory import verified_a_share_equity_venue
from fin_analyse.market.index_symbols import split_index_aliases
from fin_analyse.market.on_demand_tactical_context import (
    OnDemandTacticalContext,
    OnDemandTacticalContextReader,
    OnDemandTacticalContextRequest,
)
from fin_analyse.market.snapshot import MarketSnapshotRequest, MarketSnapshotResult
from fin_analyse.official_records.evidence import (
    OfficialRecordEvidence,
    OfficialRecordEvidenceRequest,
)
from fin_analyse.portfolio.actual_advisory import (
    ActualAdvisoryPortfolioRead,
    ActualAdvisoryPortfolioReason,
    ActualAdvisoryPortfolioStatus,
)
from fin_analyse.portfolio.user_watchlist import WatchlistRead
from fin_analyse.read_capabilities.types import (
    ProductionReadRequest,
    ProductionReadResult,
    SourceKind,
    SourceTrust,
)
from fin_analyse.researcher.context import (
    ExternalResearchContextRequest,
    ExternalResearchContextResult,
)

_TEACHER_ID = "guo"
_MAX_G_ITEMS = 8
_MAX_G_CANDIDATE_SCAN = 32
_MAX_G_SOURCE_REFS = 64
_MAX_COGNITION_ITEMS_PER_KIND = 4
_MAX_COGNITION_SELECTION_SCAN = 512
_MAX_MARKET_INSTRUMENTS = 16
_MAX_ON_DEMAND_MARKET_INSTRUMENTS = 5
_MAX_RESEARCH_INSTRUMENTS = 8
_MAX_TEXT_CHARS = 1_000
_MAX_SHORT_TEXT_CHARS = 300
_MAX_LIST_ITEMS = 8
_MAX_LIST_CANDIDATE_SCAN = 32
_MAX_CACHE_SESSION_CHARS = 128
_MAX_GAPS = 32
_MARKET_OVERVIEW_BUDGET_SECONDS = 20
_EXTERNAL_BRAIN_MACRO_KEYWORDS = (
    "大盘",
    "市场",
    "宏观",
    "政策",
    "流动性",
    "利率",
    "美联储",
    "美债",
    "海外",
    "美股",
    "地缘",
    "伊朗",
    "关税",
    "汇率",
    "商品",
    "原油",
    "黄金",
    "铜",
    "大宗",
    "经济",
    "央行",
    "降息",
    "加息",
    "指数",
    "港股",
    "科技股",
)
_EXTERNAL_BRAIN_MACRO_CLASSIFICATIONS = frozenset({"market_observation", "ai_summary_reference"})

_STRICT_G_BUCKETS = frozenset({"pinned_source", "fresh_g", "latest_commentary"})
_MARKET_SCALAR_FIELDS = (
    "price",
    "ma5",
    "ma20",
    "ma30",
    "ma60",
    "rsi14",
    "macd_histogram",
    "pe",
    "pe_percentile",
    "roe_trend",
    "flow_score",
)
_COVERAGE_FIELDS = (
    "evidence_role",
    "quality_tier",
)

_TeacherScopedItem = TypeVar(
    "_TeacherScopedItem",
    TeacherPersona,
    CognitivePattern,
    ReasoningTrace,
)


class _RuntimeContextReader(Protocol):
    def resolve(self, request: AgentRuntimeContextRequest) -> AgentRuntimeContextResult: ...


class _CognitionMemoryReader(Protocol):
    def handle(self, request: CognitionMemoryRequest) -> CognitionMemoryResult: ...


class _MarketSnapshotReader(Protocol):
    def peek_snapshot(self, request: MarketSnapshotRequest) -> MarketSnapshotResult: ...


class _MarketOverviewReader(Protocol):
    def read(self, request: AshareMarketOverviewRequest) -> AshareMarketOverviewResult: ...


class _CachedExternalResearchReader(Protocol):
    def collect(
        self,
        request: ExternalResearchContextRequest,
    ) -> ExternalResearchContextResult: ...


class _ReadyEvidenceReader(Protocol):
    def read(self, request: ProductionReadRequest) -> ProductionReadResult: ...


class _ActualPortfolioReader(Protocol):
    def read(self) -> ActualAdvisoryPortfolioRead: ...


class _UserWatchlistReader(Protocol):
    def list(self) -> WatchlistRead: ...


class ProductionReadCapabilityProvider:
    """Concrete, read-only implementation of FIN's fixed read capabilities.

    Mutable/cached domain services are injected as already-owned instances.
    This provider never constructs their stores, so it cannot implicitly create
    a directory, evict a cache entry, select an arbitrary user, or start live
    research.  The local G reader is safe to construct only with the explicit
    production knowledge root: it reads existing FIN artifacts from that root
    and never infers a checkout from imported source files.
    """

    def __init__(
        self,
        *,
        knowledge_base_root: str | Path | None = None,
        runtime_context: _RuntimeContextReader | None = None,
        cognition_mainline_reader: Any | None = None,
        cognition_memory: _CognitionMemoryReader | None = None,
        market_snapshot: _MarketSnapshotReader | None = None,
        on_demand_tactical_context: OnDemandTacticalContextReader | None = None,
        margin_evidence: MarginEvidenceReader | None = None,
        external_evidence: ExternalEvidenceReader | None = None,
        instrument_identity: ConsultationInstrumentIdentityResolver | None = None,
        market_overview: _MarketOverviewReader | None = None,
        cached_external_research: _CachedExternalResearchReader | None = None,
        ready_evidence_reader: _ReadyEvidenceReader | None = None,
        actual_portfolio: _ActualPortfolioReader | None = None,
        user_watchlist: _UserWatchlistReader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if runtime_context is None:
            if knowledge_base_root is None:
                raise ValueError("production_knowledge_base_root_required")
            runtime_context = AgentRuntimeContextProvider(
                kb_root=knowledge_base_root,
                cognition_mainline_reader=cognition_mainline_reader,
            )
        elif knowledge_base_root is not None:
            raise ValueError("production_runtime_context_root_ambiguous")
        self._runtime_context = runtime_context
        self._knowledge_base_root = (
            Path(knowledge_base_root) if knowledge_base_root is not None else None
        )
        self._cognition_memory = cognition_memory
        self._market_snapshot = market_snapshot
        self._on_demand_tactical_context = on_demand_tactical_context
        self._margin_evidence = margin_evidence
        self._external_evidence = external_evidence
        self._instrument_identity = instrument_identity
        self._market_overview = market_overview
        self._cached_external_research = cached_external_research
        self._actual_portfolio = actual_portfolio
        self._user_watchlist = user_watchlist
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ready_evidence_reader = (
            ready_evidence_reader
            if ready_evidence_reader is not None
            else RecentReferenceReadyEvidenceReader(runtime_context=self._runtime_context)
        )

    def read_g_context(self, request: ProductionReadRequest) -> ProductionReadResult:
        question, instruments = _bounded_inputs(request)
        try:
            resolved = self._runtime_context.resolve(
                AgentRuntimeContextRequest(
                    agent_id="guo_teacher",
                    question=question,
                    tickers=instruments,
                    max_g_events=_MAX_G_ITEMS,
                    now=request.as_of.isoformat() if request.as_of is not None else "",
                )
            )
        except Exception:
            return ProductionReadResult(
                value=_g_context_value([]),
                data_gaps=("g_context_read_failed", "g_context_unavailable"),
            )
        if not isinstance(resolved, AgentRuntimeContextResult) or not isinstance(
            resolved.llm_context, Mapping
        ):
            return ProductionReadResult(
                value=_g_context_value([]),
                data_gaps=("g_context_result_invalid", "g_context_unavailable"),
            )

        gaps: list[str] = []
        _extend_gaps(gaps, resolved.data_gaps)
        raw_items = resolved.llm_context.get("g_context")
        if not isinstance(raw_items, list):
            _extend_gaps(gaps, ("source_boundary_invalid",))
            raw_items = []
        if len(raw_items) > _MAX_G_CANDIDATE_SCAN:
            _extend_gaps(gaps, ("g_context_candidates_truncated",))
        audit_by_ref = _g_audit_by_ref(resolved.audit_context) if request.as_of is not None else {}
        if request.as_of is not None and audit_by_ref is None:
            _extend_gaps(gaps, ("g_context_point_in_time_unavailable",))
            audit_by_ref = {}
        assert audit_by_ref is not None
        # Slice 3b: layered G context (pinned/framework/facts/associations/external_brain)
        return ProductionReadResult(
            value=_g_layered_context_value(
                raw_items=[item for item in raw_items if isinstance(item, Mapping)],
                audit_by_ref=audit_by_ref,
                as_of=request.as_of,
                resolved=resolved,
                question=request.question,
                shared_brain_cards=(
                    match_shared_brain_cards(
                        load_shared_brain_cards(self._knowledge_base_root),
                        request.question,
                    )
                    if self._knowledge_base_root is not None
                    else []
                ),
                gaps=gaps,
            ),
            data_gaps=tuple(gaps),
        )

    def read_teacher_cognition(self, request: ProductionReadRequest) -> ProductionReadResult:
        question, instruments = _bounded_inputs(request)
        value = _teacher_cognition_value(personas=[], patterns=[], traces=[])
        if request.as_of is not None:
            return ProductionReadResult(
                value=value,
                data_gaps=(
                    "teacher_cognition_point_in_time_unavailable",
                    "teacher_cognition_unavailable",
                ),
            )
        if self._cognition_memory is None:
            return ProductionReadResult(
                value=value,
                data_gaps=(
                    "teacher_cognition_reader_unavailable",
                    "teacher_cognition_unavailable",
                ),
            )

        scope = CognitionMemoryScope(
            memory_kind="teacher_cognition",
            teacher_id=_TEACHER_ID,
            source_boundary="teacher_cognition",
        )
        gaps: list[str] = []
        personas = _read_cognition_lane(
            reader=self._cognition_memory,
            scope=scope,
            operation="list_personas",
            payload_key="personas",
            model_type=TeacherPersona,
            projector=_project_persona,
            gaps=gaps,
            question=question,
            instruments=instruments,
        )
        patterns = _read_cognition_lane(
            reader=self._cognition_memory,
            scope=scope,
            operation="list_patterns",
            payload_key="patterns",
            model_type=CognitivePattern,
            projector=_project_pattern,
            gaps=gaps,
            question=question,
            instruments=instruments,
        )
        traces = _read_cognition_lane(
            reader=self._cognition_memory,
            scope=scope,
            operation="list_traces",
            payload_key="traces",
            model_type=ReasoningTrace,
            projector=_project_trace,
            gaps=gaps,
            question=question,
            instruments=instruments,
        )

        if not personas and not patterns and not traces:
            _extend_gaps(gaps, ("teacher_cognition_unavailable",))
        return ProductionReadResult(
            value=_teacher_cognition_value(
                personas=personas,
                patterns=patterns,
                traces=traces,
            ),
            data_gaps=tuple(gaps),
        )

    def read_ready_evidence(self, request: ProductionReadRequest) -> ProductionReadResult:
        _bounded_inputs(request)
        try:
            result = self._ready_evidence_reader.read(request)
        except Exception:
            return ProductionReadResult(
                value={
                    "source_boundary": "ready_evidence",
                    "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
                    "source_trust": SourceTrust.NON_G.value,
                    "items": [],
                },
                data_gaps=(
                    "ready_evidence_reader_failed",
                    "ready_evidence_unavailable",
                ),
            )
        if not isinstance(result, ProductionReadResult):
            return ProductionReadResult(
                value={
                    "source_boundary": "ready_evidence",
                    "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
                    "source_trust": SourceTrust.NON_G.value,
                    "items": [],
                },
                data_gaps=(
                    "ready_evidence_reader_result_invalid",
                    "ready_evidence_unavailable",
                ),
            )
        return result

    def read_market_snapshot(self, request: ProductionReadRequest) -> ProductionReadResult:
        _, instruments = _bounded_inputs(request)
        gaps: list[str] = []
        if not instruments:
            return ProductionReadResult(
                value=_market_snapshot_value([]),
                data_gaps=("market_snapshot_instruments_missing", "market_snapshot_unavailable"),
            )
        if self._on_demand_tactical_context is not None:
            selected = instruments[:_MAX_ON_DEMAND_MARKET_INSTRUMENTS]
            if len(instruments) > len(selected):
                _extend_gaps(gaps, ("market_snapshot_instruments_truncated",))
            # 指数别名 lane 只挂本入口（snapshot-index-support §2.1）：命中项
            # 直接产出指数符号并从 equity 列表剔除，共享解析器保持纯个股，
            # margin/external 零外溢。
            equity_targets, index_names, index_symbols = split_index_aliases(selected)
            equity_symbols, equity_resolved_names, identity_gaps = (
                _resolve_on_demand_instruments(
                    equity_targets,
                    resolver=self._instrument_identity,
                )
            )
            _extend_gaps(gaps, identity_gaps)
            symbols = (*index_symbols, *equity_symbols)
            names = {**index_names, **equity_resolved_names}
            if not symbols:
                return ProductionReadResult(
                    value=_on_demand_market_snapshot_value(None),
                    data_gaps=(*gaps, "market_snapshot_unavailable"),
                )
            as_of = request.as_of or self._clock()
            try:
                context = self._on_demand_tactical_context.read(
                    OnDemandTacticalContextRequest(
                        instruments=symbols,
                        as_of=as_of,
                        deadline_at=request.deadline_at,
                    )
                )
            except Exception:
                return ProductionReadResult(
                    value=_on_demand_market_snapshot_value(None),
                    data_gaps=(
                        *gaps,
                        "market_snapshot_read_failed",
                        "market_snapshot_unavailable",
                    ),
                )
            if not isinstance(context, OnDemandTacticalContext):
                return ProductionReadResult(
                    value=_on_demand_market_snapshot_value(None),
                    data_gaps=(
                        *gaps,
                        "market_snapshot_result_invalid",
                        "market_snapshot_unavailable",
                    ),
                )
            if tuple(item.symbol for item in context.instruments) != symbols:
                return ProductionReadResult(
                    value=_on_demand_market_snapshot_value(None),
                    data_gaps=(
                        *gaps,
                        "market_snapshot_result_invalid",
                        "market_snapshot_unavailable",
                    ),
                )
            _extend_gaps(gaps, context.data_gaps)
            if not context.instruments:
                _extend_gaps(gaps, ("market_snapshot_unavailable",))
            return ProductionReadResult(
                value=_on_demand_market_snapshot_value(context, names=names),
                data_gaps=tuple(gaps),
            )
        if self._market_snapshot is None:
            return ProductionReadResult(
                value=_market_snapshot_value([]),
                data_gaps=("market_snapshot_reader_unavailable", "market_snapshot_unavailable"),
            )
        peek_snapshot = getattr(self._market_snapshot, "peek_snapshot", None)
        if not callable(peek_snapshot):
            return ProductionReadResult(
                value=_market_snapshot_value([]),
                data_gaps=(
                    "market_snapshot_read_only_seam_unavailable",
                    "market_snapshot_unavailable",
                ),
            )

        selected_instruments = instruments[:_MAX_MARKET_INSTRUMENTS]
        if len(instruments) > len(selected_instruments):
            _extend_gaps(gaps, ("market_snapshot_instruments_truncated",))
        items: list[dict[str, object]] = []
        for ticker in selected_instruments:
            try:
                result = peek_snapshot(
                    MarketSnapshotRequest(
                        ticker=ticker,
                        session="realtime",
                        data_mode="cache_only",
                    )
                )
            except Exception:
                _extend_gaps(gaps, (f"market_snapshot_read_failed:{ticker}",))
                continue
            if not isinstance(result, MarketSnapshotResult):
                _extend_gaps(gaps, (f"market_snapshot_result_invalid:{ticker}",))
                continue
            _extend_scoped_gaps(gaps, result.data_gaps, ticker)
            if (
                result.ticker != ticker
                or not result.cache_hit
                or result.cache_status not in {"hit", "stale_fallback"}
                or not isinstance(result.snapshot, dict)
            ):
                if not result.data_gaps:
                    _extend_gaps(gaps, (f"market_data_cache_missing:{ticker}",))
                continue
            items.append(_project_market_snapshot(result))

        if not items:
            _extend_gaps(gaps, ("market_snapshot_unavailable",))
        return ProductionReadResult(
            value=_market_snapshot_value(items),
            data_gaps=tuple(gaps),
        )

    def read_margin_evidence(self, request: ProductionReadRequest) -> ProductionReadResult:
        _, instruments = _bounded_inputs(request)
        gaps: list[str] = []
        selected = instruments[:_MAX_ON_DEMAND_MARKET_INSTRUMENTS]
        if len(instruments) > len(selected):
            _extend_gaps(gaps, ("margin_evidence_instruments_truncated",))
        symbols, names, identity_gaps = _resolve_source_native_instruments(
            selected,
            resolver=self._instrument_identity,
        )
        _extend_gaps(gaps, identity_gaps)
        if self._margin_evidence is None:
            return ProductionReadResult(
                value=_margin_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "margin_evidence_reader_unavailable",
                    "margin_evidence_unavailable",
                ),
            )
        try:
            evidence = self._margin_evidence.read(
                MarginEvidenceRequest(
                    instruments=symbols,
                    as_of=request.as_of or self._clock(),
                    deadline_at=request.deadline_at,
                )
            )
        except Exception:
            return ProductionReadResult(
                value=_margin_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "margin_evidence_read_failed",
                    "margin_evidence_unavailable",
                ),
            )
        if not isinstance(evidence, MarginEvidence):
            return ProductionReadResult(
                value=_margin_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "margin_evidence_result_invalid",
                    "margin_evidence_unavailable",
                ),
            )
        _extend_gaps(gaps, evidence.data_gaps)
        return ProductionReadResult(
            value=_margin_evidence_value(evidence, names=names),
            data_gaps=tuple(gaps),
        )

    def read_external_evidence(self, request: ProductionReadRequest) -> ProductionReadResult:
        _, instruments = _bounded_inputs(request)
        gaps: list[str] = []
        selected = instruments[:_MAX_ON_DEMAND_MARKET_INSTRUMENTS]
        if len(instruments) > len(selected):
            _extend_gaps(gaps, ("external_evidence_instruments_truncated",))
        symbols, names, identity_gaps = _resolve_source_native_instruments(
            selected,
            resolver=self._instrument_identity,
        )
        _extend_gaps(gaps, identity_gaps)
        if not symbols:
            return ProductionReadResult(
                value=_external_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "external_evidence_scope_invalid",
                    "external_evidence_unavailable",
                ),
            )
        if self._external_evidence is None:
            return ProductionReadResult(
                value=_external_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "external_evidence_reader_unavailable",
                    "external_evidence_unavailable",
                ),
            )
        try:
            evidence = self._external_evidence.read(
                OfficialRecordEvidenceRequest(
                    instruments=symbols,
                    as_of=request.as_of or self._clock(),
                    deadline_at=request.deadline_at,
                )
            )
        except Exception:
            return ProductionReadResult(
                value=_external_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "external_evidence_read_failed",
                    "external_evidence_unavailable",
                ),
            )
        if not isinstance(evidence, OfficialRecordEvidence):
            return ProductionReadResult(
                value=_external_evidence_value(None),
                data_gaps=(
                    *gaps,
                    "external_evidence_result_invalid",
                    "external_evidence_unavailable",
                ),
            )
        _extend_gaps(gaps, evidence.data_gaps)
        return ProductionReadResult(
            value=_external_evidence_value(evidence, names=names),
            data_gaps=tuple(gaps),
        )

    def read_market_overview(self, request: ProductionReadRequest) -> ProductionReadResult:
        _, instruments = _bounded_inputs(request)
        if instruments:
            return ProductionReadResult(
                value=_market_overview_value(None),
                data_gaps=(
                    "MARKET_OVERVIEW_SCOPE_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        if self._market_overview is None:
            return ProductionReadResult(
                value=_market_overview_value(None),
                data_gaps=(
                    "MARKET_OVERVIEW_READER_UNAVAILABLE",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        try:
            now = self._clock()
            deadline_at = now + timedelta(seconds=_MARKET_OVERVIEW_BUDGET_SECONDS)
            if request.deadline_at is not None:
                deadline_at = min(deadline_at, request.deadline_at)
            overview = self._market_overview.read(
                AshareMarketOverviewRequest(
                    as_of=request.as_of,
                    deadline_at=deadline_at,
                )
            )
        except Exception:
            return ProductionReadResult(
                value=_market_overview_value(None),
                data_gaps=(
                    "MARKET_OVERVIEW_READ_FAILED",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        if not isinstance(overview, AshareMarketOverviewResult):
            return ProductionReadResult(
                value=_market_overview_value(None),
                data_gaps=(
                    "MARKET_OVERVIEW_RESULT_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        return ProductionReadResult(
            value=overview.to_capability_value(),
            data_gaps=overview.data_gaps,
        )

    def read_cached_external_research(self, request: ProductionReadRequest) -> ProductionReadResult:
        question, instruments = _bounded_inputs(request)
        if self._cached_external_research is None:
            return ProductionReadResult(
                value=_cached_research_value([]),
                data_gaps=(
                    "cached_external_research_reader_unavailable",
                    "cached_external_research_unavailable",
                ),
            )

        gaps: list[str] = []
        targets = instruments or ("",)
        selected_targets = targets[:_MAX_RESEARCH_INSTRUMENTS]
        if len(targets) > len(selected_targets):
            _extend_gaps(gaps, ("cached_external_research_instruments_truncated",))
        items: list[dict[str, object]] = []
        for ticker in selected_targets:
            try:
                result = self._cached_external_research.collect(
                    ExternalResearchContextRequest(
                        ticker=ticker,
                        question=question,
                    )
                )
            except Exception:
                _extend_gaps(
                    gaps,
                    (f"cached_external_research_read_failed:{ticker or 'general'}",),
                )
                continue
            scope = ticker or "general"
            if not isinstance(result, ExternalResearchContextResult):
                _extend_gaps(
                    gaps,
                    (f"cached_external_research_result_invalid:{scope}",),
                )
                continue
            _extend_scoped_gaps(gaps, result.data_gaps, scope)
            if result.status != "ok" or not result.cache_hit:
                if not result.data_gaps:
                    _extend_gaps(gaps, (f"external_research_cache_missing:{scope}",))
                continue
            if not _bounded_text(result.short_answer) and not _bounded_text(
                result.executive_summary
            ):
                _extend_gaps(gaps, (f"external_research_cache_invalid:{scope}",))
                continue
            items.append(_project_cached_research(result, ticker=ticker))

        if not items:
            _extend_gaps(gaps, ("cached_external_research_unavailable",))
        return ProductionReadResult(
            value=_cached_research_value(items),
            data_gaps=tuple(gaps),
        )

    def read_actual_portfolio(self, request: ProductionReadRequest) -> ProductionReadResult:
        _bounded_inputs(request)
        if self._actual_portfolio is None:
            return ProductionReadResult(
                value=_actual_portfolio_value(None),
                data_gaps=("actual_portfolio_reader_unavailable",),
            )
        try:
            result = self._actual_portfolio.read()
        except Exception:
            return ProductionReadResult(
                value=_actual_portfolio_value(None),
                data_gaps=("actual_portfolio_read_failed",),
            )
        if not isinstance(result, ActualAdvisoryPortfolioRead):
            return ProductionReadResult(
                value=_actual_portfolio_value(None),
                data_gaps=("actual_portfolio_result_invalid",),
            )
        value = _actual_portfolio_value(result)
        if result.status is ActualAdvisoryPortfolioStatus.UNKNOWN:
            return ProductionReadResult(
                value=value,
                data_gaps=("actual_portfolio_unavailable",),
            )
        if value["core_usable"] is not True:
            return ProductionReadResult(
                value=value,
                data_gaps=("actual_portfolio_core_incomplete",),
            )
        return ProductionReadResult(value=value)

    def read_user_watchlist(self, request: ProductionReadRequest) -> ProductionReadResult:
        _bounded_inputs(request)
        if self._user_watchlist is None:
            return ProductionReadResult(
                value=_user_watchlist_value(None),
                data_gaps=("user_watchlist_reader_unavailable",),
            )
        try:
            result = self._user_watchlist.list()
        except Exception:
            return ProductionReadResult(
                value=_user_watchlist_value(None),
                data_gaps=("user_watchlist_read_failed",),
            )
        if not isinstance(result, WatchlistRead):
            return ProductionReadResult(
                value=_user_watchlist_value(None),
                data_gaps=("user_watchlist_result_invalid",),
            )
        # 空列表是合法态（用户自选可以为空），不是 gap。
        return ProductionReadResult(value=_user_watchlist_value(result))


def _bounded_inputs(request: ProductionReadRequest) -> tuple[str, tuple[str, ...]]:
    if not isinstance(request, ProductionReadRequest):
        raise ValueError("production_read_request_invalid")
    question = request.question.strip()
    instruments = tuple(dict.fromkeys(item.strip() for item in request.instruments))
    ProductionReadRequest(
        question=question,
        instruments=instruments,
        as_of=request.as_of,
        deadline_at=request.deadline_at,
    )
    return question, instruments


def _resolve_on_demand_instruments(
    instruments: tuple[str, ...],
    *,
    resolver: ConsultationInstrumentIdentityResolver | None,
) -> tuple[tuple[str, ...], dict[str, str | None], tuple[str, ...]]:
    if resolver is None:
        canonical_symbols: list[str] = []
        for value in instruments:
            target = _instrument_ref(value)
            ticker = target.ticker
            if ticker is None:
                continue
            code, separator, venue = ticker.partition(".")
            verified_venue = verified_a_share_equity_venue(code)
            if verified_venue is None or (separator and venue != verified_venue):
                continue
            symbol = f"{code}.{verified_venue}"
            if symbol not in canonical_symbols:
                canonical_symbols.append(symbol)
        canonical_gaps = (
            () if len(canonical_symbols) == len(instruments) else (INSTRUMENT_IDENTITY_UNRESOLVED,)
        )
        return (
            tuple(canonical_symbols),
            dict.fromkeys(canonical_symbols),
            canonical_gaps,
        )
    targets = tuple(_instrument_ref(value) for value in instruments)
    try:
        resolved = resolver.resolve_many(targets)
    except Exception:
        return (), {}, (INSTRUMENT_IDENTITY_UNRESOLVED,)
    resolved_symbols: list[str] = []
    names: dict[str, str | None] = {}
    resolution_gaps: list[str] = []
    for identity in resolved:
        _extend_gaps(resolution_gaps, identity.data_gaps)
        market_symbol = identity.market_symbol
        if identity.status != "RESOLVED" or market_symbol is None or market_symbol in names:
            continue
        resolved_symbols.append(market_symbol)
        names[market_symbol] = identity.semantic_ref.name
    if len(resolved_symbols) != len(targets):
        _extend_gaps(resolution_gaps, (INSTRUMENT_IDENTITY_UNRESOLVED,))
    return tuple(resolved_symbols), names, tuple(resolution_gaps)


def _resolve_source_native_instruments(
    instruments: tuple[str, ...],
    *,
    resolver: ConsultationInstrumentIdentityResolver | None,
) -> tuple[tuple[str, ...], dict[str, str | None], tuple[str, ...]]:
    bj_symbols: list[str] = []
    resolvable: list[str] = []
    for instrument in instruments:
        normalized = instrument.upper()
        code, separator, venue = normalized.partition(".")
        if (
            len(code) == 6
            and code.isascii()
            and code.isdigit()
            and separator == "."
            and venue == "BJ"
        ):
            if normalized not in bj_symbols:
                bj_symbols.append(normalized)
            continue
        resolvable.append(instrument)
    if resolvable:
        symbols, names, gaps = _resolve_on_demand_instruments(
            tuple(resolvable),
            resolver=resolver,
        )
    else:
        symbols, names, gaps = (), {}, ()
    for symbol in bj_symbols:
        names[symbol] = None
    return tuple(dict.fromkeys((*symbols, *bj_symbols))), names, gaps


def _instrument_ref(value: str) -> InstrumentRef:
    upper = value.upper()
    code, separator, venue = upper.partition(".")
    ticker_shaped = (
        len(code) == 6
        and code.isascii()
        and code.isdigit()
        and (not separator or venue in {"SH", "SZ", "BJ"})
    )
    return InstrumentRef(ticker=upper) if ticker_shaped else InstrumentRef(name=value)


def _read_cognition_lane(
    *,
    reader: _CognitionMemoryReader,
    scope: CognitionMemoryScope,
    operation: str,
    payload_key: str,
    model_type: type[_TeacherScopedItem],
    projector: Callable[[_TeacherScopedItem], dict[str, object]],
    gaps: list[str],
    question: str,
    instruments: tuple[str, ...],
) -> list[dict[str, object]]:
    try:
        result = reader.handle(CognitionMemoryRequest(operation=operation, scope=scope))
    except Exception:
        _extend_gaps(gaps, (f"teacher_cognition_{payload_key}_read_failed",))
        return []
    if not isinstance(result, CognitionMemoryResult) or not isinstance(result.payload, Mapping):
        _extend_gaps(gaps, (f"teacher_cognition_{payload_key}_result_invalid",))
        return []
    _extend_gaps(gaps, result.data_gaps)
    if result.status != "success" or result.source_boundary != "teacher_cognition":
        _extend_gaps(gaps, ("source_boundary_invalid",))
        return []
    raw_items = result.payload.get(payload_key)
    if not isinstance(raw_items, list):
        _extend_gaps(gaps, (f"teacher_cognition_{payload_key}_invalid",))
        return []
    if len(raw_items) > _MAX_COGNITION_SELECTION_SCAN:
        _extend_gaps(gaps, (f"teacher_cognition_{payload_key}_candidates_truncated",))

    indexed_items = list(enumerate(raw_items[-_MAX_COGNITION_SELECTION_SCAN:]))
    indexed_items.sort(
        key=lambda item: _cognition_selection_rank(
            item[1],
            question=question,
            instruments=instruments,
            position=item[0],
        ),
        reverse=True,
    )
    projected: list[dict[str, object]] = []
    for _position, raw_item in indexed_items:
        if not isinstance(raw_item, model_type):
            _extend_gaps(gaps, ("source_boundary_invalid",))
            continue
        if raw_item.teacher_id != _TEACHER_ID:
            _extend_gaps(gaps, ("source_boundary_invalid",))
            continue
        projected_item = projector(raw_item)
        if not _bounded_text(projected_item.get("source_ref"), _MAX_SHORT_TEXT_CHARS):
            _extend_gaps(gaps, ("source_boundary_invalid",))
            continue
        projected.append(projected_item)
        if len(projected) == _MAX_COGNITION_ITEMS_PER_KIND:
            break
    return projected


def _cognition_selection_rank(
    item: object,
    *,
    question: str,
    instruments: tuple[str, ...],
    position: int,
) -> tuple[int, float, int]:
    relevance_values: list[str] = []
    updated_at = ""
    if isinstance(item, TeacherPersona):
        relevance_values = [item.display_name, *item.explicit_rules]
        updated_at = item.last_built_at
    elif isinstance(item, CognitivePattern):
        relevance_values = [
            item.name,
            *item.trigger_conditions,
            *item.typical_variables,
        ]
        updated_at = item.updated_at
    elif isinstance(item, ReasoningTrace):
        relevance_values = [
            item.topic,
            *item.companies,
            *item.observed_variables,
        ]

    normalized_question = question.casefold()
    normalized_values = [value.strip().casefold() for value in relevance_values if value.strip()]
    relevance = sum(
        min(len(value), 32)
        for value in normalized_values
        if len(value) >= 2 and value in normalized_question
    )
    searchable = "\n".join(normalized_values)
    relevance += 64 * sum(1 for instrument in instruments if instrument.casefold() in searchable)
    parsed = _parse_point_in_time(updated_at) if updated_at else None
    timestamp = parsed.timestamp() if parsed is not None else float("-inf")
    return relevance, timestamp, position


def _g_context_value(
    items: list[dict[str, object]],
    *,
    freshness: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "source_boundary": "g_context",
        "source_kind": SourceKind.G.value,
        "source_trust": SourceTrust.FIN_TRUSTED_G.value,
        "items": items,
    }
    if freshness is not None:
        value["generation"] = freshness["canonical_sha256"]
        value["freshness"] = freshness
    return value


# ── Slice 3b: layered G context ──────────────────────────────────────────────

_MAX_FRAMEWORK_ITEMS = 3
_MAX_ASSOCIATION_ITEMS = 5
_MAX_EXTERNAL_BRAIN_ITEMS = 3


def _g_layered_context_value(
    *,
    raw_items: list[Mapping[object, object]],
    audit_by_ref: dict[str, Mapping[object, object]],
    as_of: datetime | None,
    resolved: AgentRuntimeContextResult,
    question: str,
    shared_brain_cards: list[dict[str, object]] = (),
    gaps: list[str],
) -> dict[str, object]:
    """Build the layered G context return for Slice 3b on-demand retrieval.

    Layers (in Agent's natural reasoning order):
      pinned         — explicitly pinned articles (user action, highest priority)
      framework      — methodology rules + cognition patterns (how to analyze)
      facts          — semantically matched G items (what the teacher said)
      associations   — knowledge-graph associations (what else is related)
      external_brain — teacher cognition + external references (non-G context)
      attestation    — retrieval metadata (auditable)
    """
    llm = resolved.llm_context if isinstance(resolved.llm_context, Mapping) else {}

    # ── pinned ──
    pinned_items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        bucket = str(raw.get("source_bucket", ""))
        if bucket != "pinned_source":
            continue
        source_ref = _bounded_text(raw.get("source_ref"), _MAX_SHORT_TEXT_CHARS)
        if not source_ref:
            continue
        if as_of is not None and not _g_item_available_at_point_in_time(
            raw, audit_by_ref.get(source_ref), as_of=as_of
        ):
            continue
        pinned_items.append(_project_g_item(raw, source_ref=source_ref, bucket=bucket))
    pinned_gaps: list[str] = []
    if not pinned_items:
        pinned_gaps.append("pinned_source_empty")

    # ── framework ──
    framework: dict[str, object] = {}
    framework_gaps: list[str] = []
    methodology = llm.get("methodology_projection")
    methodology_rules: list[dict[str, object]] = []
    if isinstance(methodology, Mapping) and methodology.get("groups"):
        for group in methodology.get("groups", []):
            if not isinstance(group, Mapping):
                continue
            for rule in group.get("rules", []):
                if not isinstance(rule, Mapping):
                    continue
                ref = str(rule.get("source_ref") or "")
                if not ref:
                    continue
                methodology_rules.append(
                    {
                        "topic": str(group.get("topic", "")),
                        "rule": str(rule.get("rule", "")),
                        "source_ref": ref,
                        "published_at": str(
                            rule.get("published_at") or rule.get("available_at") or ""
                        ),
                        "teacher_quote": str(rule.get("teacher_quote") or ""),
                        "apprentice_interpretation": str(
                            rule.get("apprentice_interpretation") or ""
                        ),
                    }
                )
                if len(methodology_rules) >= _MAX_FRAMEWORK_ITEMS:
                    break
            if len(methodology_rules) >= _MAX_FRAMEWORK_ITEMS:
                break
    if methodology_rules:
        framework["methodology_rules"] = methodology_rules
    else:
        framework_gaps.append("framework_methodology_unavailable")

    # cognition_mainline_projection → framework cognitive patterns
    cognition_proj = llm.get("cognition_mainline_projection")
    cognition_items: list[dict[str, object]] = []
    if isinstance(cognition_proj, Mapping):
        raw_cog = cognition_proj.get("items")
        if isinstance(raw_cog, list):
            for item in raw_cog:
                if not isinstance(item, Mapping):
                    continue
                cognition_items.append(
                    {
                        "source_ref": str(item.get("source_ref") or ""),
                        "title": str(item.get("title") or ""),
                        "guidance_brief": _bounded_text(
                            item.get("guidance_brief"), _MAX_TEXT_CHARS
                        ),
                        "source_bucket": str(
                            item.get("source_bucket") or "cognition_mainline_projection"
                        ),
                        "published_at": str(item.get("published_at") or ""),
                    }
                )
                # 黑话译注（NOW #14 下批）：readmodel 投影附加字段有界透传。
                _jargon = _bounded_jargon_notes(item.get("jargon_notes"))
                if _jargon:
                    cognition_items[-1]["jargon_notes"] = _jargon
                if len(cognition_items) >= _MAX_FRAMEWORK_ITEMS:
                    break
    if cognition_items:
        framework["cognitive_patterns"] = cognition_items

    if not framework:
        framework_gaps.append("framework_unavailable")

    # ── facts ──
    facts_items: list[dict[str, object]] = []
    facts_gaps: list[str] = []
    for raw in raw_items[:_MAX_G_CANDIDATE_SCAN]:
        if not isinstance(raw, Mapping):
            facts_gaps.append("source_boundary_invalid")
            continue
        bucket = _bounded_text(raw.get("source_bucket"), _MAX_SHORT_TEXT_CHARS)
        if bucket == "recent_reference":
            continue
        if bucket not in _STRICT_G_BUCKETS:
            facts_gaps.append("source_boundary_invalid")
            continue
        source_ref = _bounded_text(raw.get("source_ref"), _MAX_SHORT_TEXT_CHARS)
        if not source_ref:
            facts_gaps.append("source_boundary_invalid")
            continue
        if as_of is not None and not _g_item_available_at_point_in_time(
            raw, audit_by_ref.get(source_ref), as_of=as_of
        ):
            facts_gaps.append("g_context_point_in_time_unavailable")
            continue
        facts_items.append(_project_g_item(raw, source_ref=source_ref, bucket=bucket))
        if len(facts_items) == _MAX_G_ITEMS:
            if len(raw_items) > len(facts_items):
                facts_gaps.append("g_context_items_truncated")
            break
    if not facts_items:
        facts_gaps.append("g_context_unavailable")
    working_set_freshness = _g_working_set_freshness(resolved.audit_context)
    facts_value: dict[str, object] = {
        "source_boundary": "g_context",
        "source_kind": SourceKind.G.value,
        "source_trust": SourceTrust.FIN_TRUSTED_G.value,
        "items": facts_items,
        "data_gaps": facts_gaps,
    }
    if working_set_freshness is not None:
        facts_value["generation"] = working_set_freshness["canonical_sha256"]
        facts_value["freshness"] = working_set_freshness

    # ── associations ──
    associations: dict[str, object] = {}
    association_gaps: list[str] = []
    # mainline_projection → mainline summary
    mainline = llm.get("mainline_projection")
    if isinstance(mainline, Mapping) and mainline.get("themes"):
        themes: list[dict[str, object]] = []
        for theme in mainline.get("themes", []):
            if not isinstance(theme, Mapping):
                continue
            theses: list[dict[str, object]] = []
            for thesis in theme.get("theses", []):
                if not isinstance(thesis, Mapping):
                    continue
                theses.append(
                    {
                        "title": str(thesis.get("title") or ""),
                        "source_ref": str(thesis.get("source_ref") or ""),
                        "published_at": str(thesis.get("published_at") or ""),
                        "thesis_heads": [str(h) for h in thesis.get("thesis_heads", []) if h],
                    }
                )
            if theses:
                themes.append({"theme": str(theme.get("theme", "")), "theses": theses})
        if themes:
            associations["mainline_themes"] = themes[:_MAX_ASSOCIATION_ITEMS]
    # bound_article_ids → related articles
    if working_set_freshness is not None:
        bound = working_set_freshness.get("bound_article_ids", [])
        if isinstance(bound, list) and bound:
            associations["bound_article_ids"] = [str(aid) for aid in bound if isinstance(aid, str)][
                :_MAX_ASSOCIATION_ITEMS
            ]
    if not associations:
        association_gaps.append("associations_unavailable")

    # ── external_brain ──
    external_brain: dict[str, object] = {}
    external_gaps: list[str] = []
    # 非 G 外部关联（owner 2026-09-02：补做，但侧重点=宏观/外围，不只低优先级）
    # 只收宏观/市场/政策/海外/商品类 reference 条目；个股/行业点评不进这层，
    # 由 read_article_search / read_article 承担。数量小、排 G 主线之后。
    macro_reference_items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("source_bucket", "")) != "recent_reference":
            continue
        classification = str(raw.get("source_classification", "")).strip()
        title = str(raw.get("title") or "")
        is_macro = classification in _EXTERNAL_BRAIN_MACRO_CLASSIFICATIONS or any(
            keyword in title for keyword in _EXTERNAL_BRAIN_MACRO_KEYWORDS
        )
        if not is_macro:
            continue
        source_ref = _bounded_text(raw.get("source_ref"), _MAX_SHORT_TEXT_CHARS)
        if not source_ref:
            continue
        macro_reference_items.append(
            {
                "source_ref": source_ref,
                "title": _bounded_text(title, _MAX_SHORT_TEXT_CHARS),
                "source_bucket": "macro_reference",
                "published_at": str(raw.get("published_at") or raw.get("available_at") or ""),
            }
        )
        if len(macro_reference_items) >= _MAX_EXTERNAL_BRAIN_ITEMS:
            break
    if macro_reference_items:
        external_brain["macro_reference_items"] = macro_reference_items
    if shared_brain_cards:
        external_brain["shared_brain_cards"] = list(shared_brain_cards)[:_MAX_EXTERNAL_BRAIN_ITEMS]
    if not macro_reference_items and not shared_brain_cards and macro_search_signal(question):
        external_brain["search_needed"] = True
        external_brain["suggested_queries"] = suggested_queries(question)
    # cognition_mainline_projection items (if any remain after framework extraction)
    if cognition_proj is not None and isinstance(cognition_proj, Mapping):
        eb_items: list[dict[str, object]] = []
        raw_eb = cognition_proj.get("items")
        if isinstance(raw_eb, list):
            for item in raw_eb:
                if not isinstance(item, Mapping):
                    continue
                eb_items.append(
                    {
                        "source_ref": str(item.get("source_ref") or ""),
                        "title": str(item.get("title") or ""),
                        "source_bucket": str(item.get("source_bucket") or "cognition_mainline"),
                        "published_at": str(item.get("published_at") or ""),
                    }
                )
                if len(eb_items) >= _MAX_EXTERNAL_BRAIN_ITEMS:
                    break
        if eb_items:
            external_brain["cognition_items"] = eb_items
    if not external_brain:
        external_gaps.append("external_brain_unavailable")

    # ── attestation ──
    quality_flags = getattr(resolved, "quality_flags", None) or {}
    attestation: dict[str, object] = {
        "schema_version": "fin.g-layered-context/v1",
        "retrieval_mode": "layered",
        "layer_counts": {
            "pinned": len(pinned_items),
            "framework": len(methodology_rules) + len(cognition_items),
            "facts": len(facts_items),
            "associations": len(associations),
            "external_brain": len(external_brain),
        },
        "data_gaps": list(dict.fromkeys(gaps)),
        "quality": {
            "pinned_injected": bool(quality_flags.get("pinned_injected", False)),
            "pinned_candidate_seen": bool(quality_flags.get("pinned_candidate_seen", False)),
            "pinned_layer_count": len(pinned_items),
            "pinned_data_gaps": list(pinned_gaps),
        },
    }
    # 消费探针（设计门 g-mainline-growth-v1 部件5）：有值才加键，attestation
    # 其余消费方不受影响；server 层 _trace_summary 据此并入 trace 行。
    consumption_audit = quality_flags.get("cognition_mainline_consumption")
    if isinstance(consumption_audit, dict):
        attestation["quality"]["cognition_mainline_consumption"] = consumption_audit

    # ── assemble ──
    result: dict[str, object] = {
        "pinned": {
            "source_trust": SourceTrust.FIN_TRUSTED_G.value,
            "items": pinned_items,
            "data_gaps": pinned_gaps,
        },
        "framework": {
            **framework,
            "data_gaps": framework_gaps,
        },
        "facts": facts_value,
        "associations": {
            **associations,
            "data_gaps": association_gaps,
        },
        "external_brain": {
            **external_brain,
            "source_trust": SourceTrust.NON_G.value,
            "not_g_source": True,
            "data_gaps": external_gaps,
        },
        "attestation": attestation,
    }
    return result


def _g_working_set_freshness(audit_context: object) -> dict[str, object] | None:
    if not isinstance(audit_context, Mapping):
        return None
    fresh_g = audit_context.get("fresh_g")
    if not isinstance(fresh_g, Mapping):
        return None
    raw = fresh_g.get("working_set_freshness")
    if not isinstance(raw, Mapping):
        return None
    status = _bounded_text(raw.get("status"), _MAX_SHORT_TEXT_CHARS)
    canonical_sha256 = _bounded_text(
        raw.get("canonical_sha256"),
        _MAX_SHORT_TEXT_CHARS,
    )
    evaluated_at = _bounded_text(raw.get("evaluated_at"), _MAX_SHORT_TEXT_CHARS)
    if (
        status not in {"READY", "PARTIAL", "STALE", "MISSING"}
        or len(canonical_sha256) != 64
        or any(character not in "0123456789abcdef" for character in canonical_sha256)
        or _parse_point_in_time(evaluated_at) is None
    ):
        return None
    bound_article_ids = _bounded_strings(raw.get("bound_article_ids"))[:128]
    data_gaps = _bounded_strings(raw.get("data_gaps"))[:32]
    return {
        "status": status,
        "canonical_sha256": canonical_sha256,
        "evaluated_at": evaluated_at,
        "bound_article_ids": bound_article_ids,
        "data_gaps": data_gaps,
    }


def _teacher_cognition_value(
    *,
    personas: list[dict[str, object]],
    patterns: list[dict[str, object]],
    traces: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "source_boundary": "teacher_cognition",
        "source_kind": SourceKind.G.value,
        "source_trust": SourceTrust.FIN_TRUSTED_G.value,
        "personas": personas,
        "patterns": patterns,
        "traces": traces,
    }


def _market_snapshot_value(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_boundary": "market_snapshot_cache",
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
        "items": items,
    }


def _on_demand_market_snapshot_value(
    context: OnDemandTacticalContext | None,
    *,
    names: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    if context is None:
        return {
            "schema_version": "fin.on-demand-tactical-context/v1",
            "source_boundary": "a_share_on_demand_tactical_context",
            "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
            "source_trust": SourceTrust.NON_G.value,
            "status": "UNKNOWN",
            "as_of": None,
            "valid_until": None,
            "session_phase": "UNKNOWN",
            "instruments": [],
        }
    projection = context.to_agent_dict()
    raw_instruments = projection.get("instruments")
    if isinstance(raw_instruments, list):
        projection["instruments"] = [
            {
                **item,
                "name": (
                    names.get(str(item.get("symbol")))
                    if names is not None and isinstance(item, Mapping)
                    else None
                ),
            }
            for item in raw_instruments
            if isinstance(item, Mapping)
        ]
    return {
        "schema_version": "fin.on-demand-tactical-context/v1",
        "source_boundary": "a_share_on_demand_tactical_context",
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
        **projection,
    }


def _margin_evidence_value(
    evidence: MarginEvidence | None,
    *,
    names: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    if evidence is None:
        return {
            "schema_version": "fin.margin-evidence/v1",
            "source_boundary": "a_share_margin_evidence",
            "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
            "source_trust": SourceTrust.NON_G.value,
            "status": "UNKNOWN",
            "as_of": None,
            "valid_until": None,
            "account": {
                "status": "UNKNOWN",
                "account_snapshot_ref": None,
                "confirmed_at": None,
                "margin_debt": None,
                "net_assets": None,
                "leverage_ratio": None,
                "risk_increase_allowed": False,
                "data_gaps": [],
            },
            "markets": [],
            "instruments": [],
            "data_gaps": [],
        }
    projection = evidence.to_agent_dict()
    raw_instruments = projection.get("instruments")
    if isinstance(raw_instruments, list):
        projection["instruments"] = [
            {
                **item,
                "name": (
                    names.get(str(item.get("symbol")))
                    if names is not None and isinstance(item, Mapping)
                    else None
                ),
            }
            for item in raw_instruments
            if isinstance(item, Mapping)
        ]
    return projection


def _external_evidence_value(
    evidence: OfficialRecordEvidence | None,
    *,
    names: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    if evidence is None:
        return {
            "schema_version": "fin.official-record-evidence/v1",
            "source_boundary": "a_share_official_record_evidence",
            "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
            "source_trust": SourceTrust.NON_G.value,
            "status": "UNKNOWN",
            "as_of": None,
            "valid_until": None,
            "instruments": [],
            "data_gaps": [],
        }
    projection = evidence.to_agent_dict()
    raw_instruments = projection.get("instruments")
    if isinstance(raw_instruments, list):
        projection["instruments"] = [
            {
                **item,
                "name": (
                    names.get(str(item.get("symbol")))
                    if names is not None and isinstance(item, Mapping)
                    else None
                ),
            }
            for item in raw_instruments
            if isinstance(item, Mapping)
        ]
    return projection


def _market_overview_value(
    overview: AshareMarketOverviewResult | None,
) -> dict[str, object]:
    if overview is not None:
        return overview.to_capability_value()
    return {
        "schema_version": "fin.a-share-market-overview/v1",
        "source_boundary": "a_share_current_market_overview",
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
        "provider": "eastmoney",
        "status": "UNKNOWN",
        "queried_at": None,
        "effective_trade_date": None,
        "observation_mode": "UNKNOWN",
        "session_phase": "UNKNOWN",
        "provider_mode": "EASTMONEY_DELAYED_REFERENCE",
        "reference_only": True,
        "realtime_eligible": False,
        "provider_updated_at": None,
        "provider_observation_age_seconds": None,
        "breadth": None,
        "coverage": {"venues": ["SSE", "SZSE"], "bj_included": False},
        "major_indices": [],
        "industry": {"leaders_by_change": [], "leaders_by_turnover": []},
        "concept": {"leaders_by_change": [], "leaders_by_turnover": []},
        "turnover_leaders": [],
        "coverage_diagnostics": [],
        "limitations": ["MARKET_OVERVIEW_UNAVAILABLE"],
    }


def _cached_research_value(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_boundary": "cached_external_research",
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
        "items": items,
    }


def _user_watchlist_value(result: WatchlistRead | None) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    revision = ""
    as_of = ""
    if result is not None:
        entries = [
            {
                "market_symbol": entry.market_symbol,
                "name": entry.name,
                "added_at": entry.added_at,
                "provenance": entry.provenance,
                "tags": list(entry.tags),
            }
            for entry in result.entries
        ]
        revision = result.revision
        as_of = result.as_of
    return {
        # 设计门 F1：server 只投影 value/data_gaps，user-context 语义由 value 承载。
        "semantics": (
            "user context / focus of attention; never investment evidence; "
            "never a trade instruction"
        ),
        "revision": revision,
        "as_of": as_of,
        "entry_count": len(entries),
        "entries": entries,
    }


def _actual_portfolio_value(
    result: ActualAdvisoryPortfolioRead | None,
) -> dict[str, object]:
    if result is None:
        status = ActualAdvisoryPortfolioStatus.UNKNOWN
        reasons: tuple[ActualAdvisoryPortfolioReason, ...] = ()
        snapshot = None
    else:
        status = result.status
        reasons = result.reason_codes
        snapshot = result.snapshot

    projected_snapshot: dict[str, object] | None = None
    core_usable = False
    if snapshot is not None:
        projected_snapshot = snapshot.to_safe_dict()
        # owner 拍板 2026-08-31：无两融且永远不会有——两融项从用户面投影删除
        # （store schema 不动；margin_debt 恒 0 由快照承载）。
        projected_snapshot.pop("margin_debt", None)
        projected_snapshot.pop("margin_debt_status", None)
        projected_snapshot["revision"] = snapshot.revision
        market_values = [position.market_value for position in snapshot.positions]
        total_market_value = (
            sum((value for value in market_values if value is not None), Decimal("0"))
            if all(value is not None for value in market_values)
            else None
        )
        position_ratio = (
            total_market_value / snapshot.net_assets
            if total_market_value is not None
            and snapshot.net_assets is not None
            and snapshot.net_assets > 0
            else None
        )
        projected_snapshot.update(
            {
                "total_market_value": _portfolio_decimal_text(total_market_value),
                "total_market_value_derived": total_market_value is not None,
                "position_ratio": _portfolio_decimal_text(position_ratio),
                "position_ratio_derived": position_ratio is not None,
            }
        )
        core_usable = bool(
            snapshot.net_assets is not None
            and snapshot.available_cash is not None
            and total_market_value is not None
            and position_ratio is not None
            and all(position.average_cost is not None for position in snapshot.positions)
        )

    return {
        "schema_version": "fin.actual-portfolio-capability/v1",
        "status": status.value,
        "source_boundary": "actual_advisory_portfolio",
        "source_kind": SourceKind.USER_PORTFOLIO.value,
        "source_trust": SourceTrust.NON_G.value,
        "core_usable": core_usable,
        "reason_codes": [reason.value for reason in reasons],
        "snapshot": projected_snapshot,
    }


def _portfolio_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001")).normalize(), "f")


def _project_g_item(
    raw: Mapping[object, object],
    *,
    source_ref: str,
    bucket: str,
) -> dict[str, object]:
    item: dict[str, object] = {
        "source_ref": source_ref,
        "source_kind": SourceKind.G.value,
        "source_trust": SourceTrust.FIN_TRUSTED_G.value,
        "source_bucket": bucket,
    }
    _add_text(item, "title", raw.get("title"), _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "guidance_brief", raw.get("guidance_brief"), _MAX_TEXT_CHARS)
    _add_string_list(item, "why_available", raw.get("why_available"))
    _add_text(item, "usage_boundary", raw.get("usage_boundary"), _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "published_at", raw.get("published_at"), _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "available_at", raw.get("available_at"), _MAX_SHORT_TEXT_CHARS)
    for key in ("tickers", "companies", "theme_clusters", "keywords"):
        _add_string_list(item, key, raw.get(key))
    source_refs = _bounded_source_refs(raw.get("source_refs"))
    if source_refs:
        item["source_refs"] = list(source_refs)
    jargon_notes = _bounded_jargon_notes(raw.get("jargon_notes"))
    if jargon_notes:
        item["jargon_notes"] = jargon_notes
    return item


def _g_audit_by_ref(value: object) -> dict[str, Mapping[object, object]] | None:
    if not isinstance(value, Mapping):
        return None
    selected = value.get("selected")
    if not isinstance(selected, list) or len(selected) > _MAX_G_CANDIDATE_SCAN:
        return None
    indexed: dict[str, Mapping[object, object]] = {}
    for raw in selected:
        if not isinstance(raw, Mapping):
            return None
        source_ref = _bounded_text(
            raw.get("article_id") or raw.get("pinned_id"),
            _MAX_SHORT_TEXT_CHARS,
        )
        if not source_ref:
            continue
        if source_ref in indexed:
            return None
        indexed[source_ref] = raw
    return indexed


def _g_item_available_at_point_in_time(
    raw: Mapping[object, object],
    audit: Mapping[object, object] | None,
    *,
    as_of: datetime,
) -> bool:
    if audit is None or audit.get("source_bucket") != raw.get("source_bucket"):
        return False
    compact_source_refs = _bounded_source_refs(raw.get("source_refs"))
    audit_source_refs = _bounded_source_refs(audit.get("source_refs"))
    if (
        compact_source_refs is None
        or audit_source_refs is None
        or compact_source_refs != audit_source_refs
    ):
        return False
    published_at = _bounded_text(raw.get("published_at"), _MAX_SHORT_TEXT_CHARS)
    available_at = _bounded_text(raw.get("available_at"), _MAX_SHORT_TEXT_CHARS)
    if (
        not published_at
        or not available_at
        or published_at != _bounded_text(audit.get("published_at"), _MAX_SHORT_TEXT_CHARS)
        or available_at != _bounded_text(audit.get("available_at"), _MAX_SHORT_TEXT_CHARS)
    ):
        return False
    published = _parse_point_in_time(published_at)
    available = _parse_point_in_time(available_at)
    return bool(
        published is not None
        and available is not None
        and available >= published
        and published <= as_of
        and available <= as_of
    )


def _parse_point_in_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _project_persona(persona: TeacherPersona) -> dict[str, object]:
    item = _trusted_g_item(persona.persona_id)
    _add_text(item, "display_name", persona.display_name, _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "active_version", persona.active_version, _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "style_summary", persona.style_summary, _MAX_TEXT_CHARS)
    _add_string_list(item, "core_pattern_ids", persona.core_pattern_ids)
    _add_string_list(item, "explicit_rules", persona.explicit_rules)
    _add_string_list(item, "known_blind_spots", persona.known_blind_spots)
    _add_text(item, "last_built_at", persona.last_built_at, _MAX_SHORT_TEXT_CHARS)
    return item


def _project_pattern(pattern: CognitivePattern) -> dict[str, object]:
    item = _trusted_g_item(pattern.pattern_id)
    _add_text(item, "name", pattern.name, _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "description", pattern.description, _MAX_TEXT_CHARS)
    _add_string_list(item, "trigger_conditions", pattern.trigger_conditions)
    _add_string_list(item, "typical_variables", pattern.typical_variables)
    _add_text(item, "typical_reasoning_shape", pattern.typical_reasoning_shape, _MAX_TEXT_CHARS)
    _add_string_list(item, "supporting_trace_ids", pattern.supporting_trace_ids)
    _add_string_list(item, "counterexamples", pattern.counterexamples)
    item["confidence"] = _bounded_number(pattern.confidence)
    _add_text(item, "updated_at", pattern.updated_at, _MAX_SHORT_TEXT_CHARS)
    return item


def _project_trace(trace: ReasoningTrace) -> dict[str, object]:
    item = _trusted_g_item(trace.trace_id)
    _add_text(item, "source_evidence_id", trace.source_evidence_id, _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "topic", trace.topic, _MAX_SHORT_TEXT_CHARS)
    _add_string_list(item, "companies", trace.companies)
    _add_string_list(item, "premises", trace.premises)
    _add_string_list(item, "observed_variables", trace.observed_variables)
    _add_string_list(item, "inferred_relationships", trace.inferred_relationships)
    _add_text(item, "conclusion", trace.conclusion, _MAX_TEXT_CHARS)
    _add_text(item, "stance", trace.stance, _MAX_SHORT_TEXT_CHARS)
    _add_text(item, "time_horizon", trace.time_horizon, _MAX_SHORT_TEXT_CHARS)
    _add_string_list(item, "risk_boundaries", trace.risk_boundaries)
    _add_string_list(item, "invalidation_conditions", trace.invalidation_conditions)
    item["extraction_confidence"] = _bounded_number(trace.extraction_confidence)
    return item


def _trusted_g_item(source_ref: str) -> dict[str, object]:
    return {
        "source_ref": _bounded_text(source_ref, _MAX_SHORT_TEXT_CHARS),
        "source_kind": SourceKind.G.value,
        "source_trust": SourceTrust.FIN_TRUSTED_G.value,
    }


def _project_market_snapshot(result: MarketSnapshotResult) -> dict[str, object]:
    snapshot = result.snapshot
    cache_session = _bounded_text(result.cache_session, _MAX_CACHE_SESSION_CHARS)
    item: dict[str, object] = {
        "source_ref": _bounded_text(
            f"market_snapshot:{result.ticker}:{cache_session}",
            _MAX_SHORT_TEXT_CHARS,
        ),
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
        "ticker": result.ticker,
    }
    for key in _MARKET_SCALAR_FIELDS:
        if key in snapshot:
            item[key] = _bounded_scalar(snapshot[key])
    item["cache_status"] = result.cache_status
    item["cache_hit"] = result.cache_hit
    item["cache_session"] = cache_session
    item["data_freshness"] = _project_data_freshness(result.data_freshness)
    _add_text(item, "valuation_narrative", snapshot.get("valuation_narrative"), _MAX_TEXT_CHARS)
    signal_summary = _project_signal_summary(snapshot.get("signal_summary"))
    if signal_summary:
        item["signal_summary"] = signal_summary
    return item


def _project_cached_research(
    result: ExternalResearchContextResult,
    *,
    ticker: str,
) -> dict[str, object]:
    scope = ticker or "general"
    item: dict[str, object] = {
        "source_ref": f"external_research_cache:{scope}",
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
    }
    if ticker:
        item["ticker"] = ticker
    _add_text(item, "short_answer", result.short_answer, _MAX_TEXT_CHARS)
    _add_text(item, "executive_summary", result.executive_summary, _MAX_TEXT_CHARS)
    item["reported_confidence"] = _bounded_number(result.confidence)
    item["confidence_boost_allowed"] = False
    coverage: dict[str, object] = {}
    if isinstance(result.coverage, Mapping):
        for key in _COVERAGE_FIELDS:
            if key in result.coverage:
                coverage[key] = _bounded_scalar(result.coverage[key])
    coverage.update(
        {
            "can_support_agent_view": False,
            "can_raise_confidence": False,
            "can_drive_recommendation": False,
        }
    )
    item["coverage"] = coverage
    item["caveats"] = _bounded_strings(result.caveats)
    item["cache_hit"] = True
    return item


def _project_data_freshness(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    projected: dict[str, object] = {}
    for key in (
        "snapshot_at",
        "financial_time_series",
        "margin_detail",
        "northbound_detail",
    ):
        if key in value:
            projected[key] = _bounded_scalar(value[key])
    return projected


def _project_signal_summary(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, object]] = []
    for raw in value[:_MAX_LIST_ITEMS]:
        if not isinstance(raw, Mapping):
            continue
        signal: dict[str, object] = {}
        for key in ("key", "label", "value", "trend", "strength", "direction"):
            if key in raw:
                signal[key] = _bounded_scalar(raw[key])
        if signal:
            projected.append(signal)
    return projected


def _add_text(
    destination: dict[str, object],
    key: str,
    value: object,
    limit: int,
) -> None:
    text = _bounded_text(value, limit)
    if text:
        destination[key] = text


def _add_string_list(destination: dict[str, object], key: str, value: object) -> None:
    strings = _bounded_strings(value)
    if strings:
        destination[key] = strings


def _bounded_jargon_notes(value: object) -> list[dict[str, object]]:
    """黑话译注窄契约的有界投影;缺失/畸形 → 空(无命中不附加)。"""
    if not isinstance(value, (list, tuple)):
        return []
    notes: list[dict[str, object]] = []
    for raw in value[:_MAX_LIST_CANDIDATE_SCAN]:
        if not isinstance(raw, Mapping):
            continue
        term = _bounded_text(raw.get("term"), _MAX_SHORT_TEXT_CHARS)
        meaning = _bounded_text(raw.get("meaning"), _MAX_TEXT_CHARS)
        if not term or not meaning:
            continue
        notes.append(
            {
                "term": term,
                "meaning": meaning,
                "confidence": _bounded_text(raw.get("confidence"), _MAX_SHORT_TEXT_CHARS),
            }
        )
        if len(notes) == _MAX_LIST_ITEMS:
            break
    return notes


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _bounded_strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    strings: list[str] = []
    for raw in value[:_MAX_LIST_CANDIDATE_SCAN]:
        text = _bounded_text(raw, _MAX_SHORT_TEXT_CHARS)
        if text and text not in strings:
            strings.append(text)
        if len(strings) == _MAX_LIST_ITEMS:
            break
    return strings


def _bounded_source_refs(value: object) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_G_SOURCE_REFS:
        return None
    refs: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            return None
        ref = raw.strip()
        if not ref or len(ref) > _MAX_SHORT_TEXT_CHARS or ref in refs:
            return None
        refs.append(ref)
    return tuple(refs)


def _bounded_number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        if isfinite(number):
            return min(1.0, max(0.0, number))
    return 0.0


def _bounded_scalar(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if isfinite(float(value)) else None
    if isinstance(value, str):
        return _bounded_text(value)
    return None


def _extend_scoped_gaps(destination: list[str], gaps: object, scope: str) -> None:
    if not isinstance(gaps, (list, tuple)):
        return
    scoped = (
        f"{_bounded_text(gap, _MAX_SHORT_TEXT_CHARS)}:{scope}"
        for gap in gaps
        if _bounded_text(gap, _MAX_SHORT_TEXT_CHARS)
    )
    _extend_gaps(destination, scoped)


def _extend_gaps(destination: list[str], gaps: object) -> None:
    if isinstance(gaps, (str, bytes)) or not isinstance(gaps, Iterable):
        return
    for raw_gap in gaps:
        gap = _bounded_text(raw_gap, _MAX_SHORT_TEXT_CHARS)
        if gap and gap not in destination:
            destination.append(gap)
        if len(destination) == _MAX_GAPS:
            break
