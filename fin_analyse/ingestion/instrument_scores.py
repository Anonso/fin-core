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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "fin.instrument-scores/v1"
PARSER_VERSION = "v2"

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


_INLINE_ITEM_RE = re.compile(
    r"(?m)^\s*(?:[-*•]\s*)?(?:\d+[.、]\s*)?\*{0,2}\s*"
    r"(?P<code>(?:(?P<a6>[0-9]{6})(?:\.(?:SH|SZ|BJ))?|"
    r"[0-9]{4}\.[A-Z]{2}|[A-Z]{1,6}\.[A-Z]{2}))\s+"
    r"(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9·&（）()\-]{0,31}?)"
    r"\*{0,2}\s*[:：]\s*(?P<body>.+?)\s*$"
)
_INLINE_LABEL_ALIASES: tuple[str, ...] = tuple(
    dict.fromkeys(alias for _, aliases in _FIELD_ALIASES for alias in aliases)
)
_INLINE_LABEL_RE = re.compile(
    "|".join(re.escape(alias) for alias in _INLINE_LABEL_ALIASES)
)

_LEAD_NAME_RE = re.compile(
    r"([\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9\uFF21-\uFF3A\uFF41-\uFF5A·&（）()\- ]*)$"
)
_TABLE_CELL_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9\uFF21-\uFF3A"
    r"\uFF41-\uFF5A·&（）()\- ]*?)\s*[（(](?P<code>[0-9]{4,6}(?:\.[A-Z]{2})?)[）)]"
)


def _compact_name(value: str) -> str:
    return "".join(value.split())


def _normalize_table_cell(
    cell: str, normalized_entries: dict[str, dict[str, Any]]
) -> tuple[str, int]:
    match = _TABLE_CELL_RE.search(cell)
    if match is None:
        return cell, 0
    name = _compact_name(match.group("name"))
    entry = normalized_entries.get(name)
    if entry is None:
        return cell, 0
    expected = str(entry.get("ticker") or "")
    if not expected.isdigit():
        return cell, 0
    raw_code = match.group("code")
    if "." in raw_code:
        suffix = raw_code.rsplit(".", 1)[1]
        replacement = f"{expected}.{suffix}"
    else:
        replacement = expected
    if raw_code == replacement:
        return cell, 0
    start, end = match.span("code")
    return cell[:start] + replacement + cell[end:], 1


def normalize_inline_codes(
    text: str, name_map: Mapping[str, Any]
) -> tuple[str, int]:
    """把“代码 名称：…”行的已知 A 股代码归一为名册 ticker（不改其他行）。

    name_map 为 a_share_name_map.json 的 entries（键含代码与名称，可能带空格）。
    返回 (修正后文本, 归一行数)；用于 md/图片描述/正文的解析前统一清洗，
    避免 vision/OCR 转录错码进注册表。
    """
    normalized_entries: dict[str, dict[str, Any]] = {}
    for key, entry in name_map.items():
        compact = _compact_name(str(key))
        if compact and isinstance(entry, Mapping):
            normalized_entries.setdefault(compact, dict(entry))
    corrected = 0
    output: list[str] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            output.append(raw)
            continue
        if "|" in raw:
            cells = raw.split("|")
            rebuilt: list[str] = []
            for cell in cells:
                normalized_cell, cell_hits = _normalize_table_cell(
                    cell, normalized_entries
                )
                rebuilt.append(normalized_cell)
                corrected += cell_hits
            output.append("|".join(rebuilt))
            continue
        colons = [index for index, ch in enumerate(raw) if ch in "：:"]
        if not colons:
            output.append(raw)
            continue
        lead = raw[: colons[0]]
        rest = raw[colons[0] + 1 :]
        clean_lead = re.sub(r"^[\s\d.、*#\-]+", "", lead).rstrip("*# \t")
        name_match = _LEAD_NAME_RE.search(clean_lead)
        if name_match is None:
            output.append(raw)
            continue
        name = _compact_name(name_match.group(1))
        entry = normalized_entries.get(name)
        if entry is None:
            output.append(raw)
            continue
        expected = str(entry.get("ticker") or "")
        if not expected.isdigit():
            output.append(raw)
            continue
        output.append(
            f"{expected} {name_match.group(1).strip()}{raw[colons[0]]}{rest}"
        )
        corrected += 1
    return "\n".join(output), corrected


