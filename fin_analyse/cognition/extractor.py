"""Reasoning trace extraction for teacher-original evidence."""

from __future__ import annotations

import logging
from hashlib import sha1
from uuid import uuid4

from fin_analyse.cognition.models import EvidenceItem, ReasoningTrace

logger = logging.getLogger(__name__)


class UnavailableReasoningExtractor:
    """Extractor that refuses to produce traces when LLM is unavailable.

    Used as the runtime default when no LLM helper is configured.
    Does NOT fall back to rule-based extraction — the strict contract
    requires LLM availability for any ReasoningTrace write.

    Attributes:
        last_extraction_failed: Always True (LLM unavailable).
        last_data_gaps: Tuple of data gap identifiers.
    """

    def __init__(self, data_gaps: tuple[str, ...] = ("reasoning_trace_llm_unavailable",)) -> None:
        self.last_extraction_failed: bool = True
        self.last_data_gaps: tuple[str, ...] = data_gaps

    def extract(self, evidence: object) -> list:
        """Always returns empty — no trace without LLM consensus."""
        return []


class RuleBasedReasoningExtractor:
    """Conservative fallback extractor used for tests and no-LLM mode."""

    VARIABLE_MARKERS = (
        "利润分配",
        "订单",
        "利润率",
        "价格",
        "成交量",
        "政策",
    )

    def extract(self, evidence: EvidenceItem) -> list[ReasoningTrace]:
        if evidence.source_label.label != "teacher_original":
            return []
        variables = [m for m in self.VARIABLE_MARKERS if m in evidence.content]
        if not variables:
            return []

        trace_id = "trace-" + sha1(evidence.evidence_id.encode("utf-8")).hexdigest()[:12]
        conclusion = "关注但不追高" if "追高" in evidence.content else "保持观察"
        return [
            ReasoningTrace(
                trace_id=trace_id,
                teacher_id=evidence.source_label.teacher_id or "unknown",
                source_evidence_id=evidence.evidence_id,
                topic=evidence.topics[0] if evidence.topics else "unknown",
                companies=evidence.companies,
                premises=[evidence.title],
                observed_variables=variables,
                inferred_relationships=["政策影响需要通过订单、价格或利润率兑现"],
                conclusion=conclusion,
                stance="watch",
                time_horizon="mid",
                risk_boundaries=["标的已经启动后追高风险上升"],
                invalidation_conditions=["政策无法改变利润分配或基本面兑现"],
                action_implications=["等待事实验证而不是只跟随情绪"],
                extraction_confidence=min(0.85, evidence.source_label.confidence),
            )
        ]


