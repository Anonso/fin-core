"""Central-idea fallback: when main extraction yields no units, a T0 star-column
article with complete body may get a single central-idea unit (B in
deep-read-unlock-20260819). Quality gates: classify_g_source eligible,
body >= 150 chars, confidence >= 0.7, evidence verifiably from the source,
unit_type in the supported subset.
"""

from __future__ import annotations

from pathlib import Path

from fin_analyse.cognition.thesis_extractor import (
    LlmZsxqThesisExtractor,
    ThesisExtraction,
)
from fin_analyse.cognition.zsxq_apprentice import ZsxqCognitionApprentice

COMPLETE_BODY = (
    "今天市场大跳了，但我认为没啥好慌的。一方面是我们周一就在减了，另一方面是这波"
    "已经是剧本流了。其实没有啥散户，都是机构和主力在倒腾，丹模块在7月已经应丹尽丹"
    "了。今天杀一波就是借题发挥一下，平时不好意思杀这么多。各位可以回想一下，如果是"
    "7月初跌80个点，是不是起码百股跌停，而今天也就这样了。市场大跳不是丹模块不恐慌"
    "了，而是没啥丹模块了。机构自己都在抱怨没人了。就近期来看每天不管怎么震都是对倒"
    "的剧本。而现在真实的情况就是私募接近100%，量化大概90%，年X金和公X金都在跑步"
    "进场。"
)


def _write_article(tmp_path: Path, *, column: str = "星大派锐评", body: str = COMPLETE_BODY) -> Path:
    path = tmp_path / "20260819_zsxq-45544142412844128.md"
    path.write_text(
        f"""---
id: zsxq-45544142412844128
date: 2026-08-19 12:47
score: None
column: {column}
is_qa: False
type: talk
topic_id: 45544142412844128
content_source: zsxq_topic_cursor
source_classification: teacher_original
---

# 星大派锐评：虽然大跳了，但我认为没啥好慌的。

{body}
""",
        encoding="utf-8",
    )
    return path


class _FakeRuleExtractor:
    """Rule extractor that never produces units (main-path control)."""

    def extract(self, source: object) -> ThesisExtraction:
        return ThesisExtraction([], [], [])


class _FakeBackend:
    """CognitionLLM backend that routes by prompt marker and counts calls."""

    def __init__(self, central_idea_payload: dict | None, *, main_units: list | None = None) -> None:
        self.central_idea_payload = central_idea_payload
        self.main_units = main_units if main_units is not None else []
        self.central_idea_calls = 0

    def complete(self, prompt: str) -> str:
        if "提取关键信息单元" in prompt:
            return '{"units": %s}' % (self.main_units or [])
        if "中心思想" in prompt:
            self.central_idea_calls += 1
            import json

            return json.dumps(self.central_idea_payload or {"units": []}, ensure_ascii=False)
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


def _apprentice(tmp_path: Path, backend: _FakeBackend) -> ZsxqCognitionApprentice:
    apprentice = ZsxqCognitionApprentice(runtime_root=tmp_path / "runtime" / "cognition")
    apprentice.rule_extractor = _FakeRuleExtractor()
    apprentice.llm_extractor = LlmZsxqThesisExtractor(llm=backend)
    return apprentice


def test_central_idea_extracted_when_main_extraction_empty(tmp_path) -> None:
    path = _write_article(tmp_path)
    backend = _FakeBackend(
        {
            "unit_type": "strategic_thesis",
            "title": "跌幅有限",
            "thesis": "这次大跌主要是机构借题发挥，散户参与度低，跌幅扩散有限。",
            "evidence": "其实没有啥散户，都是机构和主力在倒腾",
            "interpretation": "存量机构博弈下的情绪释放",
            "confidence": 0.8,
            "topics": ["市场情绪", "机构博弈"],
            "companies": [],
        }
    )
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert len(result.units) == 1
    unit = result.units[0]
    assert unit.unit_type == "strategic_thesis"
    assert unit.title == "跌幅有限"
    assert unit.thesis == "这次大跌主要是机构借题发挥，散户参与度低，跌幅扩散有限。"
    assert unit.apprentice_interpretation.startswith("学徒翻译：")
    assert unit.confidence >= 0.7
    assert "其实没有啥散户" in (unit.original_evidence or ["", ""])[0]
    assert backend.central_idea_calls == 1
    assert any("central_idea_extracted" in w for w in result.warnings)


