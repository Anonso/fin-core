"""Neutral execution trace shared by research capability consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageTrace:
    """Per-stage execution trace for work and source audit trails."""

    stage: str
    status: str = "ok"  # ok | skipped | error | timeout | fallback
    source_type: str = ""
    source_ids: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    latency_ms: int = 0
    fallback_used: bool = False
    cache_hit: bool = False
    data_gaps: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "source_type": self.source_type,
            "source_ids": list(self.source_ids),
            "coverage": dict(self.coverage),
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "fallback_used": self.fallback_used,
            "cache_hit": self.cache_hit,
            "data_gaps": list(self.data_gaps),
            "error": self.error,
        }
