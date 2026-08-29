"""Platform-agnostic conversation DTOs for cognition entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fin_analyse.context.models import ContextRequestScope


@dataclass(frozen=True)
class ConversationRequest:
    """A platform-neutral request from QQ, Feishu, CLI, MCP, or future channels."""

    message_id: str
    text: str
    scope: ContextRequestScope = field(default_factory=ContextRequestScope)
    teacher_id: str = "guo"
    company: str | None = None
    ticker: str | None = None

    def to_metadata(self, *, context_type: str, request_id: str) -> dict[str, Any]:
        """Return audit metadata shared by conversation, analysis, and feedback records."""
        return {
            "context_type": context_type,
            "platform": self.scope.platform,
            "tenant_id": self.scope.tenant_id,
            "user_id": self.scope.user_id,
            "conversation_id": self.scope.conversation_id,
            "visibility": self.scope.visibility,
            "message_id": self.message_id,
            "request_id": request_id,
            "company": self.company,
            "ticker": self.ticker,
            "teacher_id": self.teacher_id,
        }


@dataclass(frozen=True)
class ConversationResponse:
    """Structured response before platform-specific formatting and truncation."""

    text: str
    analysis_id: str | None
    confidence: float | None
    warnings: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    analysis: dict[str, Any] | None = None
    research_package: dict[str, Any] | None = None
