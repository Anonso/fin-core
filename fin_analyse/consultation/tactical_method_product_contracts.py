"""Strict product contract for the tactical Agent's methodology-only mode."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from fin_analyse.guo_teacher_research.product_contracts import ProductContract

_FORBIDDEN_OPERATION_FIELDS = (
    "action",
    "buy",
    "sell",
    "execute",
    "execution",
    "order",
    "order_instruction",
    "broker",
    "approval",
    "authority",
    "position",
    "position_pct",
    "position_size",
    "sizing",
    "entry",
    "entry_price",
    "entry_timing",
    "exit",
    "exit_price",
    "exit_timing",
    "target",
    "target_price",
    "stop",
    "stop_loss",
    "stop_loss_price",
)


def a_share_tactical_method_product_contract() -> ProductContract:
    """Return the zero-market, observe-only Z methodology contract."""

    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return ProductContract(
        contract_id="a_share_tactical_method_product",
        version="v1",
        required_fields=(
            "contract_id",
            "contract_version",
            "mode",
            "as_of",
            "current_market_evaluated",
            "technical_method",
            "consultation_answer",
            "agent_reconciliation",
            "source_boundary_statement",
            "confidence_boundary",
        ),
        forbidden_fields=_FORBIDDEN_OPERATION_FIELDS,
        public_fields=(
            "contract_id",
            "contract_version",
            "mode",
            "as_of",
            "current_market_evaluated",
            "technical_method",
            "consultation_answer",
            "agent_reconciliation",
            "source_boundary_statement",
            "confidence_boundary",
        ),
        required_boundary_fields=(
            "confidence_boundary.advisory_only",
            "confidence_boundary.execution_allowed",
            "confidence_boundary.human_confirmation_required",
        ),
        canonical_json_schema=json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _schema() -> dict[str, object]:
    text_list = {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "maxItems": 16,
        "uniqueItems": True,
    }
    dimension = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "name",
            "purpose",
            "evidence_to_observe",
            "can_support",
            "cannot_support",
        ],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "evidence_to_observe": {**text_list, "minItems": 1},
            "can_support": {**text_list, "minItems": 1},
            "cannot_support": {**text_list, "minItems": 1},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "contract_id",
            "contract_version",
            "mode",
            "as_of",
            "current_market_evaluated",
            "technical_method",
            "consultation_answer",
            "agent_reconciliation",
            "source_boundary_statement",
            "confidence_boundary",
        ],
        "properties": {
            "contract_id": {"const": "a_share_tactical_method_product"},
            "contract_version": {"const": "v1"},
            "mode": {"const": "METHOD_EXPLANATION"},
            "as_of": {"type": "string", "format": "date-time"},
            "current_market_evaluated": {"const": False},
            "technical_method": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary", "dimensions", "data_gaps"],
                "properties": {
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2_000,
                    },
                    "dimensions": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 8,
                        "items": dimension,
                    },
                    "data_gaps": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "pattern": "^[A-Z][A-Z0-9_]{0,95}$",
                        },
                        "maxItems": 32,
                        "uniqueItems": True,
                    },
                },
            },
            "consultation_answer": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "headline",
                    "summary",
                    "disposition",
                    "no_action",
                    "decision_basis",
                    "watch_conditions",
                    "unknowns",
                ],
                "properties": {
                    "headline": {"type": "string", "minLength": 1, "maxLength": 300},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 3_000},
                    "disposition": {"const": "OBSERVE"},
                    "no_action": {"const": True},
                    "decision_basis": text_list,
                    "watch_conditions": text_list,
                    "unknowns": text_list,
                },
            },
            "agent_reconciliation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["guo_status", "relationship"],
                "properties": {
                    "guo_status": {"enum": ["READY", "PARTIAL", "UNKNOWN"]},
                    "relationship": {"enum": ["COMPLEMENTARY", "INDEPENDENT", "UNKNOWN"]},
                },
            },
            "source_boundary_statement": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1_000,
            },
            "confidence_boundary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "advisory_only",
                    "execution_allowed",
                    "human_confirmation_required",
                ],
                "properties": {
                    "advisory_only": {"const": True},
                    "execution_allowed": {"const": False},
                    "human_confirmation_required": {"const": True},
                },
            },
        },
    }


__all__ = ["a_share_tactical_method_product_contract"]
