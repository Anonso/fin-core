"""3.3b：问句滑窗 + compact 关键词面打分的相关性回归。"""

from __future__ import annotations

import hashlib
import json

from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextProvider,
    AgentRuntimeContextRequest,
    _apply_budget,
    _build_intent_tokens,
    _candidate_relevance_score,
)


class _Req:
    def __init__(self, question: str) -> None:
        self.question = question
        self.tickers = []
        self.ticker = None
        self.company = None
        self.positions = []
        self.topic = None


def test_question_shingle_fallback_when_no_structured_tokens() -> None:
    tokens = _build_intent_tokens(_Req("封测行业点评"))
    assert "封测" in tokens["topics"]


def test_compact_keyword_surface_scores_containment() -> None:
    tokens = _build_intent_tokens(_Req("封测行业点评"))
    candidate = {
        "title": "星大派特刊：科技半导体三大板块",
        "_enriched_keywords": [
            "先进封装（2.5D/3D、HBM、Chiplet、CPO）成为真正瓶颈",
            "封测是中游平台型环节",
        ],
    }
    assert _candidate_relevance_score(candidate, tokens) > 0


def test_latest_focus_phrasing_does_not_fall_back_to_shingles() -> None:
    """回归：broad 概览问句不被 2 字滑窗灌入噪音，latest-focus 语义保持。"""
    tokens = _build_intent_tokens(_Req("最近关注什么变化？"))
    assert tokens["topics"] == set()


def _entry(
    bucket: str,
    *,
    title: str,
    published_at: str,
    enriched_keywords: tuple[str, ...] = (),
) -> dict:
    entry = {
        "source_bucket": bucket,
        "article_id": f"{bucket}-{title}",
        "title": title,
        "published_at": published_at,
    }
    if enriched_keywords:
        entry["_enriched_keywords"] = list(enriched_keywords)
    return entry


def test_budget_question_matched_fresh_outranks_weaker_reference_and_fresh() -> None:
    """装配层：问句命中的 fresh 特刊与 reference 按相关性竞争，不再无条件被占位。"""
    tokens = _build_intent_tokens(_Req("封测行业点评"))
    candidates = [
        {
            "source_bucket": "pinned_source",
            "pinned_id": "pin-1",
            "title": "置顶",
            "published_at": "2026-08-27T00:00:00+08:00",
        },
        {
            "source_bucket": "latest_commentary",
            "article_id": "c-1",
            "title": "星大派锐评",
            "published_at": "2026-09-01T00:00:00+08:00",
        },
        _entry(
            "fresh_g",
            title="星大派特刊：科技半导体三大板块",
            published_at="2026-08-13T00:00:00+08:00",
            enriched_keywords=("封测是中游平台型环节",),
        ),
        _entry(
            "fresh_g",
            title="星大派特刊：近期小作文",
            published_at="2026-08-31T00:00:00+08:00",
        ),
        _entry(
            "recent_reference",
            title="固态电池中试线落地",
            published_at="2026-09-02T00:00:00+08:00",
        ),
    ]

    selected, _ = _apply_budget(candidates, max_events=4, intent_tokens=tokens)

    assert [c.get("article_id") or c.get("pinned_id") for c in selected] == [
        "pin-1",
        "c-1",
        "fresh_g-星大派特刊：科技半导体三大板块",
        "recent_reference-固态电池中试线落地",
    ]


def test_budget_reference_wins_tie_over_generic_fresh() -> None:
    """装配层：同等相关性时 reference 仍优先于泛 fresh（保留原巷道意图）。"""
    tokens = _build_intent_tokens(_Req("封测行业点评"))
    candidates = [
        _entry(
            "recent_reference",
            title="固态电池中试线落地",
            published_at="2026-09-02T00:00:00+08:00",
        ),
        _entry(
            "fresh_g",
            title="星大派特刊：近期小作文",
            published_at="2026-08-31T00:00:00+08:00",
        ),
    ]

    selected, _ = _apply_budget(candidates, max_events=1, intent_tokens=tokens)

    assert [c["article_id"] for c in selected] == ["recent_reference-固态电池中试线落地"]


