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
_ZSXQ_MACRO_TERMS = _MACRO_SIGNAL_TERMS + (
    "市场",
    "行情",
    "复盘",
    "资金",
    "轮动",
    "指数",
    "港股",
    "美股",
    "铜",
)
_ZSXQ_REPORT_TITLE_TERMS = (
    "报告",
    "中报",
    "半年报",
    "营收",
    "净利润",
    "毛利率",
    "订单",
    "评级",
    "深度",
    "点评",
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


def zsxq_macro_articles(
    knowledge_base_root: Path,
    *,
    as_of=None,
    window_days: int = 60,
    cap: int = 3,
) -> list[dict[str, Any]]:
    """index.json 扫描 ZSXQ 宏观条目（普通栏宏观 + 每日热点），窗口+cap。"""
    from datetime import UTC, datetime, timedelta

    index_path = Path(knowledge_base_root) / "index.json"
    if not index_path.exists():
        return []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    now = as_of if as_of is not None else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cutoff = now - timedelta(days=window_days)
    found: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        column = str(row.get("column", ""))
        if column not in ("普通", "星大派每日热点"):
            continue
        date_text = str(row.get("date", ""))[:10]
        try:
            published = datetime.fromisoformat(date_text)
        except ValueError:
            continue
        if published < cutoff.replace(tzinfo=None):
            continue
        title = str(row.get("title", ""))
        if column == "星大派每日热点":
            matched = ("ai_summary_reference",)
        else:
            if any(term in title for term in _ZSXQ_REPORT_TITLE_TERMS):
                continue
            companies = row.get("companies")
            if isinstance(companies, list) and len(companies) >= 3:
                continue
            cleaned = re.sub(r"(毛利|利润|坏账|不良|净利)率", "", title)
            matched = tuple(
                term for term in _ZSXQ_MACRO_TERMS if term in cleaned or term in title
            )
            if not matched:
                continue
        found.append(
            (
                date_text,
                {
                    "article_id": str(row.get("id", "")),
                    "title": title[:160],
                    "column": column,
                    "date": date_text,
                    "score": row.get("score"),
                    "matched_terms": list(matched),
                },
            )
        )
    found.sort(key=lambda item: item[0], reverse=True)
    return [item for _date, item in found[:cap]]


class MacroBrainQueryReader:
    """read_macro_brain：ZSXQ 宏观 + 外置大脑书卡 + search_web 引导。"""

    def __init__(self, knowledge_base_root: Path, *, max_items: int = 3) -> None:
        self._kb_root = Path(knowledge_base_root)
        self._max_items = max_items

    def read(self, request: Any) -> Any:
        from fin_analyse.read_capabilities.types import ProductionReadResult

        cards = match_shared_brain_cards(
            load_shared_brain_cards(self._kb_root), request.question, cap=self._max_items
        )
        zsxq = zsxq_macro_articles(self._kb_root, as_of=request.as_of, cap=self._max_items)
        search_needed = not cards and not zsxq and macro_search_signal(request.question)
        value: dict[str, object] = {
            "schema_version": "fin.macro-brain/v1",
            "source_boundary": "zsxq_macro_brain",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "question": request.question,
            "zsxq_macro": zsxq,
            "shared_brain_cards": [
                {
                    "item_id": str(card.get("item_id", "")),
                    "title": str(card.get("title", "")),
                    "scope": str(card.get("scope", "")),
                    "summary": str(card.get("summary", ""))[:600],
                    "source_ref": str(card.get("source_ref", "")),
                    "updated_at": str(card.get("updated_at", "")),
                    "usage_policy": str(
                        ((card.get("metadata") or {}).get("usage_policy") or "")
                    ),
                }
                for card in cards
            ],
            "search_needed": search_needed,
            "suggested_queries": suggested_queries(request.question) if search_needed else [],
        }
        gaps: tuple[str, ...] = ()
        if not cards and not zsxq:
            gaps = ("macro_brain_no_local_match",)
        return ProductionReadResult(value=value, data_gaps=gaps)
