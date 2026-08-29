"""LLM-based claim extraction for richer entity/relationship recognition."""

import json
from typing import Protocol

from fin_analyse.ingestion.models import Evidence
from fin_analyse.utils.ids import stable_id
from fin_analyse.utils.llm_text import strip_markdown_fences, truncate_for_llm

from .models import Claim


class LLMBackend(Protocol):
    """Pluggable LLM backend interface."""

    def complete(self, prompt: str) -> str:
        """Simple text completion. All backends must implement this."""
        ...

PROMPT_TEMPLATE = """你是一个金融文本分析专家。从以下知识星球文章内容中提取结构化观点。

对于每一条观点，提取：
1. subject: 主体公司/行业名称
2. predicate: 关系类型 (mentions/benefits_from/faces_risk/supplies_to/competes_with/recommends)
3. object_value: 关系的客体或数值
4. claim_type: 观点类型 (company_mention/industry_signal/event_impact/risk_warning)
5. polarity: 情感方向 (positive/negative/neutral)
6. horizon: 时效 (30d/90d/180d)
7. confidence: 置信度 (0-1)
8. evidence_text: 原文中支持该观点的句子片段

返回 JSON 数组，每条观点一个对象。不要输出其他内容。
如果文章中确实没有可提取的观点，返回空数组 []。

文章标题: {title}
文章内容:
{content}"""


class LLMClaimExtractor:
    """Use LLM to extract richer claims from evidence text."""

    def __init__(self, backend: LLMBackend | None = None):
        self.backend = backend

    def extract(self, evidence: Evidence) -> list[Claim]:
        if self.backend is None:
            return []

        title = evidence.metadata.get("title", "")
        content = truncate_for_llm(evidence.content)
        prompt = PROMPT_TEMPLATE.format(title=title, content=content)

        try:
            response = self.backend.complete(prompt)
            json_text = strip_markdown_fences(response)
            items = json.loads(json_text)
        except Exception:
            return []

        claims = []
        for item in items:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject", ""))
            predicate = str(item.get("predicate", "mentions"))
            object_val = str(item.get("object_value", ""))
            if not subject:
                continue

            claim_id = _claim_id(evidence, predicate, subject)
            claims.append(
                Claim(
                    claim_id=claim_id,
                    source_id=evidence.source_id,
                    document_id=evidence.document_id,
                    subject=subject,
                    predicate=predicate,
                    object_value=object_val,
                    claim_type=str(item.get("claim_type", "company_mention")),
                    polarity=str(item.get("polarity", "neutral")),
                    horizon=str(item.get("horizon", "180d")),
                    confidence=float(item.get("confidence", 0.7)),
                    evidence_ids=[evidence.evidence_id],
                    extracted_by="llm",
                    metadata={
                        "evidence_text": str(item.get("evidence_text", "")),
                        "title": title,
                    },
                )
            )
        return claims


def _claim_id(evidence: Evidence, predicate: str, subject: str) -> str:
    # Uses same format as RuleBasedClaimExtractor; extractor type is in Claim.extracted_by
    return stable_id(evidence.evidence_id, ":", predicate, ":", subject, prefix="claim:")
