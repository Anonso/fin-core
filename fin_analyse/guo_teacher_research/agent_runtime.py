"""Agent Runtime Port — provider-neutral agent runtime seam.

Defines the FIN-owned AgentRuntimePort Protocol, AgentRunRequest, and
AgentRunResult.  The port is a single-method contract::

    run(AgentRunRequest) -> AgentRunResult

FIN owns the request/result schema; concrete adapters (Codex, fake, future
providers) implement the Protocol.  Provider-private transcript, session IDs,
and credentials never enter the FIN public domain model.

Design: docs/architecture/fin-domain-kernel-agent-runtime.md
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

from fin_analyse.common.execution_control import ExecutionFence
from fin_analyse.guo_teacher_research.capability_broker import (
    AgentCapabilityPort,
    CapabilityCall,
    CapabilityCallResult,
    CapabilityErrorCode,
    CapabilityGrantHandle,
    CapabilityRejectedError,
    CapabilitySource,
    SourceKind,
)

CapabilityResultPublication = Callable[[], AbstractContextManager[bool]]


class CapabilityResultNotPublishedError(RuntimeError):
    """A capability completed after its owning runtime publication fence closed."""


class AgentCapabilityBridge(Protocol):
    """One-run local bridge from an Agent runtime to FIN capabilities.

    Grant and source-scope tokens remain adapter-private.  The runtime can only
    name a capability, provide its typed payload/source refs, and observe the
    broker-validated result plus a sanitized trace.
    """

    def invoke(
        self,
        capability: str,
        payload: object,
        *,
        sources: tuple[CapabilitySource, ...] = (),
        result_publication: CapabilityResultPublication | None = None,
    ) -> CapabilityCallResult:
        """Invoke one FIN-owned capability under the run's bounded grant."""
        ...

    @property
    def trace(self) -> list[dict[str, Any]]:
        """Return the sanitized trace accumulated by accepted calls."""
        ...

    @property
    def data_gaps(self) -> tuple[str, ...]:
        """Return FIN-owned gaps emitted by accepted capability calls."""
        ...

    @property
    def data_gap_events(self) -> tuple[tuple[str, str], ...]:
        """Return each raw gap together with the capability that emitted it."""
        ...

    @property
    def activity_started(self) -> bool:
        """Whether any broker invocation has started, regardless of its outcome."""
        ...

    @property
    def rejected(self) -> bool:
        """Whether any rejected or structurally invalid call was attempted."""
        ...


class AgentCapabilityGrantPort(AgentCapabilityPort, Protocol):
    """FIN-owned broker seam used to mint one bounded runtime grant."""

    def issue_grant(
        self,
        *,
        run_id: str,
        capabilities: Collection[str],
        source_scope_token: str,
        source_scope: Collection[CapabilitySource],
        allowed_source_kinds: Collection[SourceKind],
        max_calls: int,
        expires_at: datetime,
        policies: Mapping[str, object] | None = None,
    ) -> CapabilityGrantHandle:
        """Issue an opaque per-run grant after validating the resolved contract."""
        ...


# R3c：可选 G 类 capability 被拒时的稳定降级 gap（与 provider 层 gap 词汇一致）。
_OPTIONAL_G_CAPABILITY_GAPS = {
    "fin.read_g_context": "g_context_unavailable",
    "fin.read_teacher_cognition": "teacher_cognition_unavailable",
}


