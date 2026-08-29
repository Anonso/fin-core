"""Temporal service — internal FIN temporal assessment interface.

Supported scope:
- g_source_article / priority_article for ZSXQ/星大派 article chains.
- market_snapshot / market_data_freshness.
- knowledge_query_window / knowledge_window.
- dynamic_claim / dynamics_decay.
"""

from fin_analyse.temporal.models import (
    TemporalAssessment,
    TemporalAssessmentRequest,
    TemporalItem,
    TemporalTaskContext,
)
from fin_analyse.temporal.service import (
    TemporalService,
    build_dynamic_claim_temporal_request,
    build_knowledge_window_temporal_request,
    build_market_snapshot_temporal_request,
    build_priority_article_temporal_request,
)

__all__ = [
    "TemporalAssessment",
    "TemporalAssessmentRequest",
    "TemporalItem",
    "TemporalTaskContext",
    "TemporalService",
    "build_dynamic_claim_temporal_request",
    "build_knowledge_window_temporal_request",
    "build_market_snapshot_temporal_request",
    "build_priority_article_temporal_request",
]
