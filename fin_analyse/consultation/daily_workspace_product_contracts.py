"""Daily Decision Workspace product contract (V1).

#2 (decision map): the daily workspace is a product subtype carried inside
the ``fin.consultation/v1`` envelope — not a second control plane or a second
foreground tool.  V1 establishes the command surface and the product schema;
the per-trading-day chain identity (state layer) lands in the next slice.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypedDict

from fin_analyse.consultation.contracts import GContextConsumption
from fin_analyse.consultation.product_binding import has_exact_g_product_binding
from fin_analyse.guo_teacher_research.product_contracts import AgentProductContractValidator

MAX_PRIOR_PRODUCT_CONTEXT_CHARS = 12_000


class DailyWorkspaceCheckpoint(StrEnum):
    """FIN-owned checkpoint identity; the timer only passes this enum.

    ``target_at`` is fixed by ``DailyWorkspaceSchedulePolicy`` (next slice);
    the product keeps target/generated/evidence-cutoff times separately so a
    late result is never presented as an on-time fact.
    """

    PREMARKET = "premarket"  # target 09:20
    MORNING_1000 = "morning"  # target 10:00
    CLOSE_1420 = "close"  # target 14:20
    POSTMARKET = "postmarket"  # target 15:30


@dataclass(frozen=True, slots=True)
class DailyWorkspaceVersion:
    """One immutable daily workspace version."""

    workspace_ref: str
    trading_day_id: str
    checkpoint: str  # premarket | morning | close | postmarket（DailyWorkspaceCheckpoint）
    origin: str  # scheduled | on_demand
    product_version: int
    parent_product_version: int | None
    as_of: datetime
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class DailyWorkspaceFirstScreen:
    """First screen: today's most worth handling, why, what changed, unknowns."""

    top_items: list[dict[str, Any]] = field(default_factory=list)
    rationale: list[dict[str, Any]] = field(default_factory=list)
    changes_vs_previous: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    portfolio_review: list[dict[str, Any]] = field(default_factory=list)


class DailyWorkspacePriorProductSnapshot(TypedDict):
    product_version: int
    artifact_hash: str
    content: str
    truncated: bool
    source_boundary: Literal["FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE"]
    consumption_status: Literal["NOT_CONSUMED"]


class DailyWorkspaceInputSnapshot(TypedDict):
    schema: Literal["fin.daily-workspace-input-snapshot/v1"]
    trading_day_id: str
    checkpoint: Literal["on_demand"]
    origin: Literal["on_demand"]
    parent_product_version: int
    parent_artifact_hash: str
    prior_product: DailyWorkspacePriorProductSnapshot
    user_context: dict[str, str]


def bounded_prior_product_snapshot(
    *,
    product_version: int,
    artifact_hash: str,
    product: Mapping[str, object],
) -> DailyWorkspacePriorProductSnapshot:
    content = json.dumps(product, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "product_version": product_version,
        "artifact_hash": artifact_hash,
        "content": content[:MAX_PRIOR_PRODUCT_CONTEXT_CHARS],
        "truncated": len(content) > MAX_PRIOR_PRODUCT_CONTEXT_CHARS,
        "source_boundary": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
        "consumption_status": "NOT_CONSUMED",
    }


def build_product_bound_g_receipt(
    *,
    g_context: GContextConsumption,
    consultation_product: object,
) -> dict[str, object] | None:
    """Build the durable proof that a Daily Workspace conclusion used G.

    ``GContextConsumption`` carries the exact consumed ``(generation,
    source_ref)`` pairs.  The finalized consultation product is their owner.
    Persisting those pairs alongside the consumed status lets every later
    read/render/delivery boundary re-check the product it is about to expose
    without re-running the Agent.
    """

    if not isinstance(consultation_product, Mapping) or not (
        g_context.status == "CONSUMED"
        and g_context.generation
        and has_exact_g_product_binding(
            consultation_product,
            source_refs=g_context.source_refs,
            receipt_references=tuple(
                reference.model_dump(mode="python") for reference in g_context.references
            ),
        )
    ):
        return None
    references = _g_reference_pairs(consultation_product.get("shared_brain_references"))
    assert references is not None
    return {
        "status": "CONSUMED",
        "references": [
            {"generation": generation, "source_ref": source_ref}
            for generation, source_ref in references
        ],
    }


def is_verified_daily_workspace_advisory(product: object) -> bool:
    """Whether a persisted normal Agent answer is safe to expose as advice.

    G remains optional enrichment.  If the answer claims G, it must bind the
    exact consumed receipt; an answer with no G references stays publishable.
    """

    if not isinstance(product, Mapping):
        return False
    generated_via = product.get("generated_via")
    if generated_via not in {"consultation-chain-v1", "l1-direct-v1"}:
        return False
    if product.get("degraded") is True:
        return False
    provenance = product.get("agent_provenance")
    consultation_product = product.get("consultation_product")
    if not isinstance(provenance, Mapping) or not isinstance(consultation_product, Mapping):
        return False
    if not _is_valid_daily_workspace_shape(product, advisory=True):
        return False
    if not AgentProductContractValidator().validate(
        "consultation_product",
        dict(consultation_product),
    ).valid:
        return False
    first_screen = product.get("first_screen")
    top_items = first_screen.get("top_items") if isinstance(first_screen, Mapping) else None
    if product.get("consultation_status") == "completed":
        if (
            not isinstance(top_items, Sequence)
            or isinstance(top_items, (str, bytes))
            or len(top_items) != 1
            or not isinstance(top_items[0], Mapping)
            or top_items[0].get("item") != consultation_product.get("answer_text")
        ):
            return False
    elif top_items:
        return False
    if generated_via == "l1-direct-v1":
        # L1 direct lane: no agent runtime exists, so the honest provenance is
        # the flags being False and the lane identity carried in generation.
        if (
            provenance.get("runtime_invoked_at_generation") is not False
            or provenance.get("output_used") is not False
            or provenance.get("generation") != "l1-direct-v1"
        ):
            return False
    elif (
        provenance.get("output_used") is not True
        or (
            provenance.get("runtime_invoked_at_generation") is not True
            and provenance.get("generation") != "IDEMPOTENCY_REPLAY"
        )
    ):
        return False
    # Source consumption is audited in the machine-owned runtime trace.  The
    # natural-language product does not duplicate source receipts.
    return provenance.get("product_bound_g_receipt") is None


