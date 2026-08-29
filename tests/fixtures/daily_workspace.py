"""Canonical Daily Workspace products for state-owner tests."""

from __future__ import annotations


def consultation_product(
    *,
    summary: str = "当前证据只支持继续观察。",
    with_g: bool = False,
) -> dict[str, object]:
    del with_g
    return {
        "contract_id": "consultation_product",
        "contract_version": "v1",
        "answer_text": summary,
    }


def daily_workspace_advisory_product(
    *,
    trading_day_id: str,
    checkpoint: str,
    consultation_as_of: str,
    summary: str = "当前证据只支持继续观察。",
    with_g: bool = False,
) -> dict[str, object]:
    del with_g
    provenance: dict[str, object] = {
        "runtime_invoked_at_generation": True,
        "output_used": True,
    }
    return {
        "schema_version": "fin.daily_workspace_product/v1",
        "checkpoint": checkpoint,
        "trading_day_id": trading_day_id,
        "origin": "scheduled",
        "generated_via": "consultation-chain-v1",
        "consultation_status": "completed",
        "degraded": False,
        "agent_provenance": provenance,
        "context_boundaries": {
            "prior_product": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
            "user_question": "NOT_EVIDENCE",
        },
        "input_snapshot_receipt": {
            "schema": "fin.daily-workspace-input-receipt/v1",
            "consultation_as_of": consultation_as_of,
        },
        "first_screen": {
            "top_items": [{"item": summary, "disposition": "OBSERVE"}],
            "rationale": [],
            "changes_vs_previous": [],
            "unknowns": [],
            "portfolio_review": [],
        },
        "data_gaps": [],
        "consultation_product": consultation_product(summary=summary),
    }


def daily_workspace_failure_notice(
    *,
    trading_day_id: str,
    checkpoint: str,
    runtime_invoked: bool,
) -> dict[str, object]:
    gap = "daily_workspace_preparation_unavailable"
    return {
        "schema_version": "fin.daily_workspace_product/v1",
        "checkpoint": checkpoint,
        "trading_day_id": trading_day_id,
        "origin": "scheduled",
        "generated_via": "deterministic-degraded-v1",
        "consultation_status": "partial",
        "degraded": True,
        "agent_provenance": {
            "runtime_invoked_at_generation": runtime_invoked,
            "output_used": False,
        },
        "first_screen": {
            "top_items": [],
            "rationale": [],
            "changes_vs_previous": [],
            "unknowns": [gap],
            "portfolio_review": [],
        },
        "data_gaps": [gap],
        "consultation_product": None,
    }
