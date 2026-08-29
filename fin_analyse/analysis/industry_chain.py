"""LLM-driven industry chain position analysis.

Does NOT use a static knowledge graph. Instead, queries the knowledge base
for relevant articles and uses an LLM to reason about a company's position
in its industry chain, strategic importance, substitution risk, and failure
conditions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from fin_analyse.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)


class _CompletingBackend(Protocol):
    def complete(self, prompt: str) -> str: ...


# Data gaps that map to "unavailable" (no independent capacity to answer)
# rather than "error" (a call/parse actually failed).
_UNAVAILABLE_GAPS = frozenset(
    {
        "industry_chain_llm_unavailable",
        "industry_chain_consensus_backends_unavailable",
        "industry_chain_aggregator_unavailable",
    }
)


@dataclass
class IndustryChainResult:
    company: str
    ticker: str = ""
    industry: str = ""
    chain_segment: str = ""  # 上游/中游/下游/一体化/平台
    role: str = ""  # 具体角色描述
    key_products: list[str] = field(default_factory=list)
    key_customers: list[str] = field(default_factory=list)
    key_suppliers: list[str] = field(default_factory=list)
    strategic_importance: float = 0.0  # 0-10
    substitution_difficulty: str = "未知"  # 高/中/低
    bargaining_power: str = "未知"  # 强/中/弱
    moat_summary: str = ""
    confidence: float = 0.0
    evidence_sources: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)
    methodology_note: str = ""
    raw_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return dict with boundary fields for source/cognition/risk contract."""
        has_result = bool(self.industry or self.chain_segment or self.role)
        has_unavailable = any(g in _UNAVAILABLE_GAPS for g in self.data_gaps)
        has_error = bool(self.raw_response or (
            not has_result and self.data_gaps
        ))
        if has_unavailable and not has_result:
            status = "unavailable"
        elif has_error and not has_result:
            status = "error"
        elif has_result:
            status = "ok"
        else:
            status = "unavailable"

        return {
            "company": self.company,
            "ticker": self.ticker,
            "industry": self.industry,
            "chain_segment": self.chain_segment,
            "role": self.role,
            "key_products": self.key_products,
            "key_customers": self.key_customers,
            "key_suppliers": self.key_suppliers,
            "strategic_importance": self.strategic_importance,
            "substitution_difficulty": self.substitution_difficulty,
            "bargaining_power": self.bargaining_power,
            "moat_summary": self.moat_summary,
            "confidence": self.confidence,
            "evidence_sources": self.evidence_sources,
            "data_gaps": self.data_gaps,
            "failure_conditions": self.failure_conditions,
            "catalysts": self.catalysts,
            "methodology_note": self.methodology_note,
            "status": status,
            "writes_cognition": False,
            "affects_confidence": False,
            "trading_decision": False,
            "advisory_only": True,
            "execution_allowed": False,
            "source_boundary": {
                "analysis_synthesis": True,
                "writes_cognition": False,
            },
        }


INDUSTRY_CHAIN_PROMPT = """你是一个 A 股产业链分析专家。请分析以下公司在产业链中的位置。

## 公司信息
- 公司名称：{company}
- 股票代码：{ticker}

## 知识库上下文（来自星大派/郭老师文章）
{knowledge_context}

## 分析要求

请基于知识库上下文和你对 A 股产业链的了解，输出 JSON 格式的分析结果。

**核心原则：**
1. 优先使用知识库中老师明确提到的产业链信息
2. 知识库无覆盖时，基于公司名称和公开行业知识进行方法论迁移推断，降低置信度
3. 明确区分「有证据的结论」和「推断的结论」
4. 不做静态知识图谱——每次都动态推理

**输出 JSON 格式（严格按此结构）：**
```json
{{
  "industry": "所属行业（如：半导体材料、稀土永磁、新能源汽车）",
  "chain_segment": "上游原材料/中游制造/下游应用/中游零部件/一体化/平台型/技术服务",
  "role": "公司在产业链中的具体角色（1-3句话）",
  "key_products": ["核心产品1", "核心产品2"],
  "key_customers": ["下游客户类型或具体公司"],
  "key_suppliers": ["上游供应商类型或具体原料"],
  "strategic_importance": 7.5,
  "strategic_importance_reason": "战略性重要度评分理由（0-10，10=卡脖子/不可替代）",
  "substitution_difficulty": "高/中/低",
  "substitution_reason": "替代难度理由",
  "bargaining_power": "强/中/弱",
  "bargaining_power_reason": "议价能力理由",
  "moat_summary": "护城河一句话总结",
  "confidence": 0.7,
  "confidence_reason": "置信度理由（特别标注：哪些是证据、哪些是推断）",
  "evidence_sources": ["证据来源1", "证据来源2"],
  "data_gaps": ["数据缺口1"],
  "failure_conditions": ["失效条件1（什么情况下此分析不再成立）"],
  "catalysts": ["催化因素1（什么事件会提升此公司的产业链价值）"],
  "methodology_note": "分析方法论说明"
}}
```

请只输出 JSON，不要加任何额外文字。"""


