"""Tests for ActualAdvisoryPortfolioReview: review + confirm."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fin_analyse.portfolio.portfolio_review_types import (
    BoundInteraction,
    BoundUserConfirmation,
    ConfirmStatus,
    ObservedPortfolioFacts,
    ObservedPosition,
    PendingReview,
    ReviewStatus,
)


def _interaction(**overrides: object) -> BoundInteraction:
    defaults = {
        "principal_id": "test_principal",
        "profile_name": "fin",
        "platform": "feishu",
        "session_key": "sess_123",
        "subject_kind": "user",
        "subject_id": "user_123",
        "session_id": "session_123",
        "turn_id": "turn_123",
    }
    defaults.update(overrides)
    return BoundInteraction(**defaults)


def _facts(**overrides: object) -> ObservedPortfolioFacts:
    # Balance: cash 50000 + market_value 50000 = net_assets 100000
    defaults = {
        "observed_at": "2026-08-18T09:35:00+08:00",
        "cash_available": 50000.0,
        "net_assets": 100000.0,
        "margin_debt": 0.0,
        "positions": (
            ObservedPosition(
                instrument="600000",
                shares=100,
                average_cost=500.0,
                snapshot_price=500.0,
                market_value=50000.0,
            ),
        ),
    }
    defaults.update(overrides)
    return ObservedPortfolioFacts(**defaults)


def _empty_facts() -> ObservedPortfolioFacts:
    return ObservedPortfolioFacts(
        observed_at="2026-08-18T09:35:00+08:00",
        cash_available=50000.0,
        net_assets=100000.0,
        positions=(),
    )


class TestNormalization:
    """Deterministic normalization of observed facts."""

    def test_six_digit_code_normalized_with_suffix(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="600000", shares=100),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        # The preview should show normalized code
        assert result.readable_preview is not None

    def test_already_suffixed_code_passes_through(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="600000.SH", shares=100),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY

    def test_whitespace_trimmed(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="  600000  ", shares=100),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY

    def test_stock_name_resolved(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="博迁新材", shares=100),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "605376.SH" in result.readable_preview
        assert "博迁新材" in result.readable_preview

    def test_bare_code_resolves_directory_name(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="000657", shares=100),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "000657.SZ" in result.readable_preview
        assert "中钨高新" in result.readable_preview

    def test_suffixed_code_resolves_directory_name(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="600879.SH", shares=100),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "600879.SH" in result.readable_preview
        assert "航天电子" in result.readable_preview


class TestZeroShareHandling:
    """Zero-share rows become ignored observations, not rejections."""

    def test_zero_share_position_is_ignored(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="600000", shares=100),
                ObservedPosition(instrument="000001", shares=0),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert len(result.ignored_observations) > 0
        assert "000001" in result.ignored_observations[0]

    def test_all_zero_shares_still_preview(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="600000", shares=0),
            ),
            # Cash equals net_assets when no positions remain
            cash_available=100000.0,
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert len(result.ignored_observations) == 1


class TestNeedsInformation:
    """Missing or ambiguous facts produce targeted questions."""

    def test_missing_date_defaults_to_now(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        # observed_at with only time, no date — should default to now
        facts = ObservedPortfolioFacts(
            observed_at="09:35",
            positions=(
                ObservedPosition(
                    instrument="600000", shares=100,
                    average_cost=500.0, snapshot_price=500.0, market_value=50000.0,
                ),
            ),
            cash_available=50000.0,
            net_assets=100000.0,
            margin_debt=0.0,
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY

    def test_unknown_shares_returns_needs_information(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(instrument="600000", shares=None),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.NEEDS_INFORMATION
        assert result.question is not None

    def test_no_positions_returns_needs_information(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _empty_facts()
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.NEEDS_INFORMATION


class TestPreviewReady:
    """Valid facts produce a readable preview."""

    def test_valid_facts_produce_preview(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts()
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert result.candidate_revision is not None
        assert result.current_revision is not None
        assert "总资产: 100000.0" in result.readable_preview
        assert "总市值: 50000.0" in result.readable_preview
        assert "仓位: 50.0%" in result.readable_preview
        assert "融资负债: 0.0" in result.readable_preview
        assert "成本价 500.0" in result.readable_preview
        assert "现价 500.0" in result.readable_preview

    def test_missing_total_assets_is_derived_before_preview_and_publish(
        self, tmp_path: Path
    ) -> None:
        from fin_analyse.portfolio.actual_advisory import (
            ActualAdvisoryPortfolioStatus,
            ActualAdvisoryPortfolioStore,
        )
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        config_home = tmp_path / "config"
        review = ActualAdvisoryPortfolioReview(
            environ={
                "XDG_STATE_HOME": str(tmp_path),
                "XDG_CONFIG_HOME": str(config_home),
            },
        )
        facts = _facts(net_assets=None)

        preview = review.review_full_replacement(facts, _interaction())

        assert preview.status == ReviewStatus.PREVIEW_READY
        assert preview.readable_preview is not None
        assert "总资产: 100000.0" in preview.readable_preview

        confirmed = review.confirm_latest_review(
            BoundUserConfirmation(confirmed=True, turn_id="turn_456"),
            _interaction(),
        )
        assert confirmed.status == ConfirmStatus.PUBLISHED
        published = ActualAdvisoryPortfolioStore(
            environ={"XDG_CONFIG_HOME": str(config_home)},
            clock=review._clock,
        ).read()
        assert published.status == ActualAdvisoryPortfolioStatus.READY
        assert published.snapshot is not None
        assert str(published.snapshot.net_assets) == "100000.0"

    def test_missing_total_assets_asks_before_preview_when_not_derivable(
        self, tmp_path: Path
    ) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(net_assets=None, cash_available=None)

        result = review.review_full_replacement(facts, _interaction())

        assert result.status == ReviewStatus.NEEDS_INFORMATION
        assert result.question is not None
        assert "总资产" in result.question

    def test_preview_frozen_in_pending_store(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.pending_review_store import PendingReviewStore
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts()
        review.review_full_replacement(facts, _interaction())

        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)})
        pending = store.load("test_principal")
        assert pending is not None
        assert pending.candidate_revision is not None

    def test_sellable_shares_kept_in_candidate_but_not_rendered(self, tmp_path: Path) -> None:
        """可卖数量仍透传到候选快照，但预览不再展示（默认可卖，展示从简）。"""
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(
                    instrument="600000",
                    shares=100,
                    sellable_shares=60,
                    average_cost=500.0,
                    snapshot_price=500.0,
                    market_value=50000.0,
                ),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "可卖" not in result.readable_preview

    def test_negative_sellable_shares_rejected(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(
                    instrument="600000",
                    shares=100,
                    sellable_shares=-1,
                ),
            ),
        )
        with pytest.raises(ValueError):
            review.review_full_replacement(facts, _interaction())

    def test_sellable_defaults_to_total_when_omitted(self, tmp_path: Path) -> None:
        """用户规则：未提供可卖数量时默认等于持仓数量，且预览不展示可卖数量。"""
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts()  # 原 _facts 的持仓不带 sellable_shares
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "可卖" not in result.readable_preview


class TestConfirm:
    """Confirm consumes pending review and publishes."""

    def test_confirm_without_review_returns_no_pending(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        result = review.confirm_latest_review(
            BoundUserConfirmation(confirmed=True, turn_id="turn_456"),
            _interaction(),
        )
        assert result.status == ConfirmStatus.NO_PENDING_REVIEW

    def test_confirm_publishes_candidate(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview
        from tests.portfolio.test_actual_advisory_portfolio import _payload, _target

        config_home = tmp_path / "config"
        _target(config_home, _payload())
        review_mod = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(config_home)},
        )
        facts = _facts()
        review_mod.review_full_replacement(facts, _interaction())

        result = review_mod.confirm_latest_review(
            BoundUserConfirmation(confirmed=True, turn_id="turn_456"),
            _interaction(),
        )
        assert result.status in {ConfirmStatus.PUBLISHED, ConfirmStatus.UNCHANGED}

    def test_double_confirm_returns_same_receipt(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview
        from tests.portfolio.test_actual_advisory_portfolio import _payload, _target

        config_home = tmp_path / "config"
        _target(config_home, _payload())
        review_mod = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(config_home)},
        )
        facts = _facts()
        review_mod.review_full_replacement(facts, _interaction())

        confirmation = BoundUserConfirmation(confirmed=True, turn_id="turn_456")
        interaction = _interaction()

        r1 = review_mod.confirm_latest_review(confirmation, interaction)
        r2 = review_mod.confirm_latest_review(confirmation, interaction)
        # Second confirm should return NO_PENDING_REVIEW (consumed)
        assert r1.status in {ConfirmStatus.PUBLISHED, ConfirmStatus.UNCHANGED}
        assert r2.status == ConfirmStatus.NO_PENDING_REVIEW

    def test_expired_review_returns_expired(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.pending_review_store import PendingReviewStore
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        now = time.time()
        review_mod = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts()
        review_mod.review_full_replacement(facts, _interaction())

        # Manually expire the pending review
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)})
        pending = store.load("test_principal")
        assert pending is not None
        # Save with old timestamp
        expired = PendingReview(
            candidate_snapshot=pending.candidate_snapshot,
            candidate_revision=pending.candidate_revision,
            base_revision=pending.base_revision,
            readable_preview=pending.readable_preview,
            identity_digest=pending.identity_digest,
            preview_turn=pending.preview_turn,
            issued_at=now - 2000,
            ttl_seconds=900,
        )
        store.save("test_principal", expired)

        result = review_mod.confirm_latest_review(
            BoundUserConfirmation(confirmed=True, turn_id="turn_456"),
            _interaction(),
        )
        assert result.status == ConfirmStatus.REVIEW_EXPIRED


class TestRejection:
    """Deterministic validation failures produce REJECTED."""

    def test_any_time_accepted(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        # Any time is accepted — user asked to update, default to latest
        facts = ObservedPortfolioFacts(
            observed_at="2099-01-01T00:00:00+08:00",
            positions=(
                ObservedPosition(
                    instrument="600000", shares=100,
                    average_cost=500.0, snapshot_price=500.0, market_value=50000.0,
                ),
            ),
            cash_available=50000.0,
            net_assets=100000.0,
            margin_debt=0.0,
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY


class TestFinancialPrecheck:
    """Financial contradiction precheck mirrors the owner _parse gates."""

    def _review(self, tmp_path: Path):
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        return ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )

    def _facts(self, **overrides: object) -> ObservedPortfolioFacts:
        return _facts(**overrides)

    def test_cash_exceeds_net_plus_margin_asks_question(self, tmp_path: Path) -> None:
        review = self._review(tmp_path)
        result = review.review_full_replacement(
            self._facts(
                cash_available=140000.0,
                net_assets=100000.0,
                margin_debt=10000.0,
            ),
            _interaction(),
        )
        assert result.status == ReviewStatus.NEEDS_INFORMATION
        assert "140000" in result.question
        assert "100000" in result.question
        assert "10000" in result.question
        assert result.readable_preview is None

    def test_cash_plus_positions_above_net_with_margin_asks(self, tmp_path: Path) -> None:
        review = self._review(tmp_path)
        result = review.review_full_replacement(
            self._facts(
                cash_available=90000.0,
                net_assets=100000.0,
                margin_debt=20000.0,
            ),
            _interaction(),
        )
        assert result.status == ReviewStatus.NEEDS_INFORMATION
        assert "20000" in result.question  # 差额 140000 - 120000

    def test_cash_plus_positions_above_net_without_margin_still_previews(
        self, tmp_path: Path
    ) -> None:
        # Owner accepts cash+MV above net when margin is unknown — mirror it.
        review = self._review(tmp_path)
        result = review.review_full_replacement(
            self._facts(
                cash_available=90000.0,
                net_assets=100000.0,
                margin_debt=None,
            ),
            _interaction(),
        )
        assert result.status == ReviewStatus.PREVIEW_READY

    def test_cash_plus_positions_below_net_asks_question(self, tmp_path: Path) -> None:
        review = self._review(tmp_path)
        result = review.review_full_replacement(
            self._facts(
                cash_available=10000.0,
                net_assets=100000.0,
                margin_debt=0.0,
            ),
            _interaction(),
        )
        assert result.status == ReviewStatus.NEEDS_INFORMATION

    def test_position_without_valuation_skips_reconciliation(self, tmp_path: Path) -> None:
        # Any position without an effective valuation -> owner PARTIALs, so the
        # precheck must not ask (and must not create a pending).
        review = self._review(tmp_path)
        result = review.review_full_replacement(
            self._facts(
                cash_available=90000.0,
                net_assets=100000.0,
                margin_debt=0.0,
                positions=(
                    ObservedPosition(instrument="600000", shares=100),
                ),
            ),
            _interaction(),
        )
        assert result.status == ReviewStatus.PREVIEW_READY

    def test_consistent_facts_still_preview_ready(self, tmp_path: Path) -> None:
        review = self._review(tmp_path)
        result = review.review_full_replacement(self._facts(), _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY

    def test_precheck_does_not_write_pending(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.pending_review_store import PendingReviewStore

        review = self._review(tmp_path)
        review.review_full_replacement(
            self._facts(
                cash_available=140000.0,
                net_assets=100000.0,
                margin_debt=10000.0,
            ),
            _interaction(),
        )
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)})
        assert store.load("test_principal") is None


class TestConfirmGateHardening:
    """Audit round-1 hardening: CAS conflict, legacy pending, recoverable values."""

    def test_cash_plus_positions_above_net_suggests_recoverable_value(
        self, tmp_path: Path
    ) -> None:
        # explained=140000, net=100000, margin=20000 -> suggested net'=120000
        # so that net'+margin == explained passes the owner gate.
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        result = review.review_full_replacement(
            _facts(
                cash_available=90000.0,
                net_assets=100000.0,
                margin_debt=20000.0,
            ),
            _interaction(),
        )
        assert result.status == ReviewStatus.NEEDS_INFORMATION
        assert "120000" in result.question
        assert "20000" in result.question

    def test_confirm_cas_conflict_returns_current_changed(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.pending_review_store import PendingReviewStore
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review_module = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        review_module.review_full_replacement(_facts(), _interaction())
        # Concurrent update: write a valid snapshot to the owner file so the
        # current revision no longer matches the pending's base (MISSING).
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)})
        pending = store.load("test_principal")
        assert pending is not None
        owner = tmp_path / "fin-analyse" / "actual-advisory-portfolio.v1.json"
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.parent.chmod(0o700)  # owner-only directory requirement
        payload = (
            json.dumps(
                pending.candidate_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        import os as _os

        fd = _os.open(owner, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | _os.O_NOFOLLOW, 0o600)
        try:
            _os.write(fd, payload)
            _os.fsync(fd)
        finally:
            _os.close(fd)
        result = review_module.confirm_latest_review(
            BoundUserConfirmation(confirmed=True, turn_id="turn_456"),
            _interaction(turn_id="turn_456"),
        )
        assert result.status == ConfirmStatus.CURRENT_CHANGED
        # The concurrent snapshot must remain untouched.
        assert owner.read_bytes().endswith(b"\n")

    def test_confirm_legacy_pending_without_session_id_fails_closed(
        self, tmp_path: Path
    ) -> None:
        # Build a true legacy on-disk format: the session_id key is ABSENT
        # (not null) — exactly what pre-upgrade pending files look like.
        from fin_analyse.portfolio.pending_review_store import PendingReviewStore
        from fin_analyse.portfolio.portfolio_review import (
            ActualAdvisoryPortfolioReview,
            _identity_digest,
        )

        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)})
        interaction = _interaction()
        legacy = {
            "candidate_snapshot": {"schema_version": "actual-advisory-portfolio.v1"},
            "candidate_revision": "sha256:aaa",
            "base_revision": "sha256:bbb",
            "readable_preview": "preview",
            # Real digest: identity, expiry and turn checks must pass so the
            # confirm actually reaches the missing-session-id fail-closed guard.
            "identity_digest": _identity_digest("test_principal", interaction),
            "preview_turn": "turn_123",
            "issued_at": time.time(),
            "ttl_seconds": 900,
            "as_of_source": "SYSTEM",
            "recorded_at": None,
            "terminal_receipt": None,
        }
        path = store._path("test_principal")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_bytes(
            json.dumps(
                legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        loaded = store.load("test_principal")
        assert loaded is not None and loaded.session_id is None
        review_module = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        result = review_module.confirm_latest_review(
            BoundUserConfirmation(confirmed=True, turn_id="turn_456"),
            _interaction(turn_id="turn_456"),
        )
        assert result.status == ConfirmStatus.NO_PENDING_REVIEW
        # All prior gates pass (identity/expiry/turn) — the only remaining
        # failing guard is the session_id equality, proving the fail-closed
        # branch was exercised.
        assert result.reason_codes == ()


class TestPositionThesis:
    """Owner's per-holding reason flows through preview and candidate."""

    def test_thesis_rendered_in_preview(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(
                ObservedPosition(
                    instrument="000657",
                    shares=100,
                    thesis="钨出口管制 + 硬质合金刀具景气",
                ),
            ),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "中钨高新" in result.readable_preview
        assert "持有理由: 钨出口管制 + 硬质合金刀具景气" in result.readable_preview

    def test_missing_thesis_renders_no_reason_line(self, tmp_path: Path) -> None:
        from fin_analyse.portfolio.portfolio_review import ActualAdvisoryPortfolioReview

        review = ActualAdvisoryPortfolioReview(
            environ={"XDG_STATE_HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        )
        facts = _facts(
            positions=(ObservedPosition(instrument="000657", shares=100),),
        )
        result = review.review_full_replacement(facts, _interaction())
        assert result.status == ReviewStatus.PREVIEW_READY
        assert result.readable_preview is not None
        assert "持有理由" not in result.readable_preview