def test_central_idea_accepts_verbatim_non_contiguous_sentences(tmp_path) -> None:
    """Two verbatim quotes from different parts of the article are valid
    evidence (each sentence is faithfully quoted), even when they are not
    adjacent in the source.  The quality gate must check per-sentence
    verbatim presence, not contiguity of the joined evidence string."""
    path = _write_article(tmp_path)
    backend = _FakeBackend(
        {
            "unit_type": "strategic_thesis",
            "title": "机构对倒",
            "thesis": "近期震荡主要是机构对倒的剧本。",
            "evidence": "其实没有啥散户，都是机构和主力在倒腾。\n每天不管怎么震都是对倒的剧本",
            "interpretation": "老师判断当前震荡为存量机构博弈，非散户新增交易。",
            "confidence": 0.8,
            "topics": ["市场情绪", "机构博弈"],
            "companies": [],
        }
    )
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert len(result.units) == 1
    assert result.units[0].unit_type == "strategic_thesis"
    assert any("central_idea_extracted" in w for w in result.warnings)


def test_central_idea_skipped_for_short_body_without_llm_call(tmp_path) -> None:
    path = _write_article(tmp_path, body="星大派锐评：我真tm服了。 有道云笔记 密码：7XZB")
    backend = _FakeBackend({"unit_type": "strategic_thesis", "title": "x", "thesis": "y", "evidence": "z", "confidence": 0.9})
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert result.units == []
    assert backend.central_idea_calls == 0
    assert any("insufficient_content" in w for w in result.warnings)


def test_central_idea_skipped_for_non_star_column(tmp_path) -> None:
    path = _write_article(tmp_path, column="普通")
    backend = _FakeBackend({"unit_type": "strategic_thesis", "title": "x", "thesis": "y", "evidence": "z", "confidence": 0.9})
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert result.units == []
    assert backend.central_idea_calls == 0


def test_central_idea_fails_on_low_confidence(tmp_path) -> None:
    path = _write_article(tmp_path)
    backend = _FakeBackend(
        {
            "unit_type": "strategic_thesis",
            "title": "低置信",
            "thesis": "低置信判断",
            "evidence": "其实没有啥散户",
            "interpretation": "低置信的解读",
            "confidence": 0.5,
        }
    )
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert result.units == []
    assert any("central_idea_extraction_failed" in w for w in result.warnings)
    assert any("central_idea_extraction_failed:low_confidence" in w for w in result.warnings)


def test_central_idea_fails_on_fabricated_evidence(tmp_path) -> None:
    path = _write_article(tmp_path)
    backend = _FakeBackend(
        {
            "unit_type": "strategic_thesis",
            "title": "编造",
            "thesis": "编造的判断",
            "evidence": "这句证据根本不在原文里出现任何位置",
            "interpretation": "编造的解读",
            "confidence": 0.9,
        }
    )
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert result.units == []
    assert any("central_idea_extraction_failed" in w for w in result.warnings)
    assert any("central_idea_extraction_failed:evidence_not_verbatim" in w for w in result.warnings)


def test_central_idea_replaces_retryable_warning(tmp_path) -> None:
    """Main extraction backend failure leaves a retryable warning; a successful
    central idea must replace it so the artifact pair stays fresh."""
    path = _write_article(tmp_path)

    class _FailingBackend(_FakeBackend):
        def complete(self, prompt: str) -> str:
            if "提取关键信息单元" in prompt:
                raise RuntimeError("LLM extraction error: backend down")
            return super().complete(prompt)

    backend = _FailingBackend(
        {
            "unit_type": "market_timing",
            "title": "对倒剧本",
            "thesis": "近期市场每天不管怎么震都是机构对倒的剧本。",
            "evidence": "每天不管怎么震都是对倒的剧本",
            "interpretation": "老师判断当前震荡为存量机构对倒，非新增散户交易。",
            "confidence": 0.82,
        }
    )
    result = _apprentice(tmp_path, backend).deep_read(path)

    assert len(result.units) == 1
    assert result.units[0].unit_type == "market_timing"
    assert not any("LLM extraction" in w for w in result.warnings)
    assert any("central_idea_extracted" in w for w in result.warnings)
