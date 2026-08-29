"""Minimal cognitive pattern mining."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from fin_analyse.cognition.models import CognitivePattern, ReasoningTrace


class SimplePatternMiner:
    def mine(self, traces: list[ReasoningTrace]) -> list[CognitivePattern]:
        by_teacher_topic: dict[tuple[str, str], list[ReasoningTrace]] = defaultdict(list)
        for trace in traces:
            by_teacher_topic[(trace.teacher_id, trace.topic)].append(trace)

        patterns: list[CognitivePattern] = []
        for (teacher_id, topic), grouped in by_teacher_topic.items():
            variables = sorted({var for trace in grouped for var in trace.observed_variables})
            trace_ids = [trace.trace_id for trace in grouped]
            patterns.append(
                CognitivePattern(
                    pattern_id=f"pattern-{teacher_id}-{topic}",
                    teacher_id=teacher_id,
                    name=f"{topic}判断框架",
                    description=(
                        f"围绕{topic}主题，优先观察"
                        f"{'、'.join(variables) if variables else '关键变量'}等变量是否兑现。"
                    ),
                    trigger_conditions=[f"问题涉及{topic}"],
                    typical_variables=variables,
                    typical_reasoning_shape=("先看事实变量是否改变，再判断是否值得行动。"),
                    supporting_trace_ids=trace_ids,
                    counterexamples=[],
                    confidence=min(0.9, 0.5 + len(grouped) * 0.1),
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
        return patterns
