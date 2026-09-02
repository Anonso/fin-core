"""3.3b：问句滑窗 + compact 关键词面打分的相关性回归。"""

from __future__ import annotations

from fin_analyse.guo_teacher_research.runtime_context import (
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
