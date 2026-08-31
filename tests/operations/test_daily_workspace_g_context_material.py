"""G 认知材料注入的回归(daily-g-context-material 设计稿,2026-08-31 设计门 8/8 采纳)。

钉四件事:①渲染只取 strict-G 桶(recent_reference 等非 G 桶绝不混入「老师体系
证据」);②六字段冻结映射 + 4000 字按整条丢弃;③resolve 层 typed gaps 不进产品
data_gaps(一码一因);④两融项从持仓面投影删除(owner 拍板 2026-08-31)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fin_analyse.operations.daily_workspace_generator import (
    _G_CONTEXT_MAX_CHARS,
    _render_g_context,
    _render_portfolio,
)


class _Resolved:
    def __init__(self, llm_context: Mapping[str, Any], data_gaps: tuple[str, ...] = ()) -> None:
        self.llm_context = llm_context
        self.data_gaps = data_gaps


def _item(bucket: str, title: str, brief: str = "要点") -> dict[str, Any]:
    return {
        "source_bucket": bucket,
        "title": title,
        "guidance_brief": brief,
        "why_available": ["g_source_background"],
        "usage_boundary": "background_guidance_only_no_confidence_boost",
        "source_ref": "zsxq-123",
        "published_at": "2026-08-31 09:30",
    }


def test_strict_g_filter_excludes_non_g_buckets() -> None:
    resolved = _Resolved(
        {
            "g_context": [
                _item("fresh_g", "锐评要点"),
                _item("pinned_source", "置顶要点"),
                _item("latest_commentary", "最新评述"),
                _item("recent_reference", "普通栏同日问答"),
            ]
        }
    )

    text = _render_g_context(resolved)

    assert text is not None
    assert "锐评要点" in text
    assert "置顶要点" in text
    assert "最新评述" in text
    assert "普通栏同日问答" not in text


def test_empty_or_all_filtered_items_render_none() -> None:
    assert _render_g_context(_Resolved({"g_context": []})) is None
    assert (
        _render_g_context(_Resolved({"g_context": [_item("recent_reference", "非 G")]}))
        is None
    )
    assert _render_g_context(_Resolved({})) is None


def test_budget_drops_whole_items_without_mid_item_cut() -> None:
    big = _item("fresh_g", "长条目", brief="字" * 2000)
    tail = _item("fresh_g", "小条目")
    text = _render_g_context(_Resolved({"g_context": [big, big, big, tail]}))

    assert text is not None
    assert len(text) <= _G_CONTEXT_MAX_CHARS + 2 * text.count("\n")
    # 整条丢弃:任一渲染出来的条目都是完整行,不存在半条切断。
    for line in text.splitlines():
        assert line.endswith("zsxq-123）") or line.endswith("zsxq-123） ")


def test_resolve_gaps_stay_out_of_product_material() -> None:
    resolved = _Resolved({"g_context": [_item("fresh_g", "要点")]}, ("g_working_set_manifest_missing",))

    text = _render_g_context(resolved)

    assert text is not None
    assert "manifest_missing" not in text


class _Snapshot:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_safe_dict(self) -> dict[str, Any]:
        return dict(self._payload)


class _Read:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.snapshot = _Snapshot(payload)


def test_portfolio_projection_drops_margin_items() -> None:
    read = _Read(
        {
            "net_assets": "43312.33",
            "available_cash": "43312.33",
            "margin_debt": "0",
            "margin_debt_status": "KNOWN",
            "positions": [],
        }
    )

    text = _render_portfolio(read)

    assert text is not None
    assert "margin_debt" not in text
    assert "43312.33" in text


def test_prompt_carries_g_context_section_and_alignment_instruction() -> None:
    """有料时 prompt 含 G 认知节与对表指令、持仓不催更新指令。"""

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from fin_analyse.operations.daily_workspace_generator import _render_prompt

    materials: dict[str, str | None] = {
        "portfolio": "持仓快照正文",
        "market_overview": None,
        "g_context": "- 2026-08-31 锐评：算电协同要点",
        "g_reference": None,
    }
    prompt = _render_prompt(
        question="检查点问题",
        materials=materials,
        context=None,
        as_of=datetime(2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        trading_day_id="2026-08-31",
    )
    assert "# G 认知参考（老师体系证据）" in prompt
    assert "对照「G 认知参考」" in prompt
    assert "不要输出任何提示更新持仓" in prompt


def test_overview_material_uses_live_clock_not_frozen_checkpoint_clock(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """08-31 14:25 班后核对复现的确定性缺料根因。

    概览服务若被绑到检查点冻结时钟，fetch 期间新于截止瞬间的 provider 行
    时间戳必触发 PROVIDER_TIME_AFTER_QUERY 整链拒绝——交易日盘中班次确定性
    缺料，周末/盘后数据不更新故既有实证未暴露。概览是活读取，必须真实时钟；
    证据截止记账仍归检查点层。
    """

    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import fin_analyse.operations.daily_workspace_generator as gen

    captured: dict[str, object] = {}

    class _FakeService:
        def read(self, request: object) -> SimpleNamespace:
            return SimpleNamespace(
                status="PARTIAL",
                to_capability_value=lambda: {"ok": True},
            )

    def _fake_builder(*, clock: object = None) -> _FakeService:
        captured["clock"] = clock
        return _FakeService()

    monkeypatch.setattr(
        "fin_analyse.market.current_overview.build_default_a_share_market_overview",
        _fake_builder,
    )
    monkeypatch.setattr(gen, "_portfolio_name_resolver", lambda: None)
    monkeypatch.setattr(gen, "_registry_quote_reader", lambda: None)

    cst = timezone(timedelta(hours=8))
    frozen = datetime(2026, 8, 31, 13, 55, 0, 348519, tzinfo=cst)
    provider = gen.build_default_material_provider(
        knowledge_base_root=str(tmp_path),
        as_of_clock=lambda: frozen,
    )
    materials = provider("15:30 工作区检查点：今天盘面与持仓结论？")

    assert captured["clock"] is None
    assert materials["market_overview"] is not None
