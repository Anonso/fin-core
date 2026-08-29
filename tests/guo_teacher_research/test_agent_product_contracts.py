"""Tests for the active FIN semantic product contracts."""

from __future__ import annotations

from typing import Any

import pytest

from fin_analyse.guo_teacher_research.product_contracts import (
    AgentProductContractRegistry,
    AgentProductContractValidator,
)


def _research_product() -> dict[str, Any]:
    return {
        "answer_summary": "分析总结",
        "sections": [{"type": "analysis", "title": "分析", "content": "内容"}],
        "source_level": "g_direct",
        "single_stock": {"ticker": "000001", "analysis": "测试"},
        "portfolio": {"holdings": []},
        "mainlines": [{"name": "AI主线", "status": "active"}],
        "candidates": [{"ticker": "000001", "name": "测试公司", "rationale": "理由"}],
        "decision_drafts": [{"summary": "建议关注", "advisory_only": True}],
        "shared_brain_references": {"methodology": "G体系"},
    }


def _morning_strategy() -> dict[str, Any]:
    return {
        "status": "available",
        "today_thesis": "市场震荡，关注科技板块",
        "g_mainline_updates": ["AI主线持续"],
        "portfolio_review_order": [{"ticker": "000001", "review": "hold"}],
        "watch_triggers": ["上证突破3500"],
        "risk_brakes": ["单日跌幅过大时重新评估"],
        "no_trade_conditions": ["量能不足时不执行"],
        "manual_confirmation_checks": ["人工确认风险边界"],
        "data_gaps": ["缺少外资流向数据"],
        "boundary": "advisory-only; not executable trading instruction",
    }


def _display_product() -> dict[str, Any]:
    return {
        "display_intent": "general",
        "headline": "测试标题",
        "short_answer": "简短回答",
        "primary_sections": [{"section_type": "conclusion", "title": "结论", "text": "测试"}],
        "candidate_profiles": [],
        "analysis_path_summary": [{"step": "collect", "detail": "收集数据"}],
        "confidence_boundary": {
            "advisory_only": True,
            "execution_allowed": False,
            "human_confirmation_required": True,
        },
        "next_actions": [{"label": "继续观察"}],
        "omitted_details_summary": ["省略细节"],
        "planner_source": "deterministic_projection",
    }


def test_registry_contains_only_active_semantic_products() -> None:
    registry = AgentProductContractRegistry()

    assert set(registry.all()) == {
        "research_product",
        "morning_strategy",
        "display_product",
        "guo_explanation_product",
        "consultation_product",
        "a_share_tactical_method_product",
        "a_share_tactical_consultation_product",
    }
    assert registry.get("priority_event_brief") is None
    assert registry.get("decision_guidance") is None
    assert registry.get("nonexistent") is None


@pytest.mark.parametrize(
    ("contract_id", "payload"),
    [
        ("research_product", _research_product()),
        ("morning_strategy", _morning_strategy()),
        ("display_product", _display_product()),
    ],
)
def test_active_product_payloads_pass_contract(
    contract_id: str,
    payload: dict[str, Any],
) -> None:
    result = AgentProductContractValidator().validate(contract_id, payload)

    assert result.valid is True, result.errors


def test_validator_rejects_missing_required_field() -> None:
    payload = _morning_strategy()
    del payload["today_thesis"]

    result = AgentProductContractValidator().validate("morning_strategy", payload)

    assert result.valid is False
    assert any("today_thesis" in error for error in result.errors)


@pytest.mark.parametrize("forbidden_field", ["order", "position", "target_price"])
def test_validator_rejects_nested_execution_fields(forbidden_field: str) -> None:
    payload = _display_product()
    payload["detail_sections"] = [{"nested": {forbidden_field: "forbidden"}}]

    result = AgentProductContractValidator().validate("display_product", payload)

    assert result.valid is False
    assert any(forbidden_field in error for error in result.errors)


def test_display_product_rejects_internal_risk_level() -> None:
    payload = _display_product()
    payload["risk_level"] = "high"

    result = AgentProductContractValidator().validate("display_product", payload)

    assert result.valid is False
    assert any("risk_level" in error for error in result.errors)


def test_contract_shapes_exclude_retired_generic_loop_metadata() -> None:
    registry = AgentProductContractRegistry()
    research = registry.get("research_product")
    morning = registry.get("morning_strategy")
    display = registry.get("display_product")
    assert research is not None
    assert morning is not None
    assert display is not None

    assert "upgrade_recommendation" not in research.optional_fields
    assert "upgrade_recommendation" not in research.public_fields
    assert {"agent_profile", "moa_status", "display_hint"}.isdisjoint(
        (*morning.required_fields, *morning.optional_fields, *morning.public_fields)
    )
    assert "upgrade_recommendation" not in display.optional_fields
    assert "upgrade_recommendation" not in display.public_fields
    assert "level_budget_policy" not in display.optional_fields
    assert "level_budget_policy" not in display.public_fields


def test_source_attribution_does_not_weaken_trade_guard() -> None:
    payload = _display_product()
    payload["source_attribution"] = {"version": "source_attribution_v1", "items": []}
    payload["order"] = {"buy": True}

    result = AgentProductContractValidator().validate("display_product", payload)

    assert result.valid is False
    assert any("order" in error for error in result.errors)
