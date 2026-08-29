"""Bounded cache-only external research evidence projection.

This module is a FIN-owned evidence seam.  It can read previously collected
external research from an injected cache, but it cannot start an LLM, invoke a
tool, call a live provider, or write research state.  Codex remains the only
generic Agent loop.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fin_analyse.researcher.stage_trace import StageTrace

_OBSERVATION_MAX_AGE_SECONDS = 86400
_EXTERNAL_RESEARCH_CACHE_TTL = 86400


def _observations_dir() -> Path:
    """Resolve the legacy observations dir via the production knowledge-root seam."""
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    return default_knowledge_base_root() / "runtime" / "research_observations"

_EXTERNAL_RESEARCH_COVERAGE_CONTRACT = {
    "evidence_role": "external_context",
    "quality_tier": "quick_scan",
    "can_support_agent_view": False,
    "can_raise_confidence": False,
    "can_drive_recommendation": False,
}


def read_recent_observations() -> list[dict[str, Any]]:
    """Read legacy local observations for engineering health display only.

    M4 no longer writes these files.  Existing records remain readable until
    production cutover evidence confirms that the legacy state can be retired.
    """

    observations_dir = _observations_dir()
    if not observations_dir.exists():
        return []
    cutoff = time.time() - _OBSERVATION_MAX_AGE_SECONDS
    observations: list[dict[str, Any]] = []
    for path in sorted(observations_dir.glob("obs-*.json"), reverse=True):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            observation = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(observation, dict):
            observations.append(observation)
        if len(observations) >= 20:
            break
    return observations


@dataclass
class ExternalResearchContextRequest:
    """Semantic lookup key for cached non-G external evidence."""

    company: str = ""
    ticker: str = ""
    question: str = ""
    dimensions: list[str] = field(default_factory=list)
    provider_health: Any | None = None


@dataclass
class ExternalResearchContextResult:
    """Sanitized projection of cached external evidence."""

    status: str = "skipped"
    summary: str = ""
    short_answer: str = ""
    executive_summary: str = ""
    confidence: float = 0.0
    data_gaps: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(
        default_factory=lambda: dict(_EXTERNAL_RESEARCH_COVERAGE_CONTRACT)
    )
    cache_hit: bool = False
    latency_ms: int = 0
    fallback_used: bool = False
    error: str | None = None
    caveats: list[str] = field(default_factory=list)
    provider_degradation: dict[str, Any] = field(default_factory=dict)

    def to_stage_trace(self) -> StageTrace:
        """Project evidence without allowing cache payloads to relax boundaries."""

        from fin_analyse.researcher.stage_trace import StageTrace

        trace = StageTrace(stage="external_research", status=self.status, source_type="web")
        trace.coverage = dict(_EXTERNAL_RESEARCH_COVERAGE_CONTRACT)
        if self.provider_degradation:
            trace.coverage["provider_degradation"] = dict(self.provider_degradation)
        trace.confidence = self.confidence
        trace.cache_hit = self.cache_hit
        trace.latency_ms = self.latency_ms
        trace.fallback_used = self.fallback_used
        trace.error = self.error or ""
        trace.data_gaps.extend(dict.fromkeys(self.data_gaps))
        return trace


class ExternalResearchContextService:
    """Read a bounded cached evidence entry; never perform live research."""

    def __init__(
        self,
        *,
        cache: Mapping[str, object] | None = None,
        cache_ttl_seconds: float = _EXTERNAL_RESEARCH_CACHE_TTL,
    ) -> None:
        if cache_ttl_seconds <= 0:
            raise ValueError("external_research_cache_ttl_invalid")
        self._cache = cache if cache is not None else {}
        self._cache_ttl = float(cache_ttl_seconds)

    def cache_key_for(self, request: ExternalResearchContextRequest) -> str:
        """Return the deterministic same-day key for one semantic lookup."""

        payload = {
            "company": request.company.strip(),
            "ticker": request.ticker.strip(),
            "question": request.question.strip(),
            "dimensions": [item.strip() for item in request.dimensions if item.strip()],
            "day": datetime.now(UTC).strftime("%Y-%m-%d"),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return f"external-research-evidence:{digest}"

    def collect(
        self,
        request: ExternalResearchContextRequest,
    ) -> ExternalResearchContextResult:
        """Read and sanitize one cache entry, failing closed on every miss."""

        if not isinstance(request, ExternalResearchContextRequest):
            raise ValueError("external_research_context_request_invalid")
        started = time.monotonic()
        result = ExternalResearchContextResult(
            summary=request.question.strip() or f"分析{request.company.strip()}",
        )
        entry = self._cache.get(self.cache_key_for(request))
        if entry is None:
            result.data_gaps.append("external_research_cache_missing")
            return self._finish(result, request, started)
        if not isinstance(entry, tuple) or len(entry) != 2:
            result.data_gaps.append("external_research_cache_invalid")
            return self._finish(result, request, started)
        cached_at, payload = entry
        if not isinstance(cached_at, (int, float)):
            result.data_gaps.append("external_research_cache_invalid")
            return self._finish(result, request, started)
        if time.monotonic() - float(cached_at) >= self._cache_ttl:
            result.data_gaps.append("external_research_cache_expired")
            return self._finish(result, request, started)
        if not isinstance(payload, Mapping):
            result.data_gaps.append("external_research_cache_invalid")
            return self._finish(result, request, started)

        short_answer = _bounded_text(payload.get("short_answer"), limit=6000)
        executive_summary = _bounded_text(payload.get("executive_summary"), limit=12000)
        if not short_answer and not executive_summary:
            result.data_gaps.append("external_research_cache_invalid")
            return self._finish(result, request, started)

        result.status = "ok"
        result.cache_hit = True
        result.short_answer = short_answer
        result.executive_summary = executive_summary
        result.confidence = _bounded_confidence(payload.get("confidence"))
        result.caveats = _bounded_text_list(payload.get("caveats"), item_limit=500, count=20)
        result.data_gaps.extend(
            _bounded_text_list(payload.get("data_gaps"), item_limit=200, count=50)
        )
        return self._finish(result, request, started)

    @staticmethod
    def _finish(
        result: ExternalResearchContextResult,
        request: ExternalResearchContextRequest,
        started: float,
    ) -> ExternalResearchContextResult:
        if request.provider_health is not None:
            from fin_analyse.runtime.provider_degradation_policy import (
                ProviderDegradationPolicy,
            )

            decision = ProviderDegradationPolicy.evaluate(
                request.provider_health,
                consumer="external_research_context",
            )
            result.provider_degradation = decision.to_dict()
            result.data_gaps.extend(
                gap for gap in decision.data_gaps if gap not in result.data_gaps
            )
        result.coverage = dict(_EXTERNAL_RESEARCH_COVERAGE_CONTRACT)
        result.latency_ms = max(0, int((time.monotonic() - started) * 1000))
        return result


def _bounded_text(value: object, *, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _bounded_text_list(value: object, *, item_limit: int, count: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for item in value[:count]:
        text = _bounded_text(item, limit=item_limit)
        if text and text not in items:
            items.append(text)
    return items


def _bounded_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))
