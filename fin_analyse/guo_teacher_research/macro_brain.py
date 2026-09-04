"""宏观统一接口 A 的本地供料：外置大脑书卡召回 + 宏观检索信号。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from fin_analyse.cognition.macro_index import load_macro_entries, load_rules

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


def _activation_hit(card: dict[str, Any], question: str) -> bool:
    """卡自声明激活词（metadata.activation_terms）主级命中：精确/子串。"""
    terms = (card.get("metadata") or {}).get("activation_terms") or ()
    if not isinstance(terms, (list, tuple)):
        return False
    lowered = question.lower()
    return any(
        isinstance(term, str) and term.strip() and term.lower() in lowered
        for term in terms
    )


def match_shared_brain_cards(
    cards: list[dict[str, Any]], question: str, *, cap: int = 3
) -> list[dict[str, Any]]:
    """两级命中（analysis-mindset-v1 件1）：①卡自声明 activation_terms 主级
    （精确/子串，零重叠也命中——卡设计了钥匙，门必须有锁孔）；②既有
    2-gram 字面重叠兜底。同段内 updated_at 降序，cap 条。"""
    query_tokens = _tokens(question)
    if not query_tokens:
        return []
    scored: list[tuple[int, int, str, dict[str, Any]]] = []
    for card in cards:
        haystack = " ".join(
            str(card.get(key) or "") for key in ("title", "summary", "source_ref")
        )
        hits = sum(1 for token in query_tokens if token in haystack)
        tier = 0 if _activation_hit(card, question) else 1
        if tier == 0 or hits:
            scored.append((tier, hits, str(card.get("updated_at") or ""), card))
    # 稳定双排序：先 updated_at 降序，再 (tier 升, hits 降)。
    scored.sort(key=lambda item: item[2], reverse=True)
    scored.sort(key=lambda item: (item[0], -item[1]))
    return [card for _tier, _hits, _updated, card in scored[:cap]]


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
    """ZSXQ 宏观条目（macro_index 优先；无侧车时回退 index.json 启发式），窗口+cap。"""
    from datetime import UTC, datetime, timedelta

    now = as_of if as_of is not None else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cutoff = now - timedelta(days=window_days)

    index_entries = load_macro_entries(Path(knowledge_base_root))
    if index_entries is not None:
        found: list[tuple[str, dict[str, Any]]] = []
        for item in index_entries:
            date_text = str(item.get("date", ""))[:10]
            try:
                published = datetime.fromisoformat(date_text)
            except ValueError:
                continue
            if published < cutoff.replace(tzinfo=None):
                continue
            found.append(
                (
                    date_text,
                    {
                        "article_id": str(item.get("article_id", "")),
                        "title": str(item.get("title", ""))[:160],
                        "column": str(item.get("column", "")),
                        "date": date_text,
                        "score": item.get("score"),
                        "matched_terms": list(item.get("matched_terms") or ()),
                    },
                )
            )
        found.sort(key=lambda item: item[0], reverse=True)
        return [item for _date, item in found[:cap]]

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
    try:
        kept_ids = {
            str(item.get("article_id", ""))
            for item in load_rules().get("kept", [])
            if isinstance(item, dict) and item.get("article_id")
        }
    except (OSError, ValueError):
        kept_ids = set()
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
        article_id = str(row.get("id", ""))
        if column == "星大派每日热点":
            matched = ("ai_summary_reference",)
        elif article_id in kept_ids:
            matched = ()
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
                    "article_id": article_id,
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
    """read_macro_brain：ZSXQ 宏观材料 + search_web 引导（宏观纯化，v2）。

    分析思维注入 v1 件1：书卡腿整体拆出——书卡召回归知识库B 前门
    ``read_shared_brain``（SharedBrainQueryReader），本接口只管宏观材料；
    search_needed/gaps 只依赖 zsxq（否则空卡库每问误报 no_local_match）。
    """

    def __init__(self, knowledge_base_root: Path, *, max_items: int = 3) -> None:
        self._kb_root = Path(knowledge_base_root)
        self._max_items = max_items

    def read(self, request: Any) -> Any:
        from fin_analyse.read_capabilities.types import ProductionReadResult

        zsxq = zsxq_macro_articles(self._kb_root, as_of=request.as_of, cap=self._max_items)
        search_needed = not zsxq and macro_search_signal(request.question)
        value: dict[str, object] = {
            "schema_version": "fin.macro-brain/v2",
            "source_boundary": "zsxq_macro_brain",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "question": request.question,
            "zsxq_macro": zsxq,
            "search_needed": search_needed,
            "suggested_queries": suggested_queries(request.question) if search_needed else [],
        }
        gaps: tuple[str, ...] = ()
        if not zsxq:
            gaps = ("macro_brain_no_local_match",)
        return ProductionReadResult(value=value, data_gaps=gaps)
