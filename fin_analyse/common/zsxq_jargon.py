"""ZSXQ 黑话译注词表 loader（设计稿 docs/design/zsxq-jargon.md）。

词表唯一真源 = ``config/zsxq_jargon.json``，owner 可维护；任何消费方不得
在代码里硬编码词义。本模块零依赖、零副作用：惰性读文件、内存缓存、
读取失败返回空表（消费方拿不到译注时保持旧行为，不报错、不降级）。

三档 confidence：owner_confirmed / corpus_inferred / speculative。
speculative 只记录不注入、不参与召回——默认全部排除。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JARGON_CONFIG_PATH = PROJECT_ROOT / "config" / "zsxq_jargon.json"

CONFIDENCE_OWNER = "owner_confirmed"
CONFIDENCE_CORPUS = "corpus_inferred"
CONFIDENCE_SPECULATIVE = "speculative"
_KNOWN_CONFIDENCES = frozenset({CONFIDENCE_OWNER, CONFIDENCE_CORPUS, CONFIDENCE_SPECULATIVE})

#: 单段文本最多命中的词条数（有界性；长词优先已保证信息量最大的留下）。
MAX_HITS = 16


@dataclass(frozen=True)
class JargonEntry:
    term: str
    meaning: str
    confidence: str
    kind: str = ""
    note: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)


_entries_cache: dict[str, tuple[JargonEntry, ...]] = {}


def load_jargon_entries(
    *,
    include_speculative: bool = False,
    config_path: Path | None = None,
) -> tuple[JargonEntry, ...]:
    """读词表；speculative 默认排除。失败返回空表，绝不 raise。"""
    path = Path(config_path) if config_path is not None else JARGON_CONFIG_PATH
    cache_key = str(path)
    cached = _entries_cache.get(cache_key)
    if cached is None:
        cached = _read_entries(path)
        _entries_cache[cache_key] = cached
    if include_speculative:
        return cached
    return tuple(entry for entry in cached if entry.confidence != CONFIDENCE_SPECULATIVE)


def _read_entries(path: Path) -> tuple[JargonEntry, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("zsxq jargon config unavailable (%s): %s", path.name, exc)
        return ()
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        logger.warning("zsxq jargon config malformed: entries is not a list")
        return ()
    entries: list[JargonEntry] = []
    seen_terms: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term", "")).strip()
        meaning = str(raw.get("meaning", "")).strip()
        confidence = str(raw.get("confidence", "")).strip()
        if not term or not meaning or term in seen_terms:
            continue
        # 未知档位按 speculative 处理（默认排除，宁缺勿错注）。
        if confidence not in _KNOWN_CONFIDENCES:
            confidence = CONFIDENCE_SPECULATIVE
        seen_terms.add(term)
        evidence_raw = raw.get("evidence", [])
        evidence = (
            tuple(str(e) for e in evidence_raw if str(e).strip())
            if isinstance(evidence_raw, list)
            else ()
        )
        entries.append(
            JargonEntry(
                term=term,
                meaning=meaning,
                confidence=confidence,
                kind=str(raw.get("kind", "")).strip(),
                note=str(raw.get("note", "")).strip(),
                evidence=evidence,
            )
        )
    return tuple(entries)


def jargon_hits(
    text: str,
    *,
    entries: tuple[JargonEntry, ...] | None = None,
    max_hits: int = MAX_HITS,
) -> list[dict[str, object]]:
    """扫描文本返回结构化命中 [{term, meaning, confidence, start, end}]。

    长词优先抑制重叠：同一片段只归最长词（"科学家50" 命中后其中的
    "科学家" 不再单独命中）。精确子串匹配，大小写不折叠，同输入同输出。
    """
    if not text or max_hits <= 0:
        return []
    if entries is None:
        entries = load_jargon_entries()
    matches: list[tuple[int, int, JargonEntry]] = []
    for entry in entries:
        term = entry.term
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            matches.append((index, index + len(term), entry))
            start = index + 1
    # 长词优先，同长按位置靠前；已选片段占用的区间不再接受重叠命中。
    matches.sort(key=lambda match: (-(match[1] - match[0]), match[0]))
    chosen: list[tuple[int, int, JargonEntry]] = []
    occupied: list[tuple[int, int]] = []
    for match in matches:
        begin, end, _ = match
        if any(begin < other_end and other_begin < end for other_begin, other_end in occupied):
            continue
        chosen.append(match)
        occupied.append((begin, end))
        if len(chosen) >= max_hits:
            break
    chosen.sort(key=lambda match: match[0])
    return [
        {
            "term": entry.term,
            "meaning": entry.meaning,
            "confidence": entry.confidence,
            "start": begin,
            "end": end,
        }
        for begin, end, entry in chosen
    ]


def jargon_notes(
    text: str,
    *,
    entries: tuple[JargonEntry, ...] | None = None,
    limit: int = 8,
) -> list[dict[str, object]]:
    """窄契约译注 [{term, meaning, confidence}]（按 term 去重，有界）。"""
    notes: list[dict[str, object]] = []
    seen_terms: set[str] = set()
    for hit in jargon_hits(text, entries=entries):
        term = str(hit["term"])
        if term in seen_terms:
            continue
        seen_terms.add(term)
        notes.append(
            {
                "term": term,
                "meaning": str(hit["meaning"]),
                "confidence": str(hit["confidence"]),
            }
        )
        if len(notes) >= limit:
            break
    return notes


def jargon_note_lines(
    text: str,
    *,
    entries: tuple[JargonEntry, ...] | None = None,
    limit: int = 8,
) -> list[str]:
    """人类可读旁注行：「- 科学家50 = 科创50」。

    corpus_inferred 注入时标「语料推测」（设计稿口径）；speculative
    不会出现（词表加载时已排除）。
    """
    lines: list[str] = []
    for note in jargon_notes(text, entries=entries, limit=limit):
        mark = "（语料推测）" if note["confidence"] == CONFIDENCE_CORPUS else ""
        lines.append(f"- {note['term']} = {note['meaning']}{mark}")
    return lines


#: 查询双向扩展门槛：只对 owner_confirmed 的无歧义长词（≥3 字）做，
#: 避免 炒饭/柚子/亮化 这类短词在检索里引出无关语义误召回（owner 拍板口径）。
_QUERY_EXPANSION_MIN_TERM_CHARS = 3
_MAX_QUERY_EXPANSIONS = 6


def expand_query_terms(
    question: str,
    *,
    entries: tuple[JargonEntry, ...] | None = None,
    limit: int = _MAX_QUERY_EXPANSIONS,
) -> list[str]:
    """read_article_search 查询双向扩展：命中词表任一方向时给反向词。

    "科创50" → 附 "科学家50"；"科学家50" → 附 "科创50"。只返回扩展串
    （调用方拼查询，不改索引不改原文）；speculative 不参与（默认排除）。
    """
    if not question or limit <= 0:
        return []
    if entries is None:
        entries = load_jargon_entries()
    expanded: list[str] = []
    for entry in entries:
        if entry.confidence != CONFIDENCE_OWNER:
            continue
        if len(entry.term) < _QUERY_EXPANSION_MIN_TERM_CHARS:
            continue
        # 双向独立判断：term 在问句补 meaning，meaning 在问句补 term。
        candidates: list[str] = []
        if entry.term in question:
            candidates.append(entry.meaning)
        if entry.meaning in question:
            candidates.append(entry.term)
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate and candidate not in question and candidate not in expanded:
                expanded.append(candidate)
                if len(expanded) >= limit:
                    return expanded
    return expanded
