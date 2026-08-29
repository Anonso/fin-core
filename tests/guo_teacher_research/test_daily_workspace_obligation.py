"""Tests for the Daily workspace atomic obligation seam (schema v4).

TDD: tests must FAIL before implementation exists.

The product + delivery obligation must land in ONE ``BEGIN IMMEDIATE``
transaction: a crash between product commit and obligation insert would leave
a product with no delivery obligation (handoff 11:39 P1 finding).  The
obligation is uniquely bound to ``(workspace_ref, product_version)`` and
carries ``artifact_hash`` + ``presentation_hash``.  Delivery settles with an
explicit outcome; ``OUTCOME_UNKNOWN`` never auto-resends.  Claim fencing: a
settle must present the exact claim token (a late ACK cannot settle a newer
claim).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.semantic_state import (
    SCHEMA_VERSION,
    ResearchStateRepository,
    SemanticStateError,
)
from tests.fixtures.daily_workspace import daily_workspace_advisory_product

# ── Fixtures ────────────────────────────────────────────────────────────────

_TOKEN_SECRET = b"test-secret-test-secret-test-secret-test-secret"
_PRESENTATION_HASH = "sha256:" + "a" * 64


@pytest.fixture
def repository(tmp_path: Path) -> ResearchStateRepository:
    return ResearchStateRepository(
        tmp_path / "state.sqlite3",
        token_secret=_TOKEN_SECRET,
        epoch="test-epoch",
    )


def _scheduled_product(*, checkpoint: str = "morning") -> dict[str, object]:
    return daily_workspace_advisory_product(
        trading_day_id="2026-08-04",
        checkpoint=checkpoint,
        consultation_as_of="2026-08-04T09:35:00+08:00",
        summary="test",
        with_g=False,
    )


def _finalize(
    repo: ResearchStateRepository,
    *,
    principal_id: str = "test-principal",
    trading_day_id: str = "2026-08-04",
    idempotency_key: str = "daily:2026-08-04:morning",
    checkpoint: str = "morning",
    product: dict[str, object] | None = None,
) -> object:
    repo.create_daily_workspace_chain(
        principal_id=principal_id,
        trading_day_id=trading_day_id,
        idempotency_key=f"daily:{trading_day_id}:init",
        now=1_700_000_000.0,
    )
    return repo.finalize_scheduled_checkpoint(
        principal_id=principal_id,
        trading_day_id=trading_day_id,
        idempotency_key=idempotency_key,
        checkpoint=checkpoint,
        product=product or _scheduled_product(),
        now=1_700_000_100.0,
        presentation_hash=_PRESENTATION_HASH,
    )


def _claim(
    repository: ResearchStateRepository,
    finalization: object,
) -> object:
    return repository.claim_delivery(
        workspace_ref=finalization.read.workspace_ref,
        product_version=1,
        claimed_at=1_700_000_200.0,
        presentation_hash=_PRESENTATION_HASH,
    )


def _settle(
    repository: ResearchStateRepository,
    finalization: object,
    claim: object,
    *,
    settlement: str,
    settled_at: float = 1_700_000_300.0,
) -> None:
    repository.settle_delivery(
        workspace_ref=finalization.read.workspace_ref,
        product_version=1,
        settlement=settlement,
        settled_at=settled_at,
        claim_token=claim.claim_token,
    )


# ── Schema v4 ───────────────────────────────────────────────────────────────


def test_schema_version_is_eight(repository: ResearchStateRepository) -> None:
    # A5L-2 (v5) 增加 conversation_routes；B2 (v6) 增加 daily_workspace_run_ledger；
    # B3 (v7) 归一 obligations 表；R1-5 (v8) 增加投资记忆 journal
    assert SCHEMA_VERSION == 8


def test_check_constraint_rejects_illegal_state_combinations(
    repository: ResearchStateRepository,
) -> None:
    """Self-review: CHECK must reject PENDING-with-claim fields etc."""
    import sqlite3

    db_path = repository._db_path
    conn = sqlite3.connect(db_path)
    base = (
        "INSERT INTO daily_workspace_obligations("
        "workspace_ref, product_version, artifact_hash, presentation_hash,"
        "state, claim_token, claimed_at, settlement, settled_at, created_at, updated_at"
        ") VALUES (?, ?, 'a', ?, ?, ?, ?, ?, ?, 0, 0)"
    )
    illegal = [
        # PENDING 带 claim 字段（hash 应为 NULL）
        ("w1", 1, None, "PENDING", "tok", 1.0, None, None),
        # PENDING 带 presentation_hash
        ("w1b", 1, "sha256:x", "PENDING", None, None, None, None),
        # CLAIMED 无 token
        ("w2", 1, "sha256:x", "CLAIMED", None, 1.0, None, None),
        # CLAIMED 带 settlement
        ("w3", 1, "sha256:x", "CLAIMED", "tok", 1.0, "POSITIVE_ACK", None),
        # SETTLED 无 settlement
        ("w4", 1, "sha256:x", "SETTLED", "tok", 1.0, None, 1.0),
        # SETTLED 无 settled_at
        ("w5", 1, "sha256:x", "SETTLED", "tok", 1.0, "POSITIVE_ACK", None),
        # SETTLED + EXPLICIT_NOT_SENT（业务不可能态，必须回 PENDING）
        ("w6", 1, "sha256:x", "SETTLED", "tok", 1.0, "EXPLICIT_NOT_SENT", 1.0),
    ]
    try:
        for row in illegal:
            try:
                conn.execute(base, row)
                conn.commit()
                raise AssertionError(f"illegal combo accepted: {row[:2]} state={row[3]}")
            except sqlite3.IntegrityError:
                conn.rollback()
        # 合法 PENDING 必须可插入（hash NULL）
        conn.execute(
            base,
            ("w-ok", 1, None, "PENDING", None, None, None, None),
        )
        conn.commit()
    finally:
        conn.close()


def test_obligation_table_exists(repository: ResearchStateRepository) -> None:
    # Behavior proof: claim on an unknown workspace hits the obligation table
    # and fails closed with the typed code (table exists and is queryable).
    with pytest.raises(SemanticStateError) as excinfo:
        repository.claim_delivery(
            workspace_ref="no-such-ref",
            product_version=1,
            claimed_at=1_700_000_200.0,
            presentation_hash=_PRESENTATION_HASH,
        )
    assert excinfo.value.code == "daily_delivery_obligation_missing"


# ── Atomic finalize: product + obligation in one transaction ───────────────


def test_finalize_creates_product_and_pending_obligation(
    repository: ResearchStateRepository,
) -> None:
    finalization = _finalize(repository)
    read = finalization.read
    obligation = finalization.obligation

    assert read.product_version == 1
    assert obligation.state == "PENDING"
    assert obligation.workspace_ref == read.workspace_ref
    assert obligation.product_version == read.product_version
    assert obligation.artifact_hash == read.artifact_hash
    # PENDING 时 presentation_hash 为 None：真实渲染消息哈希在 claim 时绑定
    # （delivery 在 product 落库后才渲染，obligation 必须匹配实际发送消息）
    assert obligation.presentation_hash is None


def test_obligation_unique_per_workspace_version(repository: ResearchStateRepository) -> None:
    # Re-finalizing the same idempotency key must not duplicate the obligation;
    # the (workspace_ref, product_version) uniqueness holds via ON CONFLICT.
    first = _finalize(repository)
    second = _finalize(repository)
    assert second.read.workspace_ref == first.read.workspace_ref
    assert second.obligation.workspace_ref == first.obligation.workspace_ref
    # claim/settle still drive exactly one obligation row
    claim = _claim(repository, first)
    _settle(repository, first, claim, settlement="POSITIVE_ACK")
    with pytest.raises(SemanticStateError):
        repository.claim_delivery(
            workspace_ref=first.read.workspace_ref,
            product_version=1,
            claimed_at=1_700_000_400.0,
            presentation_hash=_PRESENTATION_HASH,
        )


def test_finalize_replay_is_idempotent(repository: ResearchStateRepository) -> None:
    first = _finalize(repository)
    second = _finalize(repository)  # same idempotency key
    assert second.read.product_version == first.read.product_version == 1
    # no duplicate obligation on replay: claim once, settle once, then the
    # obligation is exhausted (second claim fails with not_pending)
    claim = _claim(repository, first)
    _settle(repository, first, claim, settlement="POSITIVE_ACK")
    with pytest.raises(SemanticStateError):
        repository.claim_delivery(
            workspace_ref=first.read.workspace_ref,
            product_version=1,
            claimed_at=1_700_000_400.0,
            presentation_hash=_PRESENTATION_HASH,
        )


# ── claim / settle lifecycle ────────────────────────────────────────────────


def test_claim_transitions_pending_to_claimed(repository: ResearchStateRepository) -> None:
    finalization = _finalize(repository)
    claim = repository.claim_delivery(
        workspace_ref=finalization.read.workspace_ref,
        product_version=1,
        claimed_at=1_700_000_200.0,
        presentation_hash=_PRESENTATION_HASH,
    )
    assert claim.workspace_ref == finalization.read.workspace_ref
    assert claim.product_version == 1
    # claim 时绑定真实渲染消息哈希（PENDING obligation 时为 None）
    assert claim.presentation_hash == _PRESENTATION_HASH
    assert claim.claim_token  # fencing token issued
    # state transition proof: a second claim fails with not_pending
    with pytest.raises(SemanticStateError) as excinfo:
        repository.claim_delivery(
            workspace_ref=finalization.read.workspace_ref,
            product_version=1,
            claimed_at=1_700_000_300.0,
            presentation_hash=_PRESENTATION_HASH,
        )
    assert excinfo.value.code == "daily_delivery_obligation_not_pending"


def test_settle_positive_ack(repository: ResearchStateRepository) -> None:
    finalization = _finalize(repository)
    claim = _claim(repository, finalization)
    _settle(repository, finalization, claim, settlement="POSITIVE_ACK")
    # A crash after terminal settlement may replay the exact same fenced ACK.
    # It is idempotent, rather than turning a recoverable outbox row into an
    # unknown delivery result.
    repository.settle_delivery(
        workspace_ref=finalization.read.workspace_ref,
        product_version=1,
        settlement="POSITIVE_ACK",
        settled_at=1_700_000_400.0,
        claim_token=claim.claim_token,
    )
    # The token cannot be repurposed for a contradictory terminal result.
    with pytest.raises(SemanticStateError) as excinfo:
        repository.settle_delivery(
            workspace_ref=finalization.read.workspace_ref,
            product_version=1,
            settlement="OUTCOME_UNKNOWN",
            settled_at=1_700_000_400.0,
            claim_token=claim.claim_token,
        )
    assert excinfo.value.code == "daily_delivery_obligation_settlement_conflict"


def test_settle_explicit_not_sent_returns_to_pending_for_retry(
    repository: ResearchStateRepository,
) -> None:
    finalization = _finalize(repository)
    claim = _claim(repository, finalization)
    _settle(repository, finalization, claim, settlement="EXPLICIT_NOT_SENT")
    # EXPLICIT_NOT_SENT is not terminal: the same immutable message may be
    # re-claimed and re-sent later (fresh token issued).
    retry_claim = repository.claim_delivery(
        workspace_ref=finalization.read.workspace_ref,
        product_version=1,
        claimed_at=1_700_000_400.0,
        presentation_hash=_PRESENTATION_HASH,
    )
    assert retry_claim.workspace_ref == finalization.read.workspace_ref
    assert retry_claim.claim_token != claim.claim_token


def test_settle_outcome_unknown_allowed_but_no_autoresend(
    repository: ResearchStateRepository,
) -> None:
    finalization = _finalize(repository)
    claim = _claim(repository, finalization)
    _settle(repository, finalization, claim, settlement="OUTCOME_UNKNOWN")
    # unknown never auto-resends: obligation stays settled, a re-claim fails
    with pytest.raises(SemanticStateError) as excinfo:
        repository.claim_delivery(
            workspace_ref=finalization.read.workspace_ref,
            product_version=1,
            claimed_at=1_700_000_400.0,
            presentation_hash=_PRESENTATION_HASH,
        )
    assert excinfo.value.code == "daily_delivery_obligation_not_pending"


def test_settle_rejects_stale_claim_token(repository: ResearchStateRepository) -> None:
    """Claim fencing: a settle presenting a stale token fails closed."""
    finalization = _finalize(repository)
    claim = _claim(repository, finalization)
    # EXPLICIT_NOT_SENT returns to PENDING and revokes the old token.
    _settle(repository, finalization, claim, settlement="EXPLICIT_NOT_SENT")
    retry_claim = repository.claim_delivery(
        workspace_ref=finalization.read.workspace_ref,
        product_version=1,
        claimed_at=1_700_000_400.0,
        presentation_hash=_PRESENTATION_HASH,
    )
    # A late ACK presenting the OLD token cannot settle the NEW claim.
    with pytest.raises(SemanticStateError) as excinfo:
        repository.settle_delivery(
            workspace_ref=finalization.read.workspace_ref,
            product_version=1,
            settlement="POSITIVE_ACK",
            settled_at=1_700_000_500.0,
            claim_token=claim.claim_token,
        )
    assert excinfo.value.code == "daily_delivery_claim_token_mismatch"
    # The new claim is still CLAIMED and can settle with its own token.
    _settle(repository, finalization, retry_claim, settlement="POSITIVE_ACK")


def test_concurrent_claims_have_exactly_one_winner(
    repository: ResearchStateRepository,
) -> None:
    """Concurrent claim (TOCTOU): exactly one attempt wins, others fail closed."""
    import threading

    finalization = _finalize(repository)
    results: list[tuple[str, object]] = []

    def worker() -> None:
        try:
            claim = repository.claim_delivery(
                workspace_ref=finalization.read.workspace_ref,
                product_version=1,
                claimed_at=1_700_000_200.0,
                presentation_hash=_PRESENTATION_HASH,
            )
            results.append(("win", claim.claim_token))
        except SemanticStateError as exc:
            results.append(("lose", exc.code))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [r for r in results if r[0] == "win"]
    assert len(winners) == 1
    assert all(r[0] == "lose" for r in results if r is not winners[0])


def test_claim_rejects_unknown_workspace(repository: ResearchStateRepository) -> None:
    with pytest.raises(SemanticStateError):
        repository.claim_delivery(
            workspace_ref="unknown-ref",
            product_version=1,
            claimed_at=1_700_000_200.0,
            presentation_hash=_PRESENTATION_HASH,
        )


def test_claim_rejects_already_settled(repository: ResearchStateRepository) -> None:
    finalization = _finalize(repository)
    claim = _claim(repository, finalization)
    _settle(repository, finalization, claim, settlement="POSITIVE_ACK")
    with pytest.raises(SemanticStateError):
        repository.claim_delivery(
            workspace_ref=finalization.read.workspace_ref,
            product_version=1,
            claimed_at=1_700_000_400.0,
            presentation_hash=_PRESENTATION_HASH,
        )


def test_settle_rejects_invalid_settlement_value(repository: ResearchStateRepository) -> None:
    finalization = _finalize(repository)
    claim = _claim(repository, finalization)
    with pytest.raises(ValueError):
        repository.settle_delivery(
            workspace_ref=finalization.read.workspace_ref,
            product_version=1,
            settlement="SOMETHING_ELSE",
            settled_at=1_700_000_300.0,
            claim_token=claim.claim_token,
        )


# ── codex 第 5 轮 P1 回归 ────────────────────────────────────────────────────


def test_migration_rebuilds_obligation_table_exactly(
    tmp_path: Path,
) -> None:
    """v3→v4 migration rebuilds the obligation table from canonical DDL.

    A drifted/malformed same-named table (e.g. missing CHECK, allowing
    state='EVIL') must be replaced with the exact canonical schema — the
    migration never trusts pre-existing table shape (codex P1).
    """
    import sqlite3

    db_path = tmp_path / "state.sqlite3"
    ResearchStateRepository(db_path, token_secret=_TOKEN_SECRET, epoch="test-epoch")
    # 手工降级 schema_version 为 3 并放一个畸形 obligation 表（无 CHECK）
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS daily_workspace_obligations")
        conn.execute(
            "CREATE TABLE daily_workspace_obligations("
            " workspace_ref TEXT NOT NULL, product_version INTEGER NOT NULL,"
            " artifact_hash TEXT NOT NULL, presentation_hash TEXT,"
            " state TEXT NOT NULL, claim_token TEXT, claimed_at REAL,"
            " settled_at REAL, settlement TEXT, created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL,"
            " PRIMARY KEY (workspace_ref, product_version)"
            ")"
        )
        conn.execute("UPDATE semantic_state_meta SET schema_version=3 WHERE id=1")
        conn.commit()
    # 重新打开触发 v3→v4→v5 迁移
    ResearchStateRepository(db_path, token_secret=_TOKEN_SECRET, epoch="test-epoch")
    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id=1"
        ).fetchone()[0]
        assert version == SCHEMA_VERSION
        # 畸形表被 canonical 重建：state='EVIL' 必须被 CHECK 拒绝
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_workspace_obligations("
                "workspace_ref, product_version, artifact_hash,"
                "presentation_hash, state, created_at, updated_at"
                ") VALUES ('w', 1, 'a', NULL, 'EVIL', 0, 0)"
            )


def test_finalize_rejects_pre_existing_conflicting_obligation(
    repository: ResearchStateRepository,
) -> None:
    """A pre-existing obligation with a different artifact_hash fails closed.

    The conflict check guards against DB drift: an obligation row already
    present for the same (workspace_ref, product_version) with a mismatched
    artifact_hash must never be silently trusted.  Same-key replay is
    idempotent, so this exercises the defensive path via a drifted row.
    """
    import sqlite3

    finalization = _finalize(repository)
    # 篡改 obligation 的 artifact_hash（模拟 DB 损坏/外部写）
    with sqlite3.connect(repository._db_path) as conn:
        conn.execute(
            "UPDATE daily_workspace_obligations SET artifact_hash='sha256:WRONG' "
            "WHERE workspace_ref=? AND product_version=?",
            (finalization.read.workspace_ref, 1),
        )
        conn.commit()
    # 手动预置一个同 (workspace_ref, product_version) 但 artifact 错误的行，
    # 再触发 append 建 obligation → 必须 fail-closed
    with pytest.raises(SemanticStateError) as excinfo:
        repository.finalize_scheduled_checkpoint(
            principal_id="test-principal",
            trading_day_id="2026-08-04",
            idempotency_key="daily:2026-08-04:recovery",
            checkpoint="morning",
            product=_scheduled_product(),
            now=1_700_000_200.0,
            presentation_hash=_PRESENTATION_HASH,
        )
    assert excinfo.value.code in {
        "daily_workspace_obligation_conflict",
        "continuation_conflict",
    }
