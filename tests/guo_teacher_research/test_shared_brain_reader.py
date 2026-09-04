"""read_shared_brain（接口B）reader 单测：两级命中 + 牙齿字段逐卡透传。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.guo_teacher_research.shared_brain_reader import SharedBrainQueryReader
from fin_analyse.read_capabilities.types import ProductionReadRequest


def _seed(kb_root: Path, cards: list[dict]) -> None:
    path = kb_root / "runtime" / "shared_brain" / "items.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(card, ensure_ascii=False) for card in cards) + "\n",
        encoding="utf-8",
    )


def _request(question: str) -> ProductionReadRequest:
    return ProductionReadRequest(
        question=question, as_of=datetime(2026, 9, 4, tzinfo=UTC)
    )


def test_reader_passthrough_teeth_fields(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            {
                "item_id": "fin_red_flag_checklist",
                "title": "财报排雷清单",
                "summary": "买入候选基本面复核：红旗项清单",
                "scope": "shared_brain_framework",
                "is_g_source": False,
                "forbidden_usages": [
                    "teacher_direct",
                    "thesis_override",
                    "confidence_boost",
                ],
                "metadata": {
                    "applicable_tasks": ["fundamental_review"],
                    "usage_policy": "只用于问题清单与仓位/失效条件收紧，永不凌驾 G",
                    "activation_terms": ["排雷", "红旗"],
                },
                "source_ref": "唐朝《手把手教你读财报》",
                "updated_at": "2026-09-04T00:00:00+08:00",
            }
        ],
    )
    result = SharedBrainQueryReader(tmp_path).read(_request("帮我排雷这只票"))
    value = result.value
    assert value["schema_version"] == "fin.shared-brain/v1"
    assert value["source_boundary"] == "knowledge_brain_b"
    assert result.data_gaps == ()
    (card,) = value["cards"]
    assert card["item_id"] == "fin_red_flag_checklist"
    # 牙齿是工具契约：逐卡透传，缺一不可
    assert card["forbidden_usages"] == [
        "teacher_direct",
        "thesis_override",
        "confidence_boost",
    ]
    assert card["usage_policy"].startswith("只用于问题清单")
    assert card["applicable_tasks"] == ["fundamental_review"]
    assert card["activation_terms"] == ["排雷", "红旗"]
    assert len(card["summary"]) <= 600


def test_reader_no_match_reports_gap(tmp_path: Path) -> None:
    _seed(tmp_path, [])
    result = SharedBrainQueryReader(tmp_path).read(_request("完全无关的问题"))
    assert result.value["cards"] == []
    assert result.value["status"] == "READY"
    assert result.data_gaps == ("shared_brain_no_match",)


def test_reader_skips_g_source_cards(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            {
                "item_id": "g_card",
                "title": "G 来源卡不入接口B",
                "summary": "排雷关键词在正文",
                "scope": "shared_brain_framework",
                "is_g_source": True,
            }
        ],
    )
    result = SharedBrainQueryReader(tmp_path).read(_request("排雷"))
    assert result.value["cards"] == []
    assert result.data_gaps == ("shared_brain_no_match",)
