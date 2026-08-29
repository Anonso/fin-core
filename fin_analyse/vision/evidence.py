"""Vision Evidence Service — unified image/OCR/LLM visual evidence extraction.

Converts scraper-shaped image records into structured VisionEvidenceResult
with normalized image evidence, optional LLM-extracted visual facts, and
a stable prompt_context for downstream thesis extraction.

This is the FIN internal seam for all visual evidence processing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fin_analyse.cognition.llm import CognitionCompletionControl, CognitionLLM

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisionImageEvidence:
    """Normalized image record from a single article image."""

    file_path: str = ""
    """Image file path (relative to knowledge-base articles dir)."""

    path: str = ""
    """Local filesystem path to the image file (from CDP scraper)."""

    llm_description: str = ""
    """LLM-generated description of the image content."""

    ocr_text: str = ""
    """OCR-extracted text from the image."""

    provider: str = ""
    """Which vision backend provided the description (e.g. mimo, glm-vision, vision)."""

    vision_model: str = ""
    """Specific vision model used (e.g. mimo-v2.5, glm-4.6v-flash)."""

    fallback_chain: list[str] = field(default_factory=list)
    """Ordered list of fallback attempts, e.g. ['mimo:ok'] or ['mimo:error:timeout', 'vision:ok']."""

    error: str = ""
    """Error message if vision analysis failed completely."""

    source_url: str = ""
    """Original image URL from the source (e.g. images.zsxq.com)."""

    extraction_method: str = ""
    """How this description was obtained (e.g. vision_llm, ocr_only)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "path": self.path,
            "llm_description": self.llm_description,
            "ocr_text": self.ocr_text,
            "provider": self.provider,
            "vision_model": self.vision_model,
            "fallback_chain": list(self.fallback_chain),
            "error": self.error,
            "source_url": self.source_url,
            "extraction_method": self.extraction_method,
        }


@dataclass(frozen=True)
class VisionFact:
    """A single structured observation extracted from article images via LLM."""

    fact: str
    """Human-readable fact, e.g. '涂胶显影设备 TEL 占 95% 市场份额'."""

    companies: list[str] = field(default_factory=list)
    """Companies mentioned in this fact."""

    metrics: dict[str, str] = field(default_factory=dict)
    """Key-value metrics, e.g. {'market_share': '95%', 'segment': '涂胶显影'}."""

    confidence: float = 0.7
    """Extraction confidence [0,1]."""

    image_ref: str = ""
    """Source image filename or index for traceability."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact": self.fact,
            "companies": list(self.companies),
            "metrics": dict(self.metrics),
            "confidence": self.confidence,
            "image_ref": self.image_ref,
        }


@dataclass(frozen=True)
class VisionEvidenceRequest:
    """Input to VisionEvidenceService.extract()."""

    article_id: str
    """Article identifier for traceability."""

    image_descriptions: list[str] | None = None
    """Flat list of LLM image descriptions (legacy format from ZsxqCognitionSource)."""

    image_ocr_texts: list[str] | None = None
    """Flat list of OCR texts (legacy format from ZsxqCognitionSource)."""

    image_records: list[dict[str, Any]] | None = None
    """Scraper-shaped image records with file/llm_description/ocr_text/provider fields."""


@dataclass(frozen=True)
class VisionEvidenceResult:
    """Output of VisionEvidenceService.extract().

    Provides structured visual evidence, optional LLM-extracted facts,
    and a stable prompt_context for downstream thesis extraction.
    """

    article_id: str
    """Article identifier for traceability."""

    status: str
    """Extraction status: 'ok', 'empty', or 'error'."""

    images: list[VisionImageEvidence]
    """Normalized image evidence records."""

    visual_facts: list[VisionFact]
    """LLM-extracted structured facts from images (may be empty)."""

    company_recommendations: list[dict[str, Any]]
    """Company recommendations extracted from images (reserved for future use)."""

    prompt_context: str
    """Pre-built markdown context string for downstream LLM prompts."""

    data_gaps: tuple[str, ...]
    """Known data gaps: e.g. no_visual_inputs, visual_fact_llm_unavailable."""

    warnings: tuple[str, ...]
    """Non-fatal warnings about extraction quality or completeness."""

    provider_summary: str
    """Human-readable summary of which providers contributed."""

    source_boundary: str = "sensory"
    """Evidence source boundary — always 'sensory' for vision evidence."""

    advisory_only: bool = True
    """Vision evidence is always advisory; never generates trading actions."""

    trading_decision: bool = False
    """Vision evidence is never a trading decision."""

    execution_allowed: bool = False
    """Vision evidence never enables execution."""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict copy — isolated from the original dataclass."""
        return {
            "article_id": self.article_id,
            "status": self.status,
            "images": [img.to_dict() for img in self.images],
            "visual_facts": [f.to_dict() for f in self.visual_facts],
            "company_recommendations": [dict(rec) for rec in self.company_recommendations],
            "prompt_context": self.prompt_context,
            "data_gaps": list(self.data_gaps),
            "warnings": list(self.warnings),
            "provider_summary": self.provider_summary,
            "source_boundary": self.source_boundary,
            "advisory_only": self.advisory_only,
            "trading_decision": self.trading_decision,
            "execution_allowed": self.execution_allowed,
        }

    def to_prompt_context(self) -> str:
        """Return the pre-built markdown prompt context.

        When visual_facts is empty, returns empty string.
        """
        return self.prompt_context


