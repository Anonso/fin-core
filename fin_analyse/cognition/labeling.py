"""Source labeling for cognition learning eligibility."""

from __future__ import annotations

import logging

from fin_analyse.cognition.models import SourceLabel

logger = logging.getLogger(__name__)

# Fixed label vocabulary
VALID_LABELS = frozenset({"teacher_original", "research_report", "ai_assisted", "unknown"})


class SourceLabeler:
    """Classify whether content is teacher cognition or only evidence context."""

    AI_MARKERS = (
        "AI分析",
        "AI整理",
        "AI 辅助",
        "以下为AI",
        "由AI",
        "模型整理",
    )
    REPORT_MARKERS = (
        "研报",
        "券商",
        "给予买入评级",
        "盈利预测",
        "目标价",
        "机构上调",
    )
    ORIGINAL_MARKERS = (
        "我认为",
        "我的判断",
        "关键不在",
        "真正值得",
        "不追高",
        "需要观察",
    )

    def __init__(self, default_teacher_id: str = "guo", min_original_chars: int = 100) -> None:
        self.default_teacher_id = default_teacher_id
        self.min_original_chars = min_original_chars

    def label(self, *, title: str, content: str, author: str | None) -> SourceLabel:
        text = f"{title}\n{content}"
        reasons: list[str] = []

        # AI-assisted markers take priority
        if any(marker in text for marker in self.AI_MARKERS):
            reasons.append("matched AI-assisted marker")
            return SourceLabel("ai_assisted", self.default_teacher_id, 0.95, reasons)

        # Multiple research-report markers
        report_hits = [m for m in self.REPORT_MARKERS if m in text]
        if len(report_hits) >= 2:
            reasons.append("matched multiple research-report markers")
            return SourceLabel("research_report", self.default_teacher_id, 0.9, reasons)

        # Too short to learn from
        if len(content.strip()) < self.min_original_chars:
            reasons.append("content shorter than original-learning threshold")
            return SourceLabel("unknown", self.default_teacher_id, 0.7, reasons)

        # Original reasoning markers + teacher author
        original_hits = [m for m in self.ORIGINAL_MARKERS if m in text]
        if author and "郭" in author and len(original_hits) >= 2:
            reasons.append("matched teacher author and original reasoning markers")
            return SourceLabel("teacher_original", self.default_teacher_id, 0.85, reasons)

        reasons.append("no strong source signal")
        return SourceLabel("unknown", self.default_teacher_id, 0.5, reasons)


class LLMSourceLabeler:
    """Use an LLM backend to classify content source."""

    _CLASSIFY_PROMPT = (
        "Classify this article into exactly one of: "
        "teacher_original, research_report, ai_assisted, unknown.\n\n"
        "Definitions:\n"
        "- teacher_original: first-person original investment reasoning by the teacher, "
        "with subjective judgement, risk boundaries, or action implications.\n"
        "- research_report: third-party research, brokerage reports, price targets, "
        "earnings forecasts, or multi-source consensus summaries.\n"
        "- ai_assisted: content explicitly marked as AI-generated, AI-assisted, "
        "or AI-curated.\n"
        "- unknown: cannot determine with confidence, or content too short/empty.\n\n"
        "Return a JSON object with keys: label (string), confidence (float 0–1), "
        "reasons (list of strings, 1–3 short reasons in Chinese).\n\n"
        "Title: {title}\nAuthor: {author}\n\n{content}"
    )

    def __init__(self, llm, *, default_teacher_id: str = "guo") -> None:
        self.llm = llm
        self.default_teacher_id = default_teacher_id

    def label(self, *, title: str, content: str, author: str | None) -> SourceLabel:
        prompt = self._CLASSIFY_PROMPT.format(
            title=title,
            author=author or "unknown",
            content=content[:3000],
        )
        result = self.llm.complete_json(prompt, expected_type="source_label")
        if not result.ok or not isinstance(result.data, dict):
            logger.info("LLM source labeling failed: %s", result.error)
            return SourceLabel("unknown", self.default_teacher_id, 0.5, ["llm parse failure"])
        label = result.data.get("label", "unknown")
        confidence = float(result.data.get("confidence", 0.5))
        reasons = list(result.data.get("reasons", []))
        if label not in VALID_LABELS:
            label = "unknown"
            confidence = min(confidence, 0.6)
        confidence = max(0.0, min(confidence, 1.0))
        return SourceLabel(label, self.default_teacher_id, confidence, reasons)


class HybridSourceLabeler:
    """Rule-first, LLM-fallback source labeler.

    If the rule-based labeler returns a high-confidence non-unknown label,
    accept it directly. Otherwise consult the LLM labeler.
    """

    def __init__(
        self,
        rule_labeler: SourceLabeler,
        llm_labeler: LLMSourceLabeler,
        *,
        rule_confidence_threshold: float = 0.8,
    ) -> None:
        self.rule = rule_labeler
        self.llm = llm_labeler
        self.threshold = rule_confidence_threshold

    def label(self, *, title: str, content: str, author: str | None) -> SourceLabel:
        rule_label = self.rule.label(title=title, content=content, author=author)
        if rule_label.confidence >= self.threshold and rule_label.label != "unknown":
            return rule_label
        return self.llm.label(title=title, content=content, author=author)