def test_budget_never_displaces_pinned_or_latest_commentary() -> None:
    """装配层：pinned/commentary 冻结优先级不被 flexible 竞争破坏。"""
    tokens = _build_intent_tokens(_Req("封测行业点评"))
    candidates = [
        _entry(
            "fresh_g",
            title="星大派特刊：科技半导体三大板块",
            published_at="2026-08-13T00:00:00+08:00",
            enriched_keywords=("封测是中游平台型环节",),
        ),
        {
            "source_bucket": "latest_commentary",
            "article_id": "c-1",
            "title": "星大派锐评",
            "published_at": "2026-09-01T00:00:00+08:00",
        },
        {
            "source_bucket": "pinned_source",
            "pinned_id": "pin-1",
            "title": "置顶",
            "published_at": "2026-08-27T00:00:00+08:00",
        },
    ]

    selected, _ = _apply_budget(candidates, max_events=2, intent_tokens=tokens)

    assert [c.get("article_id") or c.get("pinned_id") for c in selected] == ["pin-1", "c-1"]


def _write_compact_deep_read(kb_root, article_id: str, article_path, theses: list) -> None:
    content_hash = hashlib.sha256(article_path.read_bytes()).hexdigest()
    common = {
        "artifact_version": "deep_read_artifact_v1",
        "article_id": article_id,
        "content_hash": content_hash,
        "pipeline_version": "1.0.0",
        "generated_at": "2026-08-13T12:00:00+08:00",
        "generation_id": "generation-3.3b",
    }
    root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    full_path = root / "full" / f"{article_id}.json"
    compact_path = root / "compact" / f"{article_id}.json"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(
        json.dumps({**common, "detail": "full", "payload": {"source": {}, "units": []}}),
        encoding="utf-8",
    )
    compact_path.write_text(
        json.dumps(
            {
                **common,
                "detail": "compact",
                "payload": {
                    "article_id": article_id,
                    "title": "星大派特刊：科技半导体三大板块",
                    "core_theses": theses,
                    "injectable_summary": "封测是中游平台型环节。"
                    + "。".join(t["thesis"] for t in theses),
                    "theme_clusters": [],
                    "suggestions": [],
                    "unit_count": len(theses),
                },
            }
        ),
        encoding="utf-8",
    )


def test_resolve_injects_question_matched_special_over_newer_generic(tmp_path) -> None:
    """端到端回归：条目化候选保留 `_enriched_*`，装配预算按问句相关性保留 8/13 类特刊。"""
    kb_root = tmp_path / "knowledge-base"
    (kb_root / "articles").mkdir(parents=True)
    article_id = "special-813"
    article_path = kb_root / "articles" / f"{article_id}.md"
    article_path.write_text(
        f"---\nid: {article_id}\ndate: 2026-08-13 10:00\ncolumn: 星大派特刊\n---\n\n# 半导体封测\n\n正文。",
        encoding="utf-8",
    )
    _write_compact_deep_read(
        kb_root,
        article_id,
        article_path,
        [
            {
                "title": "先进封装平台",
                "thesis": "封测从低附加值环节转向平台型瓶颈。",
                "confidence": 0.8,
            }
        ],
    )
    candidates = (
        {
            "article_id": article_id,
            "title": "星大派特刊：科技半导体三大板块",
            "column": "星大派特刊",
            "source_classification": "teacher_original",
            "published_at": "2026-08-13T10:00:00+08:00",
            "persona_eligible": True,
            "theme_clusters": ["半导体"],
            "guidance_brief": "老师原文背景，只作认知参考。",
        },
        {
            "article_id": "special-new",
            "title": "星大派特刊：近期小作文",
            "column": "星大派特刊",
            "source_classification": "teacher_original",
            "published_at": "2026-08-31T10:00:00+08:00",
            "persona_eligible": True,
            "theme_clusters": ["题材"],
            "guidance_brief": "老师原文背景，只作认知参考。",
        },
    )

    result = AgentRuntimeContextProvider(
        kb_root=kb_root,
        pinned_sources=(),
        knowledge_documents=[],
        fresh_g_candidates=candidates,
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="封测行业点评",
            now="2026-09-02T21:30",
        )
    )

    source_refs = [entry.get("source_ref") for entry in result.llm_context["g_context"]]
    assert "special-813" in source_refs
