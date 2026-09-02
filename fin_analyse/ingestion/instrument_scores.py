"""Parse ZSXQ 普通栏研报图片评分表 into instrument score records.

载体三种（2026-09-02 实测）：zsxq_sources.image_descriptions（vision
结构化描述）、文章 .md「## 图片描述」节、正文/OCR 扫描。评分表格只在
图片里（普通栏 39 篇实证：原始正文含“利好度/共识度”为 0/39），.md 里的
列表是 vision 识别结果被嵌入「## 图片描述」节。

评分归一化：>10 一律 ÷10（如共识度 85 → 8.5），去 “%”/“分” 后缀；
字段缺失/越界/代码非法 → status=needs_review，不丢行、不静默改。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "fin.instrument-scores/v1"
PARSER_VERSION = "v1"

_SCORE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
_CODE_RE = re.compile(
    r"\b([0-9]{6}|[0-9]{4}\.[A-Z]{2}|[A-Z]{1,6}\.[A-Z]{2})\b", re.IGNORECASE
)
_ANCHOR_RE = re.compile(
    r"(?m)^\s*(?:\d+[.、]\s*)?\*{0,2}\s*"
    r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9·&（）()\-]{1,23}?)\s*"
    r"[（(]?\s*([0-9]{6})(?:\.(SH|SZ))?[)）]?\*{0,2}\s*[:：]?\s*$"
)
_KEY_LINE_RE = re.compile(
    r"^\s*[-*•]?\s*([^：:\n]{1,24}?)[：:]\s*(.+?)\s*$"
)
_FALLBACK_PREFIX_RE = re.compile(r"^fallback_chain:\s*\[[^\]]*\]\s*")

# 别名按归一化后子串匹配；顺序 = 解析优先级
_FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lihao", ("利好度", "利好强度", "利好程度")),
    ("consensus", ("共识度", "共识")),
    ("core_business", ("核心业务", "主营业务", "核心逻辑", "简要描述")),
    ("sector", ("所属板块", "所属行业", "板块", "细分赛道")),
    ("launch_in", ("预计多久启动", "预计启动时间", "预计介入时机", "启动时长", "介入时机")),
    ("horizon", ("期待周期", "周期周期", "持有时间", "持有周期", "周期")),
)
def _header_role(cell_norm: str) -> str | None:
    """表头列角色：name / code / None（“公司代码”=code，“公司名称（代码）”=name）。"""
    if "名称" in cell_norm or "公司/etf" in cell_norm:
        return "name"
    if "代码" in cell_norm:
        return "code"
    if cell_norm in {"公司", "标的", "主体", "公募"}:
        return "name"
    return None


def _norm(value: str) -> str:
    return (
        re.sub(r"\s+", "", value)
        .replace("（", "(")
        .replace("）", ")")
        .replace("：", ":")
        .casefold()
    )


def normalize_score(value: str) -> float | None:
    """1-10 归一化：>10 一律 ÷10；去 %/分 后缀；解析失败返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    match = _SCORE_RE.search(text)
    if match is None:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    if number > 10:
        number /= 10.0
    if number <= 0 or number > 10:
        return None
    return round(number, 2)


def _clean_cell(value: str) -> str:
    return re.sub(r"[*_`>]", "", (value or "")).strip()


def _alias_field(key: str) -> str | None:
    normalized = _norm(key)
    for field_name, aliases in _FIELD_ALIASES:
        for alias in aliases:
            if _norm(alias) in normalized:
                return field_name
    return None


def _code_from_cell(cell: str) -> str | None:
    match = _CODE_RE.search(cell)
    return match.group(1).upper() if match else None


def _name_from_cell(cell: str) -> str:
    cleaned = _clean_cell(cell)
    cleaned = re.sub(
        r"[（(]\s*(?:[0-9]{6}(?:\.[A-Z]{2})?|[0-9]{4}\.[A-Z]{2}|[A-Z]{1,6}\.[A-Z]{2})\s*[)）]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+[0-9]{6}(?:\.(?:SH|SZ))?\s*$", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"^[\d.、\s]+", "", cleaned).strip()
    return cleaned[:32]


def _split_table_rows(lines: list[str]) -> list[list[str]]:
    table_lines = [line for line in lines if line.lstrip().startswith("|")]
    if not table_lines:
        return []
    rows: list[list[str]] = []
    for line in table_lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue
        rows.append(cells)
    return rows


