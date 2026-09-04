"""Cognition mainline read-model: schema, generator, validator, reader, publisher.

Owner-only read-model for manually reviewed G cognition units
(schema ``fin.cognition-mainline-readmodel/v1``).  Generator parses the
manual-annotation markdown at build time; runtime only reads the generated
artifact (never parses markdown).  Validator rejects the whole payload on any
failure (closed sets, required fields, time/identity, reference integrity,
path safety, content hash) — never partial publication.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from fin_analyse.common.zsxq_jargon import jargon_notes as scan_jargon_notes

# 标注文档时间为中国时区语境（A 股/雪球文章发表时间）。
_ANNOTATION_TZ = timezone(timedelta(hours=8))

SCHEMA_VERSION = "fin.cognition-mainline-readmodel/v1"

COGNITION_MODES = frozenset(
    {
        "current_observation",  # 当前观察
        "historical_analysis",  # 历史分析
        "structural_analysis",  # 结构分析
        "forecast",  # 预测
        "scenario",  # 情景
        "object_mapping",  # 对象映射
        "action_layer_not_cognition",  # 行动层（六类模式不适用）
    }
)
SOURCE_NATURES = frozenset(
    {
        "G_ORIGINAL",
        "MIXED_PUBLISHED_REPORT",
        "AI_ASSISTED_CONTENT_MIXED",
        # 老师口播·粉丝转述（直播总结）：archive-only——入 durable readmodel，
        # projector 不投影（设计 g-spoken-transcribed-grade v2，外审 Q1-P1）。
        "SPOKEN_FAN_TRANSCRIBED",
    }
)
CHANGE_TYPES = frozenset({"baseline", "no_change", "increment", "reframe"})
AUTHORSHIPS = frozenset({"G_ORIGINAL", "AGENT_REASONING_LABELED"})
RELATIONS = frozenset({"supports", "partially_supports", "diverges", "unknown", "no_evidence"})

UNIT_ID_RE = re.compile(r"^CU-\d{4}-[A-Z]?\d{2}$")
_ABS_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


class CognitionMainlineReadModelError(ValueError):
    """Stable fail-closed error for read-model validation/generation failures."""


def _ensure_tz_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must carry an explicit timezone (RFC3339)")
    return value


class _CognitionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    article_ref: str
    published_at: datetime
    source_nature: Literal[
        "G_ORIGINAL",
        "MIXED_PUBLISHED_REPORT",
        "AI_ASSISTED_CONTENT_MIXED",
        "SPOKEN_FAN_TRANSCRIBED",
    ]

    @field_validator("published_at")
    @classmethod
    def _published_at_tz(cls, value: datetime) -> datetime:
        return _ensure_tz_aware(value)

    @field_validator("article_ref")
    @classmethod
    def _article_ref_canonical(cls, value: str) -> str:
        issue = _article_ref_canonicality_issue(value)
        if issue is not None:
            raise ValueError(
                "article_ref must be a canonical repo-relative knowledge-base ref "
                f"({issue}): {value!r}"
            )
        return value


class _EvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    available_at: datetime
    relation: Literal["supports", "partially_supports", "diverges", "unknown", "no_evidence"]
    summary: str
    limitations: list[str] = []

    @field_validator("available_at")
    @classmethod
    def _evidence_at_tz(cls, value: datetime) -> datetime:
        return _ensure_tz_aware(value)


class _CognitionUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str
    cognition_mode: Literal[
        "current_observation",
        "historical_analysis",
        "structural_analysis",
        "forecast",
        "scenario",
        "object_mapping",
        "action_layer_not_cognition",
    ]
    secondary_modes: list[str] = []
    source_ref: str
    published_at: datetime
    observed_at: str
    effective_period: str
    forecast_window: str
    g_original_quote: str
    deepening_expression: str
    agent_reasoning: str | None = None
    material_direction: str | None = None
    material_action_guidance: str | None = None
    agent_investment_choice: str | None = None
    agent_trading_strategy: str | None = None
    source_material_quote: str | None = None
    topics: list[str] = []
    limitations: list[str]
    existing_evidence_summary: _EvidenceSummary | None = None

    @field_validator("unit_id")
    @classmethod
    def _unit_id_shape(cls, value: str) -> str:
        if not UNIT_ID_RE.fullmatch(value):
            raise ValueError(f"unit_id must match {UNIT_ID_RE.pattern!r}")
        return value

    @field_validator("published_at")
    @classmethod
    def _published_at_tz(cls, value: datetime) -> datetime:
        return _ensure_tz_aware(value)

    @field_validator("secondary_modes")
    @classmethod
    def _secondary_modes_closed_and_exclusive(cls, value: list[str]) -> list[str]:
        unknown = [mode for mode in value if mode not in COGNITION_MODES]
        if unknown:
            raise ValueError(f"secondary_modes contains unknown cognition modes: {unknown}")
        return value


class _EvolutionNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    relative_prior: str | None = None
    change_type: Literal["baseline", "no_change", "increment", "reframe"]
    kept: list[str] = []
    added: list[str] = []
    authorship: Literal["G_ORIGINAL", "AGENT_REASONING_LABELED"]
    unit_refs: list[str] = []
    available_at: datetime
    accepted: bool
    pit_working_set_identity: str | None = None

    @field_validator("available_at")
    @classmethod
    def _available_at_tz(cls, value: datetime) -> datetime:
        return _ensure_tz_aware(value)


class _ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["fin.cognition-mainline-readmodel/v1"]
    as_of: datetime
    generation: int
    content_hash: str
    annotation_ref: str
    available_at: datetime
    processed_at: datetime
    pit_working_set_identity: str
    sources: list[_CognitionSource]
    units: list[_CognitionUnit]
    evolution: list[_EvolutionNode]

    @field_validator("as_of", "available_at", "processed_at")
    @classmethod
    def _time_tz(cls, value: datetime) -> datetime:
        return _ensure_tz_aware(value)

    @field_validator("generation")
    @classmethod
    def _generation_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be a positive monotonically increasing revision")
        return value

    @field_validator("content_hash")
    @classmethod
    def _content_hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("content_hash must be a 64-char lowercase SHA-256 hex digest")
        return value


def validate_cognition_mainline_readmodel(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one read-model payload; reject the whole payload on any failure.

    Closed sets, required fields, timezone, time/identity ordering, reference
    integrity (unit.source_ref ∈ sources, evolution unit_refs ∈ units), path
    safety (no host absolute article_ref) and content hash shape are enforced.
    Any failure raises :class:`CognitionMainlineReadModelError` — never partial.
    """
    if not isinstance(payload, dict):
        raise CognitionMainlineReadModelError("read-model payload must be a JSON object")
    try:
        model = _ReadModel.model_validate(payload)
    except ValidationError as error:
        raise CognitionMainlineReadModelError(f"read-model invalid: {error}") from error
    _check_reference_integrity(model)
    _check_time_ordering(model)
    _check_mode_exclusivity(model)
    return model.model_dump(mode="json")


