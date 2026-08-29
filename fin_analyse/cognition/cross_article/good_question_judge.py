"""LLM-based good question judge for persona gate.

Determines whether a 星大派好问题 article contains sufficient
teacher-original methodology, reasoning framework, or analytical logic
to qualify as persona-eligible for cross-article synthesis.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """你是一个内容质量判断器。请判断以下「星大派好问题」文章是否包含锅老师（星大派）的原创分析方法、思考框架或逻辑推导。

判断标准：
1. 回复是否包含具体的产业链分析、上下游穿透、标的逻辑拆解？
2. 是否体现了老师独特的分析视角或方法论（如"凤A原理"、"打水漂原理"、"前店后厂"等）？
3. 回复是否有实质性的行业洞察、板块判断或方向性结论？
4. 回复长度和深度是否足以提取有价值的认知模式？

排除标准：
- 简单闲聊、情感安慰、投资心态建议
- 纯粹转述公开信息、新闻
- 极短回复（一两句话）
- 纯提问本身（没有老师回复内容）

输出一个 JSON object：
{{
  "persona_eligible": true/false,
  "reason": "简短理由（20字以内）",
  "has_methodology": true/false,
  "has_industry_insight": true/false,
  "confidence": 0.0-1.0
}}

文章内容：
标题：{title}
栏目：{column}
日期：{date}
正文：
{content}

只输出 JSON，不要加额外文字。"""


class LlmGoodQuestionJudge:
    """LLM-based judge for good question persona eligibility.

    Uses a single LLM call (T0 backend) to assess whether a good-question
    article contains teacher-original methodology worth synthesizing.
    """

    def __init__(self, backend: Any | None = None):
        self._backend = backend

    def __call__(self, article: dict[str, Any]) -> bool:
        """Return True if the article is persona-eligible."""
        if self._backend is None:
            logger.debug("No LLM backend for good_question_judge, default deny")
            return False

        title = str(article.get("title", ""))
        column = str(article.get("column", ""))
        date = str(article.get("date", ""))
        article_id = str(article.get("id", ""))

        # Read content from file if available
        content = ""
        path = article.get("path", "")
        if path:
            try:
                from pathlib import Path

                p = Path(path)
                if p.exists():
                    content = p.read_text()[:3000]
            except Exception:
                pass
        if not content:
            content = str(article.get("content_excerpt", ""))[:3000]
        if not content:
            logger.debug("No content for article %s, default deny", article_id)
            return False

        prompt = _JUDGE_PROMPT.format(
            title=title,
            column=column,
            date=date,
            content=content,
        )

        try:
            raw = self._backend.complete(prompt)
            data = self._parse_json(raw)
            eligible = bool(data.get("persona_eligible", False))
            reason = data.get("reason", "")
            if eligible:
                logger.info("Good question %s: ELIGIBLE — %s", article_id[:12], reason)
            else:
                logger.info("Good question %s: SKIP — %s", article_id[:12], reason)
            return eligible
        except Exception as exc:
            logger.warning("Good question judge LLM failed for %s: %s", article_id[:12], exc)
            return False

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            text = text[brace_start : brace_end + 1]
        result: dict[str, Any] = json.loads(text)
        return result
