from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fin_analyse.consultation.instrument_identity import (
    INSTRUMENT_IDENTITY_AMBIGUOUS,
    INSTRUMENT_IDENTITY_COLLISION,
    INSTRUMENT_IDENTITY_MISMATCH,
    INSTRUMENT_IDENTITY_NAME_NORMALIZED,
    INSTRUMENT_IDENTITY_NAME_UNVERIFIED,
    INSTRUMENT_IDENTITY_UNRESOLVED,
    INSTRUMENT_IDENTITY_UNSUPPORTED,
    AShareConsultationInstrumentIdentityResolver,
)
from fin_analyse.guo_teacher_research.semantic_contract import (
    InstrumentRef,
    MultiAssetContext,
)
from fin_analyse.market.instrument_directory import AShareInstrumentDirectoryEntry
from fin_analyse.portfolio.actual_advisory import (
    ActualAdvisoryPortfolioRead,
    ActualAdvisoryPortfolioSnapshot,
    ActualAdvisoryPortfolioStatus,
    ActualAdvisoryPosition,
)


def _directory_value(value: str):
    entries = {
        "002409": {"ticker": "002409", "market": "深交所", "name": "雅克科技"},
        "雅克科技": {"ticker": "002409", "market": "深交所", "name": "雅克科技"},
        "600000": {"ticker": "600000", "market": "上交所", "name": "浦发银行"},
        "浦发银行": {"ticker": "600000", "market": "上交所", "name": "浦发银行"},
    }
    return entries.get(value)


class StaticDirectory:
    def lookup(self, value: str):
        raw = _directory_value(value)
        if raw is None:
            return ()
        venue = {"深交所": "SZ", "上交所": "SH"}[raw["market"]]
        return (
            AShareInstrumentDirectoryEntry(
                symbol=f"{raw['ticker']}.{venue}",
                name=raw["name"],
            ),
        )


class EmptyDirectory:
    def lookup(self, _value: str):
        return ()


def test_exact_bare_code_and_name_resolve_to_one_canonical_market_identity() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=StaticDirectory())

    (result,) = resolver.resolve_many((InstrumentRef(ticker="002409", name="雅克科技"),))

    assert result.status == "RESOLVED"
    assert result.source == "A_SHARE_DIRECTORY"
    assert result.market_symbol == "002409.SZ"
    assert result.semantic_ref == InstrumentRef(ticker="002409.SZ", name="雅克科技")
    assert result.data_gaps == ()


def test_canonical_symbol_is_preserved_but_wrong_venue_and_name_are_rejected() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=StaticDirectory())

    good, bad = resolver.resolve_many(
        (
            InstrumentRef(ticker="002409.SZ", name="雅克科技"),
            InstrumentRef(ticker="002409.SH", name="雅克科技"),
        )
    )

    assert good.market_symbol == "002409.SZ"
    assert bad.market_symbol is None
    assert bad.semantic_ref == InstrumentRef(ticker="002409.SH", name="雅克科技")
    assert bad.data_gaps == (INSTRUMENT_IDENTITY_MISMATCH,)


def test_unresolved_name_remains_semantic_context_but_never_becomes_market_identity() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=EmptyDirectory())

    (result,) = resolver.resolve_many((InstrumentRef(name="尚未收录公司"),))

    assert result.semantic_ref == InstrumentRef(name="尚未收录公司")
    assert result.market_symbol is None
    assert result.data_gaps == (INSTRUMENT_IDENTITY_UNRESOLVED,)


def test_conflicting_code_and_name_fail_closed_instead_of_selecting_one_side() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=StaticDirectory())

    (result,) = resolver.resolve_many((InstrumentRef(ticker="002409", name="浦发银行"),))

    assert result.market_symbol is None
    assert result.data_gaps == (INSTRUMENT_IDENTITY_MISMATCH,)


def test_verified_bare_code_remains_usable_when_optional_directory_is_missing() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=EmptyDirectory())

    (result,) = resolver.resolve_many((InstrumentRef(ticker="002409"),))

    assert result.status == "RESOLVED"
    assert result.source == "VERIFIED_CODE_FAMILY"
    assert result.market_symbol == "002409.SZ"
    assert result.semantic_ref == InstrumentRef(ticker="002409.SZ")
    assert result.data_gaps == ()