def _parse_inline_fields(body: str) -> dict[str, Any]:
    """解析“核心业务为…，所属板块为…，利好度 X，共识度 Y”inline 字段。"""
    fields: dict[str, Any] = {}
    matches = tuple(_INLINE_LABEL_RE.finditer(body))
    for index, match in enumerate(matches):
        mapped = _alias_field(match.group(0))
        if mapped is None or mapped in fields:
            continue
        value_start = match.end()
        while value_start < len(body) and body[value_start] in "为是：: \t":
            value_start += 1
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[value_start:value_end].strip(" 　\t，,。；;、")
        if mapped in ("lihao", "consensus"):
            fields[mapped] = normalize_score(value)
        else:
            fields[mapped] = value or None
    return fields


def _parse_inline_rows(text: str) -> list[dict[str, Any]]:
    """支持“代码 名称：…核心业务为…，利好度…，共识度…”的图片描述格式。"""
    drafts: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        match = _INLINE_ITEM_RE.match(line)
        if match is None:
            continue
        fields = _parse_inline_fields(match.group("body"))
        if fields.get("lihao") is None and fields.get("consensus") is None:
            continue
        draft: dict[str, Any] = {
            "code": (match.group("a6") or match.group("code")).upper(),
            "name": _clean_cell(match.group("name")),
            "lihao": None,
            "consensus": None,
            "core_business": None,
            "sector": None,
            "launch_in": None,
            "horizon": None,
        }
        draft.update(fields)
        drafts.append(draft)
    return drafts


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
    """解析一段载体文本为行草稿（表格或列表/代码前置 inline 风格）。"""
    cleaned = _FALLBACK_PREFIX_RE.sub("", text or "")
    table_drafts = _parse_table(cleaned)
    if table_drafts:
        return table_drafts
    list_drafts = _parse_list_style(cleaned)
    if list_drafts:
        return list_drafts
    return _parse_inline_rows(cleaned)


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
    name_map: Mapping[str, Any] | None = None,
) -> list[InstrumentScoreRecord]:
    """跨载体解析一篇普通栏文章的全部评分记录（去重 + 交叉冲突标记）。"""
    normalized_entries: dict[str, dict[str, Any]] = {}
    if name_map is not None:
        for key, entry in name_map.items():
            compact = _compact_name(str(key))
            if compact and isinstance(entry, Mapping):
                normalized_entries.setdefault(compact, dict(entry))

    def normalize_draft_code(draft: Mapping[str, Any]) -> dict[str, Any]:
        fixed = dict(draft)
        name = _compact_name(str(fixed.get("name") or ""))
        entry = normalized_entries.get(name)
        if entry is None:
            return fixed
        expected = str(entry.get("ticker") or "")
        if expected.isdigit():
            fixed["code"] = expected
        return fixed

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
            draft = normalize_draft_code(draft)
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


def upsert_records(
    path: Path,
    records: list[InstrumentScoreRecord],
    remove_record_ids: Iterable[str] = (),
) -> tuple[int, int]:
    """原子 upsert；返回 (新增, 更新)。文件 0600、目录 0700。

    remove_record_ids 用于代码归一后的旧行替换：先按 record_id 删除，
    再 upsert 新行，避免错码行残留。
    """
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    existing = load_records(target)
    for record_id in remove_record_ids:
        existing.pop(str(record_id), None)
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


_HISTORY_HINT_TOKENS = frozenset(
    {
        "历史",
        "演变",
        "变化",
        "走势",
        "全部",
        "之前",
        "趋势",
        "近期",
        "近一年",
        "近半年",
        "近三月",
    }
)
_STOP_TOKENS = frozenset(
    {
        "评分",
        "评级",
        "研报",
        "zsxq",
        "股票",
        "标的",
        "公司",
        "怎么样",
        "如何",
        "哪些",
        "什么",
        "有没有",
        "查询",
        "看一下",
        "看看",
        "帮我",
        "在",
        "的",
        "里",
    }
)


