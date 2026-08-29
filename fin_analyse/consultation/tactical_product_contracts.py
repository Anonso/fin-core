"""Strict Agent product for on-demand A-share tactical consultation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from fin_analyse.guo_teacher_research.product_contracts import ProductContract

_FORBIDDEN_TRADING_FIELDS = (
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


def a_share_tactical_consultation_product_contract() -> ProductContract:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return ProductContract(
        contract_id="a_share_tactical_consultation_product",
        version="v1",
        required_fields=(
            "contract_id",
            "contract_version",
            "as_of",
            "valid_until",
            "technical_assessments",
            "consultation_answer",
            "agent_reconciliation",
            "source_boundary_statement",
            "confidence_boundary",
        ),
        forbidden_fields=_FORBIDDEN_TRADING_FIELDS,
        public_fields=(
            "contract_id",
            "contract_version",
            "as_of",
            "valid_until",
            "technical_assessments",
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
    assessment = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "symbol",
            "evidence_id",
            "status",
            "trend",
            "momentum",
            "volatility",
            "summary",
            "watch_conditions",
            "invalidation_conditions",
            "risks",
            "data_gaps",
        ],
        "properties": {
            "symbol": {"type": "string", "pattern": "^[0-9]{6}\\.(SH|SZ|BJ)$"},
            "evidence_id": {
                "type": "string",
                "pattern": "^market-evidence-[0-9a-f]{24}$",
            },
            "status": {"enum": ["READY", "PARTIAL", "UNKNOWN"]},
            "trend": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "momentum": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "volatility": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "watch_conditions": text_list,
            "invalidation_conditions": text_list,
            "risks": text_list,
            "data_gaps": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,95}$"},
                "maxItems": 32,
                "uniqueItems": True,
            },
        },
    }
    answer = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "headline",
            "summary",
            "disposition",
            "no_action",
            "manual_review_targets",
            "decision_basis",
            "watch_conditions",
            "invalidation_conditions",
            "risks",
            "unknowns",
        ],
        "properties": {
            "headline": {"type": "string", "minLength": 1, "maxLength": 300},
            "summary": {"type": "string", "minLength": 1, "maxLength": 3_000},
            "disposition": {"enum": ["OBSERVE", "MANUAL_REVIEW", "NO_ACTION"]},
            "no_action": {"type": "boolean"},
            "manual_review_targets": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[0-9]{6}\\.(SH|SZ|BJ)$"},
                "maxItems": 5,
                "uniqueItems": True,
            },
            "decision_basis": text_list,
            "watch_conditions": text_list,
            "invalidation_conditions": text_list,
            "risks": text_list,
            "unknowns": text_list,
        },
        "allOf": [
            {
                "if": {"properties": {"disposition": {"const": "MANUAL_REVIEW"}}},
                "then": {
                    "properties": {
                        "no_action": {"const": False},
                        "manual_review_targets": {"minItems": 1},
                    }
                },
                "else": {
                    "properties": {
                        "no_action": {"const": True},
                        "manual_review_targets": {"maxItems": 0},
                    }
                },
            }
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "contract_id",
            "contract_version",
            "as_of",
            "valid_until",
            "technical_assessments",
            "consultation_answer",
            "agent_reconciliation",
            "source_boundary_statement",
            "confidence_boundary",
        ],
        "properties": {
            "contract_id": {"const": "a_share_tactical_consultation_product"},
            "contract_version": {"const": "v1"},
            "as_of": {"type": "string", "format": "date-time"},
            "valid_until": {"type": "string", "format": "date-time"},
            "technical_assessments": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": assessment,
            },
            "consultation_answer": answer,
            "agent_reconciliation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["guo_status", "relationship"],
                "properties": {
                    "guo_status": {"enum": ["READY", "PARTIAL", "UNKNOWN"]},
                    "relationship": {
                        "enum": ["CONSISTENT", "CONFLICT", "COMPLEMENTARY", "UNKNOWN"]
                    },
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


__all__ = ["a_share_tactical_consultation_product_contract"]