def test_authoritative_code_normalizes_unknown_supplied_name_without_mislabeling_g() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=StaticDirectory())

    (result,) = resolver.resolve_many((InstrumentRef(ticker="002409", name="雅克科技旧称"),))

    assert result.status == "RESOLVED"
    assert result.market_symbol == "002409.SZ"
    assert result.semantic_ref == InstrumentRef(ticker="002409.SZ", name="雅克科技")
    assert result.data_gaps == (INSTRUMENT_IDENTITY_NAME_NORMALIZED,)


def test_unverified_name_is_removed_when_only_canonical_market_identity_is_known() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=EmptyDirectory())

    (result,) = resolver.resolve_many((InstrumentRef(ticker="002409.SZ", name="未经验证的公司名"),))

    assert result.status == "RESOLVED"
    assert result.market_symbol == "002409.SZ"
    assert result.semantic_ref == InstrumentRef(ticker="002409.SZ")
    assert result.data_gaps == (INSTRUMENT_IDENTITY_NAME_UNVERIFIED,)


def test_duplicate_actual_names_are_ambiguous_and_account_numbers_never_leave_resolution() -> None:
    as_of = datetime(2026, 7, 23, tzinfo=UTC)
    positions = tuple(
        ActualAdvisoryPosition(
            symbol=symbol,
            name="同名公司",
            total_shares=shares,
            sellable_shares=shares,
            average_cost=Decimal("12.34"),
            snapshot_price=Decimal("13.57"),
            market_value=Decimal(shares) * Decimal("13.57"),
            market_value_derived=False,
            weight=Decimal("0.5"),
        )
        for symbol, shares in (("600000.SH", 100), ("000001.SZ", 200))
    )
    snapshot = ActualAdvisoryPortfolioSnapshot(
        schema_version="actual-advisory-portfolio.v1",
        source_kind="USER_CONFIRMED_MANUAL",
        account_alias="不得泄漏",
        as_of=as_of,
        valid_until=as_of + timedelta(hours=24),
        net_assets=Decimal("999999"),
        available_cash=Decimal("888888"),
        margin_debt=Decimal("777777"),
        positions=positions,
        content_hash="a" * 64,
        revision="sha256:" + "a" * 64,
    )

    class Reader:
        def read(self):
            return ActualAdvisoryPortfolioRead(
                status=ActualAdvisoryPortfolioStatus.READY,
                reason_codes=(),
                snapshot=snapshot,
            )

    resolver = AShareConsultationInstrumentIdentityResolver(
        directory=EmptyDirectory(),
        actual_reader=Reader(),
    )

    (result,) = resolver.resolve_many((InstrumentRef(name="同名公司"),))

    assert result.market_symbol is None
    assert result.data_gaps == (INSTRUMENT_IDENTITY_AMBIGUOUS,)
    rendered = repr(result)
    for secret in ("不得泄漏", "999999", "888888", "777777", "12.34", "13.57"):
        assert secret not in rendered


def test_unsupported_beijing_code_never_becomes_a_guessed_shanghai_symbol() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=EmptyDirectory())

    bare, canonical = resolver.resolve_many(
        (InstrumentRef(ticker="920000"), InstrumentRef(ticker="920000.BJ"))
    )

    for result in (bare, canonical):
        assert result.status == "UNSUPPORTED"
        assert result.market_symbol is None
        assert result.data_gaps == (INSTRUMENT_IDENTITY_UNSUPPORTED,)


def test_two_semantic_targets_resolving_to_one_symbol_are_marked_as_collisions() -> None:
    resolver = AShareConsultationInstrumentIdentityResolver(directory=StaticDirectory())
    original = (InstrumentRef(ticker="002409"), InstrumentRef(name="雅克科技"))

    by_code, by_name = resolver.resolve_many(original)

    for result in (by_code, by_name):
        assert result.status == "AMBIGUOUS"
        assert result.market_symbol is None
        assert result.data_gaps == (INSTRUMENT_IDENTITY_COLLISION,)
    assert (by_code.semantic_ref, by_name.semantic_ref) == original
    assert MultiAssetContext(targets=(by_code.semantic_ref, by_name.semantic_ref))