INDUSTRY_CHAIN_AGGREGATE_PROMPT = """两个独立 LLM 对同一公司的产业链位置给出了分歧结论。\
请基于原始分析要求和两方结论，输出一个协调后的 JSON（严格遵循原结构，解决矛盾、\
取更具体且有依据的一方，必要时合并双方独有的洞见）。

## 原始分析要求
{base_prompt}

## LLM-A 结论
{ref_a}

## LLM-B 结论
{ref_b}

请只输出协调后的 JSON，不要加任何额外文字。"""


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Extract JSON from LLM response that may have markdown fences or extra text."""
    text = raw.strip()
    # Try to extract from ```json fences
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()
    # Try to find first { and last }
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        text = text[brace_start : brace_end + 1]
    data = json.loads(text)
    return dict(data) if isinstance(data, dict) else {}


class IndustryChainAnalyzer:
    """LLM-driven industry chain position analyzer.

    Queries the knowledge base for company context, then uses an LLM
    to reason about the company's position in its industry chain.
    """

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def analyze(
        self,
        company: str,
        ticker: str = "",
        *,
        backend: _CompletingBackend | None = None,
        reference_backends: Sequence[_CompletingBackend] | None = None,
        aggregator_backend: _CompletingBackend | None = None,
    ) -> IndustryChainResult:
        """Analyze the industry chain position of a company.

        Strict quality contract:
        - Fewer than two independent reference backends → not enough for a
          runtime ``ok`` result (no single-LLM fallback).
        - Two references that agree → merge (aggregator not called).
        - Two references that diverge → an INDEPENDENT aggregator backend must
          reconcile them; without one, expose a stable data gap rather than
          reusing a reference as aggregator.

        Args:
            company: Company name.
            ticker: Optional stock ticker for enriched analysis.
            backend: Backward-compatible single backend. Treated as a single
                reference — insufficient for a strict ``ok`` result.
            reference_backends: Two independent reference backends.
            aggregator_backend: Independent aggregator used only on divergence.

        Returns:
            Structured IndustryChainResult.
        """
        # 1. Gather knowledge base context and build the base prompt
        kb_articles = self._gather_kb_context(company)
        prompt = INDUSTRY_CHAIN_PROMPT.format(
            company=company,
            ticker=ticker or "未知",
            knowledge_context=kb_articles or "（知识库中暂无该公司相关文章）",
        )

        # 2. Resolve reference backends + optional aggregator
        refs, aggregator = self._resolve_backends(
            backend=backend,
            reference_backends=reference_backends,
            aggregator_backend=aggregator_backend,
        )

        # 3. No backend at all → unavailable, no fallback
        if not refs:
            return IndustryChainResult(
                company=company,
                ticker=ticker,
                methodology_note="LLM 配置不可用，无法完成产业链分析",
                data_gaps=["industry_chain_llm_unavailable"],
            )

        # 4. Fewer than two independent references → insufficient for strict output
        if len(refs) < 2:
            return IndustryChainResult(
                company=company,
                ticker=ticker,
                methodology_note="单一 LLM 参考不足以产出严格产业链结论，至少需要两路独立参考",
                data_gaps=["industry_chain_consensus_backends_unavailable"],
            )

        # 5. Call the two references independently
        ref_data: list[dict[str, Any]] = []
        for ref in refs[:2]:
            data, gap, raw = self._call_and_parse(
                ref,
                prompt,
                call_gap="industry_chain_llm_call_failed",
                parse_gap="industry_chain_llm_parse_failed",
            )
            if data is None:
                return IndustryChainResult(
                    company=company,
                    ticker=ticker,
                    methodology_note=f"参考 LLM 未产出可用结果: {gap}",
                    data_gaps=[gap],
                    raw_response=raw,
                )
            ref_data.append(data)

        data_a, data_b = ref_data[0], ref_data[1]

        # 6. Matching references → merge (no aggregator call)
        if self._references_match(data_a, data_b):
            return self._build_result(
                company, ticker, self._merge_references(data_a, data_b)
            )

        # 7. Divergent references → require an INDEPENDENT aggregator
        if aggregator is None:
            return IndustryChainResult(
                company=company,
                ticker=ticker,
                methodology_note="两路参考结论分歧，且无独立聚合 LLM，拒绝复用参考作为聚合器",
                data_gaps=["industry_chain_aggregator_unavailable"],
            )

        aggregate_prompt = INDUSTRY_CHAIN_AGGREGATE_PROMPT.format(
            base_prompt=prompt,
            ref_a=json.dumps(data_a, ensure_ascii=False),
            ref_b=json.dumps(data_b, ensure_ascii=False),
        )
        agg_data, agg_gap, agg_raw = self._call_and_parse(
            aggregator,
            aggregate_prompt,
            call_gap="industry_chain_aggregation_failed",
            parse_gap="industry_chain_aggregation_parse_failed",
        )
        if agg_data is None:
            return IndustryChainResult(
                company=company,
                ticker=ticker,
                methodology_note=f"聚合 LLM 未产出可用结果: {agg_gap}",
                data_gaps=[agg_gap],
                raw_response=agg_raw,
            )
        return self._build_result(company, ticker, agg_data)

    @staticmethod
    def _call_and_parse(
        backend: _CompletingBackend,
        prompt: str,
        *,
        call_gap: str,
        parse_gap: str,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """Call a backend and parse its JSON.

        Returns ``(data, gap, raw)``. On success ``data`` is a dict and
        ``gap`` is empty. On failure ``data`` is None with a stable ``gap`` id.
        """
        try:
            raw = backend.complete(prompt)
        except Exception as exc:
            logger.warning("LLM call failed for industry chain analysis: %s", exc)
            return None, call_gap, ""
        try:
            data = _parse_llm_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse LLM JSON for industry chain: %s", exc)
            return None, parse_gap, raw[:500]
        return data, "", ""

    @staticmethod
    def _references_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Conservative match: same normalized industry, chain_segment, role."""

        def _norm(value: object) -> str:
            return str(value or "").strip().lower()

        return (
            _norm(a.get("industry")) == _norm(b.get("industry"))
            and _norm(a.get("chain_segment")) == _norm(b.get("chain_segment"))
            and _norm(a.get("role")) == _norm(b.get("role"))
        )

    @staticmethod
    def _merge_references(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        """When references agree, keep the higher-confidence structured result."""
        conf_a = _as_float(a.get("confidence"), 0.5)
        conf_b = _as_float(b.get("confidence"), 0.5)
        return a if conf_a >= conf_b else b

    def _build_result(
        self, company: str, ticker: str, data: dict[str, Any]
    ) -> IndustryChainResult:
        """Build a structured result from a validated JSON dict."""
        confidence = max(0.0, min(1.0, _as_float(data.get("confidence"), 0.5)))
        strategic_importance = max(
            0.0, min(10.0, _as_float(data.get("strategic_importance"), 5.0))
        )
        return IndustryChainResult(
            company=company,
            ticker=ticker,
            industry=str(data.get("industry", "")),
            chain_segment=str(data.get("chain_segment", "")),
            role=str(data.get("role", "")),
            key_products=_as_str_list(data.get("key_products", [])),
            key_customers=_as_str_list(data.get("key_customers", [])),
            key_suppliers=_as_str_list(data.get("key_suppliers", [])),
            strategic_importance=strategic_importance,
            substitution_difficulty=str(data.get("substitution_difficulty", "未知")),
            bargaining_power=str(data.get("bargaining_power", "未知")),
            moat_summary=str(data.get("moat_summary", "")),
            confidence=confidence,
            evidence_sources=_as_str_list(data.get("evidence_sources", [])),
            data_gaps=_as_str_list(data.get("data_gaps", [])),
            failure_conditions=_as_str_list(data.get("failure_conditions", [])),
            catalysts=_as_str_list(data.get("catalysts", [])),
            methodology_note=str(data.get("methodology_note", "")),
        )

    def _resolve_backends(
        self,
        *,
        backend: _CompletingBackend | None,
        reference_backends: Sequence[_CompletingBackend] | None,
        aggregator_backend: _CompletingBackend | None,
    ) -> tuple[list[_CompletingBackend], _CompletingBackend | None]:
        """Resolve reference backends and an optional aggregator.

        Precedence: explicit ``reference_backends`` > backward-compatible
        single ``backend`` > config-resolved backends (first two as references,
        a third — if present — as aggregator).
        """
        if reference_backends is not None:
            return list(reference_backends), aggregator_backend
        if backend is not None:
            return [backend], aggregator_backend
        resolved = self._get_default_backends()
        if not resolved:
            return [], aggregator_backend
        refs = resolved[:2]
        aggregator = aggregator_backend
        if aggregator is None and len(resolved) >= 3:
            aggregator = resolved[2]
        return refs, aggregator

    def _gather_kb_context(self, company: str) -> str:
        """Search knowledge base for articles mentioning this company."""
        docs = self.store.documents
        matched: list[str] = []
        for doc in docs:
            if company in doc.title or company in doc.content:
                date = doc.metadata.get("date", "")
                column = doc.metadata.get("column", "")
                score = doc.metadata.get("score", "")
                title = doc.title[:120]
                # Take relevant content snippet (up to 800 chars)
                content = doc.content
                idx = content.find(company)
                if idx >= 0:
                    start = max(0, idx - 100)
                    end = min(len(content), idx + 700)
                    snippet = content[start:end]
                else:
                    snippet = content[:800]
                score_str = f"评分{score}" if score else "无评分"
                matched.append(
                    f"【{date} | {column} | {score_str}】{title}\n  片段: ...{snippet}..."
                )
                if len(matched) >= 5:
                    break

        if not matched:
            return ""

        return "\n\n".join(matched)

    @staticmethod
    def _get_default_backends() -> list[_CompletingBackend]:
        """Return configured backends in deterministic capability order.

        No live fallback backend is created — an empty list means the caller
        must handle unavailability. Order comes from llm.yaml `priorities.t0`
        (家规规则 6), falling back to glm53 > DeepSeek > Qwen, followed
        by any remaining backends in name order.
        """
        try:
            from fin_analyse.claims.config_loader import (
                configured_backend_order,
                create_backends_from_config,
            )

            backends = create_backends_from_config()
        except Exception as exc:
            logger.warning("Could not load LLM config: %s", exc)
            return []

        ordered: list[_CompletingBackend] = []
        seen: set[str] = set()
        for name in configured_backend_order("t0", ("glm53", "deepseek", "qwen")):
            if name in backends:
                ordered.append(cast(_CompletingBackend, backends[name]))
                seen.add(name)
        for name in sorted(backends):
            if name not in seen:
                ordered.append(cast(_CompletingBackend, backends[name]))
        return ordered

    @staticmethod
    def _get_default_backend() -> _CompletingBackend | None:
        """Get a single default LLM backend from config.

        Returns None when no backend is configured — the caller must
        handle unavailability rather than silently creating a fallback.
        """
        backends = IndustryChainAnalyzer._get_default_backends()
        return backends[0] if backends else None


def _as_float(val: object, default: float) -> float:
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_str_list(val: object) -> list[str]:
    if isinstance(val, list):
        return [str(item) for item in val]
    return []