class BoundedAgentCapabilityBridge:
    """Keep broker credentials private while accounting for every runtime call."""

    __slots__ = (
        "_activity_started",
        "_data_gap_events",
        "_data_gaps",
        "_grant",
        "_port",
        "_rejected",
        "_rejected_capabilities",
        "_result_attestation_calls",
        "_result_attestations",
        "_source_scope_token",
        "_state_lock",
        "_trace",
    )

    def __init__(
        self,
        *,
        port: AgentCapabilityPort,
        grant: CapabilityGrantHandle,
        source_scope_token: str,
    ) -> None:
        self._port = port
        self._grant = grant
        self._source_scope_token = source_scope_token
        self._trace: list[dict[str, Any]] = []
        self._data_gaps: list[str] = []
        self._data_gap_events: list[tuple[str, str]] = []
        self._result_attestations: dict[str, dict[str, Any]] = {}
        self._result_attestation_calls: dict[str, list[dict[str, Any]]] = {}
        self._activity_started = False
        self._rejected = False
        self._rejected_capabilities: list[str] = []
        self._state_lock = Lock()

    def invoke(
        self,
        capability: str,
        payload: object,
        *,
        sources: tuple[CapabilitySource, ...] = (),
        result_publication: CapabilityResultPublication | None = None,
    ) -> CapabilityCallResult:
        call = CapabilityCall(
            grant_token=self._grant.token,
            run_id=self._grant.run_id,
            capability=capability,
            source_scope_token=self._source_scope_token,
            payload=payload,
            sources=sources,
        )
        with self._state_lock:
            self._activity_started = True
        try:
            result = self._port.invoke(call)
        except Exception:
            with _result_publication_context(result_publication) as publish:
                if not publish:
                    raise CapabilityResultNotPublishedError(
                        "capability_result_not_published"
                    ) from None
                with self._state_lock:
                    self._rejected = True
                    self._rejected_capabilities.append(capability)
                    # R3c：被拒的可选 G 调用必须留下稳定降级 gap（B1 设计修订），
                    # 供 runner 降级分支呈现，不得静默吞掉。
                    optional_gap = _OPTIONAL_G_CAPABILITY_GAPS.get(capability)
                    if optional_gap is not None:
                        self._data_gap_events.append((capability, optional_gap))
            raise
        with _result_publication_context(result_publication) as publish:
            if not publish:
                raise CapabilityResultNotPublishedError("capability_result_not_published")
            with self._state_lock:
                expected_index = len(self._trace) + 1
                if (
                    result.trace.capability != capability
                    or result.trace.call_index != expected_index
                ):
                    # R3c audit B3：无效 trace 是安全边界违规，必须留下拒绝事实，
                    # 否则可被先前的可选 G 拒绝掩盖而误降级。
                    self._rejected = True
                    self._rejected_capabilities.append(capability)
                    raise CapabilityRejectedError(
                        CapabilityErrorCode.BOUNDARY_VIOLATION,
                        "capability broker returned an invalid trace",
                    )
                self._trace.append(
                    {
                        "capability": result.trace.capability,
                        "run_ref": result.trace.run_ref,
                        "call_index": result.trace.call_index,
                        "source_kinds": [kind.value for kind in result.trace.source_kinds],
                        "status": result.trace.status,
                    }
                )
                attestation = _capability_result_attestation(
                    capability,
                    result.value,
                    result.data_gaps,
                )
                if attestation is not None:
                    self._result_attestations[capability] = attestation
                    # 聚合：同一 capability 可被多次调用（如持仓超过单次上限
                    # 分批读取），每次调用都是独立 receipts——逐次保留供绑定
                    # 匹配，避免最后一次调用覆盖导致绑定失败。
                    self._result_attestation_calls.setdefault(capability, []).append(
                        attestation
                    )
                for gap in result.data_gaps:
                    self._data_gap_events.append((capability, gap))
                    if gap not in self._data_gaps:
                        self._data_gaps.append(gap)
        return result

    @property
    def trace(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [dict(item) for item in self._trace]

    @property
    def data_gaps(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(self._data_gaps)

    @property
    def result_attestations(self) -> dict[str, dict[str, Any]]:
        with self._state_lock:
            return deepcopy(self._result_attestations)

    @property
    def result_attestation_calls(self) -> dict[str, list[dict[str, Any]]]:
        with self._state_lock:
            return deepcopy(self._result_attestation_calls)

    @property
    def data_gap_events(self) -> tuple[tuple[str, str], ...]:
        with self._state_lock:
            return tuple(self._data_gap_events)

    @property
    def activity_started(self) -> bool:
        with self._state_lock:
            return self._activity_started

    @property
    def rejected(self) -> bool:
        with self._state_lock:
            return self._rejected

    @property
    def rejected_capabilities(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(self._rejected_capabilities)


def _result_publication_context(
    publication: CapabilityResultPublication | None,
) -> AbstractContextManager[bool]:
    if publication is None:
        return nullcontext(True)
    return publication()


def _capability_result_attestation(
    capability: str,
    value: object,
    data_gaps: tuple[str, ...],
) -> dict[str, Any] | None:
    if capability == "fin.read_market_overview":
        return _market_overview_result_attestation(value, data_gaps)
    if capability == "fin.read_market_snapshot":
        return _market_snapshot_result_attestation(value, data_gaps)
    if capability == "fin.read_teacher_cognition":
        return _teacher_cognition_result_attestation(value, data_gaps)
    if capability == "fin.read_actual_portfolio":
        return _actual_portfolio_result_attestation(value, data_gaps)
    if capability != "fin.read_g_context":
        return None
    gaps = [gap for gap in data_gaps if isinstance(gap, str) and 0 < len(gap) <= 128][:32]
    if not isinstance(value, Mapping):
        return {"status": "UNAVAILABLE", "data_gaps": gaps}
    # Slice 3b returns G metadata in the facts layer.  Keep the old flat
    # shape as a compatibility fallback for replayed receipts, while layered
    # values are authoritative when present.
    facts_value = value.get("facts")
    facts_map = facts_value if isinstance(facts_value, Mapping) else value
    pinned_value = value.get("pinned")
    pinned_map = pinned_value if isinstance(pinned_value, Mapping) else {}
    freshness = facts_map.get("freshness")
    freshness_map = freshness if isinstance(freshness, Mapping) else {}
    generation = _bounded_attestation_text(facts_map.get("generation"), 128)
    canonical_sha256 = _bounded_attestation_text(
        freshness_map.get("canonical_sha256"),
        128,
    )
    if generation and canonical_sha256 and generation != canonical_sha256:
        gaps.append("g_context_generation_mismatch")
        generation = ""
    freshness_status = _bounded_attestation_text(freshness_map.get("status"), 32)
    evaluated_at = _bounded_attestation_text(freshness_map.get("evaluated_at"), 80)
    freshness_gaps = freshness_map.get("data_gaps")
    if isinstance(freshness_gaps, list):
        for gap in freshness_gaps[:32]:
            bounded = _bounded_attestation_text(gap, 128)
            if bounded and bounded not in gaps:
                gaps.append(bounded)

    raw_bound_refs = freshness_map.get("bound_article_ids")
    bound_refs: list[str] = []
    if isinstance(raw_bound_refs, (list, tuple)):
        for candidate in raw_bound_refs[:32]:
            ref = _bounded_attestation_text(candidate, 256)
            if ref and ref not in bound_refs:
                bound_refs.append(ref)

    refs: list[str] = []
    published: list[str] = []
    available: list[str] = []
    saw_unbound_item = False
    saw_incomplete_bound_item = False
    item_layers = [facts_map.get("items")]
    if facts_map is not value:
        item_layers.append(pinned_map.get("items"))
    seen_items = 0
    for items in item_layers:
        if not isinstance(items, list):
            continue
        for item in items:
            seen_items += 1
            if seen_items > 32:
                break
            if not isinstance(item, Mapping):
                continue
            ref = _bounded_attestation_text(item.get("source_ref"), 256)
            if not ref:
                continue
            if ref not in bound_refs:
                saw_unbound_item = True
                continue
            published_at = _bounded_attestation_text(item.get("published_at"), 80)
            available_at = _bounded_attestation_text(item.get("available_at"), 80)
            if not published_at or not available_at:
                saw_incomplete_bound_item = True
                continue
            if ref not in refs:
                refs.append(ref)
                published.append(published_at)
                available.append(available_at)
        if seen_items > 32:
            break
    if not refs:
        gap = (
            "g_context_source_freshness_incomplete"
            if saw_incomplete_bound_item
            else "g_context_bound_sources_mismatch"
            if saw_unbound_item or bound_refs
            else "g_context_bound_sources_missing"
        )
        if gap not in gaps and len(gaps) < 32:
            gaps.append(gap)
    consumed = bool(generation and refs and freshness_status == "READY" and evaluated_at)
    return {
        "status": "CONSUMED" if consumed else "UNAVAILABLE",
        "generation": generation,
        "source_refs": refs,
        "published_at": published,
        "available_at": available,
        "freshness_status": freshness_status or "UNKNOWN",
        "evaluated_at": evaluated_at,
        "data_gaps": gaps,
    }


def _market_overview_result_attestation(
    value: object,
    data_gaps: tuple[str, ...],
) -> dict[str, Any]:
    gaps = [gap for gap in data_gaps if isinstance(gap, str) and 0 < len(gap) <= 128][:32]
    if not isinstance(value, Mapping):
        return {"status": "UNAVAILABLE", "data_gaps": gaps}
    fields = {
        "schema_version": _bounded_attestation_text(value.get("schema_version"), 80),
        "source_boundary": _bounded_attestation_text(value.get("source_boundary"), 80),
        "source_kind": _bounded_attestation_text(value.get("source_kind"), 40),
        "source_trust": _bounded_attestation_text(value.get("source_trust"), 40),
        "provider_mode": _bounded_attestation_text(value.get("provider_mode"), 80),
        "effective_trade_date": _bounded_attestation_text(
            value.get("effective_trade_date"),
            32,
        ),
        "observation_mode": _bounded_attestation_text(
            value.get("observation_mode"),
            80,
        ),
        "provider_updated_at": _bounded_attestation_text(
            value.get("provider_updated_at"),
            80,
        ),
        "reference_only": value.get("reference_only"),
        "realtime_eligible": value.get("realtime_eligible"),
    }
    available = bool(
        fields["schema_version"] == "fin.a-share-market-overview/v1"
        and fields["source_boundary"] == "a_share_current_market_overview"
        and fields["source_kind"] == "external_reference"
        and fields["source_trust"] == "non_g"
        and fields["provider_mode"]
        and fields["effective_trade_date"]
        and fields["observation_mode"]
        and fields["provider_updated_at"]
        and fields["reference_only"] is True
        and fields["realtime_eligible"] is False
        and value.get("status") == "PARTIAL"
    )
    return {
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        **fields,
        "_raw_value_status": value.get("status") if isinstance(value, Mapping) else None,
        "data_gaps": gaps,
    }


def _actual_portfolio_result_attestation(
    value: object,
    data_gaps: tuple[str, ...],
) -> dict[str, Any]:
    gaps = [gap for gap in data_gaps if isinstance(gap, str) and 0 < len(gap) <= 128][:32]
    if not isinstance(value, Mapping):
        return {"status": "UNAVAILABLE", "symbols": [], "data_gaps": gaps}
    snapshot = value.get("snapshot")
    snapshot_map = snapshot if isinstance(snapshot, Mapping) else {}
    revision = _bounded_attestation_text(snapshot_map.get("revision"), 80)
    revision_valid = bool(
        len(revision) == 71
        and revision.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in revision[7:])
    )
    symbols: list[str] = []
    positions = snapshot_map.get("positions")
    if isinstance(positions, list):
        for position in positions[:64]:
            if not isinstance(position, Mapping):
                continue
            symbol = _bounded_attestation_text(position.get("symbol"), 32)
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    portfolio_status = _bounded_attestation_text(value.get("status"), 16)
    source_boundary = _bounded_attestation_text(value.get("source_boundary"), 80)
    source_kind = _bounded_attestation_text(value.get("source_kind"), 40)
    source_trust = _bounded_attestation_text(value.get("source_trust"), 40)
    core_usable = value.get("core_usable") is True
    available = bool(
        value.get("schema_version") == "fin.actual-portfolio-capability/v1"
        and portfolio_status in {"READY", "PARTIAL"}
        and source_boundary == "actual_advisory_portfolio"
        and source_kind == "user_portfolio"
        and source_trust == "non_g"
        and core_usable
        and revision_valid
    )
    return {
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "portfolio_status": portfolio_status or "UNKNOWN",
        "source_boundary": source_boundary,
        "source_kind": source_kind,
        "source_trust": source_trust,
        "core_usable": core_usable,
        "revision": revision if revision_valid else "",
        "as_of": _bounded_attestation_text(snapshot_map.get("as_of"), 80),
        "position_count": len(positions) if isinstance(positions, list) else 0,
        "symbols": symbols,
        "data_gaps": gaps,
    }


def _attestation_payload_sha256(item: object) -> str | None:
    """canonical JSON SHA-256 of one raw instrument payload（content-free digest）。

    序列化失败/NaN → None（该 instrument 不得被当作可信 receipt）。
    """
    if not isinstance(item, Mapping):
        return None
    try:
        canonical = json.dumps(
            dict(item),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical).hexdigest()


def _teacher_cognition_result_attestation(
    value: object,
    data_gaps: tuple[str, ...],
) -> dict[str, Any]:
    gaps = [gap for gap in data_gaps if isinstance(gap, str) and 0 < len(gap) <= 128][:32]
    if not isinstance(value, Mapping):
        return {"status": "UNAVAILABLE", "source_refs": [], "data_gaps": gaps}
    source_refs: list[str] = []
    for lane in ("personas", "patterns", "traces"):
        items = value.get(lane)
        if not isinstance(items, list):
            continue
        for item in items[:32]:
            if not isinstance(item, Mapping):
                continue
            source_ref = _bounded_attestation_text(item.get("source_ref"), 256)
            if source_ref and source_ref not in source_refs:
                source_refs.append(source_ref)
    available = bool(
        value.get("source_boundary") == "teacher_cognition"
        and value.get("source_kind") == "g"
        and value.get("source_trust") == "fin_trusted_g"
        and source_refs
    )
    return {
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "source_boundary": "teacher_cognition",
        "source_kind": "g",
        "source_trust": "fin_trusted_g",
        "source_refs": source_refs,
        "data_gaps": gaps,
    }


def _market_snapshot_result_attestation(
    value: object,
    data_gaps: tuple[str, ...],
) -> dict[str, Any]:
    gaps = [gap for gap in data_gaps if isinstance(gap, str) and 0 < len(gap) <= 128][:32]
    if not isinstance(value, Mapping):
        return {"status": "UNAVAILABLE", "instruments": [], "data_gaps": gaps}
    fields = {
        "schema_version": _bounded_attestation_text(value.get("schema_version"), 80),
        "source_boundary": _bounded_attestation_text(value.get("source_boundary"), 80),
        "source_kind": _bounded_attestation_text(value.get("source_kind"), 40),
        "source_trust": _bounded_attestation_text(value.get("source_trust"), 40),
        "evidence_status": _bounded_attestation_text(value.get("status"), 16),
        "as_of": _bounded_attestation_text(value.get("as_of"), 80),
        "valid_until": _bounded_attestation_text(value.get("valid_until"), 80),
        "session_phase": _bounded_attestation_text(value.get("session_phase"), 80),
    }
    instruments: list[dict[str, Any]] = []
    raw_instruments = value.get("instruments")
    if isinstance(raw_instruments, list):
        for item in raw_instruments[:5]:
            if not isinstance(item, Mapping):
                continue
            quote = item.get("quote")
            quote_map = quote if isinstance(quote, Mapping) else {}
            daily = item.get("daily_bars")
            daily_map = daily if isinstance(daily, Mapping) else {}
            instrument = {
                "symbol": _bounded_attestation_text(item.get("symbol"), 32),
                "name": _bounded_attestation_text(item.get("name"), 128) or None,
                "evidence_id": _bounded_attestation_text(item.get("evidence_id"), 128),
                "status": _bounded_attestation_text(item.get("status"), 16),
                "quote_observed_at": _bounded_attestation_text(
                    quote_map.get("observed_at"),
                    80,
                )
                or None,
                "reference_only": item.get("reference_only"),
                "manual_review_eligible": item.get("manual_review_eligible"),
                "latest_completed_bar_date": _bounded_attestation_text(
                    daily_map.get("latest_completed_bar_date"),
                    32,
                )
                or None,
                "completed_bar_count": daily_map.get("completed_bar_count"),
            }
            if instrument["symbol"] and instrument["evidence_id"]:
                instruments.append(instrument)
    # 0.2 v3：content-free payload digest 走顶层并行结构（不污染 instrument
    # 投影——binding 的 subset 等值匹配要求 instrument 键集不变）。
    payload_digests: list[dict[str, Any]] = []
    for item in (raw_instruments or ()) if isinstance(raw_instruments, list) else ():
        if not isinstance(item, Mapping):
            continue
        symbol = _bounded_attestation_text(item.get("symbol"), 32)
        evidence_id = _bounded_attestation_text(item.get("evidence_id"), 128)
        digest = _attestation_payload_sha256(item)
        if symbol and evidence_id and digest:
            payload_digests.append(
                {
                    "symbol": symbol,
                    "evidence_id": evidence_id,
                    "payload_sha256": digest,
                }
            )
    available = bool(
        fields["schema_version"] == "fin.on-demand-tactical-context/v1"
        and fields["source_boundary"] == "a_share_on_demand_tactical_context"
        and fields["source_kind"] == "external_reference"
        and fields["source_trust"] == "non_g"
        and fields["evidence_status"] in {"READY", "PARTIAL"}
        and fields["as_of"]
        and fields["valid_until"]
        and fields["session_phase"]
        and instruments
    )
    return {
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "captured_at": fields["as_of"],
        "payload_digests": payload_digests,
        "schema_version": fields["schema_version"],
        "source_boundary": fields["source_boundary"],
        "source_kind": fields["source_kind"],
        "source_trust": fields["source_trust"],
        "evidence_status": fields["evidence_status"] or "UNKNOWN",
        "as_of": fields["as_of"],
        "valid_until": fields["valid_until"],
        "session_phase": fields["session_phase"],
        "instruments": instruments,
        "data_gaps": gaps,
    }


def _bounded_attestation_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    return normalized if 0 < len(normalized) <= limit else ""


# ═══════════════════════════════════════════════════════════════════════════════
# Request / Result dataclasses
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class AgentRunRequest:
    """A provider-neutral runtime request prepared by the FIN domain kernel.

    Attributes:
        use_case_ref: Canonical semantic use case id (e.g. "guo.decision_guidance").
        question: The user question or research goal.
        context_pack: The assembled ContextPack (as a dict) with source scope,
            data gaps, and freshness metadata already resolved.
        capability_scope: Registered capability names, contract IDs and task
            profile metadata the runtime is allowed to use.
        product_contracts: Registered ProductContract definitions (contract_id,
            version, required_fields, forbidden_fields) the runtime must honour.
        boundaries: Fixed safety boundaries: advisory_only, execution_allowed,
            human_confirmation_required — always True, False, True.
        model: Requested model name (or empty string for runtime default).
        timeout_seconds: Maximum wall-clock seconds for the runtime call.
        primary_route_timeout_seconds_cap: Optional cap for only the first
            route attempt. Fallback routes consume the remaining shared
            ``timeout_seconds`` budget.
        budget: Resource budget dict (token cap, cost cap hints).
        capability_bridge: One-run broker-backed capability seam.  Grant
            credentials never enter the prompt or public product.
        opaque_runtime_continuation: Optional opaque continuation token
            (provider-specific, never interpreted by FIN).
    """

    use_case_ref: str
    question: str = ""
    context_pack: dict[str, Any] = field(default_factory=dict)
    capability_scope: dict[str, Any] = field(default_factory=dict)
    product_contracts: list[dict[str, Any]] = field(default_factory=list)
    boundaries: dict[str, bool] = field(default_factory=dict)
    model: str = ""
    timeout_seconds: float = 300.0
    primary_route_timeout_seconds_cap: float | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    capability_bridge: AgentCapabilityBridge | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    opaque_runtime_continuation: dict[str, Any] = field(default_factory=dict)
    # 引导者 non-evidence 过程段（可选，≤1200）；runtime 只负责渲染为显式
    # 过程输入，Agent 可采纳、调整或拒绝。generic_research_answer 不渲染。
    process_guidance: str | None = None
    execution_fence: ExecutionFence | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # A2: 私有 clean-break 意图。sticky 路由决定 cooling handle 转 fresh 时，
    # outer 清空 opaque_runtime_continuation（本请求余下 primary/fallback 链
    # 都不得恢复旧 session）但保留此信号；内层 adapter 只在真实 fresh child
    # 启动边界消费它提交 continuity_degraded，pre-child 失败不消费。
    # 不是公共合同字段；普通调用默认 False。
    continuity_degraded_intent: bool = False


@dataclass
class AgentRunResult:
    """A sanitized provider-neutral runtime result.

    Attributes:
        status: "ok" | "partial" | "degraded" | "error".
        payload: The final public payload (research_product, display_product, …).
        data_gaps: Collected data gaps and degradation markers.
        capability_trace: Actual capability/tool usage trace (sanitized).
        provenance: Model, backend, session provenance (sanitized — no credentials).
        opaque_runtime_continuation: Optional opaque continuation token for follow-up calls.
        resource_usage: Actual resource consumption (tokens, wall time).
    """

    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    data_gaps: list[str] = field(default_factory=list)
    capability_trace: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    opaque_runtime_continuation: dict[str, Any] = field(default_factory=dict)
    resource_usage: dict[str, Any] = field(default_factory=dict)
    # A1: 技术故障在故障 origin 生成一次的 sanitized 128-bit id;沿
    # use-case/semantic/consultation 层只复制,不重生成;成功响应为 None。
    error_id: str | None = None
    # A1: 真实 child 的退出码(仅 runtime child 路径;timeout/stall/state/
    # finalization 为 None)。
    exit_code: int | None = None
    # A2: 本次调用原本要求延续(带 continuation)，但 FIN 已知实际转入 fresh
    # runtime 路径（resume 不被接受、resume-before-activity 失败后 fresh、
    # sticky handle route 冷却等）。这是执行事实，不是 status；fresh 成功
    # 与 fresh 失败都保持 True，未发生 fresh 转换时为 False。
    continuity_degraded: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# Protocol
# ═══════════════════════════════════════════════════════════════════════════════


class AgentRuntimePort(Protocol):
    """Provider-neutral agent runtime entry point.

    A single deep method::

        run(AgentRunRequest) -> AgentRunResult

    Concrete adapters must:
    - Accept an AgentRunRequest prepared by the FIN domain kernel.
    - Return a sanitized AgentRunResult with no provider credentials,
      raw transcripts or internal session state in the public payload.
    - Fail closed: on timeout, nonzero exit, missing output or malformed
      parsing, return status="error" with bounded data-gap codes.
    """

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute the agent run and return a sanitized result."""
        ...