def _parse_table(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    table = _split_table_rows(lines)
    if not table:
        return []
    header = table[0]
    header_norm = [_norm(cell) for cell in header]
    if not any("利好度" in cell for cell in header_norm):
        return []
    name_idx: int | None = None
    code_idx: int | None = None
    field_idx: dict[str, int] = {}
    for index, cell in enumerate(header_norm):
        role = _header_role(cell)
        if role == "name" and name_idx is None:
            name_idx = index
        elif role == "code" and code_idx is None:
            code_idx = index
        mapped = _alias_field(header[index])
        if mapped is not None and mapped not in field_idx:
            field_idx[mapped] = index
    drafts: list[dict[str, Any]] = []
    for row in table[1:]:
        if len(row) < 2:
            continue
        draft: dict[str, Any] = {
            "code": None,
            "name": "",
            "lihao": None,
            "consensus": None,
            "core_business": None,
            "sector": None,
            "launch_in": None,
            "horizon": None,
        }
        if name_idx is not None and name_idx < len(row):
            cell = row[name_idx]
            draft["code"] = _code_from_cell(cell)
            draft["name"] = _name_from_cell(cell)
        if code_idx is not None and code_idx < len(row):
            code = _code_from_cell(row[code_idx])
            if code is not None:
                draft["code"] = code
        for field_name, index in field_idx.items():
            if index >= len(row):
                continue
            value = _clean_cell(row[index])
            if field_name in ("lihao", "consensus"):
                draft[field_name] = normalize_score(value)
            else:
                draft[field_name] = value or None
        if draft["code"] is None and not draft["name"]:
            continue
        drafts.append(draft)
    return drafts


def _parse_list_style(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    drafts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        anchor = _ANCHOR_RE.match(line)
        if anchor:
            if current is not None:
                drafts.append(current)
            current = {
                "code": anchor.group(2),
                "name": _clean_cell(anchor.group(1)),
                "lihao": None,
                "consensus": None,
                "core_business": None,
                "sector": None,
                "launch_in": None,
                "horizon": None,
            }
            continue
        if current is None:
            continue
        key_line = _KEY_LINE_RE.match(line)
        if not key_line:
            continue
        mapped = _alias_field(key_line.group(1))
        if mapped is None:
            continue
        value = _clean_cell(key_line.group(2))
        if mapped in ("lihao", "consensus"):
            current[mapped] = normalize_score(value)
        elif current.get(mapped) is None:
            current[mapped] = value or None
    if current is not None:
        drafts.append(current)
    return drafts


def parse_rows_from_text(text: str) -> list[dict[str, Any]]:
    """解析一段载体文本为行草稿（表格或列表/段落风格）。"""
    cleaned = _FALLBACK_PREFIX_RE.sub("", text or "")
    table_drafts = _parse_table(cleaned)
    if table_drafts:
        return table_drafts
    return _parse_list_style(cleaned)


@dataclass(frozen=True, slots=True)
class InstrumentScoreRecord:
    """一条标的评分记录（普通栏研报图片评分表）。"""

    source_id: str
    topic_id: str
    column: str
    title: str
    article_date: str
    published_at: str | None
    article_score: float | None
    code: str | None
    name: str
    core_business: str | None
    sector: str | None
    lihao_score: float | None
    consensus_score: float | None
    launch_in: str | None
    horizon: str | None
    status: str
    review_reason: str | None
    raw_origin: str
    provenance: str | None
    record_id: str
    extracted_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: str = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _required_missing(draft: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in ("name", "code", "core_business", "sector", "lihao", "consensus"):
        value = draft.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing


def build_record(
    *,
    draft: Mapping[str, Any],
    article: Mapping[str, Any],
    raw_origin: str,
    provenance: str | None,
    sequence: int,
) -> InstrumentScoreRecord:
    """组装一条记录；核心字段缺失/评分非法 → needs_review。"""
    code = draft.get("code")
    name = draft.get("name")
    missing = _required_missing(draft)
    review_reason: str | None = None
    status = "ok"
    if missing:
        status = "needs_review"
        review_reason = f"missing_fields:{','.join(missing)}"
    identity = f"{article.get('source_id', '')}:{code or name}:{sequence}"
    record_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return InstrumentScoreRecord(
        source_id=str(article.get("source_id", "")),
        topic_id=str(article.get("topic_id", "") or ""),
        column=str(article.get("column", "")),
        title=str(article.get("title", ""))[:300],
        article_date=str(article.get("article_date", ""))[:10],
        published_at=article.get("published_at"),
        article_score=article.get("article_score"),
        code=code or None,
        name=str(name or "")[:64],
        core_business=draft.get("core_business"),
        sector=draft.get("sector"),
        lihao_score=draft.get("lihao"),
        consensus_score=draft.get("consensus"),
        launch_in=draft.get("launch_in"),
        horizon=draft.get("horizon"),
        status=status,
        review_reason=review_reason,
        raw_origin=raw_origin,
        provenance=provenance,
        record_id=record_id,
    )


def parse_article_records(
    *,
    article: Mapping[str, Any],
    md_text: str | None,
    source_record: Mapping[str, Any] | None,
) -> list[InstrumentScoreRecord]:
    """跨载体解析一篇普通栏文章的全部评分记录（去重 + 交叉冲突标记）。"""
    carriers: list[tuple[str, str, str | None]] = []
    if source_record:
        descriptions = source_record.get("image_descriptions") or []
        if descriptions:
            carriers.append(
                (
                    "zsxq_sources.image_descriptions",
                    "\n\n".join(str(item) for item in descriptions),
                    None,
                )
            )
        ocr = source_record.get("image_ocr") or []
        if ocr:
            carriers.append(
                ("zsxq_sources.image_ocr", "\n".join(str(item) for item in ocr), None)
            )
    if md_text:
        section_match = re.search(
            r"##\s*图片描述(.*?)(?=\n##\s|\Z)", md_text, re.S
        )
        if section_match:
            carriers.append(
                (
                    "article_md.image_desc_section",
                    section_match.group(1),
                    None,
                )
            )
        carriers.append(("article_md.body", md_text, None))

    by_code: dict[str, InstrumentScoreRecord] = {}
    conflict_codes: set[str] = set()
    for raw_origin, text, provenance in carriers:
        if not text.strip():
            continue
        drafts = parse_rows_from_text(text)
        for sequence, draft in enumerate(drafts):
            code = draft.get("code") or str(draft.get("name") or "")
            if not code:
                continue
            existing = by_code.get(code)
            if existing is None:
                record = build_record(
                    draft=draft,
                    article=article,
                    raw_origin=raw_origin,
                    provenance=provenance,
                    sequence=sequence,
                )
                by_code[code] = record
                continue
            if (
                existing.lihao_score is not None
                and draft.get("lihao") is not None
                and existing.lihao_score != draft.get("lihao")
            ) or (
                existing.consensus_score is not None
                and draft.get("consensus") is not None
                and existing.consensus_score != draft.get("consensus")
            ):
                conflict_codes.add(code)

    records = list(by_code.values())
    if conflict_codes:
        final: list[InstrumentScoreRecord] = []
        for record in records:
            if record.code in conflict_codes:
                final.append(
                    InstrumentScoreRecord(
                        **{
                            **record.to_dict(),
                            "status": "needs_review",
                            "review_reason": "cross_source_conflict",
                        }
                    )
                )
            else:
                final.append(record)
        return final
    return records


def instrument_scores_path(knowledge_base_root: Path) -> Path:
    return Path(knowledge_base_root) / "runtime" / "cognition" / "instrument_scores.jsonl"


def load_records(path: Path) -> dict[str, dict[str, Any]]:
    if not Path(path).exists():
        return {}
    loaded: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        record_id = record.get("record_id")
        if record_id:
            loaded[record_id] = record
    return loaded


def upsert_records(path: Path, records: list[InstrumentScoreRecord]) -> tuple[int, int]:
    """原子 upsert；返回 (新增, 更新)。文件 0600、目录 0700。"""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    existing = load_records(target)
    added = 0
    updated = 0
    for record in records:
        record_id = record.record_id
        payload = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
        if record_id in existing:
            if existing[record_id] != record.to_dict():
                updated += 1
                existing[record_id] = json.loads(payload)
        else:
            added += 1
            existing[record_id] = json.loads(payload)
    body = "\n".join(
        json.dumps(value, ensure_ascii=False, default=str) for value in existing.values()
    )
    if body:
        body += "\n"
    temporary = target.with_name(target.name + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(body)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(temporary, target)
    return added, updated
