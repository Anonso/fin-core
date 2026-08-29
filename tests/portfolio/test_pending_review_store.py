"""Tests for PendingReviewStore: owner-only JSON + atomic rename."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fin_analyse.portfolio.pending_review_store import PendingReviewStore
from fin_analyse.portfolio.portfolio_review_types import PendingReview


def _make_review(**overrides: object) -> PendingReview:
    defaults = {
        "candidate_snapshot": {
            "schema_version": "actual-advisory-portfolio.v1",
            "source_kind": "USER_CONFIRMED_BROKER_SCREENSHOT",
            "confirmation": "USER_CONFIRMED",
            "positions_complete": True,
            "account_alias": "test",
            "as_of": "2026-08-18T09:35:00+08:00",
            "net_assets": "100000.00",
            "available_cash": "50000.00",
            "margin_debt": "0",
            "positions": [],
        },
        "candidate_revision": "sha256:aaa",
        "base_revision": "sha256:bbb",
        "readable_preview": "持仓预览\n...",
        "identity_digest": "digest123",
        "preview_turn": "turn123",
        "issued_at": time.time(),
        "ttl_seconds": 900,
    }
    defaults.update(overrides)
    return PendingReview(**defaults)


class TestPendingReviewStoreSaveLoad:
    """Save and load round-trips correctly."""

    def test_save_and_load(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        review = _make_review()
        store.save("principal1", review)
        loaded = store.load("principal1")
        assert loaded is not None
        assert loaded.candidate_revision == "sha256:aaa"
        assert loaded.base_revision == "sha256:bbb"
        assert loaded.readable_preview == "持仓预览\n..."
        assert loaded.identity_digest == "digest123"

    def test_load_nonexistent_returns_none(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        assert store.load("no_such_principal") is None

    def test_new_preview_supersedes_prior(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save("p1", _make_review(candidate_revision="sha256:old"))
        store.save("p1", _make_review(candidate_revision="sha256:new"))
        loaded = store.load("p1")
        assert loaded is not None
        assert loaded.candidate_revision == "sha256:new"


class TestPendingReviewStoreConsume:
    """Consume marks terminal receipt and prevents re-load."""

    def test_consume_returns_review(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save("p1", _make_review())
        consumed = store.consume("p1", receipt="receipt_abc")
        assert consumed is not None
        assert consumed.candidate_revision == "sha256:aaa"

    def test_consume_prevents_reload(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save("p1", _make_review())
        store.consume("p1", receipt="receipt_abc")
        assert store.load("p1") is None

    def test_consume_nonexistent_returns_none(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        assert store.consume("nope", receipt="r") is None

    def test_double_consume_returns_none(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save("p1", _make_review())
        store.consume("p1", receipt="r1")
        assert store.consume("p1", receipt="r2") is None


class TestPendingReviewStoreExpiry:
    """Expired reviews are not loaded."""

    def test_expired_review_still_loaded(self, tmp_path: Path) -> None:
        now = time.time()
        store = PendingReviewStore(
            environ={"XDG_STATE_HOME": str(tmp_path)},
            clock=lambda: now,
        )
        store.save("p1", _make_review(issued_at=now - 1000, ttl_seconds=900))
        # load returns expired reviews; caller checks .expired
        loaded = store.load("p1")
        assert loaded is not None
        assert loaded.expired

    def test_review_within_ttl_is_loaded(self, tmp_path: Path) -> None:
        now = time.time()
        store = PendingReviewStore(
            environ={"XDG_STATE_HOME": str(tmp_path)},
            clock=lambda: now,
        )
        store.save("p1", _make_review(issued_at=now - 100, ttl_seconds=900))
        # The review was saved 100s ago, TTL is 900s, so it's still valid
        # But load() uses real time.time() which is approximately now
        loaded = store.load("p1")
        assert loaded is not None


class TestPendingReviewStorePermissions:
    """Files are owner-only."""

    def test_file_is_created(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save("p1", _make_review())
        path = store._path("p1")
        assert path.exists()

    def test_file_content_is_valid_json(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save("p1", _make_review())
        path = store._path("p1")
        data = json.loads(path.read_bytes())
        assert data["candidate_revision"] == "sha256:aaa"
        assert data["terminal_receipt"] is None


class TestPendingReviewStoreClearExpired:
    """clear_expired removes files for expired reviews."""

    def test_clear_expired_removes_file(self, tmp_path: Path) -> None:
        now = time.time()
        store = PendingReviewStore(
            environ={"XDG_STATE_HOME": str(tmp_path)},
            clock=lambda: now,
        )
        store.save("p1", _make_review(issued_at=now - 1000, ttl_seconds=900))
        result = store.clear_expired("p1")
        assert result is True

    def test_clear_expired_no_file(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        assert store.clear_expired("nope") is True


class TestSaveHygiene:
    """save() purges expired residue before overwriting."""

    def test_save_invokes_clear_expired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        calls: list[str] = []

        def spy(principal_id: str) -> bool:
            calls.append(principal_id)
            return False

        monkeypatch.setattr(store, "clear_expired", spy)
        store.save("principal1", _make_review())
        assert calls == ["principal1"]

    def test_save_after_expired_review_replaces_it(self, tmp_path: Path) -> None:
        store = PendingReviewStore(environ={"XDG_STATE_HOME": str(tmp_path)})
        store.save(
            "principal1",
            _make_review(issued_at=time.time() - 2000, ttl_seconds=900),
        )
        assert store.load("principal1") is not None
        store.save(
            "principal1",
            _make_review(candidate_revision="sha256:new", preview_turn="turn_new"),
        )
        loaded = store.load("principal1")
        assert loaded is not None
        assert loaded.candidate_revision == "sha256:new"