class LLMReasoningExtractor:
    """Extract ReasoningTrace items from teacher-original evidence using an LLM.

    Only processes evidence whose source_label is ``teacher_original``.
    System-assigns every trace_id; never trusts LLM-generated IDs.

    Attributes:
        last_extraction_failed: True when the LLM was called but returned no usable
            traces (call failed, empty response, or all items invalid).
            False when extraction was skipped (non-teacher_original) or succeeded.
            Reset on each extract() call.
    """

    _EXTRACT_PROMPT = (
        "Extract investment reasoning from this article as a JSON array of trace objects. "
        "ALWAYS return at least one trace — never return an empty array. "
        "If reasoning is incomplete or uncertain, still extract what you can and use low extraction_confidence.\n"
        "Each object must have these keys:\n"
        "- topic: string (one-line investment topic — infer from context if not explicit)\n"
        "- companies: list of strings (mentioned companies, empty if none)\n"
        "- premises: list of strings (background assumptions, even if only implied)\n"
        "- observed_variables: list of strings (key metrics, events, or data points mentioned)\n"
        "- inferred_relationships: list of strings (causal chain: A → B → C)\n"
        "- conclusion: string (the investment conclusion or recommendation)\n"
        "- stance: one of bull/bear/watch/unknown\n"
        "- time_horizon: one of short/mid/long/unknown\n"
        "- risk_boundaries: list of strings (what could go wrong)\n"
        "- invalidation_conditions: list of strings (what would prove this wrong)\n"
        "- action_implications: list of strings (what to do about it)\n"
        "- extraction_confidence: float 0-1. Use 0.3-0.5 for short/partial content, 0.7+ for clear reasoning.\n\n"
        "For Q&A format: extract from the teacher's perspective implied by the question.\n"
        "Title: {title}\nAuthor: {author}\n\n{content}"
    )

    def __init__(self, llm) -> None:
        self.llm = llm
        self.last_extraction_failed: bool = False

    def extract(self, evidence: EvidenceItem) -> list[ReasoningTrace]:
        self.last_extraction_failed = False

        if evidence.source_label.label != "teacher_original":
            return []

        prompt = self._EXTRACT_PROMPT.format(
            title=evidence.title,
            author=evidence.author or "unknown",
            content=evidence.content[:4000],
        )
        result = self.llm.complete_json(prompt, expected_type="reasoning_traces")
        if not result.ok or not isinstance(result.data, list):
            logger.warning("LLM reasoning extraction failed: %s", result.error)
            self.last_extraction_failed = True
            return []

        traces: list[ReasoningTrace] = []
        teacher_id = evidence.source_label.teacher_id or "unknown"
        max_conf = evidence.source_label.confidence

        for item in result.data:
            if not isinstance(item, dict):
                continue
            try:
                trace = ReasoningTrace(
                    trace_id="trace-" + uuid4().hex[:12],
                    teacher_id=teacher_id,
                    source_evidence_id=evidence.evidence_id,
                    topic=str(item.get("topic", "unknown")),
                    companies=_list_str(item.get("companies", [])),
                    premises=_list_str(item.get("premises", [])),
                    observed_variables=_list_str(item.get("observed_variables", [])),
                    inferred_relationships=_list_str(item.get("inferred_relationships", [])),
                    conclusion=str(item.get("conclusion", "")),
                    stance=_validate_stance(item.get("stance", "watch")),
                    time_horizon=_validate_horizon(item.get("time_horizon", "mid")),
                    risk_boundaries=_list_str(item.get("risk_boundaries", [])),
                    invalidation_conditions=_list_str(item.get("invalidation_conditions", [])),
                    action_implications=_list_str(item.get("action_implications", [])),
                    extraction_confidence=min(
                        max(float(item.get("extraction_confidence", 0.5)), 0.0),
                        max_conf,
                    ),
                )
                traces.append(trace)
            except Exception as exc:
                logger.warning("Skipping invalid trace item: %s", exc)

        if not traces:
            self.last_extraction_failed = True

        return traces


