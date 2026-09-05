"""Provider-level board alias lane tests (board-index-support §2.2, archived).

Third split at the read_market_snapshot entry: board aliases → index aliases →
shared equity resolver. Board hits carry their Chinese display name into the
projection (agent sees 「液冷」, never a bare 02GN2224.PT), and the lane does
not leak into the margin/external entries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fin_analyse.guo_teacher_research.production_capability_provider import (
    ProductionReadCapabilityProvider,
)
from fin_analyse.market.on_demand_tactical_context import (
    OnDemandTacticalContext,
    OnDemandTacticalContextRequest,
    TacticalInstrumentContext,
)
from fin_analyse.read_capabilities.types import ProductionReadRequest

_AS_OF = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)


class _StubOnDemandReader:
    """Records request symbols; echoes them back as UNKNOWN contexts."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    def read(self, request: OnDemandTacticalContextRequest) -> OnDemandTacticalContext:
        self.requests.append(request.instruments)
        instruments = tuple(
            TacticalInstrumentContext(
                symbol=symbol,
                status="UNKNOWN",
                evidence_id=f"market-evidence-stub-{symbol}",
                quote_price=None,
                quote_price_role="NONE",
                quote_disagreement_ratio=None,
                quote_facts=(),
                quote_observed_at=None,
                session_phase="AFTER_CLOSE",
                reference_only=True,
                manual_review_eligible=False,
                latest_completed_bar_date=None,
                completed_bar_count=0,
                technical_facts={},
                provider_provenance=(),
                data_gaps=("STUB",),
            )
            for symbol in request.instruments
        )
        return OnDemandTacticalContext(
            status="UNKNOWN",
            as_of=request.as_of,
            valid_until=request.as_of + timedelta(seconds=15),
            instruments=instruments,
            session_phase="AFTER_CLOSE",
        )

    def refresh_quotes(self, *args, **kwargs):  # pragma: no cover - protocol stub
        raise NotImplementedError


def _provider(tmp_path: Path, reader: _StubOnDemandReader) -> ProductionReadCapabilityProvider:
    return ProductionReadCapabilityProvider(
        knowledge_base_root=tmp_path,
        on_demand_tactical_context=reader,
    )


def _request(instruments: tuple[str, ...]) -> ProductionReadRequest:
    return ProductionReadRequest(
        question="板块线层行情",
        instruments=instruments,
        article_id=None,
        as_of=_AS_OF,
        deadline_at=_AS_OF + timedelta(seconds=32),
    )


def test_board_alias_reaches_on_demand_reader_with_display_name(tmp_path: Path) -> None:
    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    result = provider.read_market_snapshot(_request(("液冷",)))

    assert reader.requests == [("02GN2224.PT",)]
    assert result.value["instruments"][0]["symbol"] == "02GN2224.PT"
    assert result.value["instruments"][0]["name"] == "液冷"


def test_board_qualified_symbol_passes_through_directly(tmp_path: Path) -> None:
    """规范符号直传命中板块 lane（Q1-P2），不落 equity 解析器误报 UNRESOLVED。"""

    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    provider.read_market_snapshot(_request(("02GN2224.PT",)))

    assert reader.requests == [("02GN2224.PT",)]


def test_mixed_board_index_equity_splits_three_ways_in_order(tmp_path: Path) -> None:
    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    provider.read_market_snapshot(_request(("601899.SH", "液冷", "科创50")))

    assert reader.requests == [("02GN2224.PT", "000688.SH", "601899.SH")]


def test_board_alias_does_not_leak_into_margin_entry(tmp_path: Path) -> None:
    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    result = provider.read_margin_evidence(_request(("液冷",)))

    assert reader.requests == []
    assert "CONSULTATION_INSTRUMENT_IDENTITY_UNRESOLVED" in result.data_gaps


def test_bare_board_code_stays_out_of_board_lane(tmp_path: Path) -> None:
    """裸 02GN2224 无 .PT 后缀：不进板块 lane，走 equity 解析器诚实失败。"""

    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    result = provider.read_market_snapshot(_request(("02GN2224",)))

    assert reader.requests == []
    assert "CONSULTATION_INSTRUMENT_IDENTITY_UNRESOLVED" in result.data_gaps
