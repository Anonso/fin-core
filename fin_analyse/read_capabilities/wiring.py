"""Reader wiring for the read-capability thin server.

Only what the seven v1 read tools need is constructed here.  The G reader is
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

from fin_analyse.guo_teacher_research.principal_binding import PrincipalBindingError
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
from fin_analyse.portfolio.user_watchlist import UserWatchlistError
from fin_analyse.portfolio.watchlist_state import require_production_watchlist_state
from fin_analyse.portfolio.watchlist_write_service import WatchlistWriteService
from fin_analyse.runtime.knowledge_root import KnowledgeRootConfigurationError
from fin_analyse.consultation.instrument_identity import (
    AShareConsultationInstrumentIdentityResolver,
)
from fin_analyse.market.instrument_directory import RuntimeAshareInstrumentDirectory

READ_TOOL_NAMES: tuple[str, ...] = (
    "read_g_context",
    "read_actual_portfolio",
    "read_market_snapshot",
    "read_market_overview",
    "read_margin_evidence",
    "read_ready_evidence",
    "read_user_watchlist",
)
WRITE_TOOL_NAMES: tuple[str, ...] = ("update_user_watchlist",)


class ReadToolRunner(Protocol):
    """One bounded FIN read capability exposed by the thin server."""

    def __call__(self, request: ProductionReadRequest) -> ProductionReadResult: ...


@dataclass(frozen=True, slots=True)
class ReaderWiring:
    """Per-tool runners plus the construction failures recorded at startup."""

    runners: dict[str, ReadToolRunner]
    unavailable_tools: tuple[tuple[str, str], ...]  # (tool, reason code)
    watchlist_write: WatchlistWriteService | None = None

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
    """Wire the seven v1 read tools from a validated knowledge root.

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

    # Watchlist 推导是 fail-closed（identity 缺失/坏权限 root 抛 RuntimeError 系），
    # 必须在此降级为单工具 unavailable——绝不能让 server 启动崩溃（设计门 F2）。
    user_watchlist = None
    user_watchlist_principal: str | None = None
    try:
        _, principal, user_watchlist = require_production_watchlist_state(
            environ=environment,
        )
        user_watchlist_principal = principal
    except (OSError, ValueError, UserWatchlistError, PrincipalBindingError) as exc:
        _stderr_note(f"user_watchlist construction failed: {type(exc).__name__}")

    watchlist_write: WatchlistWriteService | None = None
    if user_watchlist is not None and user_watchlist_principal is not None:
        try:
            directory = RuntimeAshareInstrumentDirectory(
                path=Path(knowledge_base_root) / "runtime" / "a_share_name_map.json"
            )
            watchlist_write = WatchlistWriteService(
                store=user_watchlist,
                resolver=AShareConsultationInstrumentIdentityResolver(
                    directory=directory
                ),
                directory=directory,
                principal_id=user_watchlist_principal,
                clock=effective_clock,
            )
        except (OSError, ValueError, KnowledgeRootConfigurationError) as exc:
            _stderr_note(f"watchlist_write construction failed: {type(exc).__name__}")

    unavailable: list[tuple[str, str]] = []

    provider: ProductionReadCapabilityProvider | None = None
    try:
        provider = ProductionReadCapabilityProvider(
            knowledge_base_root=knowledge_base_root,
            market_overview=market_overview,
            on_demand_tactical_context=on_demand,
            margin_evidence=margin,
            actual_portfolio=actual_portfolio,
            user_watchlist=user_watchlist,
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

    return ReaderWiring(
        runners=runners,
        unavailable_tools=tuple(unavailable),
        watchlist_write=watchlist_write,
    )
