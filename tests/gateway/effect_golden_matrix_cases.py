"""Effect Golden Matrix — real ZSXQ default-entry validation cases.

This is a TEST-SIDE validation asset (not a runtime source): it registers real
committed ZSXQ articles, the user question a page chat would ask, and the
page-chat baseline key points that FIN's default semantic answer must surface into the
prompt / ready-evidence when the user did NOT hand-paste the article.

Each case is either:
  - ``active_smoke``  — exercised by the parametrized gateway golden test.
  - ``registered``    — logged for future activation (not yet asserted).

Boundaries encoded here:
  - Ordinary Q&A / market_observation cases stay ``recent_reference`` (not-G):
    ``expected_source_bucket == "recent_reference"`` and
    ``expected_source_level_not == "g_direct"``.
  - ``baseline_note`` is a test-acceptance description, NOT teacher cognition.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_MIN_REGISTERED_CASES = 10
_MIN_ACTIVE_CASES = 8


@dataclass(frozen=True)
class EffectGoldenCase:
    """One real ZSXQ default-entry golden case."""

    case_id: str
    article_id: str
    article_path: str  # repo-relative path to the local markdown article
    title_contains: str
    question: str
    required_prompt_terms: tuple[str, ...]
    expected_source_bucket: str
    expected_source_level_not: str
    activation_status: str  # "active_smoke" | "registered"
    baseline_note: str

    def resolved_path(self) -> Path:
        return _PROJECT_ROOT / self.article_path


EFFECT_GOLDEN_MATRIX: tuple[EffectGoldenCase, ...] = (
    EffectGoldenCase(
        case_id="leverage_deleveraging",
        article_id="0c245ea48164",
        article_path="knowledge-base/articles/20260708_0c245ea48164.md",
        title_contains="杠杆",
        question="今天杠杆雷劫对操作意味着什么？",
        required_prompt_terms=("共识拉满", "追保", "800亿", "拒绝杠杆"),
        expected_source_bucket="latest_commentary",
        expected_source_level_not="",
        activation_status="registered",  # active in scenario-watchpoints slice
        baseline_note="共识拉满、追保、800亿去化、拒绝杠杆、雷劫后修复。",
    ),
    EffectGoldenCase(
        case_id="waves_q3",
        article_id="d7477b931c00",
        article_path="knowledge-base/articles/20260708_d7477b931c00.md",
        title_contains="四浪潮",
        question="锅老师说的四浪潮现在演化到哪一浪了？",
        required_prompt_terms=("光浪潮", "WF6", "抗日材料", "替美", "算力租赁"),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="registered",  # active in reference slice
        baseline_note="光浪潮最强、WF6 短爆、抗日材料长底、替美结构性、算力租赁前浪退。",
    ),
    EffectGoldenCase(
        case_id="helium_supply_gap",
        article_id="01a99e429a3d",
        article_path="knowledge-base/articles/20260708_01a99e429a3d.md",
        title_contains="氦气",
        question="氦气涨价和供给紧张现在怎么看，国产替代到哪一步？",
        required_prompt_terms=("氦气", "卡塔尔", "30%-38%", "6N", "有价无市"),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note=(
            "卡塔尔/俄罗斯扰动、30%-38% 供给中断、5N 涨约 4 倍、6N 有价无市、"
            "BOG 提氦/新疆四川试产。"
        ),
    ),
    EffectGoldenCase(
        case_id="gold_drawdown_allocation",
        article_id="83b41958de85",
        article_path="knowledge-base/articles/20260708_83b41958de85.md",
        title_contains="黄金",
        question="黄金下行到哪里开始缓解，后续还能配置吗？",
        required_prompt_terms=("黄金", "牛市", "4000-4100", "3900-3860", "5-10%"),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note=(
            "牛市回调非反转、4000-4100 支撑、3900-3860 深支撑、央行/亚洲托底、5-10% 配置。"
        ),
    ),
    EffectGoldenCase(
        case_id="wf6_leader_valuation",
        article_id="21d6be6d1e5d",
        article_path="knowledge-base/articles/20260707_21d6be6d1e5d.md",
        title_contains="WF6",
        question="WF6 龙头目前是否被高估，监管传闻是否属实？",
        required_prompt_terms=("WF6", "六氟化钨", "高估", "Q2", "监管"),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note=("高估/情绪溢价、Q2 业绩和订单验证、监管传闻非硬禁买、7 月 18 日半年报。"),
    ),
    EffectGoldenCase(
        case_id="compute_power_synergy",
        article_id="8b08f5db407b",
        article_path="knowledge-base/articles/20260708_8b08f5db407b.md",
        title_contains="算电协同",
        question="协鑫算电协同的故事还能讲下去吗？",
        required_prompt_terms=("算电协同", "协鑫", "虚拟电厂", "AIDC", "长协"),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note=(
            "协鑫故事继续、绿电+虚拟电厂+AIDC、长协等效绑定、Token 交易服务商、"
            "西部集群/算力收入观察。"
        ),
    ),
    EffectGoldenCase(
        case_id="advanced_packaging",
        article_id="d4ff9842155b",
        article_path="knowledge-base/articles/20260708_d4ff9842155b.md",
        title_contains="先进封装",
        question="先进封装国内发展怎么样，投资主线在哪？",
        required_prompt_terms=(
            "先进封装",
            "2.5D",
            "Chiplet",
            "HBM",
            "玻璃基板",
            "OSAT",
            "量价齐升",
        ),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note="2.5D/3D/Chiplet/HBM/玻璃基板、OSAT 扩产、量价齐升、中小盘成长。",
    ),
    EffectGoldenCase(
        case_id="material_42",
        article_id="2cd60446d674",
        article_path="knowledge-base/articles/20260707_2cd60446d674.md",
        title_contains="提问",
        question="第四代材料和中重稀土的订单边界现在怎么看？",
        required_prompt_terms=(
            "抗日先锋",
            "棒子共赢",
            "国产替代",
            "达子新品",
            "中重稀土",
            "订单",
        ),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note="抗日先锋、棒子共赢、国产替代、达子新品、中重稀土涨价/订单边界。",
    ),
    EffectGoldenCase(
        case_id="robot_physical_ai",
        article_id="51ecad9ba388",
        article_path="knowledge-base/articles/20260707_51ecad9ba388.md",
        title_contains="机器人",
        question="特斯拉机器人要量产了，Physical AI 主线怎么看？",
        required_prompt_terms=(
            "机器人",
            "Optimus Gen3",
            "2026年7-8月",
            "物理AI",
            "镁合金",
            "宝武镁业",
        ),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note=(
            "Optimus Gen3 7-8 月初始生产、Physical AI 落地、镁合金轻量化、宝武镁业间接关联。"
        ),
    ),
    EffectGoldenCase(
        case_id="changxin_ipo_chain",
        article_id="2013ba16fb50",
        article_path="knowledge-base/articles/20260707_2013ba16fb50.md",
        title_contains="长鑫",
        question="长鑫存储上市对存储国产替代链怎么看？",
        required_prompt_terms=(
            "长鑫科技",
            "7月中下旬至8月初",
            "长电科技",
            "兆易创新",
            "雅克科技",
            "存储荒",
            "AI服务器",
        ),
        expected_source_bucket="recent_reference",
        expected_source_level_not="g_direct",
        activation_status="active_smoke",
        baseline_note="一周虹吸震荡、一月资金再平衡、三个月国产替代、长电/兆易/雅克映射。",
    ),
)

ACTIVE_SMOKE_CASES: tuple[EffectGoldenCase, ...] = tuple(
    c for c in EFFECT_GOLDEN_MATRIX if c.activation_status == "active_smoke"
)


def assert_matrix_has_minimum_registered_cases() -> None:
    """Guard the matrix stays a real, minimally-sized validation asset."""
    assert len(EFFECT_GOLDEN_MATRIX) >= _MIN_REGISTERED_CASES, (
        f"matrix must register >= {_MIN_REGISTERED_CASES} cases, got {len(EFFECT_GOLDEN_MATRIX)}"
    )
    assert len(ACTIVE_SMOKE_CASES) >= _MIN_ACTIVE_CASES, (
        f"matrix must have >= {_MIN_ACTIVE_CASES} active smoke cases, got {len(ACTIVE_SMOKE_CASES)}"
    )
    ids = [c.case_id for c in EFFECT_GOLDEN_MATRIX]
    assert len(ids) == len(set(ids)), f"duplicate case_id in matrix: {ids}"
