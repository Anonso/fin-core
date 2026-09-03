"""黑话译注透传链单测：candidate_entry → _build_llm_context → _project_g_item。

窄契约 [{term, meaning, confidence}]；缺失/畸形一律返回空、不附加键——
旧工件无 jargon_notes 字段时行为与引入前一致（向后兼容不变量）。
"""

from __future__ import annotations

from fin_analyse.guo_teacher_research.production_capability_provider import (
    _bounded_jargon_notes,
    _project_g_item,
)
from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextRequest,
    _build_llm_context,
    _candidate_jargon_notes,
)

_NOTE = {"term": "科学家50", "meaning": "科创50", "confidence": "owner_confirmed"}


def test_candidate_jargon_notes_normalizes_and_bounds() -> None:
    dr = {
        "jargon_notes": [
            _NOTE,
            {"term": "大光", "meaning": "光模块", "confidence": "owner_confirmed"},
            {"term": "", "meaning": "缺词不收"},
            {"term": "缺义", "meaning": ""},
            "not-a-mapping",
        ]
    }
    notes = _candidate_jargon_notes(dr)
    assert notes == [
        _NOTE,
        {"term": "大光", "meaning": "光模块", "confidence": "owner_confirmed"},
    ]
    # 长词截断有界
    long_dr = {
        "jargon_notes": [
            {"term": "长" * 40, "meaning": "义" * 120, "confidence": "owner_confirmed"}
        ]
    }
    notes = _candidate_jargon_notes(long_dr)
    assert notes[0]["term"] == "长" * 24
    assert notes[0]["meaning"] == "义" * 80
    # 缺失/畸形 → 空
    assert _candidate_jargon_notes(None) == []
    assert _candidate_jargon_notes({}) == []
    assert _candidate_jargon_notes({"jargon_notes": "bad"}) == []
    assert _candidate_jargon_notes({"jargon_notes": [{"term": "x", "meaning": 1}]}) == []


def test_build_llm_context_carries_jargon_notes() -> None:
    request = AgentRuntimeContextRequest(agent_id="guo_teacher", question="科创50怎么看")
    selected = [
        {
            "source_bucket": "fresh_g",
            "title": "锐评",
            "guidance_brief": "……",
            "article_id": "zsxq-1",
            "jargon_notes": [_NOTE],
        },
        {
            "source_bucket": "fresh_g",
            "title": "无译注条目",
            "guidance_brief": "……",
            "article_id": "zsxq-2",
        },
    ]
    ctx = _build_llm_context(request=request, selected=selected, data_gaps=[])
    entries = ctx["g_context"]
    assert entries[0]["jargon_notes"] == [_NOTE]
    # 无命中不附加
    assert "jargon_notes" not in entries[1]


def test_project_g_item_carries_bounded_jargon_notes() -> None:
    raw = {
        "title": "锐评",
        "guidance_brief": "……",
        "jargon_notes": [_NOTE, {"term": "大光", "meaning": "光模块"}],
    }
    item = _project_g_item(raw, source_ref="zsxq-1", bucket="fresh_g")
    assert item["jargon_notes"] == [
        _NOTE,
        {"term": "大光", "meaning": "光模块", "confidence": ""},
    ]
    # 缺失/畸形 → 不附加
    assert "jargon_notes" not in _project_g_item({}, source_ref="s", bucket="b")
    assert "jargon_notes" not in _project_g_item(
        {"jargon_notes": "bad"}, source_ref="s", bucket="b"
    )


def test_bounded_jargon_notes_caps_items() -> None:
    value = [
        {"term": f"词{i}", "meaning": f"义{i}", "confidence": "owner_confirmed"} for i in range(12)
    ]
    notes = _bounded_jargon_notes(value)
    assert len(notes) == 8
