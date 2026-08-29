"""Agent Product Contract Registry and Validator.

Defines product contracts for GuoTeacher agent outputs and validates
payloads against them.  This is a pure programmatic registry — it does
not replace existing dataclasses, call LLMs, or act as a runtime hard gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

_FINAL_OPERATION_FIELDS = (
    "action",
    "buy",
    "sell",
    "execute",
    "order",
    "order_instruction",
    "position",
    "position_pct",
    "position_size",
    "entry",
    "entry_price",
    "entry_timing",
    "exit",
    "exit_price",
    "exit_timing",
    "target_price",
    "stop_loss",
    "stop_loss_price",
    "current_advice",
)

# ═══════════════════════════════════════════════════════════════════════════════
# Core Models
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ProductContract:
    """A declaration of what downstream consumers can rely on for a product.

    This is the contract, not the implementation.  Domain models and
    deterministic projectors remain the source of payload generation; this
    registry declares what fields are promised, forbidden, public, or internal.
    """

    contract_id: str
    version: str
    required_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    forbidden_fields: tuple[str, ...] = ()
    public_fields: tuple[str, ...] = ()
    internal_fields: tuple[str, ...] = ()
    required_boundary_fields: tuple[str, ...] = ()
    canonical_json_schema: str = ""

    def json_schema(self) -> dict[str, Any] | None:
        """Return an isolated schema projection for prompts and hard gates."""

        if not self.canonical_json_schema:
            return None
        value = json.loads(self.canonical_json_schema)
        if not isinstance(value, dict):
            raise ValueError("product contract JSON schema must be an object")
        return value


@dataclass(frozen=True)
class ContractValidationResult:
    """Result of validating a payload against a product contract."""

    valid: bool
    contract_id: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# ═══════════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════════


class AgentProductContractRegistry:
    """Registry of all agent product contracts.

    Usage::

        registry = AgentProductContractRegistry()
        contract = registry.get("morning_strategy")
        for c in registry.all().values():
            ...
    """

    def __init__(self) -> None:
        self._contracts: dict[str, ProductContract] = {
            c.contract_id: c for c in _build_mvp_contracts()
        }

    def get(self, contract_id: str) -> ProductContract | None:
        """Return the contract for *contract_id*, or None if not found."""
        return self._contracts.get(contract_id)

    def all(self) -> dict[str, ProductContract]:
        """Return all registered contracts keyed by contract_id."""
        return dict(self._contracts)


def _build_mvp_contracts() -> list[ProductContract]:
    """Build the active semantic product contracts."""

    from fin_analyse.consultation.guo_explanation_product_contracts import (
        guo_explanation_product_contract,
    )
    from fin_analyse.consultation.product_contracts import consultation_product_contract
    from fin_analyse.consultation.tactical_method_product_contracts import (
        a_share_tactical_method_product_contract,
    )
    from fin_analyse.consultation.tactical_product_contracts import (
        a_share_tactical_consultation_product_contract,
    )

    research_product = ProductContract(
        contract_id="research_product",
        version="v1",
        required_fields=(
            "answer_summary",
            "sections",
            "source_level",
            "mainlines",
            "candidates",
            "shared_brain_references",
        ),
        optional_fields=(
            "single_stock",
            "portfolio",
            "decision_drafts",
            "source_attribution",
            "market_overview_references",
            "quality_report",
            "quality_repair",
        ),
        forbidden_fields=_FINAL_OPERATION_FIELDS,
        public_fields=(
            "answer_summary",
            "sections",
            "source_level",
            "single_stock",
            "portfolio",
            "mainlines",
            "candidates",
            "decision_drafts",
            "shared_brain_references",
            "source_attribution",
            "market_overview_references",
            "quality_report",
            "quality_repair",
        ),
    )

    morning_strategy = ProductContract(
        contract_id="morning_strategy",
        version="v1",
        required_fields=(
            "status",
            "today_thesis",
            "g_mainline_updates",
            "portfolio_review_order",
            "watch_triggers",
            "risk_brakes",
            "no_trade_conditions",
            "manual_confirmation_checks",
            "data_gaps",
            "boundary",
        ),
        optional_fields=(
            "context_pack_id",
            "context_source_scope",
        ),
        forbidden_fields=(
            "action",
            "order",
            "position",
            "recommendation_tier",
            "decision_draft",
            "risk_level",
        ),
        public_fields=(
            "status",
            "today_thesis",
            "g_mainline_updates",
            "portfolio_review_order",
            "watch_triggers",
            "risk_brakes",
            "no_trade_conditions",
            "manual_confirmation_checks",
            "data_gaps",
            "boundary",
        ),
    )

    display_product = ProductContract(
        contract_id="display_product",
        version="v2",
        required_fields=(
            "display_intent",
            "headline",
            "short_answer",
            "primary_sections",
            "candidate_profiles",
            "analysis_path_summary",
            "confidence_boundary",
            "next_actions",
            "omitted_details_summary",
            "planner_source",
        ),
        optional_fields=(
            "presentation",
            "detail_sections",
            "source_attribution",
            "source_legend",
            "quality_report",
            "quality_repair",
        ),
        forbidden_fields=(
            *_FINAL_OPERATION_FIELDS,
            "risk_level",
            "raw_chain_of_thought",
            "backend_attempts",
            "recommendation_tier",
            "decision_draft",
        ),
        public_fields=(
            "display_intent",
            "headline",
            "short_answer",
            "primary_sections",
            "candidate_profiles",
            "analysis_path_summary",
            "confidence_boundary",
            "next_actions",
            "omitted_details_summary",
            "planner_source",
            "presentation",
            "detail_sections",
            "source_attribution",
            "source_legend",
            "quality_report",
            "quality_repair",
        ),
        internal_fields=("risk_level",),
        required_boundary_fields=(
            "confidence_boundary.advisory_only",
            "confidence_boundary.execution_allowed",
            "confidence_boundary.human_confirmation_required",
        ),
    )

    return [
        research_product,
        morning_strategy,
        display_product,
        guo_explanation_product_contract(),
        consultation_product_contract(),
        a_share_tactical_method_product_contract(),
        a_share_tactical_consultation_product_contract(),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Validator
# ═══════════════════════════════════════════════════════════════════════════════


class AgentProductContractValidator:
    """Validates a payload dict against a product contract.

    MVP checks:
    1. Top-level required fields are present.
    2. Forbidden fields are absent (recursive scan).
    3. Internal fields are not in public payload (only for contracts
       that declare internal_fields).

    Contracts without a canonical JSON Schema retain the legacy field gates.
    Contracts that declare one additionally receive full nested validation;
    the shared Runner decides whether the validated candidate may cross the
    runtime boundary.
    """

    def __init__(self, registry: AgentProductContractRegistry | None = None) -> None:
        self._registry = registry or AgentProductContractRegistry()

    def validate(self, contract_id: str, payload: dict[str, Any]) -> ContractValidationResult:
        """Validate *payload* against the contract named *contract_id*.

        Returns a ContractValidationResult with valid=True/False and
        a tuple of error messages.
        """
        contract = self._registry.get(contract_id)
        if contract is None:
            return ContractValidationResult(
                valid=False,
                contract_id=contract_id,
                errors=(f"Unknown contract_id: {contract_id}",),
            )

        errors: list[str] = []

        # 1. Required fields
        for req_field in contract.required_fields:
            if req_field not in payload:
                errors.append(f"Missing required field '{req_field}' in {contract_id}")

        # 2. Forbidden fields (recursive)
        forbidden = set(contract.forbidden_fields)
        if forbidden:
            _collect_forbidden_violations(payload, forbidden, contract_id, path="$", errors=errors)

        # 3. Internal fields exposed in public payload
        internal = set(contract.internal_fields)
        if internal:
            for field in internal:
                if field in payload:
                    errors.append(
                        f"Internal field '{field}' must not appear in public {contract_id} payload"
                    )

        # 4. Full nested JSON Schema when the contract declares one.  The
        # schema is FIN-owned and canonical; runtime output never supplies it.
        try:
            schema = contract.json_schema()
            if schema is not None:
                validator = Draft202012Validator(schema)
                nested_errors = sorted(
                    validator.iter_errors(payload),
                    key=lambda item: tuple(str(part) for part in item.absolute_path),
                )
                if nested_errors:
                    errors.append(f"JSON schema violation in {contract_id}")
        except (SchemaError, TypeError, ValueError, json.JSONDecodeError):
            errors.append(f"Invalid FIN-owned JSON schema for {contract_id}")

        return ContractValidationResult(
            valid=len(errors) == 0,
            contract_id=contract_id,
            errors=tuple(errors),
        )


def product_contract_projection(contract: ProductContract) -> dict[str, Any]:
    """Project one FIN-owned contract into a provider-neutral runtime request."""

    projection: dict[str, Any] = {
        "contract_id": contract.contract_id,
        "version": contract.version,
        "required_fields": list(contract.required_fields),
        "optional_fields": list(contract.optional_fields),
        "forbidden_fields": list(contract.forbidden_fields),
        "public_fields": list(contract.public_fields),
    }
    schema = contract.json_schema()
    if schema is not None:
        projection["json_schema"] = schema
    return projection


def _canonical_schema_json(schema: dict[str, Any]) -> str:
    Draft202012Validator.check_schema(schema)
    return json.dumps(
        schema,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _collect_forbidden_violations(
    node: Any,
    forbidden: set[str],
    contract_id: str,
    path: str,
    *,
    errors: list[str],
) -> None:
    """Recursively scan *node* for forbidden field names.

    Traverses dict keys and list items.  When a forbidden key is found,
    appends a human-readable error to *errors*.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            current_path = f"{path}.{key}"
            if key in forbidden:
                errors.append(f"Forbidden field '{key}' found at {current_path} in {contract_id}")
            # Recurse into values
            _collect_forbidden_violations(
                value, forbidden, contract_id, current_path, errors=errors
            )
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _collect_forbidden_violations(
                item, forbidden, contract_id, f"{path}[{idx}]", errors=errors
            )
    # scalars: nothing to scan