def _check_reference_integrity(model: _ReadModel) -> None:
    source_ids = {source.source_id for source in model.sources}
    unit_ids = {unit.unit_id for unit in model.units}
    for unit in model.units:
        if unit.source_ref not in source_ids:
            raise CognitionMainlineReadModelError(
                f"unit {unit.unit_id} source_ref {unit.source_ref!r} is not in sources"
            )
    for node in model.evolution:
        for ref in node.unit_refs:
            if ref not in unit_ids:
                raise CognitionMainlineReadModelError(
                    f"evolution node {node.node!r} unit_ref {ref!r} is not in units"
                )


def _check_time_ordering(model: _ReadModel) -> None:
    if model.processed_at < model.available_at:
        raise CognitionMainlineReadModelError("processed_at must not be earlier than available_at")
    node_times = [node.available_at for node in model.evolution]
    if any(
        later < earlier
        for earlier, later in zip(
            node_times,
            node_times[1:],
            strict=False,  # 相邻对比较，两序列天然相差一个元素
        )
    ):
        raise CognitionMainlineReadModelError(
            "evolution available_at must be monotonically non-decreasing"
        )


def _check_mode_exclusivity(model: _ReadModel) -> None:
    for unit in model.units:
        primary = unit.cognition_mode
        if unit.cognition_mode == "action_layer_not_cognition":
            if unit.secondary_modes:
                raise CognitionMainlineReadModelError(
                    f"unit {unit.unit_id}: action_layer_not_cognition must not carry "
                    "secondary_modes"
                )
            continue
        if primary in unit.secondary_modes:
            raise CognitionMainlineReadModelError(
                f"unit {unit.unit_id}: cognition_mode must be exclusive with secondary_modes"
            )
        if "action_layer_not_cognition" in unit.secondary_modes:
            raise CognitionMainlineReadModelError(
                f"unit {unit.unit_id}: secondary_modes must not contain action_layer_not_cognition"
            )


# ---------------------------------------------------------------------------
# Generator (build-time only; runtime never parses the markdown)
# ---------------------------------------------------------------------------

