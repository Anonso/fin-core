"""Stateless, read-only G methodology projection (G 方法论层验收 2).

Projects compact ``methodology_rules`` into per-topic groups keyed by the
exact ``related_topics`` labels the teacher-original rules carry.  Pure
function of its inputs: no state, no writes, no caches, no background work,
no generative aggregation — paradigm inference belongs to the agent.  Every
rule keeps its source identity and time; absence, emptiness, malformed
entries, undersized groups or budget overflow surface as typed gaps and
never block a consultation.

Input binding: one manifest ``generation`` + content ``manifest_sha256``
must be supplied by the caller; the projection never resolves or writes
anything itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_BUDGET_DEFAULT = 10
_MIN_RULES_PER_GROUP_DEFAULT = 1
# 宁缺毋滥：低于该置信度的方法论规则不进入注入投影。
_MIN_RULE_CONFIDENCE = 0.75


@dataclass(frozen=True, slots=True)
class MethodologyRule:
    source_ref: str
    title: str
    rule: str
    teacher_quote: str
    apprentice_interpretation: str
    related_topics: tuple[str, ...]
    confidence: float
    published_at: str
    available_at: str
    generation: str


@dataclass(frozen=True, slots=True)
class MethodologyGroup:
    topic: str
    rules: tuple[MethodologyRule, ...]


@dataclass(frozen=True, slots=True)
class GMethodologyProjectionResult:
    groups: tuple[MethodologyGroup, ...]
    generation: str
    manifest_sha256: str
    data_gaps: tuple[str, ...]
    low_confidence_skipped: int = 0


def _sort_key(value: str) -> str:
    return value or ""


def project_methodology(
    rules: Sequence[Mapping[str, Any]],
    *,
    generation: str,
    manifest_sha256: str,
    budget: int = _BUDGET_DEFAULT,
    min_rules_per_group: int = _MIN_RULES_PER_GROUP_DEFAULT,
) -> GMethodologyProjectionResult:
    """Group bounded methodology rules by exact related_topics labels."""
    if budget <= 0:
        return GMethodologyProjectionResult(
            groups=(),
            generation=generation,
            manifest_sha256=manifest_sha256,
            data_gaps=("g_methodology_budget_truncated",),
        )
    gaps: list[str] = []
    low_confidence_skipped = 0
    validated: list[MethodologyRule] = []
    for raw in rules:
        if not isinstance(raw, Mapping):
            gaps.append("g_methodology_entry_invalid")
            continue
        source_ref = str(raw.get("source_ref") or "")
        title = str(raw.get("title") or "")
        rule_text = str(raw.get("rule") or "")
        published_at = str(raw.get("published_at") or "")
        topics = raw.get("related_topics")
        if (
            not source_ref
            or not title
            or not rule_text
            or not published_at
            or not isinstance(topics, (list, tuple))
        ):
            gaps.append("g_methodology_entry_invalid")
            continue
        confidence = raw.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            confidence_value = float(confidence)
        else:
            confidence_value = 0.0
        rule_obj = MethodologyRule(
            source_ref=source_ref,
            title=title,
            rule=rule_text,
            teacher_quote=str(raw.get("teacher_quote") or ""),
            apprentice_interpretation=str(raw.get("apprentice_interpretation") or ""),
            related_topics=tuple(
                str(topic) for topic in topics if isinstance(topic, str) and topic.strip()
            ),
            confidence=confidence_value,
            published_at=published_at,
            available_at=str(raw.get("available_at") or ""),
            generation=generation,
        )
        if not rule_obj.related_topics:
            gaps.append("g_methodology_entry_invalid")
            continue
        # 宁缺毋滥：低于置信门槛的规则不注入（质量筛选，计数进审计）。
        if rule_obj.confidence < _MIN_RULE_CONFIDENCE:
            low_confidence_skipped += 1
            continue
        validated.append(rule_obj)

    # 同源去重：同 source_ref 且同原话或同规则文本的重复提取只保留高置信
    # 实例——提取侧跨批次无查重，注入侧兜底（quote OR rule 语义）。quote 与
    # rule 键分属独立命名索引（跨类型的同前缀文本不得互相去重）；一条新规则
    # 可同时命中多个 prior（quote 撞 A、rule 撞 B），全部评估后再决定保留。
    by_topic: dict[str, list[MethodologyRule]] = {}
    quote_index: dict[tuple[str, str], MethodologyRule] = {}
    rule_index: dict[tuple[str, str], MethodologyRule] = {}
    final_rules: dict[int, MethodologyRule] = {}
    for rule_obj in validated:
        prior_ids: set[int] = set()
        quote_prior = quote_index.get((rule_obj.source_ref, rule_obj.teacher_quote[:80]))
        if quote_prior is not None:
            prior_ids.add(id(quote_prior))
        rule_prior = rule_index.get((rule_obj.source_ref, rule_obj.rule[:80]))
        if rule_prior is not None:
            prior_ids.add(id(rule_prior))
        priors = [final_rules[rid] for rid in prior_ids if rid in final_rules]
        if any(p.confidence >= rule_obj.confidence for p in priors):
            continue
        for prior in priors:
            quote_index.pop((prior.source_ref, prior.teacher_quote[:80]), None)
            rule_index.pop((prior.source_ref, prior.rule[:80]), None)
            final_rules.pop(id(prior), None)
        quote_index[(rule_obj.source_ref, rule_obj.teacher_quote[:80])] = rule_obj
        rule_index[(rule_obj.source_ref, rule_obj.rule[:80])] = rule_obj
        final_rules[id(rule_obj)] = rule_obj
    for rule_obj in final_rules.values():
        for topic in rule_obj.related_topics:
            by_topic.setdefault(topic, []).append(rule_obj)

    if not by_topic:
        gaps.append("g_methodology_no_samples")
        return GMethodologyProjectionResult(
            groups=(),
            generation=generation,
            manifest_sha256=manifest_sha256,
            data_gaps=tuple(dict.fromkeys(gaps)),
            low_confidence_skipped=low_confidence_skipped,
        )

    ordered_groups: list[MethodologyGroup] = []
    remaining = budget
    for topic in sorted(by_topic, key=_sort_key):
        if remaining <= 0:
            gaps.append("g_methodology_budget_truncated")
            continue
        group_rules = sorted(
            by_topic[topic],
            key=lambda r: (r.published_at, r.source_ref),
        )
        if len(group_rules) < min_rules_per_group:
            gaps.append("g_methodology_group_insufficient_samples")
            continue
        if len(group_rules) > remaining:
            gaps.append("g_methodology_budget_truncated")
            group_rules = group_rules[:remaining]
        if not group_rules:
            continue
        ordered_groups.append(MethodologyGroup(topic=topic, rules=tuple(group_rules)))
        remaining -= len(group_rules)

    return GMethodologyProjectionResult(
        groups=tuple(ordered_groups),
        generation=generation,
        manifest_sha256=manifest_sha256,
        data_gaps=tuple(dict.fromkeys(gaps)),
        low_confidence_skipped=low_confidence_skipped,
    )
