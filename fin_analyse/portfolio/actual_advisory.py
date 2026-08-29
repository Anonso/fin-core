"""User-confirmed actual portfolio context for advisory consultation only.

A brokerage screenshot does not establish an approved risk-capital limit or a
known margin balance, so those facts must be allowed to remain unknown rather
than being guessed merely to satisfy a trading-oriented schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from fin_analyse.common.owner_only_snapshot import (
    OwnerOnlyJsonSnapshotFile,
    OwnerOnlySnapshotInspectionError,
    OwnerOnlySnapshotInvalidError,
    OwnerOnlySnapshotMissingError,
    OwnerOnlySnapshotReason,
)

ACTUAL_ADVISORY_PORTFOLIO_SCHEMA = "actual-advisory-portfolio.v1"
ACTUAL_ADVISORY_PORTFOLIO_FRESHNESS = timedelta(hours=24)
ActualAdvisorySourceKind = Literal[
    "USER_CONFIRMED_BROKER_SCREENSHOT",
    "USER_CONFIRMED_MANUAL",
]

_RELATIVE_PATH = Path("fin-analyse/actual-advisory-portfolio.v1.json")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAX_BYTES = 64 * 1024
_DECIMAL_TEXT = re.compile(r"^(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,4})?$")
_REVISION_TEXT = re.compile(r"^sha256:([0-9a-f]{64})$")
_FIELDS = frozenset(
    {
        "schema_version",
        "confirmation",
        "source_kind",
        "positions_complete",
        "account_alias",
        "as_of",
        "net_assets",
        "available_cash",
        "margin_debt",
        "positions",
    }
)
_POSITION_FIELDS = frozenset(
    {
        "symbol",
        "name",
        "total_shares",
        "sellable_shares",
        "average_cost",
        "snapshot_price",
        "market_value",
        "thesis",
    }
)
# Snapshots written before the optional thesis field lack the key; reading
# them must keep working (thesis resolves to None).
_POSITION_FIELDS_LEGACY = _POSITION_FIELDS - {"thesis"}


class _SemanticValidationError(RuntimeError):
    def __init__(self, reason: ActualAdvisoryPortfolioReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ActualAdvisoryPortfolioStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class ActualAdvisoryPortfolioReason(StrEnum):
    MISSING = "ACTUAL_ADVISORY_PORTFOLIO_MISSING"
    INVALID = "ACTUAL_ADVISORY_PORTFOLIO_INVALID"
    FUTURE_AS_OF = "ACTUAL_ADVISORY_PORTFOLIO_FUTURE_AS_OF"
    STALE = "ACTUAL_ADVISORY_PORTFOLIO_STALE"
    NET_ASSETS_UNKNOWN = "ACTUAL_ADVISORY_NET_ASSETS_UNKNOWN"
    AVAILABLE_CASH_UNKNOWN = "ACTUAL_ADVISORY_AVAILABLE_CASH_UNKNOWN"
    MARGIN_DEBT_UNKNOWN = "ACTUAL_ADVISORY_MARGIN_DEBT_UNKNOWN"
    AVERAGE_COST_UNKNOWN = "ACTUAL_ADVISORY_AVERAGE_COST_UNKNOWN"
    SNAPSHOT_PRICE_UNKNOWN = "ACTUAL_ADVISORY_SNAPSHOT_PRICE_UNKNOWN"
    SUPPLIED_MARKET_VALUE_UNKNOWN = "ACTUAL_ADVISORY_SUPPLIED_MARKET_VALUE_UNKNOWN"
    POSITION_VALUATION_UNKNOWN = "ACTUAL_ADVISORY_POSITION_VALUATION_UNKNOWN"
    CHANGED = "ACTUAL_ADVISORY_PORTFOLIO_CHANGED"


@dataclass(frozen=True, slots=True)
class ActualAdvisoryPosition:
    symbol: str
    name: str
    total_shares: int
    sellable_shares: int | None
    average_cost: Decimal | None
    snapshot_price: Decimal | None
    market_value: Decimal | None
    market_value_derived: bool
    weight: Decimal | None
    thesis: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "total_shares": self.total_shares,
            "sellable_shares": self.sellable_shares,
            "average_cost": _decimal_text(self.average_cost),
            "snapshot_price": _decimal_text(self.snapshot_price),
            "market_value": _decimal_text(self.market_value),
            "market_value_derived": self.market_value_derived,
            "weight": _decimal_text(self.weight),
            "thesis": self.thesis,
        }


@dataclass(frozen=True, slots=True)
class ActualAdvisoryPortfolioSnapshot:
    schema_version: str
    source_kind: ActualAdvisorySourceKind
    account_alias: str
    as_of: datetime
    valid_until: datetime
    net_assets: Decimal | None
    available_cash: Decimal | None
    margin_debt: Decimal | None
    positions: tuple[ActualAdvisoryPosition, ...]
    content_hash: str
    revision: str

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "confirmation": "USER_CONFIRMED",
            "source_kind": self.source_kind,
            "positions_complete": True,
            "account_alias": self.account_alias,
            "as_of": self.as_of.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "net_assets": _decimal_text(self.net_assets),
            "available_cash": _decimal_text(self.available_cash),
            "margin_debt": _decimal_text(self.margin_debt),
            "margin_debt_status": "KNOWN" if self.margin_debt is not None else "UNKNOWN",
            "positions": [position.to_safe_dict() for position in self.positions],
        }


@dataclass(frozen=True, slots=True)
class ActualAdvisoryPortfolioRead:
    status: ActualAdvisoryPortfolioStatus
    reason_codes: tuple[ActualAdvisoryPortfolioReason, ...]
    snapshot: ActualAdvisoryPortfolioSnapshot | None


@dataclass(frozen=True, slots=True)
class ActualAdvisoryPortfolioPublicationRequest:
    source: Path
    candidate_revision: str
    expected_current_revision: str
    apply: bool


@dataclass(frozen=True, slots=True)
class ActualAdvisoryPortfolioPublicationResult:
    status: str
    reason_codes: tuple[str, ...]
    candidate_status: Literal["READY", "PARTIAL"] | None
    candidate_revision: str | None
    current_revision: str
    preview: dict[str, Any] | None
    confirmation_required: bool
    writes_state: bool


@dataclass(frozen=True, slots=True)
class _ParsedActualAdvisoryCandidate:
    snapshot: ActualAdvisoryPortfolioSnapshot
    reasons: tuple[ActualAdvisoryPortfolioReason, ...]

    @property
    def status(self) -> Literal["READY", "PARTIAL"]:
        return "PARTIAL" if self.reasons else "READY"


class ActualAdvisoryPortfolioStore:
    """Read the fixed owner-only advisory snapshot without repairing it."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        source = os.environ if environ is None else environ
        configured_home = source.get("XDG_CONFIG_HOME")
        config_home = (
            Path(configured_home) if configured_home is not None else Path.home() / ".config"
        )
        self._path = config_home / _RELATIVE_PATH
        self._clock = clock
        self._file = OwnerOnlyJsonSnapshotFile(
            target=self._path,
            forbidden_root=_PROJECT_ROOT,
            max_bytes=_MAX_BYTES,
        )

    def read(self) -> ActualAdvisoryPortfolioRead:
        try:
            payload = self._file.read()
        except OwnerOnlySnapshotMissingError:
            return _unknown(ActualAdvisoryPortfolioReason.MISSING)
        except (OwnerOnlySnapshotInvalidError, OSError):
            return _unknown(ActualAdvisoryPortfolioReason.INVALID)
        try:
            snapshot, reasons = _parse(payload, now=self._clock())
        except _SemanticValidationError as error:
            return _unknown(error.reason)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return _unknown(ActualAdvisoryPortfolioReason.INVALID)
        return ActualAdvisoryPortfolioRead(
            status=(
                ActualAdvisoryPortfolioStatus.PARTIAL
                if reasons
                else ActualAdvisoryPortfolioStatus.READY
            ),
            reason_codes=reasons,
            snapshot=snapshot,
        )