_MODE_ZH_TO_EN = {
    "当前观察": "current_observation",
    "历史分析": "historical_analysis",
    "结构分析": "structural_analysis",
    "预测": "forecast",
    "情景": "scenario",
    "对象映射": "object_mapping",
    "行动层": "action_layer_not_cognition",
}
_SOURCE_ID_RE = re.compile(r"S-\d{4}[A-Z]?M?")
_CU_SECTION_RE = re.compile(r"^### (CU-\d{4}-[A-Z]?\d{2})：(.+)$")
_FIELD_RE = re.compile(r"^- (.+?)[：:]\s*(.*)$")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _content_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON without the content_hash field itself."""
    projection = dict(payload)
    projection.pop("content_hash", None)
    return hashlib.sha256(_canonical_json(projection).encode("utf-8")).hexdigest()


def _parse_datetime(text: str) -> datetime:
    match = re.match(r"(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?", text.strip())
    if match is None:
        raise CognitionMainlineReadModelError(f"cannot parse datetime: {text!r}")
    date_part, time_part = match.groups()
    hour, minute = (int(part) for part in (time_part or "00:00").split(":"))
    return datetime.strptime(date_part, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=_ANNOTATION_TZ
    )


_LINE_LOCATOR_RE = re.compile(r":\d[\d,\-]*$")


def _strip_line_locator(value: str) -> str:
    """Strip only a genuine trailing line-range locator (…:15-33, …:20-24,44)."""
    match = _LINE_LOCATOR_RE.search(value)
    return value if match is None else value[: match.start()].rstrip()


def _article_ref_canonicality_issue(ref: str) -> str | None:
    """Return why ref is not canonical, or None when it is canonical.

    Single canonical predicate shared by the generator and the artifact
    validator: canonical means repo-relative, inside the knowledge-base
    namespace, no Markdown wrapper, no absolute/drive/UNC path and no
    standalone ``.``/``..`` component.  Neither side fixes or normalizes a
    ref — both fail closed on any issue.
    """
    if "`" in ref or "*" in ref:
        return "Markdown wrapper"
    if _ABS_PATH_RE.match(ref):
        return "absolute path"
    if "\\" in ref:
        return "drive/UNC/backslash"
    if not ref.startswith("knowledge-base/"):
        return "outside knowledge-base namespace"
    if any(part in {".", ".."} for part in ref.split("/")):
        return "'.'/'..' path component"
    return None


def _require_canonical_article_ref(ref: str) -> None:
    """Fail closed unless ref is a canonical repo-relative knowledge-base ref."""
    issue = _article_ref_canonicality_issue(ref)
    if issue is not None:
        raise CognitionMainlineReadModelError(f"unsafe article_ref ({issue}): {ref!r}")


def _normalize_article_ref(raw: str) -> str:
    """Normalize an annotation cell to a public-safe canonical article_ref.

    Markdown wrapper characters (backticks / emphasis stars) and a genuine
    trailing line-range locator (…:15-33) are stripped first; the first colon
    is never used to cut — a Windows drive letter must not be silently
    truncated.  Host absolute paths are mapped onto the repository
    knowledge-base.  The final ref must be canonical: repo-relative, inside
    the knowledge-base namespace, without `.`/`..` components; anything
    unmappable or unsafe rejects the whole document (fail closed).
    """
    stripped = raw.strip().strip("`*").strip()
    stripped = _strip_line_locator(stripped)
    if _ABS_PATH_RE.match(stripped):
        marker = "/knowledge-base/"
        index = stripped.find(marker)
        if index == -1:
            raise CognitionMainlineReadModelError(
                f"unmappable absolute article path (no knowledge-base segment): {stripped}"
            )
        stripped = stripped[index + 1 :]
    _require_canonical_article_ref(stripped)
    return stripped


def _is_separator_row(cells: list[str]) -> bool:
    return all(not cell or set(cell) <= {"-", ":"} for cell in cells)


def _parse_sources(text: str) -> list[dict[str, Any]]:
    section = re.search(r"^## 来源与边界验证\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    if section is None:
        raise CognitionMainlineReadModelError("annotation doc missing 来源与边界验证 section")
    rows: list[dict[str, Any]] = []
    for line in section.group(1).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] == "来源 ID" or _is_separator_row(cells):
            continue
        source_id = cells[0]
        usage = cells[3]
        if "mixed_published_report" in usage:
            nature = "MIXED_PUBLISHED_REPORT"
        elif "AI-assisted/content-mixed" in usage:
            nature = "AI_ASSISTED_CONTENT_MIXED"
        elif "spoken_fan_transcribed" in usage:
            # canonical 标记唯一入口（来源表）：无标记一律回落 G_ORIGINAL，
            # 新档只有显式标记才进——防静默升格（外审 Q2-P1a）。
            nature = "SPOKEN_FAN_TRANSCRIBED"
        else:
            nature = "G_ORIGINAL"
        rows.append(
            {
                "source_id": source_id,
                "article_ref": _normalize_article_ref(cells[2]),
                "published_at": _parse_datetime(cells[1]).isoformat(),
                "source_nature": nature,
            }
        )
    return rows


def _parse_time_index(text: str) -> dict[str, dict[str, str]]:
    section = re.search(r"^## 时间语义索引\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    if section is None:
        raise CognitionMainlineReadModelError("annotation doc missing 时间语义索引 section")
    index: dict[str, dict[str, str]] = {}
    for line in section.group(1).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 5 or cells[0] == "认知单元" or _is_separator_row(cells):
            continue
        unit_id, published_at, observed_at, effective_period, forecast_window = cells
        index[unit_id] = {
            "published_at": published_at,
            "observed_at": observed_at,
            "effective_period": effective_period,
            "forecast_window": forecast_window,
        }
    return index


def _parse_cognition_modes(field: str) -> tuple[str, list[str]]:
    """Parse 认知模式 field: 主 `X`；次 `Y`、`Z`。 or 行动层（六类模式不适用）。"""
    if "行动层" in field:
        return "action_layer_not_cognition", []
    primary_match = re.search(r"主\s*`([^`]+)`", field)
    if primary_match is None:
        raise CognitionMainlineReadModelError(f"cannot parse cognition modes: {field!r}")
    primary = _MODE_ZH_TO_EN[primary_match.group(1)]
    secondary = [_MODE_ZH_TO_EN[name.strip()] for name in re.findall(r"次\s*`([^`]+)`", field)]
    return primary, secondary


def _parse_unit_sections(
    text: str,
    time_index: dict[str, dict[str, str]],
    source_ids: set[str],
) -> list[dict[str, Any]]:
    section = re.search(r"^## 认知单元\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    if section is None:
        raise CognitionMainlineReadModelError("annotation doc missing 认知单元 section")
    units: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in section.group(1).splitlines():
        header = _CU_SECTION_RE.match(line)
        if header is not None:
            if current is not None:
                units.append(current)
            current = {"unit_id": header.group(1), "title": header.group(2).strip()}
            continue
        if current is None:
            continue
        field = _FIELD_RE.match(line)
        if field is None:
            continue
        label, value = field.group(1).strip(), field.group(2).strip()
        if label == "来源/时间":
            match = _SOURCE_ID_RE.search(value)
            if match is None:
                raise CognitionMainlineReadModelError(
                    f"{current['unit_id']}: cannot parse source id from {value!r}"
                )
            current["source_ref"] = match.group(0)
        elif label == "认知模式":
            primary, secondary = _parse_cognition_modes(value)
            current["cognition_mode"] = primary
            current["secondary_modes"] = secondary
        elif label == "G 原文":
            current["g_original_quote"] = value
        elif label == "深化表达":
            current["deepening_expression"] = value
        elif label == "Agent 推理":
            current["agent_reasoning"] = value
        elif label == "来源材料内方向/选择":
            current["material_direction"] = value
        elif label == "Agent 投资选择":
            current["agent_investment_choice"] = value
        elif label == "来源材料内行动指导":
            current["material_action_guidance"] = value
        elif label == "Agent 交易策略":
            current["agent_trading_strategy"] = value
        elif label == "发布材料原文":
            current["source_material_quote"] = value
        elif label == "来源性质":
            if "mixed_published_report" in value:
                current["source_nature"] = "MIXED_PUBLISHED_REPORT"
            elif "AI-assisted/content-mixed" in value:
                current["source_nature"] = "AI_ASSISTED_CONTENT_MIXED"
        elif label == "验证":
            # 验证行携带该单元固有限制（未独立核验/不补写等），随行必现。
            current["limitations"] = [value]
    if current is not None:
        units.append(current)

    built: list[dict[str, Any]] = []
    for unit in units:
        unit_id = unit["unit_id"]
        time_row = time_index.get(unit_id)
        if time_row is None:
            raise CognitionMainlineReadModelError(f"unit {unit_id} missing from 时间语义索引 table")
        source_ref = unit.get("source_ref")
        if source_ref not in source_ids:
            raise CognitionMainlineReadModelError(
                f"unit {unit_id} source_ref {source_ref!r} not in sources"
            )
        observed_at = time_row["observed_at"]
        forecast_window = time_row["forecast_window"]
        built.append(
            {
                "unit_id": unit_id,
                "cognition_mode": unit["cognition_mode"],
                "secondary_modes": unit.get("secondary_modes", []),
                "source_ref": source_ref,
                "published_at": _parse_datetime(time_row["published_at"]).isoformat(),
                # 标注原文保留（"unknown/not stated" 前缀即可判定；相对词是复核保留信息）。
                "observed_at": observed_at,
                "effective_period": time_row["effective_period"],
                "forecast_window": "none_stated"
                if forecast_window in {"none stated", "未明示", "未抽取", "未抽取具体预测窗口"}
                else forecast_window,
                "g_original_quote": unit.get("g_original_quote", ""),
                "deepening_expression": unit.get("deepening_expression", ""),
                "agent_reasoning": unit.get("agent_reasoning"),
                "material_direction": unit.get("material_direction"),
                "material_action_guidance": unit.get("material_action_guidance"),
                "agent_investment_choice": unit.get("agent_investment_choice"),
                "agent_trading_strategy": unit.get("agent_trading_strategy"),
                "source_material_quote": unit.get("source_material_quote"),
                # v1 标注文档无 topics 字段，不做意图编造；投影的 intent 命中
                # 在本 v1 退化为全量 G_ORIGINAL（受预算约束），后续 slice 再补。
                "topics": [],
                "limitations": unit.get("limitations") or [],
            }
        )
    return built


def _parse_evolution(
    text: str,
    units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    section = re.search(r"^### 主线变化证据\n(.*?)(?=^## |^### )", text, re.MULTILINE | re.DOTALL)
    if section is None:
        raise CognitionMainlineReadModelError("annotation doc missing 主线变化证据 section")
    nodes: list[dict[str, Any]] = []
    for line in section.group(1).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 5 or cells[0] == "节点" or _is_separator_row(cells):
            continue
        node, prior, change_type, kept, added = cells
        change = change_type.strip("` ")
        change = "baseline" if "baseline" in change else change
        # unit_refs 按节点日期前缀匹配单元的 published_at（月份/日）。
        prefix = node.split()[0]  # "2026-06" / "2026-07-30"
        unit_refs = [unit["unit_id"] for unit in units if unit["published_at"].startswith(prefix)]
        available_at: datetime
        if prefix == "2026-06":
            available_at = _parse_datetime("2026-06-01")
        else:
            available_at = _parse_datetime(prefix)
        # v1 无晋级门：baseline/reframe 是当前主线，increment 只作背景。
        accepted = change in {"baseline", "reframe"}
        nodes.append(
            {
                "node": node,
                "relative_prior": None if prior in {"无", ""} else prior,
                "change_type": change,
                "kept": [kept] if kept and kept != "无前序可比较。" else [],
                "added": [added] if added else [],
                "authorship": "AGENT_REASONING_LABELED",
                "unit_refs": unit_refs,
                "available_at": available_at.isoformat(),
                "accepted": accepted,
                # 构建期无法确知 KB G manifest canonical identity；
                # v1 用输入标注文档 SHA-256 作可审计的 revision 身份（可替换）。
                "pit_working_set_identity": None,
            }
        )
    return nodes


def generate_cognition_mainline_readmodel(
    annotation_path: str | pathlib.Path,
    *,
    generation: int = 1,
    working_set_identity: str | None = None,
) -> dict[str, Any]:
    """Deterministically build one read-model payload from the annotation markdown.

    Build-time only. The output passes :func:`validate_cognition_mainline_readmodel`
    (whole-payload rejection on any failure); host absolute article paths are
    normalized to public-safe repo-relative refs and unmappable ones reject the
    whole build.

    ``working_set_identity`` is the KB G Working Set canonical identity
    (g_working_set manifest canonical_sha256) current at build time — the PIT
    selector matches it against the request-time Working Set identity. When
    omitted (e.g. tests or build without a KB), the input annotation document
    SHA-256 is used as the auditable revision identity.
    """
    path = pathlib.Path(annotation_path)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise CognitionMainlineReadModelError("annotation document is empty")
    doc_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    as_of_match = re.search(r"as_of=(\d{4}-\d{2}-\d{2})", text)
    if as_of_match is None:
        raise CognitionMainlineReadModelError("annotation doc missing as_of")
    as_of = _parse_datetime(as_of_match.group(1))

    sources = _parse_sources(text)
    time_index = _parse_time_index(text)
    units = _parse_unit_sections(text, time_index, {source["source_id"] for source in sources})
    evolution = _parse_evolution(text, units)
    published_times = [unit["published_at"] for unit in units]
    latest_published = max(published_times) if published_times else as_of.isoformat()

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "generation": generation,
        "content_hash": "0" * 64,  # 合法形状占位，下方按最终 canonical 计算
        "annotation_ref": f"manual-annotations/{path.name}",
        "available_at": latest_published,
        # processed_at = 文档声明的复核时点（确定性；构建期时钟不进 artifact）。
        "processed_at": as_of.isoformat(),
        # v1 单 revision 语义下 revision 身份 = 构建时 G Working Set identity
        # （KB manifest canonical_sha256）；未提供时用输入标注文档 SHA-256。
        # 空串是合法显式值（runtime 侧 manifest 未 READY 时的 identity），
        # 与 projector 的精确匹配语义一致，不得被 or 吞掉。
        "pit_working_set_identity": (
            working_set_identity if working_set_identity is not None else doc_sha256
        ),
        "sources": sources,
        "units": units,
        "evolution": evolution,
    }
    validated = validate_cognition_mainline_readmodel(payload)
    # hash 覆盖最终交付的 canonical 内容（pydantic 规范化后），reader 复核同基准。
    validated["content_hash"] = _content_hash(validated)
    return validated


# ---------------------------------------------------------------------------
# Publisher & Reader (single-revision artifact; numeric-generation CAS)
# ---------------------------------------------------------------------------

_MANIFEST_NAME = "readmodel.v1.json"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _open_root_directory(root: pathlib.Path, *, create: bool) -> int:
    """Open the owner-only read-model directory (dirfd; O_NOFOLLOW; bound)."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(root, flags)
    except FileNotFoundError:
        if create:
            root.mkdir(mode=0o700, parents=False)
            os.chmod(root, 0o700)
            return os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        raise


