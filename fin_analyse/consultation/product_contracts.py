"""Small final product contract for one FIN advisory answer."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from fin_analyse.guo_teacher_research.product_contracts import ProductContract

_FIELDS = ("contract_id", "contract_version", "answer_text")
MAX_CONSULTATION_ANSWER_CHARS = 6_500
_FORBIDDEN_OPERATION_FIELDS = (
    "action",
    "buy",
    "sell",
    "execute",
    "order",
    "broker",
    "execution_authority",
)


def consultation_product_contract() -> ProductContract:
    """The Agent owns prose; FIN owns trace, identity and side-effect safety."""

    schema: dict[str, object] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": list(_FIELDS),
        "properties": {
            "contract_id": {"type": "string", "const": "consultation_product"},
            "contract_version": {"type": "string", "const": "v1"},
            "answer_text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CONSULTATION_ANSWER_CHARS,
            },
        },
    }
    Draft202012Validator.check_schema(schema)
    return ProductContract(
        contract_id="consultation_product",
        version="v1",
        required_fields=_FIELDS,
        forbidden_fields=_FORBIDDEN_OPERATION_FIELDS,
        public_fields=_FIELDS,
        canonical_json_schema=json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


__all__ = ["MAX_CONSULTATION_ANSWER_CHARS", "consultation_product_contract"]