class ActualAdvisoryPortfolioPublicationOperator:
    """Preview and CAS-publish one local, explicitly confirmed snapshot."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = ActualAdvisoryPortfolioStore(environ=environ, clock=clock)
        self._file = self._store._file
        self._clock = clock

    def preview(self, source: Path) -> ActualAdvisoryPortfolioPublicationResult:
        try:
            inspection = self._file.inspect(
                source=source,
                decode_candidate=lambda payload: _parse_candidate(payload, now=self._clock()),
                decode_current=lambda payload: _parse_candidate(payload, now=self._clock()),
            )
        except _SemanticValidationError as error:
            return _rejected(error.reason.value)
        except OwnerOnlySnapshotInspectionError as error:
            return _rejected(_publication_reason(error.reason))
        return _publication_result(
            "PREVIEW",
            inspection.candidate.value,
            inspection.current.value if inspection.current is not None else None,
            writes_state=False,
        )

    def publish(
        self,
        request: ActualAdvisoryPortfolioPublicationRequest,
    ) -> ActualAdvisoryPortfolioPublicationResult:
        outcome = self._file.publish(
            source=request.source,
            candidate_revision=request.candidate_revision,
            expected_current_revision=request.expected_current_revision,
            apply=request.apply,
            decode_candidate=lambda payload: _parse_candidate(payload, now=self._clock()),
            decode_current=lambda payload: _parse_candidate(payload, now=self._clock()),
        )
        if outcome.status == "REJECTED":
            assert outcome.reason is not None
            return _rejected(
                _publication_reason(outcome.reason),
                candidate_revision=(
                    outcome.candidate.revision if outcome.candidate is not None else None
                ),
                current_revision=(
                    outcome.current.revision
                    if outcome.current is not None
                    else "MISSING"
                    if outcome.reason
                    in {
                        OwnerOnlySnapshotReason.CANDIDATE_MISMATCH,
                        OwnerOnlySnapshotReason.CAS_MISMATCH,
                        OwnerOnlySnapshotReason.INCOMPATIBLE,
                    }
                    else "UNKNOWN"
                ),
                writes_state=outcome.writes_state,
            )
        if outcome.candidate is None:
            return _rejected(
                "ACTUAL_ADVISORY_PUBLICATION_WRITE_FAILED",
                writes_state=outcome.writes_state,
            )
        return _publication_result(
            outcome.status,
            outcome.candidate.value,
            outcome.current.value if outcome.current is not None else None,
            writes_state=outcome.writes_state,
        )


def actual_advisory_snapshot_ref(revision: str) -> str:
    """Project one stable, non-secret public reference for a snapshot revision."""

    match = _REVISION_TEXT.fullmatch(revision)
    if match is None:
        raise ValueError("invalid actual advisory snapshot revision")
    return f"actual-advisory-snapshot-{match.group(1)[:16]}"


def _parse_candidate(payload: bytes, *, now: datetime) -> _ParsedActualAdvisoryCandidate:
    snapshot, reasons = _parse(payload, now=now)
    return _ParsedActualAdvisoryCandidate(snapshot=snapshot, reasons=reasons)


def _parse(
    payload: bytes, *, now: datetime
) -> tuple[ActualAdvisoryPortfolioSnapshot, tuple[ActualAdvisoryPortfolioReason, ...]]:
    raw = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(raw, dict):
        raise ValueError
    value = cast(dict[str, Any], raw)
    if (
        set(value) != _FIELDS
        or value.get("schema_version") != ACTUAL_ADVISORY_PORTFOLIO_SCHEMA
        or value.get("confirmation") != "USER_CONFIRMED"
        or value.get("source_kind")
        not in {"USER_CONFIRMED_BROKER_SCREENSHOT", "USER_CONFIRMED_MANUAL"}
        or value.get("positions_complete") is not True
    ):
        raise ValueError
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError
    as_of = datetime.fromisoformat(_text(value["as_of"]))
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError
    if as_of > now:
        raise _SemanticValidationError(ActualAdvisoryPortfolioReason.FUTURE_AS_OF)

    net_assets = _optional_decimal(value["net_assets"])
    available_cash = _optional_decimal(value["available_cash"])
    margin_debt = _optional_decimal(value["margin_debt"])
    positions_raw = value["positions"]
    if not isinstance(positions_raw, list) or len(positions_raw) > 128:
        raise ValueError
    raw_positions = tuple(_raw_position(item) for item in positions_raw)
    symbols = tuple(position["symbol"] for position in raw_positions)
    if len(set(symbols)) != len(symbols):
        raise ValueError
    if net_assets is not None and net_assets == 0 and raw_positions:
        raise ValueError
    if (
        available_cash is not None
        and net_assets is not None
        and margin_debt is not None
        and available_cash > net_assets + margin_debt
    ):
        raise ValueError

    reasons: list[ActualAdvisoryPortfolioReason] = []
    valid_until = as_of + ACTUAL_ADVISORY_PORTFOLIO_FRESHNESS
    if now >= valid_until:
        reasons.append(ActualAdvisoryPortfolioReason.STALE)
    if net_assets is None:
        reasons.append(ActualAdvisoryPortfolioReason.NET_ASSETS_UNKNOWN)
    if available_cash is None:
        reasons.append(ActualAdvisoryPortfolioReason.AVAILABLE_CASH_UNKNOWN)
    if margin_debt is None:
        reasons.append(ActualAdvisoryPortfolioReason.MARGIN_DEBT_UNKNOWN)

    positions: list[ActualAdvisoryPosition] = []
    for raw_position in raw_positions:
        sellable = cast(int | None, raw_position["sellable_shares"])
        quantity = cast(int, raw_position["total_shares"])
        average_cost = cast(Decimal | None, raw_position["average_cost"])
        if average_cost is None:
            reasons.append(ActualAdvisoryPortfolioReason.AVERAGE_COST_UNKNOWN)
        price = cast(Decimal | None, raw_position["snapshot_price"])
        if price is None:
            reasons.append(ActualAdvisoryPortfolioReason.SNAPSHOT_PRICE_UNKNOWN)
        supplied_value = cast(Decimal | None, raw_position["market_value"])
        effective_value = supplied_value
        derived = False
        if effective_value is None and price is not None:
            effective_value = price * quantity
            derived = True
        if effective_value is None:
            # 用户拍板 2026-08-21：份额×快照价可推导市值时不算"缺失"；
            # 只有份额与价格都缺、市值确实无法估值时才报缺失/未知。
            if supplied_value is None:
                reasons.append(ActualAdvisoryPortfolioReason.SUPPLIED_MARKET_VALUE_UNKNOWN)
            reasons.append(ActualAdvisoryPortfolioReason.POSITION_VALUATION_UNKNOWN)
        if supplied_value is not None and price is not None:
            expected_value = price * quantity
            tolerance = max(Decimal("0.05"), supplied_value * Decimal("0.002"))
            if abs(supplied_value - expected_value) > tolerance:
                raise ValueError
        weight = (
            effective_value / net_assets
            if effective_value is not None and net_assets is not None and net_assets > 0
            else None
        )
        positions.append(
            ActualAdvisoryPosition(
                symbol=cast(str, raw_position["symbol"]),
                name=cast(str, raw_position["name"]),
                total_shares=quantity,
                sellable_shares=sellable,
                average_cost=average_cost,
                snapshot_price=price,
                market_value=effective_value,
                market_value_derived=derived,
                weight=weight,
                thesis=cast(str | None, raw_position["thesis"]),
            )
        )
    if (
        net_assets is not None
        and available_cash is not None
        and all(position.market_value is not None for position in positions)
    ):
        explained_assets = available_cash + sum(
            (cast(Decimal, position.market_value) for position in positions),
            start=Decimal("0"),
        )
        expected_floor = net_assets
        expected_assets = net_assets + margin_debt if margin_debt is not None else expected_floor
        tolerance = max(Decimal("5.00"), expected_assets * Decimal("0.005"))
        if explained_assets + tolerance < expected_floor or (
            margin_debt is not None and abs(explained_assets - expected_assets) > tolerance
        ):
            raise ValueError
    content_hash = hashlib.sha256(payload).hexdigest()
    snapshot = ActualAdvisoryPortfolioSnapshot(
        schema_version=ACTUAL_ADVISORY_PORTFOLIO_SCHEMA,
        source_kind=cast(ActualAdvisorySourceKind, value["source_kind"]),
        account_alias=_text(value["account_alias"]),
        as_of=as_of,
        valid_until=valid_until,
        net_assets=net_assets,
        available_cash=available_cash,
        margin_debt=margin_debt,
        positions=tuple(positions),
        content_hash=content_hash,
        revision=f"sha256:{content_hash}",
    )
    return snapshot, tuple(dict.fromkeys(reasons))


def _raw_position(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError
    value = cast(dict[str, Any], raw)
    if set(value) not in (_POSITION_FIELDS, _POSITION_FIELDS_LEGACY):
        raise ValueError
    symbol = _text(value["symbol"])
    if len(symbol) != 9 or symbol[:6].isdigit() is False or symbol[6:] not in {".SH", ".SZ", ".BJ"}:
        raise ValueError
    total_shares = _shares(value["total_shares"])
    if total_shares <= 0:
        raise ValueError
    sellable = _optional_shares(value["sellable_shares"])
    if sellable is not None and sellable > total_shares:
        raise ValueError
    thesis_raw = value.get("thesis")
    thesis = None
    if thesis_raw is not None:
        if (
            not isinstance(thesis_raw, str)
            or not thesis_raw
            or thesis_raw != thesis_raw.strip()
            or len(thesis_raw) > 2_000
        ):
            raise ValueError
        thesis = thesis_raw
    return {
        "symbol": symbol,
        "name": _text(value["name"]),
        "total_shares": total_shares,
        "sellable_shares": sellable,
        "average_cost": _optional_decimal(value["average_cost"]),
        "snapshot_price": _optional_decimal(value["snapshot_price"]),
        "market_value": _optional_decimal(value["market_value"]),
        "thesis": thesis,
    }


def _publication_result(
    status: str,
    candidate: _ParsedActualAdvisoryCandidate,
    current: _ParsedActualAdvisoryCandidate | None,
    *,
    writes_state: bool,
) -> ActualAdvisoryPortfolioPublicationResult:
    return ActualAdvisoryPortfolioPublicationResult(
        status=status,
        reason_codes=tuple(reason.value for reason in candidate.reasons),
        candidate_status=candidate.status,
        candidate_revision=candidate.snapshot.revision,
        current_revision="MISSING" if current is None else current.snapshot.revision,
        preview=candidate.snapshot.to_safe_dict(),
        confirmation_required=status == "PREVIEW",
        writes_state=writes_state,
    )


def _rejected(
    reason: str,
    *,
    candidate_revision: str | None = None,
    current_revision: str = "UNKNOWN",
    writes_state: bool = False,
) -> ActualAdvisoryPortfolioPublicationResult:
    return ActualAdvisoryPortfolioPublicationResult(
        status="REJECTED",
        reason_codes=(reason,),
        candidate_status=None,
        candidate_revision=candidate_revision,
        current_revision=current_revision,
        preview=None,
        confirmation_required=False,
        writes_state=writes_state,
    )


def _publication_reason(reason: OwnerOnlySnapshotReason) -> str:
    return f"ACTUAL_ADVISORY_PUBLICATION_{reason.value}"


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > 128
    ):
        raise ValueError
    return value


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
        raise ValueError
    try:
        parsed = Decimal(value)
    except Exception as error:
        raise ValueError from error
    exponent = parsed.as_tuple().exponent
    if not parsed.is_finite() or parsed < 0 or not isinstance(exponent, int) or exponent < -4:
        raise ValueError
    return parsed


def _shares(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _optional_shares(value: object) -> int | None:
    return None if value is None else _shares(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"unsupported JSON constant: {value}")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _unknown(reason: ActualAdvisoryPortfolioReason) -> ActualAdvisoryPortfolioRead:
    return ActualAdvisoryPortfolioRead(
        status=ActualAdvisoryPortfolioStatus.UNKNOWN,
        reason_codes=(reason,),
        snapshot=None,
    )


__all__ = [
    "ACTUAL_ADVISORY_PORTFOLIO_FRESHNESS",
    "ACTUAL_ADVISORY_PORTFOLIO_SCHEMA",
    "ActualAdvisoryPortfolioPublicationOperator",
    "ActualAdvisoryPortfolioPublicationRequest",
    "ActualAdvisoryPortfolioPublicationResult",
    "ActualAdvisoryPortfolioRead",
    "ActualAdvisoryPortfolioReason",
    "ActualAdvisoryPortfolioSnapshot",
    "ActualAdvisoryPortfolioStatus",
    "ActualAdvisoryPortfolioStore",
    "ActualAdvisoryPosition",
    "ActualAdvisorySourceKind",
    "actual_advisory_snapshot_ref",
]
