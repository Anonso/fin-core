"""Actual-advisory portfolio review: two-operation deep module.

review_full_replacement(observed_facts, interaction) -> ReviewResult
confirm_latest_review(confirmation, interaction) -> ConfirmResult

The model submits only observed facts.  Identity, confirmation, revision,
and token are injected by the transport adapter, never authored by the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fin_analyse.market.instrument_directory import (
    RuntimeAshareInstrumentDirectory,
    verified_a_share_equity_venue,
)
from fin_analyse.portfolio.actual_advisory import (
    ACTUAL_ADVISORY_PORTFOLIO_SCHEMA,
    ActualAdvisoryPortfolioPublicationOperator,
    ActualAdvisoryPortfolioPublicationRequest,
    actual_advisory_snapshot_ref,
)
from fin_analyse.portfolio.pending_review_store import PendingReviewStore
from fin_analyse.portfolio.portfolio_review_types import (
    BoundInteraction,
    BoundUserConfirmation,
    ConfirmResult,
    ConfirmStatus,
    ObservedPortfolioFacts,
    PendingReview,
    ReviewResult,
    ReviewStatus,
)

# Single source of truth for the user-facing confirmation phrase. The FIN
# response field and the readable preview must both derive from this constant;
# the Hermes bridge keeps its own copy matched by a cross-end drift test.
_CONFIRMATION_PHRASE = "确认更新持仓"


def _digest(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _identity_digest(principal_id: str, interaction: BoundInteraction) -> str:
    return _digest(
        "\x00".join(
            (
                principal_id,
                interaction.profile_name,
                interaction.platform,
                interaction.session_key,
                interaction.subject_kind,
                interaction.subject_id,
            )
        )
    )


def _money_text(value: Decimal) -> str:
    return format(value, "f")


def _invalid_decimal(value: Decimal) -> bool:
    return not value.is_finite() or value < 0


def _financial_contradiction_question(
    facts: ObservedPortfolioFacts,
    normalized_positions: list[dict[str, Any]],
) -> str | None:
    """Mirror the owner _parse financial checks (actual_advisory.py:370-449).

    Returns a precise Chinese question with concrete values when the submitted
    facts would be REJECTED by the owner for arithmetic contradiction, so the
    user can fix them in one round instead of a hard reject. Strictly mirrors
    the owner: never asks about data the owner would accept (e.g. cash+MV
    above net without margin), and skips the whole total-assets reconciliation
    when any position lacks an effective valuation (owner PARTIALs then).
    Parse/grammar anomalies are left to the owner (REJECTED -> Chinese display).
    """
    cash = facts.cash_available
    net = facts.net_assets
    margin = facts.margin_debt

    # Check 1: available_cash > net_assets + margin_debt (all three known).
    if cash is not None and net is not None and margin is not None:
        try:
            cash_d = Decimal(str(cash))
            net_d = Decimal(str(net))
            margin_d = Decimal(str(margin))
        except Exception:
            return None
        if (
            _invalid_decimal(cash_d)
            or _invalid_decimal(net_d)
            or _invalid_decimal(margin_d)
        ):
            return None
        if cash_d > net_d + margin_d:
            diff = cash_d - (net_d + margin_d)
            return (
                f"提交的资金数据存在矛盾：可用资金 {_money_text(cash_d)} "
                f"大于净资产 {_money_text(net_d)} 与两融负债 {_money_text(margin_d)} "
                f"之和（差额 {_money_text(diff)}）。"
                f"是否同时将净资产增加 {_money_text(diff)}？确认后请重新提交持仓。"
            )

    # Check 2: total-assets reconciliation (owner :434-449). Requires every
    # position to have an effective market value (supplied, or price*shares).
    if cash is None or net is None:
        return None
    try:
        cash_d = Decimal(str(cash))
        net_d = Decimal(str(net))
    except Exception:
        return None
    if _invalid_decimal(cash_d) or _invalid_decimal(net_d):
        return None
    margin_2: Decimal | None = None
    if margin is not None:
        try:
            margin_2 = Decimal(str(margin))
        except Exception:
            return None
        if _invalid_decimal(margin_2):
            return None

    position_values: list[Decimal] = []
    for position in normalized_positions:
        supplied = position.get("market_value")
        price = position.get("snapshot_price")
        shares = position.get("total_shares")
        try:
            if supplied is not None:
                value = Decimal(str(supplied))
            elif price is not None and shares is not None:
                value = Decimal(str(price)) * Decimal(str(shares))
            else:
                # Owner would PARTIAL, not reject — skip the reconciliation.
                return None
        except Exception:
            return None
        if _invalid_decimal(value):
            return None
        position_values.append(value)

    if not position_values:
        return None

    explained = cash_d + sum(position_values, start=Decimal("0"))
    expected_floor = net_d
    expected_assets = net_d + margin_2 if margin_2 is not None else expected_floor
    tolerance = max(Decimal("5.00"), expected_assets * Decimal("0.005"))
    too_low = explained + tolerance < expected_floor
    too_high = margin_2 is not None and abs(explained - expected_assets) > tolerance
    if not (too_low or too_high):
        return None
    diff = explained - expected_floor
    if too_high and diff > 0 and margin_2 is not None:
        # Owner verifies explained == net + margin. To pass, the user must set
        # net' = explained - margin; suggesting net + diff (= explained) would
        # fail again because the target moves with net.
        suggested_net = explained - margin_2
        return (
            f"提交的资金数据存在矛盾：可用资金 {_money_text(cash_d)} 与持仓市值合计 "
            f"{_money_text(explained - cash_d)} 之和为 {_money_text(explained)}，"
            f"大于净资产 {_money_text(net_d)}（及两融负债 {_money_text(margin_2)} 之和 "
            f"{_money_text(expected_assets)}），差额 {_money_text(diff)}。"
            f"若新增资金已计入可用资金，请同时将净资产增加至 "
            f"{_money_text(suggested_net)}；否则请核对可用资金数值。"
            f"确认后请重新提交持仓。"
        )
    return (
        f"提交的资金数据存在矛盾：可用资金 {_money_text(cash_d)} 与持仓市值合计 "
        f"{_money_text(explained - cash_d)} 之和为 {_money_text(explained)}，"
        f"小于净资产 {_money_text(net_d)}（差额 {_money_text(expected_floor - explained)}）。"
        f"请核对持仓与资金数值是否完整。确认后请重新提交持仓。"
    )


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}")


def _resolve_observed_at(
    observed_at: str, fallback: datetime
) -> tuple[datetime, str]:
    """Parse observed_at and return (datetime, source_quality).

    Source quality:
    - EXACT: user provided a full timezone-aware timestamp (not in the future)
    - SYSTEM: empty, partial, unparseable, or future — using system current time

    Never asks the user for a date; falls back to system time silently.
    """
    value = (observed_at or "").strip()
    if not value:
        return fallback, "SYSTEM"
    if _TIME_ONLY_RE.match(value) and not _DATE_RE.match(value):
        # Time-only: user stated a time but no date — use system date
        return fallback, "SYSTEM"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None or dt.utcoffset() is None:
            # Naive datetime: user stated but no timezone
            return fallback, "SYSTEM"
        if dt > fallback:
            # Future date: reject (likely wrong year or stale screenshot)
            return fallback, "SYSTEM"
        return dt, "EXACT"
    except (ValueError, TypeError):
        return fallback, "SYSTEM"


# ---------------------------------------------------------------------------
# Instrument normalization
# ---------------------------------------------------------------------------

_VENUES = {"SH", "SZ", "BJ"}


def _canonical_instrument(instrument: str) -> tuple[str | None, str | None, str | None]:
    """Normalize instrument to canonical form.

    Returns (canonical_code, stock_name, error_message).
    canonical_code is None if resolution failed.
    """
    normalized = instrument.strip().upper()
    if not normalized:
        return None, None, "empty instrument"

    # Already has venue suffix
    if len(normalized) == 9 and normalized[6] == "." and normalized[7:] in _VENUES:
        code = normalized[:6]
        venue = normalized[7:]
        verified = verified_a_share_equity_venue(code)
        if verified is not None:
            return f"{code}.{verified}", _directory_display_name(code), None
        # BJ not in verified directory
        if venue == "BJ":
            return normalized, None, None
        return None, None, f"unverified code {normalized}"

    # Plain 6-digit code: verify venue, then look up display name
    if len(normalized) == 6 and normalized.isdigit():
        verified = verified_a_share_equity_venue(normalized)
        if verified is None:
            return None, None, f"unverified code {normalized}"
        return f"{normalized}.{verified}", _directory_display_name(normalized), None

    # Try name lookup via instrument directory
    try:
        directory = RuntimeAshareInstrumentDirectory()
        entries = directory.lookup(instrument.strip())
        if len(entries) == 1:
            return entries[0].symbol, entries[0].name, None
        if len(entries) > 1:
            candidates = ", ".join(e.symbol for e in entries[:5])
            return None, None, (
                f"ambiguous name '{instrument}' matches multiple stocks: "
                f"{candidates}。请使用股票代码。"
            )
    except Exception:
        pass

    return None, None, f"unrecognized instrument: {instrument}"


def _directory_display_name(code: str) -> str | None:
    """Resolve the directory's authoritative display name for a bare code.

    The directory indexes bare six-digit codes only; a venue-suffixed
    query silently returns nothing, so always look up the bare form.
    """
    try:
        entries = RuntimeAshareInstrumentDirectory().lookup(code)
        if entries:
            return entries[0].name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Numeric normalization
# ---------------------------------------------------------------------------


def _normalize_optional_float(value: float | Decimal | None) -> str | None:
    if value is None:
        return None
    d = Decimal(str(value))
    return format(d, "f")


def _normalize_required_int(value: int) -> int:
    if value < 0:
        raise ValueError(f"negative shares: {value}")
    return value


def _normalize_optional_int(value: int | None) -> int | None:
    """None stays None (typed unknown); non-None must be a non-negative int."""

    if value is None:
        return None
    if value < 0:
        raise ValueError(f"negative sellable shares: {value}")
    return value


def _derive_net_assets(
    facts: ObservedPortfolioFacts,
    positions: list[dict[str, Any]],
) -> Decimal | None:
    """Derive net assets only when every arithmetic input is known."""

    if facts.cash_available is None or facts.margin_debt is None:
        return None
    try:
        total = Decimal(str(facts.cash_available)) - Decimal(str(facts.margin_debt))
        for position in positions:
            value = position.get("market_value")
            if value is None:
                price = position.get("snapshot_price")
                shares = position.get("total_shares")
                if price is None or shares is None:
                    return None
                value = Decimal(str(price)) * Decimal(str(shares))
            total += Decimal(str(value))
    except Exception:
        return None
    return None if _invalid_decimal(total) else total


# ---------------------------------------------------------------------------
# Preview construction
# ---------------------------------------------------------------------------


def _build_readable_preview(
    snapshot: dict[str, Any],
    ignored: list[str],
    corrected: list[str],
) -> str:
    lines = ["持仓预览\n"]

    as_of = snapshot.get("as_of", "未知")
    lines.append(f"时间: {as_of}")

    net_assets = snapshot.get("net_assets")
    if net_assets is not None:
        lines.append(f"总资产: {net_assets}")
    positions = snapshot.get("positions", [])
    total_market_value = _preview_total_market_value(positions)
    if total_market_value is not None:
        lines.append(f"总市值: {format(total_market_value, 'f')}")
    available_cash = snapshot.get("available_cash")
    if available_cash is not None:
        lines.append(f"可用资金: {available_cash}")
    if total_market_value is not None and net_assets is not None:
        assets = Decimal(str(net_assets))
        if assets > 0:
            ratio = total_market_value * Decimal("100") / assets
            lines.append(f"仓位: {ratio:.1f}%")
    margin_debt = snapshot.get("margin_debt")
    if margin_debt is not None:
        lines.append(f"融资负债: {margin_debt}")

    if positions:
        lines.append(f"\n持仓 ({len(positions)} 只):")
        for pos in positions:
            symbol = pos.get("symbol", "?")
            name = pos.get("name", "?")
            shares = pos.get("total_shares", 0)
            average_cost = pos.get("average_cost")
            cost_text = average_cost if average_cost is not None else "未知"
            snapshot_price = pos.get("snapshot_price")
            price_text = snapshot_price if snapshot_price is not None else "未知"
            value = pos.get("market_value", "未知")
            lines.append(
                f"  {symbol} {name}: {shares}股, 成本价 {cost_text}, "
                f"现价 {price_text}, 市值 {value}"
            )
            thesis = pos.get("thesis")
            if thesis:
                lines.append(f"    持有理由: {thesis}")
    else:
        lines.append("\n无持仓")

    if ignored:
        lines.append(f"\n已忽略 ({len(ignored)} 项):")
        for item in ignored:
            lines.append(f"  - {item}")

    if corrected:
        lines.append(f"\n已纠正 ({len(corrected)} 项):")
        for item in corrected:
            lines.append(f"  - {item}")

    lines.append(f"\n请核对后回复“{_CONFIRMATION_PHRASE}”。")
    return "\n".join(lines)


def _preview_total_market_value(positions: object) -> Decimal | None:
    if not isinstance(positions, list):
        return None
    total = Decimal("0")
    for position in positions:
        if not isinstance(position, dict):
            return None
        value = position.get("market_value")
        if value is None:
            price = position.get("snapshot_price")
            shares = position.get("total_shares")
            if price is None or shares is None:
                return None
            value = Decimal(str(price)) * Decimal(str(shares))
        total += Decimal(str(value))
    return total


# ---------------------------------------------------------------------------
# Core module
# ---------------------------------------------------------------------------


class ActualAdvisoryPortfolioReview:
    """Two-operation portfolio review module.

    review_full_replacement: model submits observed facts -> ReviewResult
    confirm_latest_review: user confirms -> ConfirmResult
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._environ = environ
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = PendingReviewStore(
            environ=dict(environ) if environ is not None else None,
        )

    def review_full_replacement(
        self,
        facts: ObservedPortfolioFacts,
        interaction: BoundInteraction,
    ) -> ReviewResult:
        """Review observed facts and produce a preview or targeted question."""
        # 1. Always use system current time — screenshot time is unreliable.
        #    User-provided observed_at is ignored unconditionally.
        now = self._clock()
        parsed_at = now
        as_of_source = "SYSTEM"
        recorded_at = time.time()

        # 2. Normalize positions
        ignored: list[str] = []
        corrected: list[str] = []
        normalized_positions: list[dict[str, Any]] = []
        questions: list[str] = []

        for i, pos in enumerate(facts.positions):
            # Check for unknown shares
            if pos.shares is None:
                questions.append(
                    f"第 {i + 1} 只股票 ({pos.instrument}) 的持股数量未知，请提供。"
                )
                continue

            # Zero-share -> ignored
            if pos.shares == 0:
                ignored.append(
                    f"{pos.instrument}: 持股为零，不作为当前持仓"
                )
                continue

            # Normalize instrument
            canonical, stock_name, error = _canonical_instrument(pos.instrument)
            if canonical is None:
                questions.append(
                    f"第 {i + 1} 只股票代码 ({pos.instrument}) 无法识别: {error}。请确认。"
                )
                continue

            if canonical != pos.instrument.strip().upper():
                corrected.append(
                    f"{pos.instrument} -> {canonical}"
                )

            # Use stock name from directory, or fall back to original input
            display_name = stock_name or pos.instrument.strip()

            total_shares = _normalize_required_int(pos.shares)
            # 用户拍板 2026-08-21：默认规则"我的持仓全部可卖"——未提供可卖
            # 数量时默认等于持仓数量；显式提供时使用提供值（≤ 持仓由发布层校验）。
            sellable_shares = _normalize_optional_int(pos.sellable_shares)
            if sellable_shares is None and total_shares is not None:
                sellable_shares = total_shares
            normalized_positions.append(
                {
                    "symbol": canonical,
                    "name": display_name,
                    "total_shares": total_shares,
                    "sellable_shares": sellable_shares,
                    "average_cost": _normalize_optional_float(pos.average_cost),
                    "snapshot_price": _normalize_optional_float(pos.snapshot_price),
                    "market_value": _normalize_optional_float(pos.market_value),
                    "thesis": (pos.thesis or "").strip() or None,
                }
            )

        # 3. If no positions in input at all, ask
        if not facts.positions and not questions:
            questions.append("未识别到任何持仓，请提供持仓信息。")

        net_assets: float | Decimal | None = facts.net_assets
        if net_assets is None:
            net_assets = _derive_net_assets(facts, normalized_positions)
            if net_assets is None:
                questions.append(
                    "缺少总资产，且无法从可用资金、融资负债和全部持仓市值可靠推算，"
                    "请补充总资产。"
                )

        # 4. If any questions, return NEEDS_INFORMATION
        if questions:
            return ReviewResult(
                status=ReviewStatus.NEEDS_INFORMATION,
                question="\n".join(questions),
            )

        # 4.5 Financial contradiction precheck: mirror the owner _parse
        # arithmetic gates so contradictory facts become a precise question
        # instead of a hard REJECTED (zero write; no pending is created).
        contradiction = _financial_contradiction_question(facts, normalized_positions)
        if contradiction is not None:
            return ReviewResult(
                status=ReviewStatus.NEEDS_INFORMATION,
                question=contradiction,
            )

        # 5. Build candidate snapshot (exactly _FIELDS, no extras)
        as_of_iso = parsed_at.isoformat()

        candidate_snapshot = {
            "schema_version": ACTUAL_ADVISORY_PORTFOLIO_SCHEMA,
            "source_kind": "USER_CONFIRMED_BROKER_SCREENSHOT",
            "confirmation": "USER_CONFIRMED",
            "positions_complete": True,
            "account_alias": interaction.principal_id,
            "as_of": as_of_iso,
            "net_assets": _normalize_optional_float(net_assets),
            "available_cash": _normalize_optional_float(facts.cash_available),
            "margin_debt": _normalize_optional_float(facts.margin_debt),
            "positions": normalized_positions,
        }

        # 6. Materialize to temp file and validate via owner preview
        payload = (
            json.dumps(
                candidate_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

        operator = ActualAdvisoryPortfolioPublicationOperator(
            environ=self._environ,
            clock=self._clock,
        )
        source = self._materialize_candidate(payload)
        try:
            preview_result = operator.preview(source)
        finally:
            with suppress(FileNotFoundError):
                source.unlink()
            with suppress(FileNotFoundError):
                source.parent.rmdir()

        if preview_result.status == "REJECTED":
            return ReviewResult(
                status=ReviewStatus.REJECTED,
                reason_codes=preview_result.reason_codes,
            )

        candidate_revision = preview_result.candidate_revision
        current_revision = preview_result.current_revision
        if candidate_revision is None:
            raise RuntimeError("approved portfolio review is missing a candidate revision")

        # 7. Build readable preview
        readable_preview = _build_readable_preview(
            candidate_snapshot, ignored, corrected
        )

        # 8. Store pending review
        identity = _identity_digest(interaction.principal_id, interaction)
        self._store.save(
            interaction.principal_id,
            PendingReview(
                candidate_snapshot=candidate_snapshot,
                candidate_revision=candidate_revision,
                base_revision=current_revision,
                readable_preview=readable_preview,
                identity_digest=identity,
                preview_turn=interaction.turn_id,
                session_id=interaction.session_id,
                issued_at=time.time(),
                ttl_seconds=900,
                as_of_source=as_of_source,
                recorded_at=recorded_at,
            ),
        )

        return ReviewResult(
            status=ReviewStatus.PREVIEW_READY,
            readable_preview=readable_preview,
            ignored_observations=tuple(ignored),
            corrected_observations=tuple(corrected),
            candidate_revision=candidate_revision,
            current_revision=current_revision,
        )

    def confirm_latest_review(
        self,
        confirmation: BoundUserConfirmation,
        interaction: BoundInteraction,
    ) -> ConfirmResult:
        """Confirm the latest pending review and publish.

        The entire load → validate → publish → consume sequence runs under
        a single per-principal file lock, preventing a concurrent save()
        from interleaving.
        """
        if not confirmation.confirmed:
            return ConfirmResult(status=ConfirmStatus.NO_PENDING_REVIEW)

        expected_identity = _identity_digest(interaction.principal_id, interaction)
        operator = ActualAdvisoryPortfolioPublicationOperator(
            environ=self._environ,
            clock=self._clock,
        )

        def _publish(review: PendingReview) -> tuple[str, str | None, tuple[str, ...]]:
            """Validate and publish.

            Returns ``(status, candidate_revision, reason_codes)`` where
            reason_codes carries the owner publication failure reasons for
            precise Chinese guidance.
            """
            # Gate checks
            if review.expired:
                return "REVIEW_EXPIRED", None, ()
            if review.identity_digest != expected_identity:
                return "NO_PENDING_REVIEW", None, ()
            if confirmation.turn_id == review.preview_turn:
                return "NO_PENDING_REVIEW", None, ()
            # Session-generation binding: confirm only within the session that
            # saw the preview. Legacy pending files (no session_id) fail closed.
            if review.session_id is None or review.session_id != interaction.session_id:
                return "NO_PENDING_REVIEW", None, ()

            # Materialize and publish
            payload = (
                json.dumps(
                    review.candidate_snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            source = self._materialize_candidate(payload)
            try:
                result = operator.publish(
                    ActualAdvisoryPortfolioPublicationRequest(
                        source=source,
                        candidate_revision=review.candidate_revision,
                        expected_current_revision=review.base_revision,
                        apply=True,
                    )
                )
            finally:
                with suppress(FileNotFoundError):
                    source.unlink()
                with suppress(FileNotFoundError):
                    source.parent.rmdir()

            if result.status in {"PUBLISHED", "UNCHANGED", "EXACT_REPLAY"}:
                norm = "PUBLISHED" if result.status == "PUBLISHED" else "UNCHANGED"
                return norm, result.candidate_revision, ()
            if "ACTUAL_ADVISORY_PUBLICATION_CAS_MISMATCH" in result.reason_codes:
                return "CURRENT_CHANGED", None, result.reason_codes
            return "UNAVAILABLE", result.candidate_revision, result.reason_codes

        review, status, candidate_revision, reason_codes = self._store.confirm_under_lock(
            interaction.principal_id,
            publish=_publish,
        )

        if status == "PUBLISHED":
            return ConfirmResult(
                status=ConfirmStatus.PUBLISHED,
                snapshot_ref=(
                    actual_advisory_snapshot_ref(candidate_revision)
                    if candidate_revision
                    else None
                ),
            )
        if status == "UNCHANGED":
            return ConfirmResult(
                status=ConfirmStatus.UNCHANGED,
                snapshot_ref=(
                    actual_advisory_snapshot_ref(candidate_revision)
                    if candidate_revision
                    else None
                ),
            )
        if status == "OUTCOME_UNKNOWN":
            return ConfirmResult(
                status=ConfirmStatus.OUTCOME_UNKNOWN,
                reason_codes=reason_codes or ("PENDING_CONSUME_FAILED",),
            )
        if status == "REVIEW_EXPIRED":
            return ConfirmResult(status=ConfirmStatus.REVIEW_EXPIRED)
        if status == "CURRENT_CHANGED":
            return ConfirmResult(
                status=ConfirmStatus.CURRENT_CHANGED,
                reason_codes=reason_codes,
            )
        if status == "NO_PENDING_REVIEW":
            return ConfirmResult(status=ConfirmStatus.NO_PENDING_REVIEW)
        return ConfirmResult(
            status=ConfirmStatus.UNAVAILABLE,
            reason_codes=reason_codes or (status,),
        )

    def _materialize_candidate(self, payload: bytes) -> Path:
        """Write candidate to owner-only temp file for CAS kernel."""
        directory = Path(
            tempfile.mkdtemp(prefix="fin-portfolio-review-confirm-")
        )
        os.chmod(directory, 0o700)
        candidate = directory / "candidate.json"
        fd = os.open(
            candidate,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return candidate


__all__ = ["ActualAdvisoryPortfolioReview"]