class ConsensusReasoningExtractor:
    """Strict dual-LLM consensus extraction for teacher_original evidence.

    两个 LLM 独立提取 → 两方必须都成功。
    - 一方失败 → 不写 trace，标记 last_extraction_failed。
    - 两方都成功、一致 → 合并高置信结果，写 trace。
    - 两方都成功、不一致 → 第三 LLM 聚合；聚合成功才写 trace。
    - 聚合失败 → 不写 trace，标记 last_extraction_failed。
    - 不提供规则降级路径。

    Attributes:
        last_extraction_failed: True when extraction cannot produce a valid result.
        last_data_gaps: Tuple of data gap identifiers when extraction fails.
    """

    _AGGREGATE_PROMPT = (
        "Two LLMs independently extracted reasoning traces from the same article "
        "and produced different results. Reconcile them into a SINGLE JSON array "
        "of trace objects. Resolve contradictions, deduplicate overlapping traces, "
        "and keep unique insights from both. Prefer the more specific version when "
        "they conflict.\n\n"
        "Article: {title} | Author: {author}\n{content}\n\n"
        "=== LLM-A traces ===\n{traces_a}\n\n"
        "=== LLM-B traces ===\n{traces_b}\n\n"
        "Each trace object must have: topic, companies, premises, "
        "observed_variables, inferred_relationships, conclusion, stance "
        "(bull/bear/watch/unknown), time_horizon (short/mid/long/unknown), "
        "risk_boundaries, invalidation_conditions, action_implications, "
        "extraction_confidence (0-1). Return ONLY the JSON array."
    )

    def __init__(
        self,
        primary_llm,  # CognitionLLM
        secondary_llm,  # CognitionLLM
        aggregator_llm=None,  # CognitionLLM | None — MUST be independent of references
    ) -> None:
        from fin_analyse.cognition.extractor import LLMReasoningExtractor

        self._primary = LLMReasoningExtractor(primary_llm)
        self._secondary = LLMReasoningExtractor(secondary_llm)
        self._aggregator_llm = aggregator_llm
        self.last_extraction_failed: bool = False
        self.last_data_gaps: tuple[str, ...] = ()

    def extract(self, evidence: EvidenceItem) -> list[ReasoningTrace]:
        self.last_extraction_failed = False
        self.last_data_gaps = ()

        if evidence.source_label.label != "teacher_original":
            return []

        # ── 两个 LLM 独立提取 ──
        traces_a = self._primary.extract(evidence)
        traces_b = self._secondary.extract(evidence)

        a_failed = self._primary.last_extraction_failed
        b_failed = self._secondary.last_extraction_failed

        # Both failed
        if a_failed and b_failed:
            self.last_extraction_failed = True
            self.last_data_gaps = ("reasoning_trace_consensus_both_failed",)
            logger.warning("Consensus: both LLMs failed for %s", evidence.evidence_id)
            return []

        # Strict: single failure → no trace, mark as failed
        if a_failed:
            self.last_extraction_failed = True
            self.last_data_gaps = ("reasoning_trace_consensus_primary_failed",)
            logger.warning(
                "Consensus: primary failed for %s — refusing single-sided write",
                evidence.evidence_id,
            )
            return []
        if b_failed:
            self.last_extraction_failed = True
            self.last_data_gaps = ("reasoning_trace_consensus_secondary_failed",)
            logger.warning(
                "Consensus: secondary failed for %s — refusing single-sided write",
                evidence.evidence_id,
            )
            return []

        # Strict: one side empty and the other non-empty → inconsistent, aggregator needed
        if not traces_a and not traces_b:
            return []
        if not traces_a or not traces_b:
            logger.info(
                "Consensus: one side empty for %s — treating as divergent",
                evidence.evidence_id,
            )
            # Fall through to aggregation below

        # ── 一致性判断 ──
        if traces_a and traces_b and self._is_consistent(traces_a, traces_b):
            logger.info("Consensus: traces consistent for %s, merging", evidence.evidence_id)
            return self._merge_best(traces_a, traces_b)

        # ── 不一致 → 第三个 LLM 聚合 ──
        logger.info(
            "Consensus: traces diverge for %s (%d vs %d), aggregating...",
            evidence.evidence_id,
            len(traces_a),
            len(traces_b),
        )
        return self._aggregate(evidence, traces_a, traces_b)

    # ── private ──────────────────────────────────────────────

    @staticmethod
    def _is_consistent(traces_a: list[ReasoningTrace], traces_b: list[ReasoningTrace]) -> bool:
        """Two extractions are consistent if their core theses align.

        Checks: same number of traces, matching topics and stances.
        """
        if len(traces_a) != len(traces_b):
            return False

        topics_a = sorted(t.topic for t in traces_a)
        topics_b = sorted(t.topic for t in traces_b)
        if topics_a != topics_b:
            return False

        stances_a = sorted(t.stance for t in traces_a)
        stances_b = sorted(t.stance for t in traces_b)
        return stances_a == stances_b

    @staticmethod
    def _merge_best(
        traces_a: list[ReasoningTrace], traces_b: list[ReasoningTrace]
    ) -> list[ReasoningTrace]:
        """When consistent, keep the higher-confidence version of each trace."""
        merged: list[ReasoningTrace] = []
        seen_topics: set[str] = set()

        # Pair traces by topic (both lists have same topics in sorted order)
        a_by_topic = {t.topic: t for t in traces_a}
        b_by_topic = {t.topic: t for t in traces_b}

        for topic in sorted({t.topic for t in traces_a + traces_b}):
            if topic in seen_topics:
                continue
            seen_topics.add(topic)

            t_a = a_by_topic.get(topic)
            t_b = b_by_topic.get(topic)

            if t_a is None:
                assert t_b is not None
                merged.append(t_b)
            elif t_b is None:
                merged.append(t_a)
            else:
                # Keep the one with higher extraction_confidence
                merged.append(
                    t_a if t_a.extraction_confidence >= t_b.extraction_confidence else t_b
                )

        return merged

    def _aggregate(
        self,
        evidence: EvidenceItem,
        traces_a: list[ReasoningTrace],
        traces_b: list[ReasoningTrace],
    ) -> list[ReasoningTrace]:
        """Use a 3rd LLM to reconcile divergent extractions."""
        import json as _json
        from uuid import uuid4

        # Strict contract: divergence requires an INDEPENDENT third backend.
        # Never reuse a reference LLM as aggregator. If none is available,
        # refuse to write and expose a stable data gap.
        if self._aggregator_llm is None:
            self.last_extraction_failed = True
            self.last_data_gaps = ("reasoning_trace_consensus_aggregator_unavailable",)
            logger.warning(
                "Consensus: divergent references for %s but no aggregator backend "
                "— refusing to reuse a reference as aggregator",
                evidence.evidence_id,
            )
            return []

        # Render traces as JSON for the prompt
        a_json = _json.dumps(
            [
                {
                    "topic": t.topic,
                    "companies": t.companies,
                    "stance": t.stance,
                    "conclusion": t.conclusion,
                    "time_horizon": t.time_horizon,
                    "extraction_confidence": t.extraction_confidence,
                }
                for t in traces_a
            ],
            ensure_ascii=False,
        )
        b_json = _json.dumps(
            [
                {
                    "topic": t.topic,
                    "companies": t.companies,
                    "stance": t.stance,
                    "conclusion": t.conclusion,
                    "time_horizon": t.time_horizon,
                    "extraction_confidence": t.extraction_confidence,
                }
                for t in traces_b
            ],
            ensure_ascii=False,
        )

        prompt = self._AGGREGATE_PROMPT.format(
            title=evidence.title,
            author=evidence.author or "unknown",
            content=evidence.content[:3000],
            traces_a=a_json,
            traces_b=b_json,
        )

        result = self._aggregator_llm.complete_json(prompt, expected_type="reasoning_traces")
        if not result.ok or not isinstance(result.data, list):
            logger.warning(
                "Consensus aggregator failed for %s: %s",
                evidence.evidence_id,
                result.error,
            )
            self.last_extraction_failed = True
            self.last_data_gaps = ("reasoning_trace_consensus_aggregator_failed",)
            return []

        # Parse aggregated traces (reuse LLMReasoningExtractor's parsing logic)
        traces: list[ReasoningTrace] = []
        teacher_id = evidence.source_label.teacher_id or "unknown"
        max_conf = evidence.source_label.confidence

        for item in result.data:
            if not isinstance(item, dict):
                continue
            try:
                trace = ReasoningTrace(
                    trace_id="trace-" + uuid4().hex[:12],
                    teacher_id=teacher_id,
                    source_evidence_id=evidence.evidence_id,
                    topic=str(item.get("topic", "unknown")),
                    companies=_list_str(item.get("companies", [])),
                    premises=_list_str(item.get("premises", [])),
                    observed_variables=_list_str(item.get("observed_variables", [])),
                    inferred_relationships=_list_str(item.get("inferred_relationships", [])),
                    conclusion=str(item.get("conclusion", "")),
                    stance=_validate_stance(item.get("stance", "watch")),
                    time_horizon=_validate_horizon(item.get("time_horizon", "mid")),
                    risk_boundaries=_list_str(item.get("risk_boundaries", [])),
                    invalidation_conditions=_list_str(item.get("invalidation_conditions", [])),
                    action_implications=_list_str(item.get("action_implications", [])),
                    extraction_confidence=min(
                        max(float(item.get("extraction_confidence", 0.5)), 0.0),
                        max_conf,
                    ),
                )
                traces.append(trace)
            except Exception as exc:
                logger.warning("Consensus aggregator: skipping invalid item: %s", exc)

        if not traces:
            self.last_extraction_failed = True
            self.last_data_gaps = ("reasoning_trace_consensus_aggregator_empty",)

        return traces


