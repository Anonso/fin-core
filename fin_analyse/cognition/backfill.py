"""Backfill runner: converts knowledge-base Markdown → EvidenceItem → ReasoningTrace."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from fin_analyse.cognition.models import EvidenceItem, ReasoningTrace, SourceLabel

logger = logging.getLogger(__name__)

VALID_LABELS = frozenset({"teacher_original", "research_report", "ai_assisted", "unknown"})


@dataclass
class BackfillReport:
    scanned_count: int = 0
    evidence_saved_count: int = 0
    labeled_count: int = 0
    teacher_original_count: int = 0
    research_report_count: int = 0
    ai_assisted_count: int = 0
    unknown_count: int = 0
    persona_eligible_count: int = 0
    persona_rejected_count: int = 0
    persona_gate_unknown_count: int = 0
    traces_created_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)
    sample_trace_ids: list[str] = field(default_factory=list)
    llm_available: bool = True
    llm_failed_ids: list[str] = field(default_factory=list)
    llm_failed_count: int = 0

    @property
    def total_processed(self) -> int:
        return self.evidence_saved_count + self.skipped_count


def _parse_markdown(filepath: Path) -> dict | None:
    """Parse a Markdown file with YAML frontmatter.

    Returns None if the file cannot be read.
    """
    import yaml

    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Cannot read %s", filepath)
        return None

    if not text.startswith("---"):
        return {"content": text.strip(), "meta": {}}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {"content": text.strip(), "meta": {}}
    raw_meta = parts[1].strip()
    content = parts[2].strip()

    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        logger.warning("Invalid YAML frontmatter in %s", filepath)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    return {"content": content, "meta": meta}


def _markdown_to_evidence(filepath: Path, data: dict, teacher_id: str) -> EvidenceItem | None:
    meta = data.get("meta", {})
    content = str(data.get("content", ""))
    if not content.strip():
        return None

    # Skip empty-shell articles (title-only, no real body)
    body = content.strip()
    title_line = body.split("\n", 1)[0].lstrip("#").strip()
    remainder = body.replace(title_line, "", 1).strip()
    if remainder in ("", "_知识星球栏目文章_") or len(remainder) < 30:
        return None

    article_id = str(meta.get("id", filepath.stem))
    evidence_id = f"ev-{article_id}"
    source_id = str(meta.get("id", filepath.stem))
    title = (content.split("\n", 1)[0] or filepath.stem).lstrip("#").strip()[:200]
    date_str = str(meta.get("date", ""))
    companies_raw = meta.get("companies", [])
    if isinstance(companies_raw, list):
        companies = [str(c) for c in companies_raw]
    elif isinstance(companies_raw, str):
        companies = [c.strip() for c in companies_raw.split(",") if c.strip()]
    else:
        companies = []
    tags_raw = meta.get("tags", [])
    if isinstance(tags_raw, list):
        topics = [str(t) for t in tags_raw]
    elif isinstance(tags_raw, str):
        topics = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        topics = []
    author = str(meta.get("column", ""))

    return EvidenceItem(
        evidence_id=evidence_id,
        source_type="zsxq_article",
        source_id=source_id,
        title=title,
        content=content,
        author=author or None,
        published_at=date_str or None,
        collected_at=date_str or "",
        companies=companies,
        topics=topics,
        source_label=SourceLabel("unknown", teacher_id, 0.5, ["pending labeling"]),
        reliability=0.5,
        metadata={
            "filepath": str(filepath),
            "score": meta.get("score"),
            "is_qa": meta.get("is_qa", False),
            "column": author,
        },
    )


class CognitionBackfillRunner:
    """Walk knowledge-base Markdown files and backfill into cognition repos."""

    def __init__(
        self,
        kb_root: Path,
        service,  # CognitiveService
        *,
        teacher_id: str = "guo",
        limit: int | None = None,
        resume: bool = False,
        dry_run: bool = False,
        sample_size: int = 20,
    ) -> None:
        self.kb_root = kb_root
        self.service = service
        self.teacher_id = teacher_id
        self.limit = limit
        self.resume = resume
        self.dry_run = dry_run
        self.sample_size = sample_size

    def run(self) -> BackfillReport:
        report = BackfillReport(llm_available=self.service.llm_available)
        article_dir = self.kb_root / "articles"
        if not article_dir.is_dir():
            logger.warning("Article directory not found: %s", article_dir)
            return report

        paths = sorted(article_dir.glob("*.md"))
        for filepath in paths:
            if self.limit is not None and report.total_processed >= self.limit:
                break
            report.scanned_count += 1

            data = _parse_markdown(filepath)
            if data is None:
                report.error_count += 1
                if len(report.errors_sample) < self.sample_size:
                    report.errors_sample.append(f"cannot read {filepath.name}")
                continue

            evidence = _markdown_to_evidence(filepath, data, self.teacher_id)
            if evidence is None:
                report.skipped_count += 1
                continue

            # Resume: skip if evidence already exists
            if self.resume and self._evidence_exists(evidence.evidence_id):
                report.skipped_count += 1
                continue

            if not self.dry_run:
                self.service.save_evidence(evidence)
            report.evidence_saved_count += 1

            # Label
            label = self._do_label(evidence)
            report.labeled_count += 1
            self._count_label(label.label, report)

            decision = self._do_persona_gate(evidence.evidence_id)
            self._count_persona_gate(decision, report)

            if not decision.allows_persona:
                continue

            # Extract traces
            traces = self._do_extract(evidence, report)
            for t in traces:
                report.traces_created_count += 1
                if len(report.sample_trace_ids) < self.sample_size:
                    report.sample_trace_ids.append(t.trace_id)

        return report

    def _evidence_exists(self, evidence_id: str) -> bool:
        try:
            matches = self.service.evidence_repo.find(lambda item: item.evidence_id == evidence_id)
            return bool(matches)
        except Exception:
            return False

    def _do_label(self, evidence: EvidenceItem) -> SourceLabel:
        try:
            return cast(SourceLabel, self.service.label_evidence(evidence.evidence_id))
        except Exception:
            pass
        # Fallback: use labeler directly
        try:
            return cast(
                SourceLabel,
                self.service.labeler.label(
                    title=evidence.title,
                    content=evidence.content,
                    author=evidence.author,
                ),
            )
        except Exception:
            return SourceLabel("unknown", self.teacher_id, 0.3, ["labeling error"])

    def _do_persona_gate(self, evidence_id: str):
        try:
            return self.service.persona_gate_decision(evidence_id)
        except Exception:
            from fin_analyse.cognition.persona_gate import PersonaGateDecision

            return PersonaGateDecision(
                evidence_id=evidence_id,
                allows_persona=False,
                category="persona_gate_error",
                source_classification="unknown_reference",
                confidence=0.0,
                half_life_class="medium_logic",
                reasons=["persona gate error"],
            )

    def _do_extract(
        self, evidence: EvidenceItem, report: BackfillReport | None = None
    ) -> list[ReasoningTrace]:
        llm_failed = False
        try:
            traces = cast(
                list[ReasoningTrace], self.service.extract_teacher_reasoning(evidence.evidence_id)
            )
        except Exception:
            traces = []
        if not traces:
            # Check if LLM was attempted and failed (extraction was attempted
            # but returned empty).  Do NOT fall back to direct extractor call —
            # the service's write gate is the only path to ReasoningTrace writes.
            try:
                ext = self.service.extractor
                if hasattr(ext, "last_extraction_failed") and getattr(
                    ext, "last_extraction_failed", False
                ) or hasattr(ext, "llm") and getattr(ext.llm, "last_extraction_failed", False):
                    llm_failed = True
            except Exception:
                pass

        if llm_failed and report is not None:
            report.llm_failed_ids.append(evidence.evidence_id)
            report.llm_failed_count += 1

        return traces

    @staticmethod
    def _count_label(label: str, report: BackfillReport) -> None:
        if label == "teacher_original":
            report.teacher_original_count += 1
        elif label == "research_report":
            report.research_report_count += 1
        elif label == "ai_assisted":
            report.ai_assisted_count += 1
        else:
            report.unknown_count += 1

    @staticmethod
    def _count_persona_gate(decision, report: BackfillReport) -> None:
        if getattr(decision, "allows_persona", False):
            report.persona_eligible_count += 1
        elif getattr(decision, "category", "") == "persona_gate_error":
            report.persona_gate_unknown_count += 1
        else:
            report.persona_rejected_count += 1
