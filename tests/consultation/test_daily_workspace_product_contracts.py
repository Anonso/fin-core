"""Public-safety contract for persisted Daily Workspace products."""

from __future__ import annotations

from fin_analyse.consultation.daily_workspace_product_contracts import (
    is_explicit_daily_workspace_availability_notice,
    is_public_daily_workspace_product,
    is_verified_daily_workspace_advisory,
)


def _consultation_product() -> dict[str, object]:
    return {
        "contract_id": "consultation_product",
        "contract_version": "v1",
        "answer_text": "当前证据只支持继续观察。",
    }


def _advisory_product() -> dict[str, object]:
    return {
        "schema_version": "fin.daily_workspace_product/v1",
        "workspace_ref": "workspace-opaque-ref",
        "product_version": 1,
        "parent_product_version": 0,
        "checkpoint": "premarket",
        "trading_day_id": "2026-08-10",
        "origin": "scheduled",
        "generated_via": "consultation-chain-v1",
        "consultation_status": "completed",
        "degraded": False,
        "agent_provenance": {
            "runtime_invoked_at_generation": True,
            "output_used": True,
            "product_bound_g_receipt": None,
        },
        "context_boundaries": {
            "prior_product": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
            "user_question": "NOT_EVIDENCE",
        },
        "input_snapshot_receipt": {
            "schema": "fin.daily-workspace-input-receipt/v1",
            "consultation_as_of": "2026-08-10T09:10:00+08:00",
        },
        "first_screen": {
            "top_items": [
                {"item": "当前证据只支持继续观察。", "disposition": "OBSERVE"}
            ],
            "rationale": [],
            "changes_vs_previous": [],
            "unknowns": [],
            "portfolio_review": [],
        },
        "data_gaps": [],
        "consultation_product": _consultation_product(),
    }


def test_normal_daily_advisory_uses_the_same_complete_answer_product() -> None:
    product = _advisory_product()

    assert is_verified_daily_workspace_advisory(product)
    assert is_public_daily_workspace_product(product)


def test_replayed_agent_answer_remains_public() -> None:
    product = _advisory_product()
    product["agent_provenance"] = {
        "runtime_invoked_at_generation": False,
        "output_used": True,
        "generation": "IDEMPOTENCY_REPLAY",
        "product_bound_g_receipt": None,
    }

    assert is_public_daily_workspace_product(product)


def test_normal_daily_advisory_rejects_malformed_or_rewritten_answer() -> None:
    product = _advisory_product()
    product["first_screen"] = {}
    assert not is_public_daily_workspace_product(product)

    product = _advisory_product()
    product["consultation_product"] = {"answer_text": "missing contract identity"}
    assert not is_public_daily_workspace_product(product)

    product = _advisory_product()
    product["first_screen"] = {
        "top_items": [{"item": "绕过合同的另一段答案", "disposition": "OBSERVE"}],
        "rationale": [],
        "changes_vs_previous": [],
        "unknowns": [],
        "portfolio_review": [],
    }
    assert not is_public_daily_workspace_product(product)


def test_answer_product_does_not_duplicate_source_receipts() -> None:
    product = _advisory_product()
    product["agent_provenance"] = {
        "runtime_invoked_at_generation": True,
        "output_used": True,
        "product_bound_g_receipt": {
            "status": "CONSUMED",
            "references": [{"generation": "g-generation-1", "source_ref": "g:1"}],
        },
    }

    assert not is_public_daily_workspace_product(product)


def test_only_explicit_degraded_payload_is_a_non_advisory_notice() -> None:
    product = {
        "schema_version": "fin.daily_workspace_product/v1",
        "checkpoint": "premarket",
        "trading_day_id": "2026-08-10",
        "origin": "scheduled",
        "generated_via": "deterministic-degraded-v1",
        "consultation_status": "partial",
        "degraded": True,
        "agent_provenance": {
            "runtime_invoked_at_generation": False,
            "output_used": False,
        },
        "consultation_product": None,
        "first_screen": {
            "top_items": [],
            "rationale": [],
            "changes_vs_previous": [],
            "unknowns": ["daily_workspace_preparation_unavailable"],
            "portfolio_review": [],
        },
        "data_gaps": ["daily_workspace_preparation_unavailable"],
    }

    assert is_explicit_daily_workspace_availability_notice(product)
    assert is_public_daily_workspace_product(product)

    product["consultation_product"] = {"answer_text": "不能伪装成咨询"}
    assert not is_explicit_daily_workspace_availability_notice(product)
    assert not is_public_daily_workspace_product(product)
