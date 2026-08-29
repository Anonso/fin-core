"""Serializable models for cross-article cognitive synthesis.

All dataclasses are serializable via to_dict()/from_dict().
Validation is local and does not require LLM or I/O.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# ── Trade field disallowed tokens ──────────────────────────────────────────

_TRADE_DISALLOWED = frozenset(
    [
        "action",
        "position_pct",
        "target_price",
        "stop_loss",
        "buy",
        "sell",
        "加仓",
        "减仓",
        "买入",
        "卖出",
        "仓位",
    ]
)


def validate_no_trade_fields(data: dict[str, Any]) -> None:
    """Raise ValueError if any disallowed trade field key is present."""
    for key in data:
        if key.lower() in _TRADE_DISALLOWED:
            raise ValueError(f"trade field '{key}' is disallowed in advisory-only outputs")


def _clamp_confidence(value: float, cap: float | None = None) -> float:
    """Clamp confidence to [0, 1], optionally applying an upper cap."""
    clamped = max(0.0, min(1.0, float(value)))
    if cap is not None:
        clamped = min(clamped, float(cap))
    return clamped


def _require_source_refs(items: list[dict], field_name: str) -> None:
    """Require source_clusters and source_article_ids on each item."""
    for item in items:
        if not item.get("source_clusters"):
            raise ValueError(f"{field_name} item missing source_clusters: {item}")
        if not item.get("source_article_ids"):
            raise ValueError(f"{field_name} item missing source_article_ids: {item}")


# ── Models ──────────────────────────────────────────────────────────────────


@dataclass
class QualityFlags:
    """Quality status flags carried by every synthesis response."""

    cache_hit: bool = False
    degraded: bool = False
    stale: bool = False
    fallback: bool = False
    partial: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityFlags:
        return cls(
            cache_hit=bool(data.get("cache_hit", False)),
            degraded=bool(data.get("degraded", False)),
            stale=bool(data.get("stale", False)),
            fallback=bool(data.get("fallback", False)),
            partial=bool(data.get("partial", False)),
        )


@dataclass
class ArticleRef:
    """Minimal input for cross_article pipeline. Callers do not pass raw dicts."""

    article_id: str
    title: str
    published_at: str
    column: str
    path: str
    source_classification: str
    persona_eligible: bool
    content_excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    _REQUIRED = frozenset(
        [
            "article_id",
            "title",
            "published_at",
            "column",
            "path",
            "source_classification",
            "persona_eligible",
        ]
    )

    def __post_init__(self) -> None:
        if not self.persona_eligible:
            raise ValueError(
                f"persona_eligible must be True for cross_article pipeline; "
                f"article {self.article_id} is persona_eligible=False"
            )
        missing = [f for f in self._REQUIRED if not getattr(self, f, None)]
        if missing:
            raise ValueError(f"ArticleRef missing required fields: {missing}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArticleRef:
        return cls(
            article_id=str(data["article_id"]),
            title=str(data.get("title", "")),
            published_at=str(data.get("published_at", "")),
            column=str(data.get("column", "")),
            path=str(data.get("path", "")),
            source_classification=str(data.get("source_classification", "")),
            persona_eligible=bool(data.get("persona_eligible", False)),
            content_excerpt=str(data.get("content_excerpt", "")),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ClusterInfo:
    """Cluster metadata — a lightweight view, not teacher memory."""

    cluster_id: str
    theme: str
    created_at: str
    updated_at: str
    article_ids: list[str] = field(default_factory=list)
    centroid_summary: str = ""
    source_boundary: str = "persona_eligible_xingdapai"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterInfo:
        return cls(
            cluster_id=str(data["cluster_id"]),
            theme=str(data.get("theme", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            article_ids=list(data.get("article_ids", [])),
            centroid_summary=str(data.get("centroid_summary", "")),
            source_boundary=str(data.get("source_boundary", "persona_eligible_xingdapai")),
        )


@dataclass
class ClusterAnalysis:
    """Phase 2 output — per-cluster evidence extraction and viewpoint analysis."""

    analysis_id: str
    cluster_id: str
    generated_at: str
    article_ids: list[str]
    core_viewpoints: list[dict[str, Any]]
    mentioned_stocks: list[dict[str, Any]]
    evidence_sufficiency: dict[str, Any]
    quality_mode: str = "single_model"
    viewpoint_evolution: dict[str, Any] = field(default_factory=dict)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    half_life_assessment: dict[str, Any] = field(default_factory=dict)
    cross_cluster_links: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        cap = self.evidence_sufficiency.get("confidence_cap", 1.0)
        # Clamp mentioned_stocks confidence
        clamped_stocks = []
        for s in self.mentioned_stocks:
            clamped = dict(s)
            clamped["confidence"] = _clamp_confidence(
                s.get("confidence", 0.5),
                cap if s.get("reference_type") == "inferred_from_logic" else None,
            )
            clamped_stocks.append(clamped)
        # Only assign if changed (avoid mutation issues in frozen-like context)
        object.__setattr__(self, "mentioned_stocks", clamped_stocks)

        # Clamp core_viewpoints confidence
        clamped_views = []
        for v in self.core_viewpoints:
            cv = dict(v)
            cv["confidence"] = _clamp_confidence(v.get("confidence", 0.5))
            clamped_views.append(cv)
        object.__setattr__(self, "core_viewpoints", clamped_views)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterAnalysis:
        return cls(
            analysis_id=str(data["analysis_id"]),
            cluster_id=str(data["cluster_id"]),
            generated_at=str(data.get("generated_at", "")),
            article_ids=list(data.get("article_ids", [])),
            core_viewpoints=list(data.get("core_viewpoints", [])),
            mentioned_stocks=list(data.get("mentioned_stocks", [])),
            evidence_sufficiency=data.get("evidence_sufficiency", {}),
            quality_mode=str(data.get("quality_mode", "single_model")),
            viewpoint_evolution=data.get("viewpoint_evolution", {}),
            contradictions=list(data.get("contradictions", [])),
            half_life_assessment=data.get("half_life_assessment", {}),
            cross_cluster_links=list(data.get("cross_cluster_links", [])),
        )


@dataclass
class SynthesisReport:
    """Phase 3 output — advisory-only cross-cluster synthesis."""

    synthesis_id: str
    generated_at: str
    source_article_ids: list[str]
    source_cluster_ids: list[str]
    sector_directions: list[dict[str, Any]]
    focused_stocks: list[dict[str, Any]]
    viewpoint_changes: list[dict[str, Any]]
    quality_flags: QualityFlags
    confidence: float
    time_range: dict[str, str] = field(default_factory=dict)
    risks_and_blind_spots: list[dict[str, Any]] = field(default_factory=list)
    cross_cluster_contradictions: list[dict[str, Any]] = field(default_factory=list)
    advisory_only: bool = True
    execution_allowed: bool = False
    previous_synthesis_id: str = ""
    suggested_signal_queries: list[SuggestedSignalQuery] = field(default_factory=list)
    source_mode: str = ""  # formal_g | degraded | reference_only | none

    def __post_init__(self) -> None:
        if not self.advisory_only:
            raise ValueError("SynthesisReport.advisory_only must be True")
        if self.execution_allowed is not False:
            raise ValueError("SynthesisReport.execution_allowed must be False")
        if not self.source_article_ids:
            raise ValueError("SynthesisReport requires source_article_ids")
        if not self.source_cluster_ids:
            raise ValueError("SynthesisReport requires source_cluster_ids")

        # Clamp overall confidence
        clamped_conf = _clamp_confidence(self.confidence)
        object.__setattr__(self, "confidence", clamped_conf)

        # Validate source refs on list fields
        _require_source_refs(self.sector_directions, "sector_directions")
        _require_source_refs(self.focused_stocks, "focused_stocks")
        _require_source_refs(self.viewpoint_changes, "viewpoint_changes")

        # Clamp focused_stocks confidence + strip reference-only sources
        clamped_stocks = []
        stripped_ref_stocks = 0
        for s in self.focused_stocks:
            cs = dict(s)
            ref_type = cs.get("reference_type", "")

            # ── Source authority guard: research_reference_mentions cannot enter G ──
            if ref_type == "research_reference_mentions":
                stripped_ref_stocks += 1
                continue

            if ref_type == "inferred_from_logic":
                cs["confidence"] = _clamp_confidence(s.get("confidence", 0.5), 0.7)
            elif ref_type == "direct_mention":
                cs["confidence"] = _clamp_confidence(s.get("confidence", 0.5), 0.9)
            else:
                # Unknown reference_type: treat as inferred (conservative)
                cs["confidence"] = _clamp_confidence(s.get("confidence", 0.5), 0.6)
                cs["reference_type"] = "inferred_from_logic"
            # Fill empty derivation_chain
            if not cs.get("derivation_chain", "").strip():
                ref_label = "老师直接点名" if ref_type == "direct_mention" else "从行业逻辑推导"
                cs["derivation_chain"] = f"（{ref_label}，来自 cluster 综合）"
            clamped_stocks.append(cs)
        object.__setattr__(self, "focused_stocks", clamped_stocks)

        # ── Set source_mode ──
        if not self.source_mode:
            has_direct = any(s.get("reference_type") == "direct_mention" for s in clamped_stocks)
            has_inferred = any(
                s.get("reference_type") == "inferred_from_logic" for s in clamped_stocks
            )
            if stripped_ref_stocks > 0 and not clamped_stocks:
                object.__setattr__(self, "source_mode", "reference_only")
            elif stripped_ref_stocks > 0:
                object.__setattr__(self, "source_mode", "degraded")
            elif has_direct or has_inferred:
                object.__setattr__(self, "source_mode", "formal_g")
            else:
                object.__setattr__(self, "source_mode", "none")

        # Reject trade fields anywhere in the output
        for stock in self.focused_stocks:
            validate_no_trade_fields(stock)
        for sector in self.sector_directions:
            validate_no_trade_fields(sector)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quality_flags"] = self.quality_flags.to_dict()
        d["suggested_signal_queries"] = [q.to_dict() for q in self.suggested_signal_queries]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SynthesisReport:
        qf_data = data.get("quality_flags", {})
        focused_stocks = list(data.get("focused_stocks", []))
        raw_queries = list(data.get("suggested_signal_queries", []))
        suggested_queries = [SuggestedSignalQuery.from_dict(q) for q in raw_queries]
        # Backfill: legacy synthesis has focused_stocks but no suggested_signal_queries
        if not suggested_queries and focused_stocks:
            suggested_queries = build_suggested_signal_queries(
                focused_stocks=focused_stocks,
            )
        return cls(
            synthesis_id=str(data["synthesis_id"]),
            generated_at=str(data.get("generated_at", "")),
            source_article_ids=list(data.get("source_article_ids", [])),
            source_cluster_ids=list(data.get("source_cluster_ids", [])),
            sector_directions=list(data.get("sector_directions", [])),
            focused_stocks=focused_stocks,
            viewpoint_changes=list(data.get("viewpoint_changes", [])),
            quality_flags=QualityFlags.from_dict(qf_data),
            confidence=float(data.get("confidence", 0.0)),
            time_range=data.get("time_range", {}),
            risks_and_blind_spots=list(data.get("risks_and_blind_spots", [])),
            cross_cluster_contradictions=list(data.get("cross_cluster_contradictions", [])),
            advisory_only=bool(data.get("advisory_only", True)),
            execution_allowed=bool(data.get("execution_allowed", False)),
            previous_synthesis_id=str(data.get("previous_synthesis_id", "")),
            suggested_signal_queries=suggested_queries,
            source_mode=str(data.get("source_mode", "")),
        )


@dataclass
class DegradationEvent:
    """Append-only degradation event for Hermes notification and daily summary."""

    event_id: str
    event_type: str = "cross_article_degradation"
    severity: str = "warning"
    created_at: str = ""
    fallback_reason: str = ""
    cache_key: str = ""
    synthesis_id: str = ""
    quality_flags: dict[str, bool] = field(default_factory=dict)
    dedupe_key: str = ""
    notify_policy: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            object.__setattr__(
                self,
                "dedupe_key",
                f"{self.created_at}:{self.fallback_reason}:{self.cache_key}",
            )
        if not self.notify_policy:
            object.__setattr__(
                self,
                "notify_policy",
                {
                    "immediate": True,
                    "daily_summary": True,
                    "dedupe_window": "1d",
                    "delivery_owner": "hermes",
                },
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DegradationEvent:
        return cls(
            event_id=str(data.get("event_id", "")),
            event_type=str(data.get("event_type", "cross_article_degradation")),
            severity=str(data.get("severity", "warning")),
            created_at=str(data.get("created_at", "")),
            fallback_reason=str(data.get("fallback_reason", "")),
            cache_key=str(data.get("cache_key", "")),
            synthesis_id=str(data.get("synthesis_id", "")),
            quality_flags=data.get("quality_flags", {}),
            dedupe_key=str(data.get("dedupe_key", "")),
            notify_policy=data.get("notify_policy", {}),
        )

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(frozen=True)
class SuggestedSignalQuery:
    """Retained internal G→Z query-intent compatibility model.

    Each entry records proposed Z-tool arguments, priority, and evidence metadata.
    The model is not part of the governed Hermes surface.

    Constraints:
    - priority is 'high'/'medium'/'low', driven by reference_type and evidence.
    - Building the record does not execute any Z tool.
    - ``query_tools`` and ``tool_args`` contain only retained read tools.
    """

    company: str
    ticker: str
    reason: str
    priority: str  # high / medium / low
    query_tools: list[str]
    tool_args: dict[str, dict[str, Any]]
    reference_type: str  # direct_mention / inferred_from_logic
    source_clusters: list[str]
    source_article_ids: list[str]
    evidence_mode: str = "sufficient"  # sufficient / observation_only

    _VALID_PRIORITIES = frozenset({"high", "medium", "low"})

    def __post_init__(self) -> None:
        if self.priority not in self._VALID_PRIORITIES:
            raise ValueError(
                f"SuggestedSignalQuery.priority must be one of {self._VALID_PRIORITIES}, "
                f"got {self.priority}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SuggestedSignalQuery:
        allowed_tools = ("analyze_stock", "get_market_snapshot")
        raw_tools = data.get("query_tools", [])
        query_tools = [tool for tool in raw_tools if tool in allowed_tools]
        raw_args = data.get("tool_args", {})
        tool_args = (
            {tool: raw_args[tool] for tool in query_tools if isinstance(raw_args.get(tool), dict)}
            if isinstance(raw_args, dict)
            else {}
        )
        return cls(
            company=str(data.get("company", "")),
            ticker=str(data.get("ticker", "")),
            reason=str(data.get("reason", "")),
            priority=str(data.get("priority", "medium")),
            query_tools=query_tools,
            tool_args=tool_args,
            reference_type=str(data.get("reference_type", "inferred_from_logic")),
            source_clusters=list(data.get("source_clusters", [])),
            source_article_ids=list(data.get("source_article_ids", [])),
            evidence_mode=str(data.get("evidence_mode", "sufficient")),
        )


def build_suggested_signal_queries(
    *,
    focused_stocks: list[dict[str, Any]],
) -> list[SuggestedSignalQuery]:
    """Pure builder for retained G→Z query-intent records.

    Rules:
    - direct_mention + sufficient evidence → high, query retained Z tools
    - direct_mention + observation_only → medium, query analyze_stock only
    - inferred_from_logic → medium, query analyze_stock only
    - No ticker → uses company name only in analyze_stock

    Building these records executes no tool. They are retained for internal
    compatibility and are not instructions for Hermes.
    """
    queries: list[SuggestedSignalQuery] = []

    for stock in focused_stocks:
        ref_type = str(stock.get("reference_type", "inferred_from_logic"))

        # ── Source authority guard: research_reference_mentions cannot generate G→Z queries ──
        if ref_type == "research_reference_mentions":
            continue

        company = str(stock.get("company", ""))
        if not company:
            continue
        ticker = str(stock.get("ticker", "")).strip()
        evidence = str(stock.get("evidence_mode", "sufficient"))
        confidence = float(stock.get("confidence", 0.5))
        clusters = list(stock.get("source_clusters", []))
        articles = list(stock.get("source_article_ids", []))
        derivation = str(stock.get("derivation_chain", ""))

        # ── Determine priority and tool set ──
        if ref_type == "direct_mention" and evidence == "sufficient":
            priority = "high"
            query_tools = [
                "analyze_stock",
                "get_market_snapshot",
            ]
            reason = (
                f"G 中直接点名「{company}」且多篇文章持续确认"
                if len(articles) > 1
                else f"G 中直接点名「{company}」，建议用 Z 验证信号是否支持"
            )
        elif ref_type == "direct_mention":
            priority = "medium"
            query_tools = ["analyze_stock"]
            reason = f"G 点名「{company}」但证据弱（{evidence}），Z 仅做参考验证"
        elif ref_type == "inferred_from_logic":
            priority = "medium"
            query_tools = ["analyze_stock"]
            reason = (
                f"G 中从逻辑推导「{company}」（{derivation[:40]}），"
                f"confidence={confidence:.0%}，建议 Z 做低置信交叉检查"
            )
        else:
            priority = "low"
            query_tools = ["analyze_stock"]
            reason = f"G 提及「{company}」但归因不明，Z 仅供了解"

        # ── Build tool_args ──
        tool_args: dict[str, dict[str, Any]] = {}
        tool_args["analyze_stock"] = {
            "company": company,
            "ticker": ticker,
        }
        if "get_market_snapshot" in query_tools and ticker:
            tool_args["get_market_snapshot"] = {"ticker": ticker}
        queries.append(
            SuggestedSignalQuery(
                company=company,
                ticker=ticker,
                reason=reason,
                priority=priority,
                query_tools=query_tools,
                tool_args=tool_args,
                reference_type=ref_type,
                source_clusters=clusters,
                source_article_ids=articles,
                evidence_mode=evidence,
            )
        )

    return queries


@dataclass
class CrossArticleSynthesisResponse:
    """MCP response envelope for get_cross_article_synthesis."""

    synthesis: dict[str, Any] | None
    clusters: list[dict[str, Any]]
    previous_synthesis_id: str | None
    generated_at: str
    quality_flags: dict[str, bool]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestionResult:
    """Returned by CrossArticleSynthesisService.ingest_articles()."""

    processed: int
    skipped: int
    degraded: int
    skip_reasons: list[dict[str, str]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SelectionResult:
    """Returned by CrossArticleSourceSelector.select()."""

    selected: list[ArticleRef]
    skipped: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": [a.to_dict() for a in self.selected],
            "skipped": self.skipped,
        }


# ── Model policy ────────────────────────────────────────────────────────────


@dataclass
class ModelPolicy:
    """Resolved backend references for cross_article phases."""

    t0_backend: Any | None = None
    t1_backend: Any | None = None
    t0_name: str = ""
    t1_name: str = ""

    def t0_available(self) -> bool:
        return self.t0_backend is not None

    def t1_available(self) -> bool:
        return self.t1_backend is not None
