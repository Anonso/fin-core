"""Target-bound deep typed claim contract for the consultation G explanation module."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from fin_analyse.guo_teacher_research.product_contracts import ProductContract

_FORBIDDEN_OPERATION_FIELDS = (
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
    "broker",
    "execution_authority",
)


def guo_explanation_product_contract() -> ProductContract:
    """Return the FIN-owned zero-tool contract for source-bound claims."""

    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return ProductContract(
        contract_id="guo_explanation_product",
        version="v1",
        required_fields=("contract_id", "contract_version", "target", "claims"),
        forbidden_fields=_FORBIDDEN_OPERATION_FIELDS,
        public_fields=("contract_id", "contract_version", "target", "claims"),
        canonical_json_schema=json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _schema() -> dict[str, object]:
    refs = {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 256},
        "maxItems": 16,
        "uniqueItems": True,
    }
    claim = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "kind",
            "derivation",
            "text",
            "g_source_refs",
            "reference_source_refs",
        ],
        "properties": {
            "kind": {
                "enum": [
                    "SUMMARY",
                    "BASIS",
                    "RISK",
                    "WATCH",
                    "INVALIDATION",
                    "UNKNOWN",
                ]
            },
            "derivation": {
                "enum": [
                    "G_DIRECT",
                    "G_INFERENCE",
                    "G_MAPPED_INFERENCE",
                    "NON_G_REFERENCE",
                    "UNKNOWN",
                ]
            },
            "text": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "g_source_refs": refs,
            "reference_source_refs": refs,
        },
        "allOf": [
            {
                "if": {
                    "properties": {"kind": {"const": "UNKNOWN"}},
                    "required": ["kind"],
                },
                "then": {
                    "properties": {
                        "derivation": {"const": "UNKNOWN"},
                        "g_source_refs": {"maxItems": 0},
                        "reference_source_refs": {"maxItems": 0},
                    }
                },
            },
            {
                "if": {
                    "properties": {"derivation": {"const": "UNKNOWN"}},
                    "required": ["derivation"],
                },
                "then": {
                    "properties": {
                        "kind": {"const": "UNKNOWN"},
                        "g_source_refs": {"maxItems": 0},
                        "reference_source_refs": {"maxItems": 0},
                    }
                },
            },
            {
                "if": {
                    "properties": {"derivation": {"enum": ["G_DIRECT", "G_INFERENCE"]}},
                    "required": ["derivation"],
                },
                "then": {
                    "properties": {
                        "g_source_refs": {"minItems": 1},
                        "reference_source_refs": {"maxItems": 0},
                    }
                },
            },
            {
                "if": {
                    "properties": {"derivation": {"const": "G_MAPPED_INFERENCE"}},
                    "required": ["derivation"],
                },
                "then": {
                    "properties": {
                        "g_source_refs": {"minItems": 1},
                        "reference_source_refs": {"minItems": 1},
                    }
                },
            },
            {
                "if": {
                    "properties": {"derivation": {"const": "NON_G_REFERENCE"}},
                    "required": ["derivation"],
                },
                "then": {
                    "properties": {
                        "g_source_refs": {"maxItems": 0},
                        "reference_source_refs": {"minItems": 1},
                    }
                },
            },
            {
                "if": {
                    "properties": {"derivation": {"const": "UNKNOWN"}},
                    "required": ["derivation"],
                },
                "then": {
                    "properties": {
                        "g_source_refs": {"maxItems": 0},
                        "reference_source_refs": {"maxItems": 0},
                    }
                },
            },
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["contract_id", "contract_version", "target", "claims"],
        "properties": {
            "contract_id": {"const": "guo_explanation_product"},
            "contract_version": {"const": "v1"},
            "target": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ticker", "name"],
                "properties": {
                    "ticker": {
                        "type": "string",
                        "pattern": "^[0-9]{6}\\.(SH|SZ|BJ)$",
                    },
                    "name": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 120,
                    },
                },
            },
            "claims": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": claim,
            },
        },
    }
