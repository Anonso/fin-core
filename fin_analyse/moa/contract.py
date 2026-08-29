"""MoA Kernel Contract — validates and normalizes MoA deliberation results.

Contract hardening ensures that every MoAEngine.deliberate() result carries
explicit source/risk boundaries, data gap markers, and advisory-only flags
before downstream consumers interpret it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from fin_analyse.moa.models import MoARequest

MOA_CONTRACT_VERSION = "1.0.0"


@dataclass(frozen=True)
class MoAContractCheck:
    """Result of a single MoA kernel contract validation step."""

    ok: bool
    reason: str = ""
    missing_required: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)


class MoAKernelContract:
    """Validates inputs and normalizes outputs across the MoA deliberation seam.

    Two-phase lifecycle:
    1. validate_request — cheap pre-flight before any LLM call.
    2. validate_final   — structural guard after aggregator JSON parse.
    3. normalize_boundary — ensure every final dict carries data_gaps,
       source_boundary, and risk_boundary markers.
    """

    @staticmethod
    def validate_request(request: MoARequest) -> MoAContractCheck:
        """Validate the MoA request before deliberation begins."""
        if not request.task_id:
            return MoAContractCheck(ok=False, reason="missing task_id")
        if not request.task_type:
            return MoAContractCheck(ok=False, reason="missing task_type")
        if not request.aggregator_prompt:
            return MoAContractCheck(ok=False, reason="missing aggregator_prompt")
        for field_name in (
            "reference_timeout_seconds",
            "aggregator_timeout_seconds",
        ):
            value = getattr(request, field_name)
            if value is not None and (not isfinite(value) or value <= 0):
                return MoAContractCheck(ok=False, reason=f"invalid {field_name}")
        return MoAContractCheck(ok=True)

    @staticmethod
    def validate_final(
        final: dict[str, Any],
        request: MoARequest,
    ) -> MoAContractCheck:
        """Validate the aggregator final output against expected_schema.required.

        Returns ok=False with missing_required populated when the final dict
        is missing fields that expected_schema declares as required.
        """
        schema = request.expected_schema
        if schema is None:
            return MoAContractCheck(ok=True)

        required = schema.get("required", [])
        if not isinstance(required, list) or not required:
            return MoAContractCheck(ok=True)

        missing = [field for field in required if field not in final or final.get(field) is None]
        if missing:
            return MoAContractCheck(
                ok=False,
                reason=f"missing required fields: {missing}",
                missing_required=missing,
            )

        return MoAContractCheck(ok=True)

    @staticmethod
    def normalize_boundary(final: dict[str, Any]) -> dict[str, Any]:
        """Ensure every final dict carries boundary fields with safe defaults.

        Downstream consumers (claims, research, signal review, portfolio advice,
        persona MoA) MUST find these fields present — even when the aggregator
        LLM did not emit them.
        """
        result = dict(final)
        result.setdefault("data_gaps", [])
        result.setdefault(
            "source_boundary",
            {"advisory_only": True},
        )
        result.setdefault(
            "risk_boundary",
            {"human_confirmation_required": True},
        )
        return result
