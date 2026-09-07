"""元认知调节器 loader 测试（设计门 g-meta-calibrator-20260906 探针面）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.meta_calibration import (
    REQUIRED_TEMPLATE_MARKS,
    MetaProfileError,
    load_meta_calibration_block,
    validate_meta_profile,
)

_TEMPLATE = (
    "以下为该老师历史判断的分维度可靠度记录，仅用于调节你对 G 内容的采纳强度与"
    "表述确定性，不得作为市场方向依据；低可靠度≠反向信号，不改变 G 原文的一阶含义；"
    "不得在回答中向用户引述可靠度分级表本身；unverifiable 维度的内容不得作为事实转述；"
    "本画像为工作假说级校准记录，基于有限验证批次，随新批次修订（数据截至 {as_of}）。"
)


def _profile(**over) -> dict:
    p = {
        "schema_version": "fin.g-meta-profile/v1",
        "as_of": "2026-09-06",
        "version": 1,
        "max_age_days": 90,
        "dimensions": [
            {"class": "point_time", "reliability": "high",
             "basis": "batch:20260904#CHK-0828-02-monthly", "note": ""},
            {"class": "direction", "reliability": "low",
             "basis": "section:8/20 窗口表 CU-0624-01 行;"
                      "batch:20260904#CHK-0827-01-spread", "note": "聚焦指令口径"},
        ],
        "injection_template": _TEMPLATE,
    }
    p.update(over)
    return p


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "meta-profile.v1.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_valid_profile_loads_block(tmp_path):
    path = _write(tmp_path, _profile())
    block = load_meta_calibration_block(path, ref_date=date(2026, 9, 6))
    assert block is not None
    assert block["as_of"] == "2026-09-06"
    assert block["version"] == 1
    assert len(block["dimensions"]) == 2
    assert "工作假说" in str(block["boundary"])
    assert "{as_of}" not in str(block["boundary"])
    assert "2026-09-06" in str(block["boundary"])


def test_missing_or_none_path_fails_open(tmp_path):
    assert load_meta_calibration_block(None, ref_date=date(2026, 9, 6)) is None
    assert load_meta_calibration_block(
        tmp_path / "absent.json", ref_date=date(2026, 9, 6)
    ) is None


def test_corrupt_json_fails_open(tmp_path):
    path = tmp_path / "meta-profile.v1.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_meta_calibration_block(path, ref_date=date(2026, 9, 6)) is None


def test_stale_profile_not_injected(tmp_path):
    path = _write(tmp_path, _profile(as_of="2026-06-01"))
    assert load_meta_calibration_block(path, ref_date=date(2026, 9, 6)) is None
    # 90 天内仍注入（6/8 → 9/6 = 90 天）
    path2 = _write(tmp_path, _profile(as_of="2026-06-08"))
    assert load_meta_calibration_block(path2, ref_date=date(2026, 9, 6)) is not None


def test_future_profile_not_injected_pit(tmp_path):
    path = _write(tmp_path, _profile(as_of="2026-09-07"))
    assert load_meta_calibration_block(path, ref_date=date(2026, 9, 6)) is None


def test_max_age_days_override(tmp_path):
    path = _write(tmp_path, _profile(as_of="2026-06-01", max_age_days=120))
    assert load_meta_calibration_block(path, ref_date=date(2026, 9, 6)) is not None


def test_missing_boundary_mark_rejected(tmp_path):
    bad = _profile()
    bad["injection_template"] = "仅供参考。"  # 缺全部边界要素
    path = _write(tmp_path, bad)
    assert load_meta_calibration_block(path, ref_date=date(2026, 9, 6)) is None
    with pytest.raises(MetaProfileError):
        validate_meta_profile(bad)
    assert set(REQUIRED_TEMPLATE_MARKS) <= {
        "不得作为市场方向依据", "低可靠度≠反向信号",
        "不得在回答中向用户引述可靠度分级表本身", "不得作为事实转述", "工作假说",
    }


def test_basis_must_be_pointer_style(tmp_path):
    bad = _profile()
    bad["dimensions"][1]["basis"] = "8 月科创50 相对银行跑输 10 个百分点以上"
    with pytest.raises(MetaProfileError):
        validate_meta_profile(bad)
    assert load_meta_calibration_block(
        _write(tmp_path, bad), ref_date=date(2026, 9, 6)
    ) is None


def test_closed_sets_enforced(tmp_path):
    bad = _profile()
    bad["dimensions"][0]["reliability"] = "oracle"
    with pytest.raises(MetaProfileError):
        validate_meta_profile(bad)
    bad2 = _profile(dimensions=[dict(_profile()["dimensions"][0], **{"class": "oracle"})])
    with pytest.raises(MetaProfileError):
        validate_meta_profile(bad2)
    assert load_meta_calibration_block(
        _write(tmp_path, bad), ref_date=date(2026, 9, 6)
    ) is None


# ── provider 入口层（audit P2-2：逐字节等价断言） ─────────────

def _stub_provider(meta_profile_path):
    from fin_analyse.guo_teacher_research.production_capability_provider import (
        ProductionReadCapabilityProvider,
    )
    from fin_analyse.guo_teacher_research.runtime_context import (
        AgentRuntimeContextResult,
    )

    class _StubRuntime:
        def resolve(self, request):
            return AgentRuntimeContextResult(
                available=True, llm_context={"g_context": []}
            )

    return ProductionReadCapabilityProvider(
        runtime_context=_StubRuntime(), meta_profile_path=meta_profile_path
    )


def test_provider_no_profile_value_is_untyped_equivalent(tmp_path):
    from fin_analyse.read_capabilities.types import ProductionReadRequest

    bare = _stub_provider(None)
    with_absent = _stub_provider(tmp_path / "absent.json")
    v1 = bare.read_g_context(ProductionReadRequest(question="科技怎么看")).value
    v2 = with_absent.read_g_context(ProductionReadRequest(question="科技怎么看")).value
    assert "meta_calibration" not in v1
    assert v1 == v2  # 逐字节等价（无画像=现状）


def test_provider_with_profile_injects_block(tmp_path):
    from fin_analyse.read_capabilities.types import ProductionReadRequest

    path = _write(tmp_path, _profile())
    provider = _stub_provider(path)
    value = provider.read_g_context(ProductionReadRequest(question="科技怎么看")).value
    block = value["meta_calibration"]
    assert block["as_of"] == "2026-09-06"
    assert len(block["dimensions"]) == 2


def test_loader_mixed_basis_segments_rejected(tmp_path):
    bad = _profile()
    bad["dimensions"][1]["basis"] = (
        "batch:20260904#CHK-0827-01-spread;8 月科技跑输银行十个百分点"
    )
    with pytest.raises(MetaProfileError):
        validate_meta_profile(bad)
    assert load_meta_calibration_block(
        _write(tmp_path, bad), ref_date=date(2026, 9, 6)
    ) is None


def test_duplicate_class_rejected(tmp_path):
    dims = _profile()["dimensions"]
    bad = _profile(dimensions=[dims[0], dims[0]])
    with pytest.raises(MetaProfileError, match="duplicate class"):
        validate_meta_profile(bad)


def test_bool_max_age_and_cap_enforced(tmp_path):
    with pytest.raises(MetaProfileError):
        validate_meta_profile(_profile(max_age_days=True))
    dims = [_profile()["dimensions"][0]] * 9
    with pytest.raises(MetaProfileError, match="max 8"):
        validate_meta_profile(_profile(dimensions=dims))
    long_basis = "batch:20260904#CHK-0827-01-spread" + "x" * 300
    with pytest.raises(MetaProfileError, match="300"):
        validate_meta_profile(
            _profile(dimensions=[dict(_profile()["dimensions"][0], basis=long_basis)])
        )


def test_provider_live_query_uses_wall_clock_not_injected_market_clock(tmp_path):
    """audit 后实弹发现：wiring effective_clock 滞后日历日时，当日新画像曾被误拒。"""
    from datetime import datetime, UTC
    from fin_analyse.read_capabilities.types import ProductionReadRequest

    today = date.today().isoformat()
    path = _write(tmp_path, _profile(as_of=today))
    provider = _stub_provider(path)
    provider._clock = lambda: datetime(2020, 1, 1, tzinfo=UTC)  # 滞后市场口径时钟
    value = provider.read_g_context(ProductionReadRequest(question="x")).value
    assert value["meta_calibration"]["as_of"] == today
