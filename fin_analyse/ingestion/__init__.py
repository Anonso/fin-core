"""Ingestion primitives for source adapters."""

from .adapters import SourceAdapter
from .models import (
    DocumentVersion,
    Evidence,
    IngestionRun,
    ParseArtifact,
    RawDocument,
    SourceInfo,
)
from .runtime_health import (
    IngestionRuntimeHealthRequest,
    IngestionRuntimeHealthResult,
    IngestionRuntimeHealthService,
)

__all__ = [
    "DocumentVersion",
    "Evidence",
    "IngestionRun",
    "IngestionRuntimeHealthRequest",
    "IngestionRuntimeHealthResult",
    "IngestionRuntimeHealthService",
    "ParseArtifact",
    "RawDocument",
    "SourceAdapter",
    "SourceInfo",
]