class HybridReasoningExtractor:
    """LLM-first reasoning extractor. LLM 失败时不回退规则引擎。

    LLM 提取成功 → 返回 ReasoningTrace 列表。
    LLM 被调用但失败（调用异常 / 空响应 / 无可解析结果）→ 返回空列表，
    由上游 backfill runner 记录为 llm_extraction_failed，供每日简报引用。
    非 teacher_original 证据 → 跳过，不视为失败。
    """

    def __init__(
        self,
        llm_extractor: LLMReasoningExtractor | ConsensusReasoningExtractor,
        rule_extractor: RuleBasedReasoningExtractor,
    ) -> None:
        self.llm = llm_extractor
        self.rule = rule_extractor

    @property
    def last_extraction_failed(self) -> bool:
        return bool(getattr(self.llm, "last_extraction_failed", False))

    @property
    def last_data_gaps(self) -> tuple[str, ...]:
        return tuple(getattr(self.llm, "last_data_gaps", ()))

    def extract(self, evidence: EvidenceItem) -> list[ReasoningTrace]:
        traces = self.llm.extract(evidence)
        if traces:
            return traces
        # LLM 被调用但失败了 → 不回退规则，让上游感知失败
        if self.llm.last_extraction_failed:
            return []
        # LLM 未被调用（非 teacher_original）→ 也不回退规则
        return []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_VALID_STANCES = frozenset({"bull", "bear", "watch", "unknown"})
_VALID_HORIZONS = frozenset({"short", "mid", "long", "unknown"})


def _list_str(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _validate_stance(raw: object) -> str:
    s = str(raw).strip().lower()
    return s if s in _VALID_STANCES else "watch"


def _validate_horizon(raw: object) -> str:
    h = str(raw).strip().lower()
    return h if h in _VALID_HORIZONS else "mid"
