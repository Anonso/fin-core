"""Model policy for cross_article phases.

Resolves T0/T1 backends with a hard-coded fallback chain:
  Phase 1: T0-first → T1 JSON-capable → text-similarity fallback
  Phase 2: T0-first → T1 → rule-only degraded
  Phase 3: T0 aggregator + T0/T1 capability slots (managed by MoA engine)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fin_analyse.cognition.cross_article.models import ModelPolicy

logger = logging.getLogger(__name__)

# ── Phase 1 fingerprint prompt ──────────────────────────────────────────────

_FINGERPRINT_PROMPT = """你是一个文章主题指纹提取器。给定一篇文章，输出一个 JSON object，字段如下：

{{
  "core_topic": "核心主题（10字以内）",
  "sectors": ["相关板块1", "相关板块2"],
  "mentioned_companies": [{{"name": "公司名", "reference_type": "direct_mention", "context": "简短上下文"}}],
  "viewpoint_type": "新观点 / 强化 / 修正 / 否定",
  "key_claims": ["核心观点1", "核心观点2"],
  "half_life_category": "short / medium / long",
  "cluster_hint": {{
    "relation_to_existing": "属于已有 cluster / 新建 cluster / 不确定",
    "target_cluster_id": "cluster_id or null",
    "reason": "简要理由"
  }}
}}

已有 clusters（供参考）：
{existing_clusters}

只输出 JSON，不要加额外文字。"""

# ── Text fallback prompt ────────────────────────────────────────────────────

_TEXT_FALLBACK_PROMPT = (
    """从以下文章标题或摘要中提取最多 3 个主题关键词（逗号分隔），只输出关键词："""
)


class CrossArticleModelPolicy:
    """Resolves backends and provides per-phase LLM call wrappers.

    In production, t0/t1 backends are injected from create_backends_from_config().
    For testing, fake backends with a .complete(prompt) -> str interface work.
    """

    def __init__(
        self,
        t0_backend: Any | None = None,
        t1_backend: Any | None = None,
    ) -> None:
        self._t0 = t0_backend
        self._t1 = t1_backend

    @property
    def t0(self) -> Any | None:
        return self._t0

    @property
    def t1(self) -> Any | None:
        return self._t1

    def to_model_policy(self) -> ModelPolicy:
        return ModelPolicy(
            t0_backend=self._t0,
            t1_backend=self._t1,
            t0_name=getattr(self._t0, "model", "t0"),
            t1_name=getattr(self._t1, "model", "t1"),
        )

    # ── Phase 1 fingerprint ─────────────────────────────────────────────

    def extract_phase1_fingerprint(
        self,
        content: str,
        *,
        existing_clusters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Extract article fingerprint for clustering.

        Fallback chain: T0 → T1 → text-similarity.
        """
        clusters = existing_clusters or []
        clusters_text = json.dumps(
            [{"cluster_id": c.get("cluster_id"), "theme": c.get("theme")} for c in clusters],
            ensure_ascii=False,
            indent=2,
        )
        prompt = _FINGERPRINT_PROMPT.format(existing_clusters=clusters_text)
        full = f"{prompt}\n\n文章内容：\n{content[:6000]}"

        # Try T0
        result = self._try_llm_json(self._t0, full)
        if result is not None:
            result["degraded"] = False
            return result

        # Try T1
        result = self._try_llm_json(self._t1, full)
        if result is not None:
            result["degraded"] = False
            return result

        # Text fallback
        return self._text_fallback(content)

    # ── Phase 2 single-model analysis ────────────────────────────────────

    def run_phase2_single_model(
        self,
        prompt: str,
    ) -> dict[str, Any] | None:
        """Run a Phase 2 single-model analysis. T0-first, then T1."""
        result = self._try_llm_json(self._t0, prompt)
        if result is not None:
            return result
        return self._try_llm_json(self._t1, prompt)

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _try_llm_json(backend: Any | None, prompt: str) -> dict[str, Any] | None:
        if backend is None:
            return None
        try:
            raw = backend.complete(prompt)
            data = CrossArticleModelPolicy._parse_json(raw)
            return data
        except Exception:
            logger.debug("LLM call failed for fingerprint", exc_info=True)
            return None

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        # Strip markdown fences
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()
        # Find outermost braces
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            text = text[brace_start : brace_end + 1]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Not a JSON object")
        return data

    @staticmethod
    def _text_fallback(content: str) -> dict[str, Any]:
        """Minimal text-based fingerprint when all LLMs are unavailable."""
        # Take first meaningful segment as core_topic
        topic = content[:20].strip() if content else "未知主题"
        return {
            "core_topic": topic,
            "sectors": [],
            "mentioned_companies": [],
            "viewpoint_type": "unknown",
            "key_claims": [],
            "half_life_category": "short",
            "cluster_hint": {"relation_to_existing": "新建 cluster", "reason": "text fallback"},
            "degraded": True,
        }
