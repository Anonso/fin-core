"""Index alias lane unit tests (snapshot-index-support design §2.1/§2.4)."""

from __future__ import annotations

import pytest

from fin_analyse.market.index_symbols import (
    INDEX_NAMES,
    MAJOR_INDEX_SYMBOLS,
    split_index_aliases,
)
from fin_analyse.market.on_demand_tactical_context import _FallbackDailyBarReader
from fin_analyse.market.qualified_daily_bars import QualifiedDailyBarReadRequest


class _RecordingReader:
    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._result = result
        self._error = error

    def read(self, request):
        self.calls.append(request.symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _request(symbol: str) -> QualifiedDailyBarReadRequest:
    from datetime import UTC, date, datetime

    return QualifiedDailyBarReadRequest(
        symbol=symbol,
        trade_date=date(2026, 9, 4),
        decision_cutoff_at=datetime(2026, 9, 4, 2, 0, tzinfo=UTC),
        minimum_completed_bars=1,
    )


class TestSplitIndexAliases:
    def test_name_and_qualified_symbol_hit(self) -> None:
        equity, names, ordered = split_index_aliases(("科创50", "000688.SH", "601899.SH"))
        assert equity == ("601899.SH",)
        assert ordered == ("000688.SH",)
        assert names == {"000688.SH": "科创50"}

    def test_bare_code_stays_equity(self) -> None:
        """裸六位码与个股真撞名（000688=国城矿业）：必须留给 equity lane。"""

        equity, names, ordered = split_index_aliases(("000688", "000001"))
        assert equity == ("000688", "000001")
        assert names == {}
        assert ordered == ()

    def test_g_jargon_alias_maps_to_kechuang50(self) -> None:
        _, names, ordered = split_index_aliases(("科学家50",))
        assert ordered == ("000688.SH",)
        assert names["000688.SH"] == "科创50"

    def test_duplicate_aliases_dedupe_by_symbol(self) -> None:
        """「上证指数」+「沪指」同一指数：按符号去重，不算未解析。"""

        equity, names, ordered = split_index_aliases(("上证指数", "沪指"))
        assert equity == ()
        assert ordered == ("000001.SH",)
        assert len(names) == 1

    def test_alias_table_covers_all_named_symbols(self) -> None:
        assert set(INDEX_NAMES) == MAJOR_INDEX_SYMBOLS
        for symbol in MAJOR_INDEX_SYMBOLS:
            assert INDEX_ALIASES_SELF_CONTAINED(symbol)

    def test_non_string_passes_through(self) -> None:
        equity, _, ordered = split_index_aliases((42, "科创50"))  # type: ignore[arg-type]
        assert equity == (42,)
        assert ordered == ("000688.SH",)


def INDEX_ALIASES_SELF_CONTAINED(symbol: str) -> bool:
    from fin_analyse.market.index_symbols import INDEX_ALIASES

    return INDEX_ALIASES.get(symbol) == symbol


class TestFallbackDailyBarReaderRouting:
    def test_index_symbol_bypasses_eastmoney_entirely(self) -> None:
        """指数直发腾讯：东财零尝试（东财指数 secid 空 data + opencli 分钟级烧预算）。"""

        eastmoney = _RecordingReader()
        tencent = _RecordingReader(result=object())
        reader = _FallbackDailyBarReader(primary=eastmoney, fallback=tencent)  # type: ignore[arg-type]

        result = reader.read(_request("000688.SH"))

        assert eastmoney.calls == []
        assert tencent.calls == ["000688.SH"]
        assert result is not None

    def test_index_tencent_failure_propagates_without_eastmoney_fallback(self) -> None:
        """绕过=真绕过：腾讯失败原样上抛，不落回东财（预算必杀不复活）。"""

        eastmoney = _RecordingReader(result=object())
        boom = RuntimeError("tencent down")
        tencent = _RecordingReader(error=boom)
        reader = _FallbackDailyBarReader(primary=eastmoney, fallback=tencent)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="tencent down"):
            reader.read(_request("000001.SH"))
        assert eastmoney.calls == []

    def test_equity_symbol_keeps_eastmoney_first(self) -> None:
        eastmoney = _RecordingReader(result=object())
        tencent = _RecordingReader()
        reader = _FallbackDailyBarReader(primary=eastmoney, fallback=tencent)  # type: ignore[arg-type]

        reader.read(_request("601899.SH"))

        assert eastmoney.calls == ["601899.SH"]
        assert tencent.calls == []

    def test_equity_primary_failure_still_falls_back(self) -> None:
        eastmoney = _RecordingReader(error=RuntimeError("eastmoney down"))
        tencent = _RecordingReader(result=object())
        reader = _FallbackDailyBarReader(primary=eastmoney, fallback=tencent)  # type: ignore[arg-type]

        result = reader.read(_request("601899.SH"))

        assert result is not None
        assert tencent.calls == ["601899.SH"]


class TestSharedEquityResolverUnaffected:
    def test_shared_helper_stays_pure_equity(self) -> None:
        """防外溢守护：共享解析器对指数别名仍走个股语义（不产指数符号）。"""

        from fin_analyse.guo_teacher_research.production_capability_provider import (
            _resolve_on_demand_instruments,
        )

        symbols, names, gaps = _resolve_on_demand_instruments(
            ("科创50", "000688"), resolver=None
        )
        # 「科创50」非裸码被跳过；000688 解为国城矿业（equity 语义不变）。
        assert "000688.SH" not in symbols
        assert symbols == ("000688.SZ",)
        # 「科创50」在共享 helper 语义下不可解析 → UNRESOLVED gap 如实上报，
        # 这正是别名 lane 只挂 snapshot 入口、不外溢的证据。
        assert gaps == ("CONSULTATION_INSTRUMENT_IDENTITY_UNRESOLVED",)
        assert names == {"000688.SZ": None}
