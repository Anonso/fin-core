"""Reader wiring for the read-capability thin server.

Only what the six v1 read tools need is constructed here.  The G reader is
built by the provider's ``__init__`` from ``kb_root`` (its default
construction path); this module never rebuilds it.  Each reader failure is
kept individually: a missing knowledge root fails closed (startup error),
while a single reader that cannot construct degrades to a permanently
``*_unavailable`` tool (design §6 two-level asymmetry, recorded on stderr).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fin_analyse.guo_teacher_research.production_capability_provider import (
    ProductionReadCapabilityProvider,
)
from fin_analyse.guo_teacher_research.production_capability_provider import (
    ProductionReadRequest,
    ProductionReadResult,
)
from fin_analyse.margin.evidence import build_default_margin_evidence
from fin_analyse.market.current_overview import build_default_a_share_market_overview
from fin_analyse.market.on_demand_tactical_context import (
    build_default_on_demand_tactical_context,
)
from fin_analyse.portfolio.actual_advisory import ActualAdvisoryPortfolioStore

READ_TOOL_NAMES: tuple[str, ...] = (
    "read_g_context",
    "read_actual_portfolio",
    "read_market_snapshot",
    "read_market_overview",
    "read_margin_evidence",
    "read_ready_evidence",
)


class ReadToolRunner(Protocol):
    """One bounded FIN read capability exposed by the thin server."""

    def __call__(self, request: ProductionReadRequest) -> ProductionReadResult: ...


@dataclass(frozen=True, slots=True)
class ReaderWiring:
    """Per-tool runners plus the construction failures recorded at startup."""

    runners: dict[str, ReadToolRunner]
    unavailable_tools: tuple[tuple[str, str], ...]  # (tool, reason code)

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self.runners)


def _stderr_note(message: str) -> None:
    print(f"[read-capability] {message}", file=sys.stderr)


def build_reader_wiring(
    knowledge_base_root: str | Path,
    *,
    environ: dict[str, str] | None = None,
    clock=None,
) -> ReaderWiring:
    """Wire the six v1 read tools from a validated knowledge root.

    ``knowledge_base_root`` must already be validated (fail-closed happens in
    the server preflight, mirroring ``mcp_server``).  Reader-level failures
    are per-tool: the tool stays registered but reports ``*_unavailable``.
    """

    def _now() -> datetime:
        return datetime.now(UTC)

    effective_clock = clock or _now
    environment = environ

    # Independent reader constructions, each isolated per design §6.
    market_overview = None
    try:
        market_overview = build_default_a_share_market_overview(
            clock=effective_clock,
        )
    except (OSError, ValueError) as exc:
        _stderr_note(f"market_overview construction failed: {type(exc).__name__}")

    on_demand = None
    try:
        on_demand = build_default_on_demand_tactical_context(
            environ=environment,
            clock=effective_clock,
        )
    except (OSError, ValueError) as exc:
        _stderr_note(f"on_demand construction failed: {type(exc).__name__}")

    margin = None
    try:
        margin = build_default_margin_evidence(
            environ=environment,
            clock=effective_clock,
        )
    except (OSError, ValueError) as exc:
        _stderr_note(f"margin construction failed: {type(exc).__name__}")

    actual_portfolio = None
    try:
        actual_portfolio = ActualAdvisoryPortfolioStore(
            environ=environment,
            clock=effective_clock,
        )
    except (OSError, ValueError) as exc:
        _stderr_note(f"actual_portfolio construction failed: {type(exc).__name__}")

    unavailable: list[tuple[str, str]] = []

    provider: ProductionReadCapabilityProvider | None = None
    try:
        provider = ProductionReadCapabilityProvider(
            knowledge_base_root=knowledge_base_root,
            market_overview=market_overview,
            on_demand_tactical_context=on_demand,
            margin_evidence=margin,
            actual_portfolio=actual_portfolio,
            clock=effective_clock,
        )
    except (OSError, ValueError) as exc:
        # Provider construction covers the G reader and the default
        # ready-evidence reader; without it those tools are unavailable.
        _stderr_note(f"provider construction failed: {type(exc).__name__}: {exc}")

    runners: dict[str, ReadToolRunner] = {}
    if provider is not None:
        for tool in READ_TOOL_NAMES:
            runners[tool] = getattr(provider, tool)
    else:
        unavailable.extend(
            (tool, "read_provider_unavailable") for tool in READ_TOOL_NAMES
        )

    return ReaderWiring(runners=runners, unavailable_tools=tuple(unavailable))