# ---------------------------------------------------------------------------
# Fact extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """你是一个金融数据提取助手。从以下图片描述和OCR文字中提取结构化的关键事实。

对于每条事实，提取：
- fact: 用中文简洁描述这个事实（1-2句）
- companies: 涉及的公司名称列表
- metrics: 关键指标，如 {指标名: 值和单位}
- confidence: 0-1之间的置信度

重要规则：
1. 只提取有明确数据支撑的事实，不要推断
2. 表格数据优先提取数值和百分比
3. 如果描述中没有可提取的事实，返回空数组
4. 所有字段使用中文

返回JSON格式:
{"facts": [{"fact": "...", "companies": ["..."], "metrics": {"key": "value"}, "confidence": 0.9, "image_ref": "img_1"}]}
"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


# Sentinel to distinguish "no argument" (auto-resolve) from "explicit None" (no LLM).
_UNSET = object()


def _resolve_default_llm_backend() -> object | None:
    """Best-effort resolve a CognitionLLM backend from project config.

    Returns the backend object on success, or None when config is unavailable.
    Never raises.
    """
    try:
        from fin_analyse.cognition.llm import CognitionLLM

        llm = CognitionLLM.from_config(
            preferred=("glm53", "deepseek", "qwen", "claude")
        )
        return getattr(llm, "backend", None)
    except Exception:
        logger.debug("VisionEvidenceService: could not auto-resolve CognitionLLM backend")
        return None


