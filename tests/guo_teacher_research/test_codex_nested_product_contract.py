from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from fin_analyse.guo_teacher_research.agent_runtime import AgentRunRequest
from fin_analyse.guo_teacher_research.codex_runtime import CodexCliAgentRuntimeAdapter
from fin_analyse.guo_teacher_research.product_contracts import (
    AgentProductContractRegistry,
    product_contract_projection,
)


def _contract_projection() -> dict[str, Any]:
    contract = AgentProductContractRegistry().get("consultation_product")
    assert contract is not None
    return product_contract_projection(contract)


def test_consultation_prompt_preserves_direct_agent_quality_floor() -> None:
    captured: list[str] = []
    question = "__UNIQUE_USER_QUESTION__ 海光信息最关键的投资矛盾是什么？"

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured[:] = command
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    CodexCliAgentRuntimeAdapter(workspace_path="/tmp", runner=runner).run(
        AgentRunRequest(
            use_case_ref="consultation.decision_support",
            question=question,
            product_contracts=[_contract_projection()],
            budget={"max_tool_calls": 0},
        )
    )

    prompt = captured[-1]
    assert prompt.count(question) == 1
    assert "sole A-share advisory analysis Agent" in prompt
    assert "Missing optional data must not suppress" in prompt
    assert "answer_text containing the complete Markdown answer" in prompt
    for retired in (
        "consultation_candidate",
        "selected_context_option_id",
        "analysis_profile",
        "desired_disposition",
        "claim taxonomy",
        "action_readiness",
        "SUMMARY claim",
    ):
        assert retired not in prompt


def test_consultation_answer_is_enforced_by_codex_output_schema() -> None:
    captured_schema: dict[str, Any] = {}

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        schema_index = command.index("--output-schema")
        captured_schema.update(
            json.loads(Path(command[schema_index + 1]).read_text(encoding="utf-8"))
        )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    CodexCliAgentRuntimeAdapter(workspace_path="/tmp", runner=runner).run(
        AgentRunRequest(
            use_case_ref="consultation.decision_support",
            product_contracts=[_contract_projection()],
            budget={"max_tool_calls": 0},
        )
    )

    assert captured_schema["additionalProperties"] is False
    assert captured_schema["required"] == [
        "contract_id",
        "contract_version",
        "answer_text",
    ]
    assert set(captured_schema["properties"]) == set(captured_schema["required"])
    assert captured_schema["properties"]["contract_id"] == {
        "const": "consultation_product",
        "type": "string",
    }
    assert captured_schema["properties"]["answer_text"]["type"] == "string"


def test_consultation_provider_schema_has_an_explicit_type_at_every_node() -> None:
    missing: list[str] = []
    schema = _contract_projection()["json_schema"]

    def visit(node: dict[str, Any], path: str) -> None:
        if not any(key in node for key in ("type", "anyOf", "$ref")):
            missing.append(path)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                if isinstance(child, dict):
                    visit(child, f"{path}.{name}")

    assert isinstance(schema, dict)
    visit(schema, "$")
    assert missing == []
