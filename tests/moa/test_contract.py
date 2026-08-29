"""Tests for MoA kernel contract validation and normalization."""

from __future__ import annotations

from fin_analyse.moa.contract import (
    MOA_CONTRACT_VERSION,
    MoAContractCheck,
    MoAKernelContract,
)
from fin_analyse.moa.models import MoARequest


class TestMoAKernelContractValidateFinal:
    def test_validate_final_rejects_missing_required_fields(self):
        """When expected_schema lists required fields that the final dict
        is missing, validate_final must return ok=False with the missing
        field names."""
        request = MoARequest(
            task_id="test-1",
            task_type="unit_test",
            context={},
            aggregator_prompt="请综合。",
            expected_schema={
                "type": "object",
                "required": ["claims", "consensus", "confidence"],
            },
        )
        final: dict = {"consensus": ["some consensus"]}

        check = MoAKernelContract.validate_final(final, request)

        assert check.ok is False
        assert "claims" in check.missing_required
        assert "confidence" in check.missing_required

    def test_validate_final_passes_when_all_required_present(self):
        """When all required fields are present, validate_final returns ok=True."""
        request = MoARequest(
            task_id="test-2",
            task_type="unit_test",
            context={},
            aggregator_prompt="请综合。",
            expected_schema={
                "type": "object",
                "required": ["claims", "consensus", "confidence"],
            },
        )
        final: dict = {
            "claims": [],
            "consensus": [],
            "confidence": 0.8,
        }

        check = MoAKernelContract.validate_final(final, request)

        assert check.ok is True
        assert check.missing_required == []

    def test_validate_final_passes_when_no_expected_schema(self):
        """When request has no expected_schema, validation is skipped (ok=True)."""
        request = MoARequest(
            task_id="test-3",
            task_type="unit_test",
            context={},
            aggregator_prompt="请综合。",
            expected_schema=None,
        )
        final: dict = {}

        check = MoAKernelContract.validate_final(final, request)

        assert check.ok is True

    def test_validate_final_rejects_null_required_fields(self):
        """Fields present in final but set to None are treated as missing."""
        request = MoARequest(
            task_id="test-4",
            task_type="unit_test",
            context={},
            aggregator_prompt="请综合。",
            expected_schema={
                "type": "object",
                "required": ["claims", "confidence"],
            },
        )
        final: dict = {"claims": None, "confidence": 0.5}

        check = MoAKernelContract.validate_final(final, request)

        assert check.ok is False
        assert "claims" in check.missing_required


class TestMoAKernelContractNormalizeBoundary:
    def test_normalize_boundary_adds_default_fields_when_missing(self):
        """When the final dict lacks boundary fields, normalize adds safe defaults."""
        final: dict = {"answer": "ok"}

        normalized = MoAKernelContract.normalize_boundary(final)

        assert normalized["answer"] == "ok"
        assert "data_gaps" in normalized
        assert isinstance(normalized["data_gaps"], list)
        assert "source_boundary" in normalized
        assert normalized["source_boundary"]["advisory_only"] is True
        assert "risk_boundary" in normalized
        assert normalized["risk_boundary"]["human_confirmation_required"] is True

    def test_normalize_boundary_preserves_existing_boundary_fields(self):
        """Existing boundary fields are preserved, not overwritten."""
        final: dict = {
            "answer": "ok",
            "data_gaps": ["no price data for 2026-07-06"],
            "source_boundary": {"advisory_only": True, "max_confidence": 0.6},
            "risk_boundary": {
                "human_confirmation_required": True,
                "max_position_pct": 5.0,
            },
        }

        normalized = MoAKernelContract.normalize_boundary(final)

        assert normalized["data_gaps"] == ["no price data for 2026-07-06"]
        assert normalized["source_boundary"]["max_confidence"] == 0.6
        assert normalized["risk_boundary"]["max_position_pct"] == 5.0


class TestMoAKernelContractValidateRequest:
    def test_validate_request_rejects_missing_task_id(self):
        """Empty task_id should fail request validation."""
        request = MoARequest(
            task_id="",
            task_type="unit_test",
            context={},
            aggregator_prompt="请综合。",
        )

        check = MoAKernelContract.validate_request(request)

        assert check.ok is False
        assert "task_id" in check.reason

    def test_validate_request_rejects_empty_task_type(self):
        """Empty task_type should fail request validation."""
        request = MoARequest(
            task_id="test-ok",
            task_type="",
            context={},
            aggregator_prompt="请综合。",
        )

        check = MoAKernelContract.validate_request(request)

        assert check.ok is False
        assert "task_type" in check.reason

    def test_validate_request_rejects_missing_aggregator_prompt(self):
        """Empty aggregator_prompt should fail request validation."""
        request = MoARequest(
            task_id="test-ok",
            task_type="unit_test",
            context={},
            aggregator_prompt="",
        )

        check = MoAKernelContract.validate_request(request)

        assert check.ok is False
        assert "aggregator_prompt" in check.reason

    def test_validate_request_passes_for_valid_request(self):
        """A well-formed request passes validation."""
        request = MoARequest(
            task_id="test-ok",
            task_type="unit_test",
            context={},
            aggregator_prompt="请综合。",
        )

        check = MoAKernelContract.validate_request(request)

        assert check.ok is True


class TestMoAContractCheck:
    def test_contract_check_ok_defaults(self):
        """MoAContractCheck defaults are sensible."""
        check = MoAContractCheck(ok=True)
        assert check.ok is True
        assert check.reason == ""
        assert check.missing_required == []
        assert check.data_gaps == []

    def test_contract_check_not_ok_with_reason(self):
        """MoAContractCheck captures reason and missing fields."""
        check = MoAContractCheck(
            ok=False,
            reason="missing required fields: ['claims']",
            missing_required=["claims"],
        )
        assert check.ok is False
        assert "claims" in check.reason
        assert check.missing_required == ["claims"]


def test_moa_contract_version_is_defined():
    """MOA_CONTRACT_VERSION must be a non-empty string."""
    assert isinstance(MOA_CONTRACT_VERSION, str)
    assert len(MOA_CONTRACT_VERSION) > 0
