"""BUG-012 回归：读能力工具描述不得静默回落通用默认。

通用默认描述使 agent 无从判断调用时机（公告探针不触发 read_ready_evidence 的
直接根因）；本测试钉住已写专项描述的能力，防止再次回落。
"""

from fin_analyse.guo_teacher_research.local_capability_transport import (
    _DEFAULT_READ_TOOL_DESCRIPTION,
    _READ_TOOL_DESCRIPTIONS,
)


def test_every_description_is_fin_capability_and_specific() -> None:
    assert _READ_TOOL_DESCRIPTIONS, "descriptions must not be empty"
    for capability, description in _READ_TOOL_DESCRIPTIONS.items():
        assert capability.startswith("fin.")
        assert description
        assert description != _DEFAULT_READ_TOOL_DESCRIPTION


def test_ready_evidence_description_sets_trigger_and_boundary() -> None:
    description = _READ_TOOL_DESCRIPTIONS["fin.read_ready_evidence"]
    assert "same-day" in description
    assert "read_external_evidence" in description