class VisionEvidenceService:
    """Extract structured visual evidence from article images.

    Usage::

        service = VisionEvidenceService()
        result = service.extract(VisionEvidenceRequest(
            article_id="...",
            image_descriptions=source.image_descriptions,
            image_ocr_texts=source.image_ocr,
        ))
        prompt_ctx = result.to_prompt_context()
    """

    def __init__(self, llm_backend: object | None = _UNSET) -> None:
        """Create a VisionEvidenceService.

        Args:
            llm_backend: Optional LLM backend with a ``complete(prompt) -> str``
                method. When omitted (default), auto-resolves a CognitionLLM
                backend via ``CognitionLLM.from_config()`` (best-effort).
                Pass ``None`` explicitly to skip LLM fact extraction entirely.
        """
        if llm_backend is _UNSET:
            self._llm_backend = _resolve_default_llm_backend()
        else:
            self._llm_backend = llm_backend

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        request: VisionEvidenceRequest,
        *,
        control: CognitionCompletionControl | None = None,
    ) -> VisionEvidenceResult:
        """Extract visual evidence from the given request.

        Returns a VisionEvidenceResult even when inputs are empty. A supplied
        background control may raise when its cooperative deadline closes.
        """
        article_id = request.article_id

        # ── Normalize inputs ──────────────────────────────────────────
        descriptions = _normalize_str_list(request.image_descriptions)
        ocr_texts = _normalize_str_list(request.image_ocr_texts)

        # ── Empty input fast path ─────────────────────────────────────
        if not descriptions and not ocr_texts and not request.image_records:
            return VisionEvidenceResult(
                article_id=article_id,
                status="empty",
                images=[],
                visual_facts=[],
                company_recommendations=[],
                prompt_context="",
                data_gaps=("no_visual_inputs",),
                warnings=(),
                provider_summary="no visual inputs available",
                source_boundary="sensory",
                advisory_only=True,
            )

        # ── Normalize image evidence records ──────────────────────────
        images = _normalize_image_records(
            request.image_records, descriptions, ocr_texts
        )

        # ── Extract visual facts via LLM ──────────────────────────────
        if control is not None:
            control.checkpoint_or_raise()
        facts, fact_warnings, fact_gaps = _extract_visual_facts(
            images,
            self._llm_backend,
            control=control,
        )
        if control is not None:
            control.checkpoint_or_raise()

        # ── Build prompt context ──────────────────────────────────────
        prompt_context = _build_prompt_context(facts)

        # ── Build provider summary ────────────────────────────────────
        provider_summary = _build_provider_summary(images, facts)

        # ── Assemble warnings and data gaps ───────────────────────────
        all_warnings = list(fact_warnings)
        all_gaps: list[str] = []
        if not images:
            all_gaps.append("no_visual_inputs")
        all_gaps.extend(fact_gaps)

        return VisionEvidenceResult(
            article_id=article_id,
            status="ok",
            images=images,
            visual_facts=facts,
            company_recommendations=[],
            prompt_context=prompt_context,
            data_gaps=tuple(all_gaps),
            warnings=tuple(all_warnings),
            provider_summary=provider_summary,
            source_boundary="sensory",
            advisory_only=True,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_str_list(val: list[str] | None) -> list[str]:
    """Return a safe list of non-empty strings."""
    if val is None:
        return []
    return [s.strip() for s in val if s.strip()]


def _normalize_image_records(
    records: list[dict[str, Any]] | None,
    descriptions: list[str],
    ocr_texts: list[str],
) -> list[VisionImageEvidence]:
    """Normalize scraper-shaped dicts into VisionImageEvidence list.

    When records are provided, use them as the primary source.
    Supports both legacy test keys (file, llm_description, provider) and real
    CdpBridgeScraper._process_images keys (filename, path, llm_desc,
    vision_provider, vision_model, fallback_chain, error, source_url).

    Otherwise, fall back to building evidence from flat descriptions/ocr lists.
    """
    result: list[VisionImageEvidence] = []

    if records:
        for rec in records:
            # file_path: prefer "path" (CDP scraper), then "file" (legacy test),
            # then "filename" (CDP scraper fallback)
            file_path = str(
                rec.get("path")
                or rec.get("file")
                or rec.get("filename")
                or ""
            )

            # llm_description: prefer "llm_desc" (CDP scraper), then
            # "llm_description" (legacy/compat)
            llm_description = str(
                rec.get("llm_desc") or rec.get("llm_description") or ""
            )

            ocr_text = str(rec.get("ocr_text", ""))

            # provider: prefer "vision_provider" (CDP scraper), then
            # "provider" (legacy/compat)
            provider = str(rec.get("vision_provider") or rec.get("provider") or "")

            result.append(
                VisionImageEvidence(
                    file_path=file_path,
                    path=str(rec.get("path", "")),
                    llm_description=llm_description,
                    ocr_text=ocr_text,
                    provider=provider,
                    vision_model=str(rec.get("vision_model", "")),
                    fallback_chain=list(rec.get("fallback_chain", [])),
                    error=str(rec.get("error", "")),
                    source_url=str(rec.get("source_url", "")),
                    extraction_method=str(rec.get("extraction_method", "")),
                )
            )
    else:
        # Legacy fallback: build evidence from flat lists
        max_len = max(len(descriptions), len(ocr_texts))
        if max_len == 0:
            return []
        for i in range(max_len):
            desc = descriptions[i] if i < len(descriptions) else ""
            ocr = ocr_texts[i] if i < len(ocr_texts) else ""
            if desc or ocr:
                result.append(
                    VisionImageEvidence(
                        file_path="",
                        llm_description=desc,
                        ocr_text=ocr,
                    )
                )

    return result


def _extract_visual_facts(
    images: list[VisionImageEvidence],
    llm_backend: object | None,
    *,
    control: CognitionCompletionControl | None = None,
) -> tuple[list[VisionFact], list[str], list[str]]:
    """Extract structured VisionFacts from image evidence via LLM.

    Returns (facts, warnings, data_gaps).
    """
    if llm_backend is None:
        return [], [], ["visual_fact_llm_unavailable"]

    # Collect descriptions and OCR texts
    desc_parts: list[str] = []
    ocr_parts: list[str] = []
    for i, img in enumerate(images):
        if img.llm_description.strip():
            desc_parts.append(f"[图片{i + 1} 描述]\n{img.llm_description}")
        if img.ocr_text.strip():
            ocr_parts.append(f"[图片{i + 1} OCR文字]\n{img.ocr_text}")

    all_parts = desc_parts + ocr_parts
    if not all_parts:
        return [], [], []

    prompt = _EXTRACTION_PROMPT + "\n---\n" + "\n\n".join(all_parts)

    try:
        if control is not None:
            raw = CognitionLLM(backend=llm_backend).complete_text(
                prompt,
                control=control,
            )
        else:
            complete = getattr(llm_backend, "complete", None)
            if complete is None:
                return [], [], ["visual_fact_llm_unavailable"]
            raw = str(complete(prompt))
    except Exception as exc:
        logger.warning("Vision evidence: LLM fact extraction failed: %s", exc)
        return [], [f"LLM extraction error: {exc}"], ["visual_fact_llm_failed"]

    if not raw.strip():
        return (
            [],
            ["LLM returned empty response for visual facts"],
            ["visual_fact_llm_empty"],
        )

    # Parse JSON
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError as exc:
        logger.debug("Vision evidence: JSON parse failed for visual facts: %s", exc)
        return (
            [],
            [f"Visual fact JSON parse failed: {exc}"],
            ["visual_fact_llm_parse_failed"],
        )

    if not isinstance(data, dict):
        return (
            [],
            ["LLM returned unexpected format for visual facts"],
            ["visual_fact_llm_invalid_response"],
        )

    facts_data = data.get("facts", [])
    if not isinstance(facts_data, list):
        return (
            [],
            ["LLM facts field is not a list"],
            ["visual_fact_llm_invalid_response"],
        )

    facts: list[VisionFact] = []
    for item in facts_data:
        if not isinstance(item, dict):
            continue
        fact_text = str(item.get("fact", "")).strip()
        if not fact_text:
            continue
        facts.append(
            VisionFact(
                fact=fact_text,
                companies=_as_str_list(item.get("companies", [])),
                metrics=_as_str_dict(item.get("metrics", {})),
                confidence=float(item.get("confidence", 0.7)),
                image_ref=str(item.get("image_ref", "")),
            )
        )

    warnings: list[str] = []
    if facts:
        logger.info("Vision evidence: extracted %d visual facts", len(facts))
    else:
        warnings.append("LLM produced no valid visual facts from images")

    return facts, warnings, []


def _build_prompt_context(facts: list[VisionFact]) -> str:
    """Render visual facts as markdown lines for inclusion in extraction prompts."""
    if not facts:
        return ""
    lines = ["## 图片结构化事实"]
    for f in facts:
        metrics_str = "；".join(f"{k}: {v}" for k, v in f.metrics.items())
        companies_str = "、".join(f.companies)
        line = f"- {f.fact}"
        if companies_str:
            line += f" (相关公司: {companies_str})"
        if metrics_str:
            line += f" [{metrics_str}]"
        lines.append(line)
    return "\n".join(lines)


def _build_provider_summary(
    images: list[VisionImageEvidence], facts: list[VisionFact]
) -> str:
    """Build a human-readable summary of what was extracted."""
    if not images:
        return "no visual inputs available"

    parts = [f"{len(images)} image(s) processed"]

    # Count providers
    providers: set[str] = set()
    for img in images:
        if img.provider:
            providers.add(img.provider)
    if providers:
        parts.append(f"providers: {', '.join(sorted(providers))}")

    if facts:
        parts.append(f"{len(facts)} visual facts extracted")
    else:
        parts.append("no visual facts extracted")

    return "; ".join(parts)


def _extract_json(text: str) -> str:
    """Extract a JSON object or array from text, preferring fenced code blocks."""
    import re

    fenced = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        return obj_match.group(0)

    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        return arr_match.group(0)

    return text.strip()


def _as_str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(v) for v in val if v]


def _as_str_dict(val: Any) -> dict[str, str]:
    if not isinstance(val, dict):
        return {}
    return {str(k): str(v) for k, v in val.items() if k and v}
