from __future__ import annotations

import pytest
from pydantic import ValidationError

from fin_analyse.consultation.contracts import ConsultationPortfolioScope, ConsultCommand
from fin_analyse.consultation.product_contracts import MAX_CONSULTATION_ANSWER_CHARS
from fin_analyse.guo_teacher_research.product_contracts import (
    AgentProductContractRegistry,
    AgentProductContractValidator,
)


def _valid_product(*, answer_text: str = "当前最关键的是增长兑现与估值预期之间的差。") -> dict:
    return {
        "contract_id": "consultation_product",
        "contract_version": "v1",
        "answer_text": answer_text,
    }


def test_consultation_product_is_the_small_registered_final_contract() -> None:
    contract = AgentProductContractRegistry().get("consultation_product")

    assert contract is not None
    assert contract.version == "v1"
    assert contract.required_fields == ("contract_id", "contract_version", "answer_text")
    assert AgentProductContractValidator().validate(
        "consultation_product", _valid_product()
    ).valid
    assert AgentProductContractRegistry().get("consultation_candidate") is None


def test_consultation_contract_preserves_one_complete_natural_answer() -> None:
    answer = "结论：继续观察。\n\n" + "理由、反证与失效条件需要完整说明。" * 100

    assert len(answer) > 1_000
    assert AgentProductContractValidator().validate(
        "consultation_product", _valid_product(answer_text=answer)
    ).valid


@pytest.mark.parametrize(
    "patch",
    (
        {"answer_text": ""},
        {"answer_text": "x" * (MAX_CONSULTATION_ANSWER_CHARS + 1)},
        {"analysis_profile": "EXPLAIN"},
        {"order": {"symbol": "600111.SH", "shares": 100}},
    ),
)
def test_consultation_product_rejects_missing_extra_or_execution_fields(
    patch: dict,
) -> None:
    product = _valid_product()
    product.update(patch)

    assert not AgentProductContractValidator().validate(
        "consultation_product", product
    ).valid


def test_portfolio_scope_never_silently_defaults_to_paper() -> None:
    with pytest.raises(ValidationError):
        ConsultationPortfolioScope()


@pytest.mark.parametrize(
    "value",
    (" ", " leading", "internal whitespace", "line\nbreak", "request-\x00-key"),
)
def test_consult_command_rejects_unstable_idempotency_keys(value: str) -> None:
    with pytest.raises(ValidationError):
        ConsultCommand(question="解释当前认知。", idempotency_key=value)


@pytest.mark.parametrize(
    "value",
    (None, "request:v1/ABC-123._~", "请求:v1/中文-Ω-é", "k" * 256),
)
def test_consult_command_preserves_valid_opaque_idempotency_keys(
    value: str | None,
) -> None:
    command = ConsultCommand(question="解释当前认知。", idempotency_key=value)

    assert ConsultCommand.model_validate(command.model_dump(mode="json")).idempotency_key == value
