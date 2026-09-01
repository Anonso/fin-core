"""Point-in-time NON_G mapping evidence from FIN's local runtime context.

The reader deliberately reuses :class:`AgentRuntimeContextProvider` as the
selection owner.  It only projects already-selected ``recent_reference``
materials into the stricter ``fin.read_ready_evidence`` contract; it never
loads a network source, creates an artifact, or grants instruction authority.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Protocol

from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextRequest,
    AgentRuntimeContextResult,
)
from fin_analyse.read_capabilities.types import (
    ProductionReadRequest,
    ProductionReadResult,
    SourceKind,
    SourceTrust,
)

_MAX_CANDIDATES = 32
_MAX_ITEMS = 8
_MAX_GAPS = 32
_MAX_REF_CHARS = 128
_MAX_TITLE_CHARS = 300
_MAX_SUMMARY_CHARS = 1_000
_MAX_FACT_CHARS = 400
_MAX_FACTS = 8
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TICKER = re.compile(r"^[036][0-9]{5}(?:\.(?:SH|SZ))?$")
_REFERENCE_USAGE_BOUNDARY = "reference_not_g_source_advisory_only"
_REFERENCE_SCOPE = "reference"
_REQUIRED_REFERENCE_FLAGS = frozenset({"same_day_reference", "reference_not_g_source"})
_REFERENCE_CLASSIFICATIONS = frozenset({"market_observation", "observation"})
_QA_COLUMNS = frozenset({"星大派好问题", "好问题", "问题回答", "回答问题"})
_STRICT_G_COLUMNS = frozenset(
    {"星大派特刊", "星大派锐评", "星大派好问题", "星大派每日热点", "星大派人脉", "凤仙郡小故事", "星大派"}
)
_MATERIAL_KINDS = frozenset({"inline_candidate", "deep_read_compact", "knowledge_markdown"})


class RuntimeContextReader(Protocol):
    """The existing FIN-owned semantic selection seam used by this adapter."""

    def resolve(self, request: AgentRuntimeContextRequest) -> AgentRuntimeContextResult: ...


class RecentReferenceReadyEvidenceReader:
    """Project local, selected recent references into formal mapping evidence."""

    def __init__(self, *, runtime_context: RuntimeContextReader) -> None:
        self._runtime_context = runtime_context

    def read(self, request: ProductionReadRequest) -> ProductionReadResult:
        if not isinstance(request, ProductionReadRequest):
            return _empty_result(("ready_evidence_request_invalid",))
        if request.as_of is None:
            return _empty_result(
                (
                    "ready_evidence_as_of_unavailable",
                    "ready_evidence_unavailable",
                )
            )

        instruments = tuple(dict.fromkeys(item.strip() for item in request.instruments))
        try:
            resolved = self._runtime_context.resolve(
                AgentRuntimeContextRequest(
                    agent_id="guo_teacher",
                    question=request.question.strip(),
                    tickers=instruments,
                    max_g_events=_MAX_ITEMS,
                    now=request.as_of.isoformat(),
                )
            )
        except Exception:
            return _empty_result(
                (
                    "ready_evidence_context_read_failed",
                    "ready_evidence_unavailable",
                )
            )
        if not isinstance(resolved, AgentRuntimeContextResult):
            return _empty_result(
                (
                    "ready_evidence_context_result_invalid",
                    "ready_evidence_unavailable",
                )
            )

        return _project_ready_evidence(resolved=resolved, as_of=request.as_of)


def _project_ready_evidence(
    *,
    resolved: AgentRuntimeContextResult,
    as_of: datetime,
) -> ProductionReadResult:
    if not isinstance(resolved.data_gaps, tuple) or any(
        not isinstance(gap, str) or not gap.strip() or len(gap) > 256 for gap in resolved.data_gaps
    ):
        return _empty_result(
            (
                "ready_evidence_context_result_invalid",
                "ready_evidence_unavailable",
            )
        )
    gaps: list[str] = []
    _extend_gaps(gaps, resolved.data_gaps)
    llm_context = resolved.llm_context
    audit_context = resolved.audit_context
    if not isinstance(llm_context, Mapping) or not isinstance(audit_context, Mapping):
        return _empty_result(
            (
                *gaps,
                "ready_evidence_context_result_invalid",
                "ready_evidence_unavailable",
            )
        )
    raw_items = llm_context.get("g_context")
    raw_audit = audit_context.get("selected")
    if not isinstance(raw_items, list) or not isinstance(raw_audit, list):
        return _empty_result(
            (
                *gaps,
                "ready_evidence_context_result_invalid",
                "ready_evidence_unavailable",
            )
        )
    if len(raw_items) > _MAX_CANDIDATES or len(raw_audit) > _MAX_CANDIDATES:
        return _empty_result(
            (
                *gaps,
                "ready_evidence_context_oversized",
                "ready_evidence_unavailable",
            )
        )

    audit_by_ref: dict[str, Mapping[object, object]] = {}
    duplicate_audit_refs: set[str] = set()
    for raw in raw_audit:
        if not isinstance(raw, Mapping):
            _append_gap(gaps, "ready_evidence_provenance_invalid")
            continue
        ref = _strict_text(raw.get("article_id"), _MAX_REF_CHARS)
        if not ref:
            _append_gap(gaps, "ready_evidence_provenance_invalid")
            continue
        if ref in audit_by_ref:
            duplicate_audit_refs.add(ref)
        audit_by_ref[ref] = raw

    reference_refs = [
        _strict_text(raw.get("source_ref"), _MAX_REF_CHARS)
        for raw in raw_items
        if isinstance(raw, Mapping) and raw.get("source_bucket") == "recent_reference"
    ]
    duplicate_refs = {ref for ref, count in Counter(reference_refs).items() if ref and count > 1}
    non_reference_refs = {
        _strict_text(raw.get("source_ref"), _MAX_REF_CHARS)
        for raw in raw_items
        if isinstance(raw, Mapping) and raw.get("source_bucket") != "recent_reference"
    }
    selection_policy = _strict_text(audit_context.get("selection_policy"), 160)
    if not selection_policy:
        return _empty_result(
            (
                *gaps,
                "ready_evidence_provenance_invalid",
                "ready_evidence_unavailable",
            )
        )

    items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or raw.get("source_bucket") != "recent_reference":
            continue
        ref = _strict_text(raw.get("source_ref"), _MAX_REF_CHARS)
        if ref in duplicate_refs or ref in duplicate_audit_refs:
            _append_gap(gaps, "ready_evidence_duplicate_ref")
            continue
        if ref and ref in non_reference_refs:
            _append_gap(gaps, "ready_evidence_cognition_ref_overlap")
            continue
        audit = audit_by_ref.get(ref)
        item, item_gap = _project_item(
            raw=raw,
            audit=audit,
            as_of=as_of,
            selection_policy=selection_policy,
        )
        if item_gap:
            _append_gap(gaps, item_gap)
            continue
        assert item is not None
        items.append(item)
        if len(items) == _MAX_ITEMS:
            if len(reference_refs) > len(items):
                _append_gap(gaps, "ready_evidence_items_truncated")
            break

    if not items:
        _append_gap(gaps, "ready_evidence_unavailable")
    return ProductionReadResult(
        value=_ready_evidence_value(items),
        data_gaps=tuple(gaps),
    )


def _project_item(
    *,
    raw: Mapping[object, object],
    audit: Mapping[object, object] | None,
    as_of: datetime,
    selection_policy: str,
) -> tuple[dict[str, object] | None, str]:
    ref = _strict_text(raw.get("source_ref"), _MAX_REF_CHARS)
    if not ref or not _SAFE_REF.fullmatch(ref):
        return None, "ready_evidence_identity_invalid"
    if audit is None:
        return None, "ready_evidence_provenance_missing"
    if audit.get("article_id") != ref:
        return None, "ready_evidence_identity_invalid"
    if (
        audit.get("source_bucket") != "recent_reference"
        or raw.get("source_scope") != _REFERENCE_SCOPE
        or audit.get("source_scope") != _REFERENCE_SCOPE
        or raw.get("usage_boundary") != _REFERENCE_USAGE_BOUNDARY
        or audit.get("usage_boundary") != _REFERENCE_USAGE_BOUNDARY
        or raw.get("instruction_authority") != "none"
        or audit.get("instruction_authority") != "none"
    ):
        return None, "ready_evidence_source_boundary_invalid"

    classification = _strict_text(raw.get("source_classification"), 80)
    column = _strict_text(raw.get("column"), 80)
    if (
        classification != _strict_text(audit.get("source_classification"), 80)
        or column != _strict_text(audit.get("column"), 80)
        or column in _STRICT_G_COLUMNS
        or not (
            classification in _REFERENCE_CLASSIFICATIONS
            or (classification == "teacher_original" and column in _QA_COLUMNS)
        )
    ):
        return None, "ready_evidence_source_classification_invalid"

    why_available = _strict_strings(raw.get("why_available"), limit=8, item_limit=120)
    if (
        why_available is None
        or not _REQUIRED_REFERENCE_FLAGS.issubset(why_available)
        or tuple(why_available)
        != _strict_strings(audit.get("why_available"), limit=8, item_limit=120)
    ):
        return None, "ready_evidence_reference_flags_invalid"

    title = _strict_text(raw.get("title"), _MAX_TITLE_CHARS)
    if not title or title != _strict_text(audit.get("title"), _MAX_TITLE_CHARS):
        return None, "ready_evidence_identity_invalid"
    published_at = _strict_text(raw.get("published_at"), 80)
    available_at = _strict_text(raw.get("available_at"), 80)
    if published_at != _strict_text(audit.get("published_at"), 80) or available_at != _strict_text(
        audit.get("available_at"), 80
    ):
        return None, "ready_evidence_provenance_invalid"
    published = _parse_datetime(published_at)
    available = _parse_datetime(available_at)
    if published is None or available is None or available < published:
        return None, "ready_evidence_time_invalid"
    if published > as_of or available > as_of:
        return None, "ready_evidence_future_material"

    selected_material = _selected_material(raw.get("selected_material"))
    audit_material = _selected_material(audit.get("selected_material"))
    if (
        selected_material is None
        or audit_material is None
        or selected_material != audit_material
        or selected_material["available_at"] != available_at
    ):
        return None, "ready_evidence_material_provenance_invalid"

    tickers = _strict_strings(raw.get("tickers"), limit=8, item_limit=16)
    companies = _strict_strings(raw.get("companies"), limit=8, item_limit=120)
    themes = _strict_strings(raw.get("theme_clusters"), limit=8, item_limit=120)
    key_points = _strict_strings(raw.get("reference_key_points"), limit=8, item_limit=400)
    chain_facts = _strict_strings(raw.get("industry_chain_facts"), limit=8, item_limit=400)
    if None in (tickers, companies, themes, key_points, chain_facts):
        return None, "ready_evidence_content_invalid"
    assert tickers is not None
    assert companies is not None
    assert themes is not None
    assert key_points is not None
    assert chain_facts is not None
    if any(not _valid_ticker(ticker) for ticker in tickers):
        return None, "ready_evidence_mapping_identity_invalid"
    if not tickers and not companies and not chain_facts:
        return None, "ready_evidence_mapping_facts_missing"

    summary = _strict_text(raw.get("reference_summary"), _MAX_SUMMARY_CHARS)
    if (
        tickers != _strict_strings(audit.get("tickers"), limit=8, item_limit=16)
        or companies != _strict_strings(audit.get("companies"), limit=8, item_limit=120)
        or themes != _strict_strings(audit.get("theme_clusters"), limit=8, item_limit=120)
        or key_points
        != _strict_strings(
            audit.get("reference_key_points"),
            limit=_MAX_FACTS,
            item_limit=_MAX_FACT_CHARS,
        )
        or chain_facts
        != _strict_strings(
            audit.get("industry_chain_facts"),
            limit=_MAX_FACTS,
            item_limit=_MAX_FACT_CHARS,
        )
        or summary != _strict_text(audit.get("reference_summary"), _MAX_SUMMARY_CHARS)
    ):
        return None, "ready_evidence_provenance_invalid"
    ready_flags = raw.get("local_ready_evidence")
    if not isinstance(ready_flags, Mapping):
        return None, "ready_evidence_local_status_invalid"
    normalized_flags = {
        key: ready_flags.get(key)
        for key in (
            "ready",
            "summary_available",
            "key_points_available",
            "deep_read_complete",
            "local_only",
        )
    }
    if (
        any(not isinstance(value, bool) for value in normalized_flags.values())
        or normalized_flags["ready"] is not True
        or normalized_flags["local_only"] is not True
        or normalized_flags["summary_available"] is not bool(summary)
        or normalized_flags["key_points_available"] is not bool(key_points)
        or not summary
        and not key_points
        or normalized_flags != audit.get("local_ready_evidence")
    ):
        return None, "ready_evidence_local_content_unavailable"

    return (
        {
            "source_ref": ref,
            "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
            "source_trust": SourceTrust.NON_G.value,
            "source_bucket": "recent_reference",
            "source_scope": _REFERENCE_SCOPE,
            "usage_boundary": _REFERENCE_USAGE_BOUNDARY,
            "instruction_authority": "none",
            "article_id": ref,
            "material_id": selected_material["ref"],
            "material_kind": selected_material["kind"],
            "material_sha256": selected_material["raw_sha256"],
            "title": title,
            "published_at": published_at,
            "available_at": available_at,
            "tickers": list(tickers),
            "companies": list(companies),
            "theme_clusters": list(themes),
            "industry_chain_facts": list(chain_facts),
            "reference_key_points": list(key_points),
            "content_summary": summary,
            "local_ready_evidence": normalized_flags,
            "why_available": list(why_available),
            "original_provenance": {
                "source_ref": ref,
                "article_id": ref,
                "source_bucket": "recent_reference",
                "source_scope": _REFERENCE_SCOPE,
                "source_classification": classification,
                "column": column,
                "published_at": published_at,
                "available_at": available_at,
                "selection_policy": selection_policy,
                "instruction_authority": "none",
                "selected_material": dict(selected_material),
            },
        },
        "",
    )


def _ready_evidence_value(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_boundary": "ready_evidence",
        "source_kind": SourceKind.EXTERNAL_REFERENCE.value,
        "source_trust": SourceTrust.NON_G.value,
        "items": items,
    }


def _empty_result(gaps: tuple[str, ...]) -> ProductionReadResult:
    normalized: list[str] = []
    _extend_gaps(normalized, gaps)
    return ProductionReadResult(
        value=_ready_evidence_value([]),
        data_gaps=tuple(normalized),
    )


def _strict_text(value: object, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > limit
    ):
        return ""
    return value


def _strict_strings(
    value: object,
    *,
    limit: int,
    item_limit: int,
) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        return None
    strings: list[str] = []
    for raw in value:
        text = _strict_text(raw, item_limit)
        if not text or text in strings:
            return None
        strings.append(text)
    return tuple(strings)


def _selected_material(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "kind",
        "ref",
        "available_at",
        "raw_sha256",
    }:
        return None
    kind = _strict_text(value.get("kind"), 64)
    ref = _strict_text(value.get("ref"), _MAX_REF_CHARS)
    available_at = _strict_text(value.get("available_at"), 80)
    raw_sha256 = _strict_text(value.get("raw_sha256"), 64)
    if (
        kind not in _MATERIAL_KINDS
        or not _SAFE_REF.fullmatch(ref)
        or _parse_datetime(available_at) is None
        or not _SHA256.fullmatch(raw_sha256)
    ):
        return None
    return {
        "kind": kind,
        "ref": ref,
        "available_at": available_at,
        "raw_sha256": raw_sha256,
    }


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _valid_ticker(value: str) -> bool:
    if not _SAFE_TICKER.fullmatch(value):
        return False
    code, _, exchange = value.partition(".")
    if not exchange:
        return True
    return (exchange == "SH" and code.startswith("6")) or (
        exchange == "SZ" and code.startswith(("0", "3"))
    )


def _append_gap(destination: list[str], gap: str) -> None:
    if gap not in destination and len(destination) < _MAX_GAPS:
        destination.append(gap)


def _extend_gaps(destination: list[str], gaps: Iterable[object]) -> None:
    for raw in gaps:
        gap = _strict_text(raw, 256)
        if gap:
            _append_gap(destination, gap)


__all__ = ["RecentReferenceReadyEvidenceReader", "RuntimeContextReader"]
