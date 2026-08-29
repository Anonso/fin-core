from __future__ import annotations

from copy import deepcopy

from fin_analyse.consultation.presentation import (
    MAX_PRESENTATION_CHARS,
    PRESENTATION_SCHEMA,
    project_consultation_presentation,
    render_consultation_markdown,
)
from tests.consultation.test_daily_workspace_product_contracts import _advisory_product


def _payload(answer: str = "## 结论\n\n值得继续研究，但当前不追价。") -> dict[str, object]:
    return {
        "schema_version": "fin.consultation/v1",
        "status": "completed",
        "answer": {"summary": "被污染的 envelope 答案"},
        "product": {
            "contract_id": "consultation_product",
            "contract_version": "v1",
            "answer_text": answer,
        },
        "data_gaps": ["OPTIONAL_CONTEXT_UNAVAILABLE"],
    }


def test_complete_agent_answer_is_the_only_visible_answer() -> None:
    answer = (
        "## 结论\n\n"
        "这家公司值得继续研究，但当前估值没有留下足够安全边际。\n\n"
        "## 为什么\n\n"
        "- 收入质量改善需要由现金流确认。\n"
        "- 如果订单兑现低于预期，核心判断失效。"
    )
    payload = _payload(answer)

    assert render_consultation_markdown(payload) == answer
    assert "被污染" not in render_consultation_markdown(payload)
    assert "数据缺口" not in render_consultation_markdown(payload)
    assert "边界：" not in render_consultation_markdown(payload)


def test_presentation_does_not_rewrite_agent_time_expression() -> None:
    answer = "截至 2026-07-31T08:00:00Z，继续观察。"
    assert render_consultation_markdown(_payload(answer)) == answer


def test_degraded_fresh_appends_one_honest_notice_after_complete_answer() -> None:
    payload = _payload()
    payload["result_meta"] = {"continuity": "DEGRADED_FRESH"}

    rendered = render_consultation_markdown(payload)

    answer = payload["product"]["answer_text"]  # type: ignore[index]
    assert isinstance(answer, str)
    assert rendered.startswith(answer)
    assert rendered.count(answer) == 1
    assert rendered.count("连续性已降级") == 1


def test_degraded_model_appends_one_honest_notice_after_complete_answer() -> None:
    payload = _payload()
    payload["result_meta"] = {"model_quality": "DEGRADED"}

    rendered = render_consultation_markdown(payload)

    assert rendered.startswith("## 结论")
    assert rendered.count("模型已降级") == 1


def test_unavailable_presentation_keeps_problem_and_error_id_once() -> None:
    error_id = "err_" + "b" * 32
    rendered = render_consultation_markdown(
        {
            "status": "unavailable",
            "answer": {"summary": "暂时无法完成本次咨询。"},
            "problem": {
                "code": "consultation_unavailable",
                "error_id": error_id,
            },
        }
    )

    assert "暂时无法完成本次咨询。" in rendered
    assert "consultation_unavailable" in rendered
    assert rendered.count(error_id) == 1


def test_degraded_fresh_failure_is_not_silently_presented_as_resume() -> None:
    rendered = render_consultation_markdown(
        {
            "status": "unavailable",
            "answer": {"summary": "暂时无法完成本次咨询。"},
            "result_meta": {"continuity": "DEGRADED_FRESH"},
        }
    )

    assert rendered.count("连续性已降级") == 1
    assert "仍未形成可用答复" in rendered


def test_daily_workspace_displays_the_same_complete_answer_verbatim() -> None:
    product = _advisory_product()
    answer = product["consultation_product"]["answer_text"]  # type: ignore[index]
    payload = {
        "schema_version": "fin.consultation/v1",
        "action": "daily_workspace_scheduled",
        "status": "completed",
        "workspace_ref": "workspace-opaque-ref",
        "product": product,
    }

    assert render_consultation_markdown(payload) == answer


def test_projection_is_pure_and_bounded() -> None:
    payload = _payload("观察。" * 3_000)
    before = deepcopy(payload)

    presentation = project_consultation_presentation(payload)

    assert payload == before
    assert presentation["schema_version"] == PRESENTATION_SCHEMA
    assert presentation["format"] == "markdown"
    assert 0 < len(presentation["text"]) <= MAX_PRESENTATION_CHARS