class InstrumentScoreQueryReader:
    """read_instrument_scores 只读查询：窗口默认、needs_review 不计入列表。"""

    def __init__(
        self,
        knowledge_base_root: Path,
        *,
        window_config_path: Path | None = None,
    ) -> None:
        self._kb_root = Path(knowledge_base_root)
        self._store_path = instrument_scores_path(self._kb_root)
        self._window_config_path = Path(window_config_path) if window_config_path else None

    def _window_days(self, column: str) -> int:
        default_days = 60
        if self._window_config_path is not None and self._window_config_path.exists():
            try:
                payload = json.loads(self._window_config_path.read_text(encoding="utf-8"))
                windows = payload.get("windows") if isinstance(payload, dict) else None
                if isinstance(windows, dict):
                    entry = windows.get(column)
                    if isinstance(entry, dict) and isinstance(entry.get("days"), int):
                        return entry["days"]
                if isinstance(payload, dict) and isinstance(payload.get("default_days"), int):
                    return payload["default_days"]
            except (OSError, ValueError):
                pass
        return default_days

    @staticmethod
    def _keyword_tokens(question: str) -> tuple[str, ...]:
        expanded: list[str] = []
        for token in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", question):
            if len(token) < 2 or token.casefold() in _STOP_TOKENS:
                continue
            if any(stop in token for stop in _STOP_TOKENS):
                continue
            if len(token) >= 4 and re.search(r"[\u4e00-\u9fa5]", token):
                expanded.extend(
                    dict.fromkeys(
                        token[index : index + 2]
                        for index in range(len(token) - 1)
                        if token[index : index + 2] not in _STOP_TOKENS
                    )
                )
            else:
                expanded.append(token)
        return tuple(expanded)

    @staticmethod
    def _sort_time(record: Mapping[str, Any]) -> str:
        """时间线排序键：published_at 优先，缺省回退 article_date（D-037）。"""
        return str(
            record.get("published_at") or record.get("article_date") or ""
        )

    def read(self, request: Any) -> Any:
        """Return ProductionReadResult-compatible value (avoid hard import in tests)."""
        from fin_analyse.read_capabilities.types import (
            ProductionReadResult,
            SourceKind,
            SourceTrust,
        )

        if not self._store_path.exists():
            return ProductionReadResult(
                value={"status": "EMPTY", "counts": {}},
                data_gaps=("instrument_scores_unavailable",),
            )
        records = [
            json.loads(line)
            for line in self._store_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        instruments = tuple(
            item.strip().upper() for item in request.instruments if item.strip()
        )
        tokens = self._keyword_tokens(request.question)
        include_history = any(
            hint in request.question for hint in _HISTORY_HINT_TOKENS
        )
        query_tokens = tuple(
            token
            for token in tokens
            if not any(hint in token for hint in _HISTORY_HINT_TOKENS)
        )

        def matches(record: Mapping[str, Any]) -> bool:
            code = str(record.get("code") or "")
            name = str(record.get("name") or "")
            haystack = " ".join(
                str(record.get(key) or "")
                for key in ("name", "core_business", "sector", "title")
            )
            if instruments:
                for instrument in instruments:
                    if instrument.isdigit() and code == instrument:
                        return True
                    if not instrument.isdigit() and instrument in name:
                        return True
                return False
            return (
                any(token in haystack for token in query_tokens)
                if query_tokens
                else True
            )

        matched = [record for record in records if matches(record)]
        if query_tokens:
            matched.sort(
                key=lambda record: (
                    sum(
                        1
                        for token in query_tokens
                        if token
                        in " ".join(
                            str(record.get(key) or "")
                            for key in ("name", "core_business", "sector", "title")
                        )
                    ),
                    self._sort_time(record),
                ),
                reverse=True,
            )
        as_of = request.as_of if request.as_of is not None else datetime.now(UTC)
        window_days = max(
            (self._window_days(str(record.get("column", ""))) for record in matched),
            default=60,
        )
        cutoff = as_of - timedelta(days=window_days)

        def in_window(record: Mapping[str, Any]) -> bool:
            try:
                date_text = str(
                    record.get("published_at") or record.get("article_date") or ""
                )
                article_date = datetime.fromisoformat(
                    date_text
                ).replace(tzinfo=None)
            except ValueError:
                return False
            return article_date >= cutoff.replace(tzinfo=None)

        ok_records = [record for record in matched if record.get("status") == "ok"]
        scoped_ok = (
            ok_records
            if include_history
            else [record for record in ok_records if in_window(record)]
        )
        scoped_needs_review = [
            record
            for record in matched
            if record.get("status") == "needs_review"
            and (include_history or in_window(record))
        ]
        scoped_ok.sort(
            key=lambda record: self._sort_time(record), reverse=True
        )
        returned = scoped_ok[:20]
        value: dict[str, object] = {
            "schema_version": "fin.instrument-scores-query/v1",
            "source_boundary": "zsxq_ordinary_research_scores",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "as_of": as_of.isoformat(),
            "window_days": window_days,
            "windowed": not include_history,
            "query": {"instruments": list(instruments), "keywords": list(query_tokens)},
            "counts": {
                "matched": len(matched),
                "ok": len(scoped_ok),
                "needs_review": len(scoped_needs_review),
                "returned": len(returned),
            },
            "records": returned,
        }
        gaps: tuple[str, ...] = ()
        if not returned:
            gaps = ("instrument_scores_no_match",)
        return ProductionReadResult(
            value=value,
            sources=(),
            data_gaps=gaps,
        )