def is_explicit_daily_workspace_availability_notice(product: object) -> bool:
    """Whether a product is the one allowed non-advisory daily notification."""

    if not isinstance(product, Mapping):
        return False
    provenance = product.get("agent_provenance")
    return bool(
        product.get("generated_via") == "deterministic-degraded-v1"
        and product.get("degraded") is True
        and product.get("consultation_product") is None
        and isinstance(provenance, Mapping)
        and isinstance(provenance.get("runtime_invoked_at_generation"), bool)
        and provenance.get("output_used") is False
        and _is_valid_daily_workspace_shape(product, advisory=False)
    )


def is_public_daily_workspace_product(product: object) -> bool:
    """The only two durable Daily Workspace products a public boundary may use."""

    return is_verified_daily_workspace_advisory(product) or (
        is_explicit_daily_workspace_availability_notice(product)
    )


def _g_reference_pairs(value: object) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if not 0 < len(value) <= 32:
        return None
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        generation = item.get("generation")
        source_ref = item.get("source_ref")
        if not isinstance(generation, str) or not generation:
            return None
        if not isinstance(source_ref, str) or not source_ref:
            return None
        pairs.append((generation, source_ref))
    if len(pairs) != len(set(pairs)) or len({source_ref for _, source_ref in pairs}) != len(pairs):
        return None
    return tuple(pairs)


def _is_valid_daily_workspace_shape(
    product: Mapping[str, object],
    *,
    advisory: bool,
) -> bool:
    if (
        product.get("schema_version") != "fin.daily_workspace_product/v1"
        or product.get("checkpoint")
        not in {"premarket", "morning", "close", "postmarket", "on_demand"}
        or product.get("origin") not in {"scheduled", "on_demand"}
        or product.get("consultation_status") not in {"completed", "partial"}
    ):
        return False
    trading_day_id = product.get("trading_day_id")
    if not isinstance(trading_day_id, str):
        return False
    try:
        parsed_day = datetime.strptime(trading_day_id, "%Y-%m-%d")
    except ValueError:
        return False
    if parsed_day.strftime("%Y-%m-%d") != trading_day_id:
        return False
    gaps = product.get("data_gaps")
    first_screen = product.get("first_screen")
    if (
        not _valid_string_sequence(gaps, max_items=64)
        or not isinstance(first_screen, Mapping)
        or "top_items" not in first_screen
        or "unknowns" not in first_screen
        or not _valid_string_sequence(first_screen.get("unknowns"), max_items=64)
        or not all(
            _valid_mapping_sequence(first_screen.get(field, ()), max_items=64)
            for field in (
                "top_items",
                "rationale",
                "changes_vs_previous",
                "portfolio_review",
            )
        )
    ):
        return False
    top_items = first_screen.get("top_items")
    if not isinstance(top_items, Sequence) or isinstance(top_items, (str, bytes)):
        return False
    if advisory:
        boundaries = product.get("context_boundaries")
        receipt = product.get("input_snapshot_receipt")
        if (
            not isinstance(boundaries, Mapping)
            or boundaries.get("prior_product")
            != "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE"
            or boundaries.get("user_question") != "NOT_EVIDENCE"
            or not isinstance(receipt, Mapping)
            or receipt.get("schema") != "fin.daily-workspace-input-receipt/v1"
            or not _aware_iso_datetime(receipt.get("consultation_as_of"))
            or (product.get("consultation_status") == "completed" and not top_items)
        ):
            return False
        return all(
            isinstance(item.get("item"), str)
            and bool(item["item"])
            and item.get("disposition")
            in {None, "OBSERVE", "MANUAL_REVIEW", "NO_ACTION"}
            for item in top_items
            if isinstance(item, Mapping)
        )
    return bool(
        gaps
        and not top_items
        and not first_screen.get("rationale")
        and not first_screen.get("changes_vs_previous")
        and not first_screen.get("portfolio_review")
    )


def _valid_string_sequence(value: object, *, max_items: int) -> bool:
    return bool(
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) <= max_items
        and all(isinstance(item, str) and item for item in value)
    )


def _valid_mapping_sequence(value: object, *, max_items: int) -> bool:
    return bool(
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) <= max_items
        and all(isinstance(item, Mapping) for item in value)
    )


def _aware_iso_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return bool(
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.isoformat() == value
    )


# The candidate/final product schema is expressed declaratively in the
# consultation product contract; this module owns the version identity and
# first-screen shape only.
