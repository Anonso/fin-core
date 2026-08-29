"""Tests for G methodology projection (G 方法论层验收 2:确定性分组).

TDD: tests must FAIL before implementation exists.
"""

from __future__ import annotations

from fin_analyse.guo_teacher_research.g_methodology_projection import project_methodology


def _rule(
    *,
    source_ref: str,
    title: str = "卡口分析",
    published_at: str = "2026-08-10T10:00:00+08:00",
    topics: tuple[str, ...] = ("半导体",),
    generation: str = "gen-1",
    confidence: float = 0.8,
) -> dict:
    return {
        "source_ref": source_ref,
        "title": title,
        "rule": "先识别卡口环节,再看供需缺口",
        "teacher_quote": "老师原话",
        "apprentice_interpretation": "推演",
        "related_topics": list(topics),
        "confidence": confidence,
        "published_at": published_at,
        "generation": generation,
    }


def test_groups_rules_by_exact_related_topic_label():
    """同一 related_topics 标签跨文章 → 同一组;组内按 published_at 升序。"""
    rules = [
        _rule(source_ref="zsxq-1", published_at="2026-08-11T10:00:00+08:00"),
        _rule(source_ref="zsxq-2", published_at="2026-08-10T10:00:00+08:00"),
        _rule(source_ref="zsxq-3", topics=("供需缺口",)),
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    assert result.data_gaps == ()
    topics = [g.topic for g in result.groups]
    assert topics == ["供需缺口", "半导体"]  # 确定性排序
    semi = [g for g in result.groups if g.topic == "半导体"][0]
    assert [r.source_ref for r in semi.rules] == ["zsxq-2", "zsxq-1"]  # published_at 升序


def test_one_rule_can_belong_to_multiple_topic_groups():
    result = project_methodology(
        (_rule(source_ref="zsxq-1", topics=("半导体", "卡口")),),
        generation="gen-1",
        manifest_sha256="m1",
    )
    assert [g.topic for g in result.groups] == ["半导体", "卡口"]
    assert all(len(g.rules) == 1 for g in result.groups)


def test_rules_below_confidence_threshold_are_not_injected():
    """宁缺毋滥：confidence < 0.75 的规则静默跳过（质量筛选，非错误 gap）。"""
    rules = [
        _rule(source_ref="zsxq-1"),
        _rule(source_ref="zsxq-2", confidence=0.74),
        _rule(source_ref="zsxq-3", confidence=0.65),
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    refs = [r.source_ref for g in result.groups for r in g.rules]
    assert refs == ["zsxq-1"]
    assert result.data_gaps == ()


def test_duplicate_rules_from_same_source_keep_highest_confidence():
    """同源同原话的重复提取只保留高置信实例（提取侧无查重，注入侧兜底）。"""
    rules = [
        _rule(source_ref="zsxq-1", title="避免反复横跳", confidence=0.76),
        _rule(source_ref="zsxq-1", title="避免反复横跳", confidence=0.81),
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    flat = [r for g in result.groups for r in g.rules]
    assert len(flat) == 1
    assert flat[0].confidence == 0.81


def test_empty_input_returns_no_samples_gap():
    result = project_methodology((), generation="gen-1", manifest_sha256="m1")
    assert result.groups == ()
    assert "g_methodology_no_samples" in result.data_gaps


def test_malformed_entries_are_skipped_with_gap():
    rules = [
        {"source_ref": "", "title": "", "rule": ""},  # 畸形:缺全部
        _rule(source_ref="zsxq-ok"),
        "not-a-dict",
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    assert "g_methodology_entry_invalid" in result.data_gaps
    assert [g.topic for g in result.groups] == ["半导体"]
    assert result.groups[0].rules[0].source_ref == "zsxq-ok"


def test_budget_truncation_records_gap():
    rules = [_rule(source_ref=f"zsxq-{i}", topics=(f"topic-{i}",)) for i in range(6)]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1", budget=3)
    assert "g_methodology_budget_truncated" in result.data_gaps
    assert len(result.groups) == 3


def test_min_rules_per_group_drops_undersized_groups():
    rules = [
        _rule(source_ref="zsxq-1", topics=("半导体",)),
        _rule(source_ref="zsxq-2", topics=("供需缺口",)),
    ]
    result = project_methodology(
        tuple(rules),
        generation="gen-1",
        manifest_sha256="m1",
        min_rules_per_group=2,
    )
    assert result.groups == ()
    assert "g_methodology_group_insufficient_samples" in result.data_gaps


def test_projection_is_deterministic():
    rules = [
        _rule(source_ref="zsxq-2", published_at="2026-08-11T10:00:00+08:00", topics=("b",)),
        _rule(source_ref="zsxq-1", published_at="2026-08-10T10:00:00+08:00", topics=("a",)),
        _rule(source_ref="zsxq-3", published_at="2026-08-09T10:00:00+08:00", topics=("b",)),
    ]
    first = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    second = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    assert [g.topic for g in first.groups] == [g.topic for g in second.groups]
    assert [r.source_ref for g in first.groups for r in g.rules] == [
        r.source_ref for g in second.groups for r in g.rules
    ]


def test_generation_and_manifest_bind_through():
    result = project_methodology(
        (_rule(source_ref="zsxq-1"),),
        generation="gen-abc",
        manifest_sha256="sha-m",
    )
    assert result.generation == "gen-abc"
    assert result.manifest_sha256 == "sha-m"
    assert all(r.generation == "gen-abc" for g in result.groups for r in g.rules)


def test_duplicate_detected_by_rule_text_even_with_different_quotes() -> None:
    """OR 语义:同源不同原话但同规则文本的重复同样去重(留高置信)。"""
    rules = [
        _rule(source_ref="zsxq-1", title="同规则", confidence=0.78),
        {
            **_rule(source_ref="zsxq-1", title="同规则", confidence=0.85),
            "teacher_quote": "另一处原话表述",
        },
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    flat = [r for g in result.groups for r in g.rules]
    assert len(flat) == 1
    assert flat[0].confidence == 0.85


def test_low_confidence_filter_keeps_audit_count() -> None:
    """宁缺毋滥的过滤留下独立审计事实（计数），非静默。"""
    rules = [
        _rule(source_ref="zsxq-1"),
        _rule(source_ref="zsxq-2", confidence=0.5),
        _rule(source_ref="zsxq-3", confidence=0.6),
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    assert result.low_confidence_skipped == 2
    refs = [r.source_ref for g in result.groups for r in g.rules]
    assert refs == ["zsxq-1"]


def test_dedupe_handles_quote_and_rule_collisions_across_multiple_priors() -> None:
    """r2 审计复现:(q1,r1,.80),(q2,r2,.91),(q1,r2,.85)——第三条 quote 撞
    第一条、rule 撞第二条,应被更高置信 prior 抑制,只输出第二条。"""
    base = {
        "source_ref": "zsxq-1",
        "title": "规则",
        "related_topics": ["纪律"],
        "confidence": 0.8,
        "published_at": "2026-08-10T10:00:00+08:00",
        "generation": "gen-1",
    }
    rules = [
        {**base, "rule": "规则文本一", "teacher_quote": "原话一", "confidence": 0.80},
        {**base, "rule": "规则文本二", "teacher_quote": "原话二", "confidence": 0.91},
        {**base, "rule": "规则文本二", "teacher_quote": "原话一", "confidence": 0.85},
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    flat = [r for g in result.groups for r in g.rules]
    # 第三条(.85)被 .91 prior 抑制;第一条(.80)与第二条是不同规则,均保留。
    assert len(flat) == 2
    assert sorted(r.confidence for r in flat) == [0.80, 0.91]


def test_quote_and_rule_prefixes_are_namespaced() -> None:
    """quote 与 rule 键分属独立命名空间:同源下 A 的原话恰等于 B 的规则文本
    前缀时不得误去重。"""
    base = {
        "source_ref": "zsxq-1",
        "title": "规则",
        "related_topics": ["纪律"],
        "confidence": 0.8,
        "published_at": "2026-08-10T10:00:00+08:00",
        "generation": "gen-1",
    }
    rules = [
        {**base, "rule": "先看卡口", "teacher_quote": "same-prefix 原文"},
        {**base, "rule": "same-prefix 规则文本", "teacher_quote": "另一原话"},
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    flat = [r for g in result.groups for r in g.rules]
    assert len(flat) == 2


def test_all_low_confidence_rules_still_report_audit_count() -> None:
    """全部被置信门过滤时,审计计数不得因空组早返丢失。"""
    rules = [
        _rule(source_ref="zsxq-1", confidence=0.5),
        _rule(source_ref="zsxq-2", confidence=0.6),
    ]
    result = project_methodology(tuple(rules), generation="gen-1", manifest_sha256="m1")
    assert result.groups == ()
    assert result.low_confidence_skipped == 2
    assert "g_methodology_no_samples" in result.data_gaps
