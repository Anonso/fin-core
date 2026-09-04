"""知识库B 前门（接口B）：``read_shared_brain`` 书卡查询 reader。

分析思维注入 v1 件1（设计门 22/22 采纳，冻结稿 8930b22）：宏观接口纯化后，
书卡召回唯一前门。返回卡字段逐卡透传 ``forbidden_usages``/``usage_policy``
——牙齿是工具契约，不是纯文本纪律。matcher 与 read_g_context.external_brain
槽共用同一实现（macro_brain.match_shared_brain_cards），一处修复两处受益。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fin_analyse.guo_teacher_research.macro_brain import (
    load_shared_brain_cards,
    match_shared_brain_cards,
)

_SCHEMA_VERSION = "fin.shared-brain/v1"
_SUMMARY_MAX_CHARS = 600


class SharedBrainQueryReader:
    """read_shared_brain：非 G 框架卡两级命中，cap 条，牙齿字段逐卡透传。"""

    def __init__(self, knowledge_base_root: Path, *, max_items: int = 3) -> None:
        self._kb_root = Path(knowledge_base_root)
        self._max_items = max_items

    def read(self, request: Any) -> Any:
        from fin_analyse.read_capabilities.types import ProductionReadResult

        cards = match_shared_brain_cards(
            load_shared_brain_cards(self._kb_root), request.question, cap=self._max_items
        )
        value: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "source_boundary": "knowledge_brain_b",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "question": request.question,
            "cards": [
                {
                    "item_id": str(card.get("item_id", "")),
                    "title": str(card.get("title", "")),
                    "scope": str(card.get("scope", "")),
                    "summary": str(card.get("summary", ""))[:_SUMMARY_MAX_CHARS],
                    "applicable_tasks": list(
                        (card.get("metadata") or {}).get("applicable_tasks") or ()
                    ),
                    "activation_terms": list(
                        (card.get("metadata") or {}).get("activation_terms") or ()
                    ),
                    "forbidden_usages": list(card.get("forbidden_usages") or ()),
                    "usage_policy": str(
                        ((card.get("metadata") or {}).get("usage_policy") or "")
                    ),
                    "source_ref": str(card.get("source_ref", "")),
                    "updated_at": str(card.get("updated_at", "")),
                }
                for card in cards
            ],
        }
        gaps: tuple[str, ...] = ()
        if not cards:
            gaps = ("shared_brain_no_match",)
        return ProductionReadResult(value=value, data_gaps=gaps)
