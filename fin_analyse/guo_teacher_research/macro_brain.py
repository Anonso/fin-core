"""宏观统一接口 A 的本地供料：外置大脑书卡召回 + 宏观检索信号。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

_SHARED_BRAIN_PATH = Path("runtime/shared_brain/items.jsonl")
_SHARED_BRAIN_SCOPES = frozenset(
    {"methodology_memory", "shared_brain_framework", "external_reference"}
)
_MACRO_SIGNAL_TERMS = (
    "宏观",
    "大盘",
    "政策",
    "流动性",
    "利率",
    "美联储",
    "美债",
    "海外",
    "地缘",
    "关税",
    "汇率",
    "商品",
    "原油",
    "黄金",
    "大宗",
    "央行",
    "降息",
    "加息",
    "通胀",
    "政治经济学",
    "地产",
    "财政",
)


def load_shared_brain_cards(knowledge_base_root: Path) -> list[dict[str, Any]]:
    path = Path(knowledge_base_root) / _SHARED_BRAIN_PATH
    if not path.exists():
        return []
    cards: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        if str(item.get("scope", "")) not in _SHARED_BRAIN_SCOPES:
            continue
        if item.get("is_g_source") is True:
            continue
        cards.append(item)
    return cards


def _tokens(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for segment in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", text):
        if len(segment) >= 4 and re.search(r"[\u4e00-\u9fa5]", segment):
            tokens.extend(
                dict.fromkeys(segment[index : index + 2] for index in range(len(segment) - 1))
            )
        elif len(segment) >= 2:
            tokens.append(segment)
    return tuple(tokens)


def match_shared_brain_cards(
    cards: list[dict[str, Any]], question: str, *, cap: int = 3
) -> list[dict[str, Any]]:
    """按标题/摘要命中数召回书卡，cap 条、updated_at 降序。"""
    query_tokens = _tokens(question)
    if not query_tokens:
        return []
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for card in cards:
        haystack = " ".join(
            str(card.get(key) or "") for key in ("title", "summary", "source_ref")
        )
        hits = sum(1 for token in query_tokens if token in haystack)
        if hits:
            scored.append((hits, str(card.get("updated_at") or ""), card))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [card for _hits, _updated, card in scored[:cap]]


def macro_search_signal(question: str) -> bool:
    return any(term in question for term in _MACRO_SIGNAL_TERMS)


def suggested_queries(question: str, *, cap: int = 3) -> list[str]:
    tokens = [token for token in _tokens(question) if len(token) >= 2]
    kept = []
    for token in tokens:
        if token not in kept:
            kept.append(token)
        if len(kept) >= cap:
            break
    return [f"{token} 最新 宏观 政策 流动性" for token in kept] if kept else []
