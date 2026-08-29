"""Cross-article cognitive synthesis for Xingdapai article analysis."""

from fin_analyse.cognition.cross_article.good_question_judge import LlmGoodQuestionJudge
from fin_analyse.cognition.cross_article.models import (
    ArticleRef,
    ClusterAnalysis,
    ClusterInfo,
    CrossArticleSynthesisResponse,
    DegradationEvent,
    IngestionResult,
    ModelPolicy,
    QualityFlags,
    SelectionResult,
    SuggestedSignalQuery,
    SynthesisReport,
    build_suggested_signal_queries,
    validate_no_trade_fields,
)

__all__ = [
    "ArticleRef",
    "ClusterAnalysis",
    "ClusterInfo",
    "CrossArticleSynthesisResponse",
    "DegradationEvent",
    "IngestionResult",
    "LlmGoodQuestionJudge",
    "ModelPolicy",
    "QualityFlags",
    "SelectionResult",
    "SuggestedSignalQuery",
    "SynthesisReport",
    "build_suggested_signal_queries",
    "validate_no_trade_fields",
]