def _require_directory_bound(dir_fd: int, root: pathlib.Path) -> None:
    actual = os.readlink(f"/proc/self/fd/{dir_fd}")
    expected = str(root.resolve())
    if actual.rstrip("/") != expected.rstrip("/"):
        raise CognitionMainlineReadModelError("read-model directory changed during operation")


def _read_manifest_at(dir_fd: int, name: str) -> tuple[bytes, str] | None:
    """Read one manifest file under dir_fd; returns (raw, sha256) or None when missing."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise CognitionMainlineReadModelError("read-model target is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CognitionMainlineReadModelError("read-model target must be 0600")
        raw = bytearray()
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > _MAX_MANIFEST_BYTES:
                raise CognitionMainlineReadModelError("read-model manifest exceeds max bytes")
        raw_bytes = bytes(raw)
        return raw_bytes, hashlib.sha256(raw_bytes).hexdigest()
    finally:
        os.close(fd)


def _publish_raw(dir_fd: int, name: str, raw: bytes) -> None:
    """Atomically replace one manifest file (temp + fsync + replace + dir fsync)."""
    import uuid

    temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    temp_fd = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        os.fchmod(temp_fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(temp_fd, view)
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=dir_fd)


@dataclass(frozen=True)
class CognitionMainlinePublicationResult:
    """Unique disposition for a publish attempt (order is the contract)."""

    disposition: Literal["PUBLISHED", "ALREADY_PUBLISHED", "REJECTED"]
    reason: str | None = None


class CognitionMainlinePublisher:
    """Build-time owner: atomically publish one read-model revision (numeric CAS).

    Disposition order (frozen contract, aligned with the G Working Set
    reference protocol g_working_set.py:766/779 — raw-identical first):
      1. current.raw == candidate.raw            -> ALREADY_PUBLISHED (idempotent)
      2. current.generation >= candidate.generation (raw differs) -> REJECTED/GENERATION_REGRESSION
      3. current identity != expected_prior      -> REJECTED/PRIOR_DRIFT
      4. candidate content_hash != canonical content hash (checked before publish)
                                                 -> REJECTED/CONTENT_HASH_MISMATCH
      5. publish atomically + read-back verify   -> PUBLISHED
    """

    def __init__(self, root: pathlib.Path) -> None:
        supplied = pathlib.Path(root)
        if ".." in supplied.parts:
            raise ValueError("read-model root cannot contain parent traversal")
        self._root = supplied.absolute()
        self._manifest_name = _MANIFEST_NAME

    def publish(
        self,
        payload: dict[str, Any],
        *,
        expected_prior_identity: str | None,
    ) -> CognitionMainlinePublicationResult:
        validated = validate_cognition_mainline_readmodel(payload)
        candidate_raw = _canonical_json(validated).encode("utf-8")
        candidate_generation = int(validated["generation"])

        directory_fd = _open_root_directory(self._root, create=True)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            _require_directory_bound(directory_fd, self._root)
            current = _read_manifest_at(directory_fd, self._manifest_name)
            if current is not None:
                current_raw, current_sha = current
                if current_raw == candidate_raw:
                    return CognitionMainlinePublicationResult(disposition="ALREADY_PUBLISHED")
                current_payload = json.loads(current_raw.decode("utf-8"))
                current_generation = int(current_payload["generation"])
                if current_generation >= candidate_generation:
                    return CognitionMainlinePublicationResult(
                        disposition="REJECTED", reason="GENERATION_REGRESSION"
                    )
                if expected_prior_identity is not None and current_sha != expected_prior_identity:
                    return CognitionMainlinePublicationResult(
                        disposition="REJECTED", reason="PRIOR_DRIFT"
                    )
            elif expected_prior_identity is not None and expected_prior_identity != "MISSING":
                return CognitionMainlinePublicationResult(
                    disposition="REJECTED", reason="PRIOR_DRIFT"
                )
            # 发布前核对候选 canonical content_hash（raw-identical/generation/prior
            # 判定之后、replace 之前）：仅形状合法（64 hex）不足以免除核对，否则错误
            # hash 的更高 generation 会替换健康 artifact（reader 后验 drift）。
            if _content_hash(validated) != validated["content_hash"]:
                return CognitionMainlinePublicationResult(
                    disposition="REJECTED", reason="CONTENT_HASH_MISMATCH"
                )
            _publish_raw(directory_fd, self._manifest_name, candidate_raw)
            _require_directory_bound(directory_fd, self._root)
            published = _read_manifest_at(directory_fd, self._manifest_name)
            if published is None or published[0] != candidate_raw:
                raise OSError("read-model publication verification failed")
            return CognitionMainlinePublicationResult(disposition="PUBLISHED")
        finally:
            with suppress(OSError):
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            os.close(directory_fd)


@dataclass(frozen=True)
class CognitionMainlineReadout:
    """Typed read result: either a parsed payload or one typed failure code."""

    generation: int = 0
    content_hash: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None

    @classmethod
    def failed(cls, code: str) -> CognitionMainlineReadout:
        return cls(failure_code=code)


class CognitionMainlineReadModelReader:
    """Owner-only reader: canonical root, typed failure, no implicit creation.

    Reads the single current artifact (v1 single-revision semantics); the PIT
    selector (§ design 6.2) decides availability for a request as_of.
    """

    def __init__(self, root: pathlib.Path) -> None:
        supplied = pathlib.Path(root)
        if ".." in supplied.parts:
            raise ValueError("read-model root cannot contain parent traversal")
        self._root = supplied.absolute()
        self._manifest_name = _MANIFEST_NAME

    def read(self) -> CognitionMainlineReadout:
        try:
            directory_fd = _open_root_directory(self._root, create=False)
        except FileNotFoundError:
            return CognitionMainlineReadout.failed("missing")
        try:
            snapshot = _read_manifest_at(directory_fd, self._manifest_name)
            if snapshot is None:
                return CognitionMainlineReadout.failed("missing")
            raw, stored_sha = snapshot
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return CognitionMainlineReadout.failed("corrupt")
            if not isinstance(payload, dict):
                return CognitionMainlineReadout.failed("corrupt")
            try:
                validated = validate_cognition_mainline_readmodel(payload)
            except CognitionMainlineReadModelError:
                return CognitionMainlineReadout.failed("schema_drift")
            declared = validated.get("content_hash")
            if not isinstance(declared, str) or _content_hash(validated) != declared:
                return CognitionMainlineReadout.failed("hash_drift")
            return CognitionMainlineReadout(
                generation=int(validated["generation"]),
                content_hash=declared,
                payload=validated,
            )
        finally:
            os.close(directory_fd)


# ---------------------------------------------------------------------------
# Projector (pure read-only; PIT selector + whole-unit budget eviction)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CognitionMainlineProjection:
    """Bounded G_ORIGINAL projection items plus typed data gaps."""

    items: tuple[dict[str, object], ...] = ()
    data_gaps: tuple[str, ...] = ()


def _parse_rfc3339(text: str) -> datetime:
    return datetime.fromisoformat(text)


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


def _text_tokens(text: str) -> frozenset[str]:
    """Deterministic bag of CJK bigrams + alphanumeric words for relevance."""

    bigrams: set[str] = set()
    for run in _CJK_RUN.findall(text):
        bigrams.update(run[index : index + 2] for index in range(len(run) - 1))
    words = {word.lower() for word in _ALNUM_RUN.findall(text)}
    return frozenset(bigrams | words)


def _unit_relevance_score(
    question_tokens: frozenset[str],
    unit: dict[str, Any],
) -> int:
    if not question_tokens:
        return 0
    unit_text = " ".join(
        str(unit.get(key) or "")
        for key in (
            "g_original_quote",
            "deepening_expression",
            "effective_period",
            "forecast_window",
            "cognition_mode",
        )
    )
    return len(question_tokens & _text_tokens(unit_text))


def _render_unit(unit: dict[str, Any], source: dict[str, Any]) -> str:
    """Bounded text with G 原话 / 深化分列标注（Agent 可区分，不冒充 G）。"""
    lines = [f"G 原文（来源时点语境）：{unit['g_original_quote']}"]
    deepening = unit.get("deepening_expression") or ""
    if deepening:
        lines.append(f"深化表达（机器提炼，非 G 原话）：{deepening}")
    lines.append(
        f"时间：published_at {unit['published_at']}；observed_at {unit['observed_at']}；"
        f"effective_period {unit['effective_period']}；forecast_window {unit['forecast_window']}"
    )
    limitations = unit.get("limitations") or []
    if limitations:
        lines.append("限制：" + "；".join(limitations))
    return "\n".join(lines)


def project_cognition_mainline(
    payload: dict[str, Any],
    *,
    as_of: datetime,
    working_set_identity: str,
    budget_bytes: int = 4096,
    max_refs: int = 32,
    question: str = "",
) -> CognitionMainlineProjection:
    """Project G_ORIGINAL units under the PIT selector and byte/ref budget.

    PIT selector order (frozen, design §6.2):
      1. revision gate: artifact.available_at/processed_at <= as_of, else
         ``g_cognition_pit_artifact_not_available`` (v1 single-revision:
         earlier as_of is a pure typed gap — no backfill);
      2. identity gate: top-level pit_working_set_identity must equal the
         request-time G Working Set identity, else
         ``g_cognition_pit_identity_mismatch``;
      3. node gate: evolution node available_at <= as_of (node identity, when
         present, must also match), else ``g_cognition_pit_node_not_available``.
    Then only ``G_ORIGINAL`` units are rendered; every unit is injected
    whole or evicted whole (never string-cut); the shared byte budget is
    consumed relevance-first (deterministic CJK-bigram/alnum-word overlap with
    ``question``), with latest-published-first as the tie-break and the
    fallback for an empty or non-matching question — not a truth/freshness
    verdict over G; refs are capped at ``max_refs``.
    """
    gaps: list[str] = []
    available_at = _parse_rfc3339(payload["available_at"])
    processed_at = _parse_rfc3339(payload["processed_at"])
    if available_at > as_of or processed_at > as_of:
        return CognitionMainlineProjection(data_gaps=("g_cognition_pit_artifact_not_available",))
    if payload["pit_working_set_identity"] != working_set_identity:
        return CognitionMainlineProjection(data_gaps=("g_cognition_pit_identity_mismatch",))

    usable_unit_ids: set[str] = set()
    for node in payload["evolution"]:
        if _parse_rfc3333_node(node) > as_of:
            gaps.append("g_cognition_pit_node_not_available")
            continue
        node_identity = node.get("pit_working_set_identity")
        if node_identity is not None and node_identity != working_set_identity:
            gaps.append("g_cognition_pit_identity_mismatch")
            continue
        usable_unit_ids.update(node.get("unit_refs", []))

    sources_by_id = {source["source_id"]: source for source in payload["sources"]}
    candidates = [
        unit
        for unit in payload["units"]
        if unit["unit_id"] in usable_unit_ids
        and unit["source_ref"] in sources_by_id
        and sources_by_id[unit["source_ref"]]["source_nature"] == "G_ORIGINAL"
    ]
    question_tokens = _text_tokens(question)
    candidates.sort(
        key=lambda unit: (
            _unit_relevance_score(question_tokens, unit),
            unit["published_at"],
            unit["unit_id"],
        ),
        reverse=True,
    )

    items: list[dict[str, object]] = []
    used_bytes = 0
    for unit in candidates:
        source = sources_by_id[unit["source_ref"]]
        rendered = _render_unit(unit, source)
        size = len(rendered.encode("utf-8"))
        if size > budget_bytes - used_bytes:
            gaps.append("g_cognition_unit_budget_evicted")
            continue
        ref = source["article_ref"]
        # 黑话译注（NOW #14 下批，设计稿「新消费者=调一次旁注函数」）：渲染文本
        # 含 G 逐字引句，黑话可经引句露出；投影侧确定性附加，不改 PIT 密封工件
        # schema、不计入 budget_bytes（附加开销 ≤4 条窄契约）。
        jargon_notes = scan_jargon_notes(rendered, limit=4)
        item: dict[str, object] = {
            "source_bucket": "cognition_mainline_projection",
            "source_ref": ref,
            "source_refs": [ref],
            "title": f"G 认知单元 {unit['unit_id']}",
            "guidance_brief": rendered,
            "published_at": unit["published_at"],
            "available_at": unit["published_at"],
            "usage_boundary": "background_guidance_only_no_confidence_boost",
            "why_available": ["cognition_mainline_projection", "g_source_background"],
        }
        if jargon_notes:
            item["jargon_notes"] = jargon_notes
        items.append(item)
        used_bytes += size
    if len(items) > max_refs:
        items = items[:max_refs]
        gaps.append("g_cognition_ref_budget_truncated")
    return CognitionMainlineProjection(
        items=tuple(items),
        data_gaps=tuple(dict.fromkeys(gaps)),
    )


def _parse_rfc3333_node(node: dict[str, Any]) -> datetime:
    return _parse_rfc3339(node["available_at"])
