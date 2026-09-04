"""Provider-level index alias lane tests (snapshot-index-support §2.1).

The lane lives at the read_market_snapshot entry only: alias hits reach the
on-demand reader as index symbols, while the shared equity resolver (and the
margin/external entries that reuse it) never see them.
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

_AS_OF = datetime(2026, 9, 4, 2, 0, tzinfo=UTC)


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
                session_phase="CONTINUOUS_AM",
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
            session_phase="CONTINUOUS_AM",
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
        question="大盘与持仓标的的行情线",
        instruments=instruments,
        article_id=None,
        as_of=_AS_OF,
        deadline_at=_AS_OF + timedelta(seconds=32),
    )


def test_index_alias_reaches_on_demand_reader(tmp_path: Path) -> None:
    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    result = provider.read_market_snapshot(_request(("科创50",)))

    assert reader.requests == [("000688.SH",)]
    assert result.value["instruments"][0]["symbol"] == "000688.SH"


def test_mixed_index_and_equity_preserves_both_lanes(tmp_path: Path) -> None:
    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    provider.read_market_snapshot(_request(("科创50", "601899.SH")))

    assert reader.requests == [("000688.SH", "601899.SH")]


def test_qualified_index_symbol_resolves_without_equity_resolver(tmp_path: Path) -> None:
    """限定符符号显式声明 venue：直接命中指数 lane，不进个股 venue 校验。"""

    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    provider.read_market_snapshot(_request(("000688.SH",)))

    assert reader.requests == [("000688.SH",)]


def test_bare_index_like_code_stays_equity(tmp_path: Path) -> None:
    """裸 000688 是国城矿业（SZ equity）：仍走 equity 裸码族解析，不进指数 lane。"""

    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    provider.read_market_snapshot(_request(("000688",)))

    assert reader.requests == [("000688.SZ",)]


def test_index_alias_does_not_leak_into_margin_entry(tmp_path: Path) -> None:
    """margin/external 复用共享解析器：别名不外溢（同输入在两处语义不同）。"""

    reader = _StubOnDemandReader()
    provider = _provider(tmp_path, reader)

    result = provider.read_margin_evidence(_request(("科创50",)))

    assert reader.requests == []
    assert "CONSULTATION_INSTRUMENT_IDENTITY_UNRESOLVED" in result.data_gaps
