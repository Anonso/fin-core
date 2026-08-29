"""ZSXQ 老师原文 cognition apprentice pipeline."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.cognition.dynamic_clock import evaluate_dynamic_clock
from fin_analyse.cognition.evidence_store import JsonlRepository
from fin_analyse.cognition.llm import CognitionCompletionControl
from fin_analyse.cognition.models import (
    DynamicClock,
    EvidenceChain,
    InformationUnit,
    InvestmentResearchSuggestion,
    ThemeCluster,
    ZsxqApprenticeResult,
    ZsxqCognitionSource,
)
from fin_analyse.cognition.research_suggestion import generate_research_suggestion
from fin_analyse.cognition.theme_cluster import assign_theme_clusters
from fin_analyse.cognition.thesis_extractor import (
    LlmZsxqThesisExtractor,
    RuleBasedZsxqThesisExtractor,
    ThesisExtraction,
    replace_central_idea_warnings,
)
from fin_analyse.utils.ids import stable_id

_XINGDAPAI_COLUMNS = {"星大派特刊", "星大派锐评", "星大派好问题"}
_FENGXIANJUN_COLUMNS = {"凤仙郡小故事"}

logger = logging.getLogger(__name__)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 4 :].lstrip()
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _extract_section_items(body: str, section_title: str) -> list[str]:
    marker = f"## {section_title}"
    start = body.find(marker)
    if start == -1:
        return []
    rest = body[start + len(marker) :]
    next_section = rest.find("\n## ")
    if next_section != -1:
        rest = rest[:next_section]
    chunks: list[str] = []
    current: list[str] = []
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            if current:
                chunks.append("\n".join(current).strip())
                current = []
            continue
        current.append(stripped)
    if current:
        chunks.append("\n".join(current).strip())
    return chunks


def _strip_image_sections(body: str) -> str:
    for marker in ("\n## 图片描述", "\n## 图片OCR文字"):
        idx = body.find(marker)
        if idx != -1:
            return body[:idx].strip()
    return body.strip()


def _classify_source_rank(column: str, body: str) -> str:
    if column in _XINGDAPAI_COLUMNS:
        return "t0_xingdapai"
    if column in _FENGXIANJUN_COLUMNS:
        return "t0_fengxian"
    if "星大派特刊" in body or "星大派锐评" in body or "星大派好问题" in body:
        return "unknown"
    return "external_context"


def _classify_completeness(body: str, source_rank: str) -> str:
    if source_rank == "unknown":
        return "aggregate"
    clean_body = _strip_image_sections(body)
    if source_rank in {"t0_xingdapai", "t0_fengxian"} and len(clean_body) < 150:
        return "partial"
    return "full"


def load_zsxq_cognition_source(article_path: str | Path) -> ZsxqCognitionSource:
    path = Path(article_path)
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    title = _first_heading(body)
    column = meta.get("column", "")
    source_rank = _classify_source_rank(column, body)
    completeness = _classify_completeness(body, source_rank)
    content = _strip_image_sections(body)
    article_id = meta.get("id")
    topic_id = meta.get("topic_id")
    published_at = meta.get("date", "")
    source_id = stable_id("zsxq-source", str(path), article_id or "", topic_id or "", published_at)

    return ZsxqCognitionSource(
        source_id=source_id,
        article_path=str(path),
        article_id=article_id,
        topic_id=topic_id,
        published_at=published_at,
        column=column,
        title=title,
        content=content,
        image_descriptions=_extract_section_items(body, "图片描述"),
        image_ocr=_extract_section_items(body, "图片OCR文字"),
        source_rank=source_rank,
        completeness=completeness,
        metadata=dict(meta),
    )


def _merge_extractions(
    rule: ThesisExtraction,
    llm: ThesisExtraction,
) -> ThesisExtraction:
    """Merge rule-based and LLM extraction results.

    Rule-based units are always kept (safety boundary). LLM units are
    added when they don't duplicate existing units by title or thesis.
    LLM confidence is scaled down slightly since it's not rule-verified.
    """
    if not rule.units and not llm.units:
        all_warnings: list[str] = []
        for w in list(rule.warnings) + list(llm.warnings):
            if w not in all_warnings:
                all_warnings.append(w)
        return ThesisExtraction([], [], all_warnings)

    # Build dedup keys from rule units
    seen_titles: set[str] = {u.title for u in rule.units}
    seen_theses: set[str] = {u.thesis for u in rule.units}

    merged_units = list(rule.units)
    merged_warnings = list(rule.warnings)

    for llm_unit in llm.units:
        # Skip near-duplicates
        if llm_unit.title in seen_titles or llm_unit.thesis in seen_theses:
            continue
        # Scale down LLM confidence slightly
        scaled = InformationUnit(
            unit_id=llm_unit.unit_id,
            source_id=llm_unit.source_id,
            teacher_id=llm_unit.teacher_id,
            unit_type=llm_unit.unit_type,
            title=llm_unit.title,
            thesis=llm_unit.thesis,
            original_evidence=llm_unit.original_evidence,
            apprentice_interpretation=llm_unit.apprentice_interpretation,
            confidence=round(llm_unit.confidence * 0.9, 2),
            related_companies=llm_unit.related_companies,
            related_topics=llm_unit.related_topics,
            theme_cluster_ids=llm_unit.theme_cluster_ids,
            usage_policy=llm_unit.usage_policy,
            created_at=llm_unit.created_at,
            metadata=dict(llm_unit.metadata),
        )
        merged_units.append(scaled)
        seen_titles.add(llm_unit.title)
        seen_theses.add(llm_unit.thesis)

    for w in llm.warnings:
        if w not in merged_warnings:
            merged_warnings.append(w)

    # Build chains for all merged units
    chains = [
        _make_evidence_chain_for_unit(rule, u) or _make_evidence_chain_for_unit(llm, u)
        for u in merged_units
    ]
    filtered_chains: list[EvidenceChain] = [c for c in chains if c is not None]

    return ThesisExtraction(merged_units, filtered_chains, merged_warnings)


def _make_evidence_chain_for_unit(
    extraction: ThesisExtraction,
    unit: InformationUnit,
) -> EvidenceChain | None:
    """Find the evidence chain for a unit in an extraction result."""
    for chain in extraction.evidence_chains:
        if chain.unit_id == unit.unit_id:
            return chain
    return None


def _merge_theme_cluster(existing: ThemeCluster, incoming: ThemeCluster) -> ThemeCluster:
    """Merge incoming cluster into existing, preserving accumulated unit_ids."""
    merged_unit_ids = list(dict.fromkeys(existing.unit_ids + incoming.unit_ids))
    merged_source_ids = sorted(set(existing.source_ids + incoming.source_ids))
    merged_theses = list(dict.fromkeys(existing.core_theses + incoming.core_theses))
    return replace(
        existing,
        unit_ids=merged_unit_ids,
        source_ids=merged_source_ids,
        core_theses=merged_theses,
        active_status="reinforced"
        if len(merged_unit_ids) > len(existing.unit_ids)
        else existing.active_status,
        priority=max(existing.priority, incoming.priority),
        last_reinforced_at=incoming.last_reinforced_at,
    )


class ZsxqCognitionApprentice:
    def __init__(self, runtime_root: Path | None = None) -> None:
        if runtime_root is None:
            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            runtime_root = default_knowledge_base_root() / "runtime" / "cognition"
        root = runtime_root
        self.runtime_root = root
        self.source_repo = JsonlRepository(
            root / "zsxq_sources.jsonl", ZsxqCognitionSource, "source_id"
        )
        self.unit_repo = JsonlRepository(
            root / "information_units.jsonl", InformationUnit, "unit_id"
        )
        self.chain_repo = JsonlRepository(root / "evidence_chains.jsonl", EvidenceChain, "chain_id")
        self.cluster_repo = JsonlRepository(
            root / "theme_clusters.jsonl", ThemeCluster, "cluster_id"
        )
        self.clock_repo = JsonlRepository(root / "dynamic_clocks.jsonl", DynamicClock, "unit_id")
        self.suggestion_repo = JsonlRepository(
            root / "research_suggestions.jsonl",
            InvestmentResearchSuggestion,
            "suggestion_id",
        )
        self.rule_extractor = RuleBasedZsxqThesisExtractor()
        self.llm_extractor = LlmZsxqThesisExtractor()

    @staticmethod
    def _central_idea_gate(source: ZsxqCognitionSource) -> tuple[bool, str | None]:
        """Whether the central-idea fallback may attempt, plus a skip reason.

        Gates (deep-read-unlock-20260819 design v4): classify_g_source
        eligible (star column + teacher_original provenance) and body
        completeness (>= 150 chars, matching _classify_completeness).
        Ineligible sources skip silently (same observable shape as the
        existing "skip non-T0" path); short bodies record the skip.
        """
        from fin_analyse.guo_teacher_research.source_contract import classify_g_source

        meta = source.metadata or {}
        decision = classify_g_source(
            source.column,
            teacher_original=(
                str(meta.get("source_classification", "")) == "teacher_original"
            ),
            is_qa=str(meta.get("is_qa", "")).lower() in ("true", "1"),
        )
        if not decision.eligible:
            # 静默跳过绝根（06 侦查教训）：原因码嵌入完整 data_gap，
            # 未分类栏目单独成码，不折叠。
            return False, (
                f"central_idea_skipped_{decision.data_gap or 'g_source_type_unknown'}"
            )
        if source.completeness == "partial":
            return False, "central_idea_skipped_insufficient_content"
        return True, None

    def deep_read(
        self,
        article_path: str | Path,
        *,
        now: str | None = None,
        control: CognitionCompletionControl | None = None,
    ) -> ZsxqApprenticeResult:
        from fin_analyse.vision.evidence import VisionEvidenceRequest, VisionEvidenceService

        if control is not None:
            control.checkpoint_or_raise()
        actual_now = now or datetime.now(UTC).replace(microsecond=0).isoformat()
        source = load_zsxq_cognition_source(article_path)
        self.source_repo.upsert(source)

        # Visual facts: VisionEvidenceService extraction from image descriptions (best-effort)
        vision_request = VisionEvidenceRequest(
            article_id=source.article_id or source.source_id,
            image_descriptions=source.image_descriptions,
            image_ocr_texts=source.image_ocr,
        )
        vision_service = VisionEvidenceService()
        vision_result = (
            vision_service.extract(vision_request)
            if control is None
            else vision_service.extract(vision_request, control=control)
        )
        if control is not None:
            control.checkpoint_or_raise()
        visual_text = vision_result.to_prompt_context()

        # Merge vision data gaps and warnings into extraction warnings
        vision_warnings: list[str] = []
        if vision_result.data_gaps:
            vision_warnings.append(f"vision data gaps: {', '.join(vision_result.data_gaps)}")
        for w in vision_result.warnings:
            vision_warnings.append(f"[vision] {w}")

        # Dual-track extraction
        rule_extraction = self.rule_extractor.extract(source)
        if control is not None:
            control.checkpoint_or_raise()
        llm_extraction = (
            self.llm_extractor.extract(source, visual_facts_text=visual_text)
            if control is None
            else self.llm_extractor.extract(
                source,
                visual_facts_text=visual_text,
                control=control,
            )
        )
        if control is not None:
            control.checkpoint_or_raise()
        extraction = _merge_extractions(rule_extraction, llm_extraction)
        # Fold vision warnings into extraction warnings only when there were
        # actual image inputs to process (avoid noise from empty-article paths).
        if source.image_descriptions or source.image_ocr:
            all_warnings = list(extraction.warnings) + vision_warnings
        else:
            all_warnings = list(extraction.warnings)
        extraction = ThesisExtraction(
            units=extraction.units,
            evidence_chains=extraction.evidence_chains,
            warnings=all_warnings,
        )
        if not extraction.units:
            if control is not None:
                control.checkpoint_or_raise()
            may_attempt, skip_reason = self._central_idea_gate(source)
            if not may_attempt:
                warnings = (
                    extraction.warnings + [skip_reason] if skip_reason else extraction.warnings
                )
                return ZsxqApprenticeResult(
                    source=source,
                    units=[],
                    evidence_chains=[],
                    theme_clusters=[],
                    clocks=[],
                    suggestions=[],
                    warnings=warnings,
                )
            central, central_failure = self.llm_extractor.extract_central_idea(
                source,
                control=control,
            )
            if central is None:
                reason = central_failure or "unknown"
                logger.warning(
                    "central_idea_extraction_failed reason=%s source_id=%s",
                    reason,
                    getattr(source, "article_id", None)
                    or getattr(source, "source_id", None),
                )
                return ZsxqApprenticeResult(
                    source=source,
                    units=[],
                    evidence_chains=[],
                    theme_clusters=[],
                    clocks=[],
                    suggestions=[],
                    warnings=extraction.warnings
                    + [f"central_idea_extraction_failed:{reason}"],
                )
            # 中心思想成功：替换 retryable/空提取 warning（pair fresh 判定依赖
            # 该终态），进入共享 downstream（theme/clock/suggestion/repos）。
            extraction = ThesisExtraction(
                units=[central],
                evidence_chains=[],
                warnings=replace_central_idea_warnings(extraction.warnings),
            )

        clustered_units, clusters = assign_theme_clusters(extraction.units, now=actual_now)
        unit_ids = {unit.unit_id for unit in clustered_units}
        chains = [chain for chain in extraction.evidence_chains if chain.unit_id in unit_ids]
        clocks = [evaluate_dynamic_clock(unit, now=actual_now) for unit in clustered_units]
        clock_by_unit_id = {clock.unit_id: clock for clock in clocks}
        suggestions = [
            generate_research_suggestion(unit, clock_by_unit_id[unit.unit_id])
            for unit in clustered_units
        ]

        if control is not None:
            control.checkpoint_or_raise()
        for unit in clustered_units:
            self.unit_repo.upsert(unit)
        for chain in chains:
            self.chain_repo.upsert(chain)
        for cluster in clusters:
            # Merge with existing cluster to avoid losing unit_ids from
            # previous deep_read calls on the same theme.
            cluster_id = cluster.cluster_id

            def same_cluster(c: ThemeCluster, *, cluster_id: str = cluster_id) -> bool:
                return c.cluster_id == cluster_id

            existing_list = self.cluster_repo.find(same_cluster)
            if existing_list:
                cluster = _merge_theme_cluster(existing_list[0], cluster)
            self.cluster_repo.upsert(cluster)
        for clock in clocks:
            self.clock_repo.upsert(clock)
        for suggestion in suggestions:
            self.suggestion_repo.upsert(suggestion)
        if control is not None:
            control.checkpoint_or_raise()

        return ZsxqApprenticeResult(
            source=source,
            units=clustered_units,
            evidence_chains=chains,
            theme_clusters=clusters,
            clocks=clocks,
            suggestions=suggestions,
            warnings=extraction.warnings,
        )
