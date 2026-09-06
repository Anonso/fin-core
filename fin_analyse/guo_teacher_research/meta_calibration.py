"""G 元认知调节器画像 loader（设计稿 docs/design/g-meta-calibrator.md v2）。

元认知层（回放线）的二阶认知经 owner 终审画像进入问询：read_g_context 返回
结构追加可选 ``meta_calibration`` 块。本模块只做校验与 fail-open 读取——
画像缺失/损坏/过期/模板缺边界条款一律返回 None，问询行为退化为现状，
绝不阻塞（设计门 g-meta-calibrator-20260906 裁决 #2/#9/#11/#12）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# class 五值（画像口径）与回放线 claim_type/caliber 的归并映射（设计稿 §2.1）。
# 仅指导 owner 组装画像与后续 v2 的 per-unit 搬运；loader 不反查单元。
CLAIM_TYPE_TO_CLASS: dict[str, tuple[str, ...]] = {
    "current_observation": ("observation",),
    "forecast": ("point_time", "direction"),
    "scenario": ("point_time",),
    "structural_analysis": ("direction", "narrative"),
    "historical_analysis": ("narrative",),
    "action_layer": ("discipline",),
    "mixed_published_report": (),
}

CLASS_CLOSED = frozenset(
    {"observation", "point_time", "direction", "narrative", "discipline"}
)
RELIABILITY_CLOSED = frozenset({"high", "mid", "low", "unverifiable"})

#: 注入模板必须包含的边界要素（设计门裁决 #9/#11/#12：防一阶化/反向误读/
#: 分级表泄漏/不可验叙事当事实/假精度），缺任一 loader 拒绝注入。
REQUIRED_TEMPLATE_MARKS: tuple[str, ...] = (
    "不得作为市场方向依据",
    "低可靠度≠反向信号",
    "不得在回答中向用户引述可靠度分级表本身",
    "不得作为事实转述",
    "工作假说",
)

_DEFAULT_MAX_AGE_DAYS = 90
_BASIS_PREFIXES = ("batch:", "section:")


class MetaProfileError(ValueError):
    """画像 schema/边界条款违规（loader 捕获后 fail-open，不上抛）。"""


def _require(d: dict, key: str, types: tuple[type, ...]) -> object:
    if key not in d:
        raise MetaProfileError(f"missing key: {key}")
    if not isinstance(d[key], types):
        raise MetaProfileError(f"bad type for {key}")
    return d[key]


def validate_meta_profile(d: object) -> dict:
    """画像 schema 闭集校验；任一违规抛 MetaProfileError。"""
    if not isinstance(d, dict):
        raise MetaProfileError("profile must be an object")
    if d.get("schema_version") != "fin.g-meta-profile/v1":
        raise MetaProfileError("schema_version mismatch")
    as_of = str(_require(d, "as_of", (str,)))
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise MetaProfileError("as_of not ISO date") from exc
    int(_require(d, "version", (int,)))
    template = str(_require(d, "injection_template", (str,)))
    for mark in REQUIRED_TEMPLATE_MARKS:
        if mark not in template:
            raise MetaProfileError(f"injection_template missing boundary mark: {mark}")
    raw_max_age = d.get("max_age_days", _DEFAULT_MAX_AGE_DAYS)
    if not isinstance(raw_max_age, int) or isinstance(raw_max_age, bool) or raw_max_age <= 0:
        raise MetaProfileError("max_age_days must be positive int")
    dimensions = _require(d, "dimensions", (list,))
    if not dimensions:
        raise MetaProfileError("empty dimensions")
    seen: set[str] = set()
    for dim in dimensions:
        if not isinstance(dim, dict):
            raise MetaProfileError("dimension must be object")
        cls = str(_require(dim, "class", (str,)))
        if cls not in CLASS_CLOSED:
            raise MetaProfileError(f"class not in closed set: {cls}")
        if cls in seen:
            raise MetaProfileError(f"duplicate class: {cls}")
        seen.add(cls)
        rel = str(_require(dim, "reliability", (str,)))
        if rel not in RELIABILITY_CLOSED:
            raise MetaProfileError(f"reliability not in closed set: {rel}")
        basis = str(_require(dim, "basis", (str,)))
        if not basis:
            raise MetaProfileError("empty basis")
        for part in basis.split(";"):
            part = part.strip()
            if not part.startswith(_BASIS_PREFIXES):
                raise MetaProfileError(f"basis not pointer-style: {part[:24]}")
        _require(dim, "note", (str,))
    return d


def load_meta_calibration_block(
    path: Path | None, *, ref_date: date
) -> dict[str, object] | None:
    """读画像并渲染注入块；任何问题 fail-open 返回 None（含目录不可创建面）。

    ref_date 语义：PIT 查询=request.as_of 的日期（画像 as_of 晚于它=未来
    事实，不注入）；实时查询=provider clock 日期。
    """
    if path is None:
        return None
    try:
        profile = validate_meta_profile(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    profile_as_of = date.fromisoformat(str(profile["as_of"]))
    age_days = (ref_date - profile_as_of).days
    if age_days < 0 or age_days > int(profile["max_age_days"]):
        return None
    return {
        "as_of": profile["as_of"],
        "version": profile["version"],
        "dimensions": profile["dimensions"],
        # replace 而非 format：模板含其他花括号时不抛 KeyError（在 fail-open 面外）
        "boundary": str(profile["injection_template"]).replace(
            "{as_of}", str(profile["as_of"])
        ),
    }
