"""Resolve user-facing A-share references into canonical market identities.

Names remain useful semantic context for G, while market/evidence readers need
an exchange-qualified symbol.  This module owns that distinction so neither
Hermes nor downstream market adapters have to guess a venue.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.market.instrument_directory import (
    AShareInstrumentDirectory,
    AShareInstrumentDirectoryEntry,
    RuntimeAshareInstrumentDirectory,
    verified_a_share_equity_venue,
)
from fin_analyse.portfolio.actual_advisory import ActualAdvisoryPortfolioRead

_CANONICAL_SYMBOL = re.compile(r"^(?P<code>[0-9]{6})\.(?P<venue>SH|SZ|BJ)$")
_BARE_CODE = re.compile(r"^[0-9]{6}$")

INSTRUMENT_IDENTITY_UNRESOLVED = "CONSULTATION_INSTRUMENT_IDENTITY_UNRESOLVED"
INSTRUMENT_IDENTITY_AMBIGUOUS = "CONSULTATION_INSTRUMENT_IDENTITY_AMBIGUOUS"
INSTRUMENT_IDENTITY_MISMATCH = "CONSULTATION_INSTRUMENT_IDENTITY_MISMATCH"
INSTRUMENT_IDENTITY_UNSUPPORTED = "CONSULTATION_INSTRUMENT_IDENTITY_UNSUPPORTED"
INSTRUMENT_IDENTITY_COLLISION = "CONSULTATION_INSTRUMENT_IDENTITY_COLLISION"
INSTRUMENT_IDENTITY_NAME_NORMALIZED = "CONSULTATION_INSTRUMENT_NAME_NORMALIZED"
INSTRUMENT_IDENTITY_NAME_UNVERIFIED = "CONSULTATION_INSTRUMENT_NAME_UNVERIFIED"

InstrumentIdentityStatus = Literal[
    "RESOLVED",
    "UNRESOLVED",
    "AMBIGUOUS",
    "MISMATCH",
    "UNSUPPORTED",
]
InstrumentIdentitySource = Literal[
    "CANONICAL_INPUT",
    "VERIFIED_CODE_FAMILY",
    "A_SHARE_DIRECTORY",
    "ACTUAL_PORTFOLIO_IDENTITY",
]


class _ActualPortfolioReader(Protocol):
    def read(self) -> ActualAdvisoryPortfolioRead: ...


@dataclass(frozen=True, slots=True)
class ConsultationInstrumentIdentity:
    """One resolved semantic reference and its optional market identity."""

    status: InstrumentIdentityStatus
    semantic_ref: InstrumentRef
    market_symbol: str | None
    source: InstrumentIdentitySource | None = None
    data_gaps: tuple[str, ...] = ()


class ConsultationInstrumentIdentityResolver(Protocol):
    def resolve_many(
        self,
        targets: Sequence[InstrumentRef],
    ) -> tuple[ConsultationInstrumentIdentity, ...]: ...


@dataclass(frozen=True, slots=True)
class _IdentityEntry:
    symbol: str
    name: str
    source: InstrumentIdentitySource


class AShareConsultationInstrumentIdentityResolver:
    """Exact, read-only identity resolution for consultation targets.

    The shared A-share directory is primary.  A user-confirmed actual
    portfolio may supply an identity-only fallback, but its quantities,
    prices, cash and account metadata never leave this module.
    """

    def __init__(
        self,
        *,
        directory: AShareInstrumentDirectory | None = None,
        actual_reader: _ActualPortfolioReader | None = None,
    ) -> None:
        self._directory = directory or RuntimeAshareInstrumentDirectory()
        self._actual_reader = actual_reader

    def resolve_many(
        self,
        targets: Sequence[InstrumentRef],
    ) -> tuple[ConsultationInstrumentIdentity, ...]:
        actual_entries: tuple[_IdentityEntry, ...] | None = None

        def fallback_entries() -> tuple[_IdentityEntry, ...]:
            nonlocal actual_entries
            if actual_entries is None:
                actual_entries = self._read_actual_identity_entries()
            return actual_entries

        resolved = [self._resolve_one(target, fallback_entries) for target in targets]
        symbols: dict[str, list[int]] = {}
        for index, item in enumerate(resolved):
            if item.market_symbol is not None:
                symbols.setdefault(item.market_symbol, []).append(index)
        for indexes in symbols.values():
            if len(indexes) < 2:
                continue
            for index in indexes:
                resolved[index] = replace(
                    resolved[index],
                    status="AMBIGUOUS",
                    semantic_ref=targets[index],
                    market_symbol=None,
                    source=None,
                    data_gaps=(INSTRUMENT_IDENTITY_COLLISION,),
                )
        return tuple(resolved)

    def _resolve_one(
        self,
        target: InstrumentRef,
        fallback_entries: Callable[[], tuple[_IdentityEntry, ...]],
    ) -> ConsultationInstrumentIdentity:
        ticker = target.ticker
        name = target.name
        canonical = _canonical(ticker)
        code = canonical[:6] if canonical is not None else ticker
        verified_venue: str | None = None
        if ticker is not None and (not isinstance(code, str) or _BARE_CODE.fullmatch(code) is None):
            return _unresolved(
                target,
                status="UNSUPPORTED",
                gap=INSTRUMENT_IDENTITY_UNSUPPORTED,
            )
        if isinstance(code, str):
            verified_venue = verified_a_share_equity_venue(code)
            if verified_venue is None:
                return _unresolved(
                    target,
                    status="UNSUPPORTED",
                    gap=INSTRUMENT_IDENTITY_UNSUPPORTED,
                )
            if canonical is not None and not canonical.endswith(f".{verified_venue}"):
                return _unresolved(
                    target,
                    status="MISMATCH",
                    gap=INSTRUMENT_IDENTITY_MISMATCH,
                )
        market_symbol = canonical
        if market_symbol is None and isinstance(code, str) and verified_venue is not None:
            market_symbol = f"{code}.{verified_venue}"

        ticker_entries = self._directory_candidates(ticker)
        name_entries = self._directory_candidates(name)
        if (ticker is not None and not ticker_entries) or (name is not None and not name_entries):
            actual = fallback_entries()
            if ticker is not None:
                ticker_entries = _merge_entries(
                    ticker_entries,
                    _actual_candidates(actual, ticker=ticker),
                )
            if name is not None:
                name_entries = _merge_entries(
                    name_entries,
                    _actual_candidates(actual, name=name),
                )

        if market_symbol is not None:
            if (
                ticker_entries and market_symbol not in {entry.symbol for entry in ticker_entries}
            ) or (name_entries and market_symbol not in {entry.symbol for entry in name_entries}):
                return _unresolved(
                    target,
                    status="MISMATCH",
                    gap=INSTRUMENT_IDENTITY_MISMATCH,
                )
            matched = _entry_for_symbol(
                (*ticker_entries, *name_entries),
                market_symbol,
            )
            semantic_name = name
            gaps: tuple[str, ...] = ()
            if name is not None and matched is None:
                semantic_name = None
                gaps = (INSTRUMENT_IDENTITY_NAME_UNVERIFIED,)
            elif (
                name is not None
                and matched is not None
                and name.casefold() != matched.name.casefold()
            ):
                semantic_name = matched.name
                gaps = (INSTRUMENT_IDENTITY_NAME_NORMALIZED,)
            elif name is None and matched is not None:
                semantic_name = matched.name
            return ConsultationInstrumentIdentity(
                status="RESOLVED",
                semantic_ref=InstrumentRef(
                    ticker=market_symbol,
                    name=semantic_name,
                ),
                market_symbol=market_symbol,
                source=(
                    matched.source
                    if matched is not None
                    else ("CANONICAL_INPUT" if canonical is not None else "VERIFIED_CODE_FAMILY")
                ),
                data_gaps=gaps,
            )

        name_symbols = {entry.symbol for entry in name_entries}
        if not name_symbols:
            return _unresolved(
                target,
                status="UNRESOLVED",
                gap=INSTRUMENT_IDENTITY_UNRESOLVED,
            )
        if len(name_symbols) != 1:
            return _unresolved(
                target,
                status="AMBIGUOUS",
                gap=INSTRUMENT_IDENTITY_AMBIGUOUS,
            )

        symbol = next(iter(name_symbols))
        matched = _entry_for_symbol((*ticker_entries, *name_entries), symbol)
        assert matched is not None
        return ConsultationInstrumentIdentity(
            status="RESOLVED",
            semantic_ref=InstrumentRef(
                ticker=symbol,
                name=name or matched.name,
            ),
            market_symbol=symbol,
            source=matched.source,
        )

    def _directory_candidates(self, value: str | None) -> tuple[_IdentityEntry, ...]:
        if value is None:
            return ()
        query = value[:6] if _canonical(value) is not None else value
        try:
            entries = self._directory.lookup(query)
        except Exception:
            return ()
        return tuple(
            _IdentityEntry(
                symbol=entry.symbol,
                name=entry.name,
                source="A_SHARE_DIRECTORY",
            )
            for entry in entries
            if isinstance(entry, AShareInstrumentDirectoryEntry)
        )

    def _read_actual_identity_entries(self) -> tuple[_IdentityEntry, ...]:
        if self._actual_reader is None:
            return ()
        try:
            result = self._actual_reader.read()
        except Exception:
            return ()
        snapshot = result.snapshot
        if snapshot is None:
            return ()
        entries: list[_IdentityEntry] = []
        for position in snapshot.positions:
            symbol = _canonical(position.symbol)
            name = position.name.strip()
            if (
                symbol is not None
                and name
                and verified_a_share_equity_venue(symbol[:6]) == symbol[7:]
            ):
                entries.append(
                    _IdentityEntry(
                        symbol=symbol,
                        name=name,
                        source="ACTUAL_PORTFOLIO_IDENTITY",
                    )
                )
        return tuple(entries)


def _actual_candidates(
    entries: tuple[_IdentityEntry, ...],
    *,
    ticker: str | None = None,
    name: str | None = None,
) -> tuple[_IdentityEntry, ...]:
    if ticker is not None:
        canonical = _canonical(ticker)
        code = canonical[:6] if canonical is not None else ticker
        return tuple(
            entry
            for entry in entries
            if entry.symbol == canonical
            or (_BARE_CODE.fullmatch(code) and entry.symbol[:6] == code)
        )
    assert name is not None
    normalized = name.strip().casefold()
    return tuple(entry for entry in entries if entry.name.casefold() == normalized)


def _merge_entries(
    left: tuple[_IdentityEntry, ...],
    right: tuple[_IdentityEntry, ...],
) -> tuple[_IdentityEntry, ...]:
    distinct: dict[tuple[str, str], _IdentityEntry] = {
        (entry.symbol, entry.name): entry for entry in (*left, *right)
    }
    return tuple(distinct.values())


def _entry_for_symbol(
    entries: Sequence[_IdentityEntry],
    symbol: str,
) -> _IdentityEntry | None:
    return next((entry for entry in entries if entry.symbol == symbol), None)


def _canonical(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized if _CANONICAL_SYMBOL.fullmatch(normalized) is not None else None


def _unresolved(
    target: InstrumentRef,
    *,
    status: Literal["UNRESOLVED", "AMBIGUOUS", "MISMATCH", "UNSUPPORTED"],
    gap: str,
) -> ConsultationInstrumentIdentity:
    return ConsultationInstrumentIdentity(
        status=status,
        semantic_ref=target,
        market_symbol=None,
        data_gaps=(gap,),
    )


__all__ = [
    "AShareConsultationInstrumentIdentityResolver",
    "ConsultationInstrumentIdentity",
    "ConsultationInstrumentIdentityResolver",
    "INSTRUMENT_IDENTITY_AMBIGUOUS",
    "INSTRUMENT_IDENTITY_COLLISION",
    "INSTRUMENT_IDENTITY_MISMATCH",
    "INSTRUMENT_IDENTITY_NAME_NORMALIZED",
    "INSTRUMENT_IDENTITY_NAME_UNVERIFIED",
    "INSTRUMENT_IDENTITY_UNRESOLVED",
    "INSTRUMENT_IDENTITY_UNSUPPORTED",
]
