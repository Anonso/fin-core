from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from fin_analyse.consultation.daily_workspace import DailyWorkspaceService
from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)
from fin_analyse.consultation.daily_workspace_schedule import DailyWorkspaceSchedulePolicy
from fin_analyse.consultation.presentation import project_consultation_presentation
from fin_analyse.guo_teacher_research.semantic_state import ResearchStateRepository
from fin_analyse.operations.daily_workspace_delivery import (
    DailyWorkspaceDeliveryError,
    DailyWorkspaceExplicitSendFailureError,
    DispatchAcceptanceOutcome,
    HermesCliMessageSender,
    SqliteDailyWorkspaceDeliveryOutbox,
)
from fin_analyse.operations.daily_workspace_runner import (
    DailyWorkspaceCheckpointRunner,
    DailyWorkspaceCheckpointRunRequest,
    DailyWorkspaceRunPhase,
    DailyWorkspaceRunStatus,
    DailyWorkspaceStateProductAdapter,
    PreparedDailyWorkspaceProduct,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAY = "2026-08-03"
_TARGET = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
_PREPARED = datetime(2026, 8, 3, 9, 35, tzinfo=_SHANGHAI)
_GENERATED = datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI)
_EVIDENCE_CUTOFF = datetime(2026, 8, 3, 9, 50, tzinfo=_SHANGHAI)
_DELIVERED = datetime(2026, 8, 3, 10, 0, 1, tzinfo=_SHANGHAI)
_ARTIFACT_HASH = "sha256:" + "b" * 64
_PRESENTATION_HASH = "sha256:" + "c" * 64


class _Repository:
    def __init__(self, read: object) -> None:
        self.read = read
        self.calls: list[dict[str, str]] = []

    def find_daily_workspace_version_by_key(self, **kwargs: str) -> object:
        self.calls.append(kwargs)
        return self.read


class _Sender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> str:
        self.messages.append(message)
        return "message-1"


class _UnknownSender:
    def send(self, message: str) -> None:
        del message
        raise RuntimeError("secret-target secret-message")


class _ExplicitlyFailOnceSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> str:
        self.messages.append(message)
        if len(self.messages) == 1:
            raise DailyWorkspaceExplicitSendFailureError()
        return "message-1"


class _AcceptancePort:
    """B0: fake dispatch-acceptance port (records calls)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, int, str, str | None]] = []

    def record_dispatch_acceptance(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        acceptance: object,
    ) -> None:
        self.records.append(
            (
                workspace_ref,
                product_version,
                acceptance.outcome.value,
                acceptance.message_id,
            )
        )


class _ObligationPort:
    """Fake FIN-owned delivery obligation port (schema v4 seam).

    Tracks claim/settle calls; settle outcome captured per version.  Retry
    scenarios share one instance across reopened outboxes to mirror the
    persistent FIN state owner.
    """

    def __init__(self) -> None:
        self.claims: list[tuple[str, int]] = []
        self.settlements: list[tuple[str, int, str]] = []
        self._state: dict[tuple[str, int], str] = {}
        self._tokens: dict[tuple[str, int], str] = {}
        self._settlement: dict[tuple[str, int], str] = {}

    def claim_delivery(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        claimed_at: float,
        presentation_hash: str,
    ) -> object:
        self.claims.append((workspace_ref, product_version))
        key = (workspace_ref, product_version)
        if self._state.get(key) not in (None, "PENDING"):
            error = RuntimeError("daily_delivery_obligation_not_pending")
            error.code = "daily_delivery_obligation_not_pending"  # type: ignore[attr-defined]
            raise error
        token = f"token-{len(self.claims)}"
        self._state[key] = "CLAIMED"
        self._tokens[key] = token
        return SimpleNamespace(
            workspace_ref=workspace_ref,
            product_version=product_version,
            presentation_hash=_PRESENTATION_HASH,
            claimed_at=claimed_at,
            claim_token=token,
        )

    def settle_delivery(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        settlement: str,
        settled_at: float,
        claim_token: str,
    ) -> None:
        key = (workspace_ref, product_version)
        if self._state.get(key) == "PENDING":
            error = RuntimeError("daily_delivery_obligation_not_claimed")
            error.code = "daily_delivery_obligation_not_claimed"  # type: ignore[attr-defined]
            raise error
        if self._tokens.get(key) != claim_token:
            error = RuntimeError("daily_delivery_claim_token_mismatch")
            error.code = "daily_delivery_claim_token_mismatch"  # type: ignore[attr-defined]
            raise error
        if self._state.get(key) == "SETTLED":
            if self._settlement.get(key) == settlement:
                return
            error = RuntimeError("daily_delivery_obligation_settlement_conflict")
            error.code = "daily_delivery_obligation_settlement_conflict"  # type: ignore[attr-defined]
            raise error
        if self._state.get(key) != "CLAIMED":
            error = RuntimeError("daily_delivery_obligation_not_claimed")
            error.code = "daily_delivery_obligation_not_claimed"  # type: ignore[attr-defined]
            raise error
        self.settlements.append((workspace_ref, product_version, settlement))
        if settlement == "EXPLICIT_NOT_SENT":
            self._state[key] = "PENDING"  # not terminal: retry may re-claim
            self._tokens.pop(key, None)
        else:
            self._state[key] = "SETTLED"
            self._settlement[key] = settlement


class _BlockingSender:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.messages: list[str] = []

    def send(self, message: str) -> str:
        self.messages.append(message)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test sender was not released")
        return "message-1"


class _ForbiddenRuntime:
    def handle(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("delivery must not call the consultation runtime")

    def generate(self, **_kwargs: object) -> object:
        raise AssertionError("delivery must not call the full generator")


def _consultation_product() -> dict[str, object]:
    return {
        "contract_id": "consultation_product",
        "contract_version": "v1",
        "answer_text": "核对盘中变化",
    }


def _stored_product(*, degraded: bool = False) -> dict[str, object]:
    return {
        "schema_version": "fin.daily_workspace_product/v1",
        "workspace_ref": "dw:opaque",
        "trading_day_id": _DAY,
        "checkpoint": "morning",
        "origin": "scheduled",
        "generated_via": ("deterministic-degraded-v1" if degraded else "consultation-chain-v1"),
        "product_version": 2,
        "parent_product_version": 1,
        "consultation_status": "partial" if degraded else "completed",
        "degraded": degraded,
        "agent_provenance": {
            "runtime_invoked_at_generation": True,
            "output_used": not degraded,
            **(
                {}
                if degraded
                else {}
            ),
        },
        "delivery_timing": {
            "schema": "fin.daily-workspace-delivery-timing/v1",
            "target_at": _TARGET.isoformat(),
            "prepared_at": _PREPARED.isoformat(),
            "generated_at": _GENERATED.isoformat(),
            "evidence_cutoff_at": (None if degraded else _EVIDENCE_CUTOFF.isoformat()),
        },
        "context_boundaries": {
            "prior_product": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
            "user_question": "NOT_EVIDENCE",
        },
        "input_snapshot_receipt": {
            "schema": "fin.daily-workspace-input-receipt/v1",
            "consultation_as_of": _EVIDENCE_CUTOFF.isoformat(),
        },
        "first_screen": {
            "top_items": (
                []
                if degraded
                else [{"item": "核对盘中变化", "disposition": "OBSERVE"}]
            ),
            "rationale": (
                []
                if degraded
                else [
                    {
                        "text": "最新证据仍支持观察。",
                        "source_class": "G_DERIVED_COGNITION",
                        "as_of": _EVIDENCE_CUTOFF.isoformat(),
                        "freshness_status": "READY",
                    }
                ]
            ),
            "changes_vs_previous": (
                [] if degraded else [{"text": "成交结构出现新变量。"}]
            ),
            "unknowns": (
                ["model_route_unavailable"] if degraded else ["成交结构仍待确认"]
            ),
            "portfolio_review": (
                []
                if degraded
                else [
                    {
                        "account_mode": "PAPER",
                        "status": "READY",
                        "position_count": 2,
                        "as_of": _EVIDENCE_CUTOFF.isoformat(),
                    }
                ]
            ),
        },
        "data_gaps": ["model_route_unavailable"] if degraded else [],
        "consultation_product": (
            None if degraded else _consultation_product()
        ),
    }


def _prepared(*, degraded: bool = False) -> PreparedDailyWorkspaceProduct:
    return PreparedDailyWorkspaceProduct(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        workspace_ref="dw:opaque",
        product_version=2,
        artifact_hash=_ARTIFACT_HASH,
        target_at=_TARGET,
        prepared_at=_PREPARED,
        generated_at=_GENERATED,
        evidence_cutoff_at=None if degraded else _EVIDENCE_CUTOFF,
        degraded=degraded,
        data_gaps=(("model_route_unavailable",) if degraded else ()),
    )


def _read(*, degraded: bool = False) -> object:
    return SimpleNamespace(
        workspace_ref="dw:opaque",
        trading_day_id=_DAY,
        product_version=2,
        status="partial" if degraded else "completed",
        artifact_hash=_ARTIFACT_HASH,
        as_of=_GENERATED.timestamp(),
        created_at=_GENERATED.timestamp(),
        product=_stored_product(degraded=degraded),
    )


def test_dispatch_sends_exact_stored_product_and_returns_receipt(tmp_path: Path) -> None:
    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    receipt = outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert receipt.artifact_hash == _ARTIFACT_HASH
    assert receipt.delivered_at == _DELIVERED
    assert receipt.already_delivered is False
    assert repository.calls == [
        {
            "principal_id": "finp_daily",
            "trading_day_id": _DAY,
            "idempotency_key": "daily:2026-08-03:morning",
        }
    ]
    expected = project_consultation_presentation(
        {
            "schema_version": "fin.consultation/v1",
            "action": "daily_workspace_scheduled",
            "status": "completed",
            "as_of": _GENERATED.isoformat(),
            "workspace_ref": "dw:opaque",
            "product": _stored_product(),
            "data_gaps": [],
        }
    )["text"]
    assert sender.messages == [expected]
    assert sender.messages[0].startswith("核对盘中变化")
    assert "FIN Daily Workspace" not in sender.messages[0]
    assert "###" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        message, presentation_hash = connection.execute(
            "SELECT message, presentation_hash FROM daily_workspace_delivery_outbox"
        ).fetchone()
    assert message == expected
    assert (
        presentation_hash
        == "sha256:" + hashlib.sha256(expected.encode("utf-8", errors="strict")).hexdigest()
    )
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_outbox_rejects_symlinked_parent_before_creating_database(tmp_path: Path) -> None:
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o700)
    outbox_root = tmp_path / "outbox"
    outbox_root.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        SqliteDailyWorkspaceDeliveryOutbox(
            db_path=outbox_root / "delivery.sqlite3",
            repository=_Repository(_read()),
            principal_id="finp_daily",
            sender=_Sender(),
            obligation_port=_ObligationPort(),
            acceptance_port=_AcceptancePort(),
        )

    assert error.value.code == "DAILY_WORKSPACE_OUTBOX_PATH_INVALID"
    assert not (redirected / "delivery.sqlite3").exists()


def test_concurrent_dispatch_sends_once_and_second_caller_fails_closed(
    tmp_path: Path,
) -> None:
    sender = _BlockingSender()
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(outbox.dispatch, _prepared(), delivered_at=_DELIVERED)
        assert sender.started.wait(timeout=5)
        try:
            with pytest.raises(DailyWorkspaceDeliveryError) as error:
                outbox.dispatch(_prepared(), delivered_at=_DELIVERED)
            assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
        finally:
            sender.release.set()
        assert first.result(timeout=5).already_delivered is False

    assert len(sender.messages) == 1


def test_real_state_owner_missing_product_delivers_one_failure_notice(
    tmp_path: Path,
) -> None:
    repository = ResearchStateRepository(
        tmp_path / "state.sqlite3",
        token_secret=b"s" * 32,
    )
    principal = SimpleNamespace(principal_id="finp_daily")
    forbidden = _ForbiddenRuntime()
    service = DailyWorkspaceService(
        consultation_runner=forbidden,  # type: ignore[arg-type]
        state_repository=repository,
        clock=lambda: _DELIVERED,
    )
    products = DailyWorkspaceStateProductAdapter(
        service=service,  # type: ignore[arg-type]
        repository=repository,
        generator=forbidden,
        principal=principal,
        clock=lambda: _DELIVERED,
    )
    sender = _Sender()
    runner = DailyWorkspaceCheckpointRunner(
        schedule=DailyWorkspaceSchedulePolicy(is_open_date=lambda _value: True),
        products=products,
        outbox=SqliteDailyWorkspaceDeliveryOutbox(
            db_path=tmp_path / "outbox" / "delivery.sqlite3",
            repository=repository,
            principal_id=principal.principal_id,
            sender=sender,
            obligation_port=repository,  # real FIN-owned schema v4 seam
            acceptance_port=_AcceptancePort(),
        ),
        clock=lambda: _DELIVERED,
    )

    request = DailyWorkspaceCheckpointRunRequest(
        trading_day=date(2026, 8, 3),
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        phase=DailyWorkspaceRunPhase.DELIVER,
    )
    result = runner.run(request)
    replay = runner.run(request)

    assert result.status is DailyWorkspaceRunStatus.FAILURE_NOTICE_DELIVERED
    assert replay.status is DailyWorkspaceRunStatus.FAILURE_NOTICE_DELIVERED
    assert replay.artifact_hash == result.artifact_hash
    assert result.artifact_hash is not None
    assert result.artifact_hash.startswith("sha256:")
    assert result.data_gaps == ("daily_workspace_prepared_product_missing",)
    assert len(sender.messages) == 1
    assert "定时咨询未能生成结论" in sender.messages[0]
    assert "daily_workspace_prepared_product_missing" not in sender.messages[0]
    assert (
        repository.find_daily_workspace_version_by_key(
            principal_id=principal.principal_id,
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:morning",
        )
        is None
    )
    assert (
        repository.find_daily_workspace_version_by_key(
            principal_id=principal.principal_id,
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:morning:delivery-fallback",
        )
        is not None
    )


def test_delivered_artifact_retry_is_persistent_and_does_not_send_again(
    tmp_path: Path,
) -> None:
    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    first = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    first.dispatch(_prepared(), delivered_at=_DELIVERED)
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )

    assert receipt.already_delivered is True
    assert receipt.delivered_at == _DELIVERED
    assert len(sender.messages) == 1


def test_delivered_message_id_resolves_exact_workspace_binding_after_restart(
    tmp_path: Path,
) -> None:
    """A positive platform ACK is the sole durable reply-binding source."""

    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    first = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    sent = first.dispatch(_prepared(), delivered_at=_DELIVERED)

    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    binding = reopened.find_delivered_workspace_by_message_id(
        principal_id="finp_daily",
        message_id="message-1",
    )

    assert sent.message_id == "message-1"
    assert binding is not None
    assert binding.workspace_ref == "dw:opaque"
    assert binding.trading_day_id == _DAY
    assert binding.checkpoint == "morning"
    assert binding.product_version == 2
    assert binding.artifact_hash == _ARTIFACT_HASH
    assert binding.delivered_at == _DELIVERED
    assert (
        reopened.find_delivered_workspace_by_message_id(
            principal_id="other-principal",
            message_id="message-1",
        )
        is None
    )
    replay = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )
    assert replay.already_delivered is True
    assert replay.message_id == "message-1"
    assert len(sender.messages) == 1


def test_restart_recovers_positive_ack_before_local_binding_is_finalized(
    tmp_path: Path,
) -> None:
    """A crash after the ACK/settlement must not discard the exact binding."""

    class _CrashAfterAcceptanceOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _record_acceptance_settling(
            self,
            product: PreparedDailyWorkspaceProduct,
            *,
            outcome: DispatchAcceptanceOutcome,
            message_id: str | None,
            observed_at: datetime,
            settlement: str,
            settled_at: datetime,
            claim_token: str,
        ) -> None:
            super()._record_acceptance_settling(
                product,
                outcome=outcome,
                message_id=message_id,
                observed_at=observed_at,
                settlement=settlement,
                settled_at=settled_at,
                claim_token=claim_token,
            )
            raise RuntimeError("simulated process crash after positive ACK")

    sender = _Sender()
    acceptance = _AcceptancePort()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashAfterAcceptanceOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=acceptance,
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=acceptance,
    )

    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )

    assert receipt.already_delivered is True
    assert receipt.delivered_at == _DELIVERED
    assert receipt.message_id == "message-1"
    assert sender.messages
    assert len(sender.messages) == 1
    binding = reopened.find_delivered_workspace_by_message_id(
        principal_id="finp_daily",
        message_id="message-1",
    )
    assert binding is not None
    assert binding.workspace_ref == "dw:opaque"


def test_restart_replays_ledger_ack_after_ledger_commit_before_local_stage(
    tmp_path: Path,
) -> None:
    """The ledger's committed ACK is replayed idempotently after a crash."""

    from fin_analyse.operations.daily_workspace_delivery import (
        PublicEntryLedgerDispatchAcceptancePort,
    )
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    class _CrashAfterLedgerAcceptanceOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _record_acceptance(
            self,
            product: PreparedDailyWorkspaceProduct,
            *,
            outcome: DispatchAcceptanceOutcome,
            message_id: str | None,
            observed_at: datetime,
            claim_token: str,
        ) -> None:
            super()._record_acceptance(
                product,
                outcome=outcome,
                message_id=message_id,
                observed_at=observed_at,
                claim_token=claim_token,
            )
            raise RuntimeError("simulated process crash after ledger acceptance")

    sender = _Sender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    ledger_path = tmp_path / "runtime-truth.sqlite3"
    ledger = PublicEntryLedger(ledger_path, realm="production")
    acceptance = PublicEntryLedgerDispatchAcceptancePort(
        ledger=ledger,
        principal_id="finp_daily",
    )
    first = _CrashAfterLedgerAcceptanceOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=acceptance,
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=acceptance,
    )
    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )

    assert receipt.already_delivered is True
    assert receipt.delivered_at == _DELIVERED
    assert receipt.message_id == "message-1"
    assert len(sender.messages) == 1
    assert (
        reopened.find_delivered_workspace_by_message_id(
            principal_id="finp_daily",
            message_id="message-1",
        )
        is not None
    )
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM public_entry_delivery_events"
        ).fetchone() == (1,)


def test_concurrent_recovery_finalizes_one_existing_positive_ack_binding(
    tmp_path: Path,
) -> None:
    """Concurrent restarts converge through the final outbox CAS, never send."""

    class _CrashAfterAcceptanceOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _record_acceptance_settling(
            self,
            product: PreparedDailyWorkspaceProduct,
            *,
            outcome: DispatchAcceptanceOutcome,
            message_id: str | None,
            observed_at: datetime,
            settlement: str,
            settled_at: datetime,
            claim_token: str,
        ) -> None:
            super()._record_acceptance_settling(
                product,
                outcome=outcome,
                message_id=message_id,
                observed_at=observed_at,
                settlement=settlement,
                settled_at=settled_at,
                claim_token=claim_token,
            )
            raise RuntimeError("simulated process crash after positive ACK")

    class _BarrierRecoveryOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        _barrier: Barrier

        def _finalize_positive_ack_binding(
            self,
            *,
            product: PreparedDailyWorkspaceProduct,
            presentation_hash: str,
            message: str,
            message_id: str,
            claim_token: str,
            delivered_at: datetime,
        ) -> None:
            self._barrier.wait(timeout=5)
            super()._finalize_positive_ack_binding(
                product=product,
                presentation_hash=presentation_hash,
                message=message,
                message_id=message_id,
                claim_token=claim_token,
                delivered_at=delivered_at,
            )

    sender = _Sender()
    acceptance = _AcceptancePort()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashAfterAcceptanceOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=acceptance,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    barrier = Barrier(2)
    recovered = [
        _BarrierRecoveryOutbox(
            db_path=db_path,
            repository=_Repository(_read()),
            principal_id="finp_daily",
            sender=sender,
            obligation_port=obligation,
            acceptance_port=acceptance,
        )
        for _ in range(2)
    ]
    for outbox in recovered:
        outbox._barrier = barrier
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda outbox: outbox.dispatch(
                    _prepared(),
                    delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
                ),
                recovered,
            )
        )

    assert all(receipt.already_delivered for receipt in receipts)
    assert all(receipt.message_id == "message-1" for receipt in receipts)
    assert len(sender.messages) == 1
    assert (
        recovered[0].find_delivered_workspace_by_message_id(
            principal_id="finp_daily",
            message_id="message-1",
        )
        is not None
    )


def test_concurrent_recovery_accepts_reordered_real_ledger_attempts(
    tmp_path: Path,
) -> None:
    """A duplicate ledger attempt may write the claim event before `new`."""

    from fin_analyse.operations.daily_workspace_delivery import (
        PublicEntryLedgerDispatchAcceptancePort,
    )
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryAttempt,
        PublicEntryLedger,
    )

    class _CrashBeforeAcceptanceOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _record_acceptance(
            self,
            product: PreparedDailyWorkspaceProduct,
            *,
            outcome: DispatchAcceptanceOutcome,
            message_id: str | None,
            observed_at: datetime,
            claim_token: str,
        ) -> None:
            del product, outcome, message_id, observed_at, claim_token
            raise RuntimeError("simulated process crash before ledger acceptance")

    class _ReorderedLedger(PublicEntryLedger):
        def __init__(self, db_path: Path) -> None:
            super().__init__(db_path, realm="production")
            self._begun = Barrier(2)
            self._duplicate_recorded = Event()
            self._new_attempt_id: str | None = None

        def begin(
            self,
            *,
            tool_name: str,
            principal_namespace: str,
            principal_id: str,
            idempotency_key: str | None,
            request_payload: object,
        ) -> PublicEntryAttempt:
            attempt = super().begin(
                tool_name=tool_name,
                principal_namespace=principal_namespace,
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if attempt.dedupe_disposition == "new":
                self._new_attempt_id = attempt.attempt_id
            self._begun.wait(timeout=5)
            return attempt

        def record_delivery_event(
            self,
            *,
            event_id: str,
            attempt_id: str,
            channel: str,
            stage: str,
            status: str,
            source_contract: str | None = None,
            message_id: str | None = None,
            allow_same_request_replay: bool = False,
        ) -> None:
            if attempt_id == self._new_attempt_id:
                assert self._duplicate_recorded.wait(timeout=5)
            super().record_delivery_event(
                event_id=event_id,
                attempt_id=attempt_id,
                channel=channel,
                stage=stage,
                status=status,
                source_contract=source_contract,
                message_id=message_id,
                allow_same_request_replay=allow_same_request_replay,
            )
            if attempt_id != self._new_attempt_id:
                self._duplicate_recorded.set()

    sender = _Sender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashBeforeAcceptanceOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    ledger_path = tmp_path / "runtime-truth.sqlite3"
    ledger = _ReorderedLedger(ledger_path)
    acceptance = PublicEntryLedgerDispatchAcceptancePort(
        ledger=ledger,
        principal_id="finp_daily",
    )
    recovered = [
        SqliteDailyWorkspaceDeliveryOutbox(
            db_path=db_path,
            repository=_Repository(_read()),
            principal_id="finp_daily",
            sender=sender,
            obligation_port=obligation,
            acceptance_port=acceptance,
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda outbox: outbox.dispatch(
                    _prepared(),
                    delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
                ),
                recovered,
            )
        )

    assert all(receipt.already_delivered for receipt in receipts)
    assert all(receipt.message_id == "message-1" for receipt in receipts)
    assert len(sender.messages) == 1
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM public_entry_delivery_events"
        ).fetchone() == (1,)


def test_legacy_outbox_migrates_before_recording_message_binding(tmp_path: Path) -> None:
    db_path = tmp_path / "delivery.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE daily_workspace_delivery_outbox(
                artifact_hash TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                trading_day_id TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                workspace_ref TEXT NOT NULL,
                product_version INTEGER NOT NULL,
                presentation_hash TEXT NOT NULL,
                message TEXT NOT NULL,
                state TEXT NOT NULL CHECK(
                    state IN ('DISPATCHING', 'DELIVERED', 'FAILED')
                ),
                attempted_at TEXT NOT NULL,
                delivered_at TEXT
            )
            """
        )
    db_path.chmod(0o600)

    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=_Sender(),
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(daily_workspace_delivery_outbox)")
        }
    assert {"message_id", "claim_token", "acceptance_outcome", "settlement"} <= columns
    assert (
        outbox.find_delivered_workspace_by_message_id(
            principal_id="finp_daily",
            message_id="message-1",
        )
        is not None
    )


def test_duplicate_platform_message_id_never_rebinds_another_workspace(
    tmp_path: Path,
) -> None:
    sender = _Sender()
    repository = _Repository(_read())
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    replacement_hash = "sha256:" + "d" * 64
    replacement = replace(
        _prepared(),
        workspace_ref="dw:second",
        product_version=3,
        artifact_hash=replacement_hash,
    )
    replacement_read = _read()
    replacement_read.workspace_ref = "dw:second"
    replacement_read.product_version = 3
    replacement_read.artifact_hash = replacement_hash
    replacement_read.product = {
        **_stored_product(),
        "workspace_ref": "dw:second",
        "product_version": 3,
    }
    repository.read = replacement_read

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        outbox.dispatch(replacement, delivered_at=_DELIVERED)

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
    binding = outbox.find_delivered_workspace_by_message_id(
        principal_id="finp_daily",
        message_id="message-1",
    )
    assert binding is not None
    assert binding.workspace_ref == "dw:opaque"
    assert len(sender.messages) == 2


def test_outbox_rejects_database_replacement_before_retry(tmp_path: Path) -> None:
    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)
    replacement_path = tmp_path / "replacement.sqlite3"
    SqliteDailyWorkspaceDeliveryOutbox(
        db_path=replacement_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    replacement_path.replace(db_path)

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        outbox.dispatch(
            _prepared(),
            delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
        )

    assert error.value.code == "DAILY_WORKSPACE_OUTBOX_PATH_INVALID"
    assert len(sender.messages) == 1


def test_unknown_send_outcome_stays_dispatching_and_retry_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _Repository(_read())
    db_path = tmp_path / "delivery.sqlite3"
    obligation = _ObligationPort()
    first = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=_UnknownSender(),
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as first_error:
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert first_error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
    assert "secret" not in str(first_error.value)
    assert (
        first.find_delivered_workspace_by_message_id(
            principal_id="finp_daily",
            message_id="message-1",
        )
        is None
    )
    retry_sender = _Sender()
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=retry_sender,
        obligation_port=obligation,  # shared: same persistent obligation lifecycle
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(DailyWorkspaceDeliveryError) as retry_error:
        reopened.dispatch(_prepared(), delivered_at=_DELIVERED)
    assert retry_error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
    assert retry_sender.messages == []


def test_explicit_send_failure_can_retry_same_immutable_message(tmp_path: Path) -> None:
    repository = _Repository(_read())
    sender = _ExplicitlyFailOnceSender()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    with pytest.raises(DailyWorkspaceExplicitSendFailureError) as first_error:
        outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert first_error.value.code == "DAILY_WORKSPACE_DELIVERY_SEND_FAILED"
    receipt = outbox.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 0, 2, tzinfo=_SHANGHAI),
    )
    assert receipt.already_delivered is False
    assert len(sender.messages) == 2
    assert sender.messages[0] == sender.messages[1]


def test_restart_after_explicit_settlement_marks_failed_and_allows_retry(
    tmp_path: Path,
) -> None:
    """A crash after semantic release still returns the outbox to FAILED."""

    class _CrashBeforeFailedOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _mark_explicit_send_failure(
            self,
            *,
            product: PreparedDailyWorkspaceProduct,
            message_id: str | None,
            claim_token: str,
        ) -> None:
            del product, message_id, claim_token
            raise RuntimeError("simulated process crash before explicit failure mark")

    repository = _Repository(_read())
    sender = _ExplicitlyFailOnceSender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashBeforeFailedOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(DailyWorkspaceExplicitSendFailureError):
        reopened.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert len(sender.messages) == 1
    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 0, 2, tzinfo=_SHANGHAI),
    )

    assert receipt.message_id == "message-1"
    assert len(sender.messages) == 2
    assert sender.messages[0] == sender.messages[1]


def test_explicit_recovery_rejects_a_newer_claim_without_sending(tmp_path: Path) -> None:
    """A stale explicit stage cannot finalize after another claim wins."""

    class _CrashBeforeFailedOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _mark_explicit_send_failure(
            self,
            *,
            product: PreparedDailyWorkspaceProduct,
            message_id: str | None,
            claim_token: str,
        ) -> None:
            del product, message_id, claim_token
            raise RuntimeError("simulated process crash before explicit failure mark")

    repository = _Repository(_read())
    sender = _ExplicitlyFailOnceSender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashBeforeFailedOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    newer_claim = obligation.claim_delivery(
        workspace_ref="dw:opaque",
        product_version=2,
        claimed_at=_DELIVERED.timestamp(),
        presentation_hash=_PRESENTATION_HASH,
    )
    assert newer_claim.claim_token == "token-2"
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        reopened.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OBLIGATION_UNAVAILABLE"
    assert len(sender.messages) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM daily_workspace_delivery_outbox"
        ).fetchone() == ("DISPATCHING",)


def test_hermes_sender_uses_fixed_argv_and_message_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> object:
        invocation["argv"] = argv
        invocation.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "platform": "feishu",
                    "chat_id": "daily-owner",
                    "message_id": "message-1",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "fin_analyse.operations.daily_workspace_delivery.subprocess.run",
        fake_run,
    )
    sender = HermesCliMessageSender(target="feishu:daily-owner", timeout_seconds=12.0)

    message_id = sender.send("private daily workspace message")

    assert message_id == "message-1"

    assert invocation == {
        "argv": [
            "hermes",
            "--profile",
            "fin",
            "send",
            "--to",
            "feishu:daily-owner",
            "--file",
            "-",
            "--json",
        ],
        "input": "private daily workspace message",
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "capture_output": True,
        "timeout": 12.0,
        "check": False,
    }


def test_dispatch_rejects_stored_timing_drift_before_send(tmp_path: Path) -> None:
    read = _read()
    timing = read.product["delivery_timing"]  # type: ignore[attr-defined]
    timing["generated_at"] = datetime(  # type: ignore[index]
        2026,
        8,
        3,
        9,
        52,
        tzinfo=_SHANGHAI,
    ).isoformat()
    sender = _Sender()
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=_Repository(read),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert error.value.code == "DAILY_WORKSPACE_STORED_PRODUCT_MISMATCH"
    assert sender.messages == []


def test_dispatch_accepts_normal_product_without_g_receipt(
    tmp_path: Path,
) -> None:
    read = _read()
    sender = _Sender()
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=_Repository(read),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    receipt = outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert receipt.message_id == "message-1"
    assert sender.messages


def test_degraded_delivery_names_state_and_data_gaps(tmp_path: Path) -> None:
    sender = _Sender()
    repository = _Repository(_read(degraded=True))
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    outbox.dispatch(_prepared(degraded=True), delivered_at=_DELIVERED)

    assert sender.messages[0].startswith("定时咨询失败通知：")
    assert "原因：另有部分数据缺口未逐项展示，已由 FIN 记录" in sender.messages[0]
    assert "model_route_unavailable" not in sender.messages[0]
    assert "边界：仅供咨询" not in sender.messages[0]
    assert repository.calls[0]["idempotency_key"].endswith(":delivery-fallback")


@pytest.mark.parametrize(
    ("field", "different"),
    (
        ("workspace_ref", "dw:different"),
        ("product_version", 3),
        ("artifact_hash", "c" * 64),
    ),
)
def test_dispatch_rejects_stored_version_identity_drift(
    tmp_path: Path,
    field: str,
    different: object,
) -> None:
    read = _read()
    setattr(read, field, different)
    sender = _Sender()
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=_Repository(read),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert error.value.code == "DAILY_WORKSPACE_STORED_PRODUCT_MISMATCH"
    assert sender.messages == []


def test_hermes_failure_does_not_disclose_target_or_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rejected(argv: list[str], **kwargs: object) -> object:
        del argv, kwargs
        return SimpleNamespace(
            returncode=2,
            stderr="lark:private-target private-message",
        )

    monkeypatch.setattr(
        "fin_analyse.operations.daily_workspace_delivery.subprocess.run",
        rejected,
    )
    sender = HermesCliMessageSender(target="feishu:private-target")

    with pytest.raises(DailyWorkspaceExplicitSendFailureError) as error:
        sender.send("private-message")

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_SEND_FAILED"
    assert "private" not in str(error.value)


def test_hermes_backend_failure_has_unknown_outcome_and_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(argv: list[str], **kwargs: object) -> object:
        del argv, kwargs
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(
        "fin_analyse.operations.daily_workspace_delivery.subprocess.run",
        failed,
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        HermesCliMessageSender(target="feishu:daily-owner").send("message")

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"


@pytest.mark.parametrize(
    "payload",
    (
        {"success": True, "platform": "feishu", "message_id": ""},
        {"success": True, "platform": "feishu"},
    ),
)
def test_hermes_zero_exit_requires_positive_feishu_message_ack(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    def ambiguous(argv: list[str], **kwargs: object) -> object:
        del argv, kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(
        "fin_analyse.operations.daily_workspace_delivery.subprocess.run",
        ambiguous,
    )

    # B0: 平台接受成功但未返回 message_id → send 返回 None（outcome 未知由
    # outbox 记录 OUTCOME_UNKNOWN 终态），不再于 sender 内 raise（raise 会
    # 绕过 acceptance 落账）。
    message_id = HermesCliMessageSender(target="feishu:daily-owner").send("message")
    assert message_id is None


@pytest.mark.parametrize(
    "payload",
    (
        {"success": True, "skipped": True, "platform": "feishu"},
        {"success": True, "platform": "lark", "message_id": "message-1"},
        {"success": False, "platform": "feishu", "message_id": "message-1"},
    ),
)
def test_hermes_send_rejects_non_positive_ack(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    """非正向 ack（skipped/lark/失败）保持 fail closed。"""

    def ambiguous(argv: list[str], **kwargs: object) -> object:
        del argv, kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(
        "fin_analyse.operations.daily_workspace_delivery.subprocess.run",
        ambiguous,
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        HermesCliMessageSender(target="feishu:daily-owner").send("message")

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"


def test_hermes_zero_exit_with_malformed_json_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(argv: list[str], **kwargs: object) -> object:
        del argv, kwargs
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(
        "fin_analyse.operations.daily_workspace_delivery.subprocess.run",
        malformed,
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        HermesCliMessageSender(target="feishu:daily-owner").send("message")

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"


@pytest.mark.parametrize(
    "target",
    ("daily-owner", "feishu:", "lark:daily-owner", "http://daily-owner"),
)
def test_hermes_target_must_name_feishu(target: str) -> None:
    with pytest.raises(ValueError):
        HermesCliMessageSender(target=target)


def test_dispatch_records_acceptance_with_message_id(tmp_path: Path) -> None:
    """B0: 成功派发把 dispatch acceptance（message_id）落 ledger。"""

    from datetime import UTC
    from datetime import datetime as _dt

    from fin_analyse.operations.daily_workspace_delivery import (
        PublicEntryLedgerDispatchAcceptancePort,
    )
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    clock = [_dt(2026, 8, 1, 10, 0, tzinfo=UTC)]

    def _clock() -> _dt:
        return clock[0]

    ledger = PublicEntryLedger(
        db_path=tmp_path / "runtime-truth.sqlite3",
        realm="production",
        clock=_clock,
    )
    port = PublicEntryLedgerDispatchAcceptancePort(
        ledger=ledger,
        principal_id="finp_daily",
    )
    sender = _Sender()
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=tmp_path / "delivery.sqlite3",
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=port,
    )

    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    import sqlite3 as _sqlite3

    con = _sqlite3.connect(tmp_path / "runtime-truth.sqlite3")
    event = con.execute(
        "SELECT stage, status, message_id FROM public_entry_delivery_events"
    ).fetchone()
    attempt = con.execute(
        "SELECT tool_name FROM public_entry_attempts a "
        "JOIN public_entry_requests r ON r.request_id = a.request_id "
        "WHERE r.tool_name='daily_workspace_delivery'"
    ).fetchone()
    con.close()
    assert event == ("dispatched", "succeeded", "message-1")
    assert attempt is not None


def test_dispatch_acceptance_replay_uses_one_claim_scoped_ledger_event(
    tmp_path: Path,
) -> None:
    """A post-ACK restart records one acceptance fact, not a duplicate."""

    from datetime import UTC
    from datetime import datetime as _dt

    from fin_analyse.operations.daily_workspace_delivery import (
        DispatchAcceptanceOutcome,
        DispatchAcceptanceRecord,
        PublicEntryLedgerDispatchAcceptancePort,
    )
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    ledger = PublicEntryLedger(
        db_path=tmp_path / "runtime-truth.sqlite3",
        realm="production",
        clock=lambda: _dt(2026, 8, 1, 10, 0, tzinfo=UTC),
    )
    port = PublicEntryLedgerDispatchAcceptancePort(
        ledger=ledger,
        principal_id="finp_daily",
    )
    acceptance = DispatchAcceptanceRecord(
        platform="feishu",
        message_id="message-1",
        observed_at=_DELIVERED,
        outcome=DispatchAcceptanceOutcome.SUCCEEDED,
        claim_token="claim-token-1",
    )

    port.record_dispatch_acceptance(
        workspace_ref="dw:opaque",
        product_version=2,
        acceptance=acceptance,
    )
    port.record_dispatch_acceptance(
        workspace_ref="dw:opaque",
        product_version=2,
        acceptance=acceptance,
    )

    with sqlite3.connect(tmp_path / "runtime-truth.sqlite3") as connection:
        attempts = connection.execute("SELECT count(*) FROM public_entry_attempts").fetchone()
        events = connection.execute(
            "SELECT count(*), message_id FROM public_entry_delivery_events"
        ).fetchone()
    assert attempts == (2,)
    assert events == (1, "message-1")


def test_acceptance_write_failure_still_settles_outcome_unknown(tmp_path: Path) -> None:
    """B0: acceptance 落账失败 → 仍建立 durable OUTCOME_UNKNOWN 终态（不悬挂）。"""

    class FailingAcceptancePort:
        def record_dispatch_acceptance(self, **kwargs: object) -> None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")

    sender = _Sender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=FailingAcceptancePort(),
    )

    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        outbox.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
    assert obligation.settlements
    assert obligation.settlements[-1][2] == "OUTCOME_UNKNOWN"
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(DailyWorkspaceDeliveryError) as replay_error:
        reopened.dispatch(_prepared(), delivered_at=_DELIVERED)
    assert replay_error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
    assert len(sender.messages) == 1
    assert (
        reopened.find_delivered_workspace_by_message_id(
            principal_id="finp_daily",
            message_id="message-1",
        )
        is None
    )


def test_restart_settles_unknown_after_crash_between_local_unknown_stage_and_semantic_settlement(
    tmp_path: Path,
) -> None:
    """A durable unknown stage closes its same claim after a process crash."""

    class FailingAcceptancePort:
        def record_dispatch_acceptance(self, **kwargs: object) -> None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")

    class _CrashBeforeUnknownSettlementOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _settle(
            self,
            *,
            product: PreparedDailyWorkspaceProduct,
            settlement: str,
            settled_at: datetime,
            claim_token: str,
        ) -> None:
            if settlement == "OUTCOME_UNKNOWN":
                raise RuntimeError("simulated process crash before unknown settlement")
            super()._settle(
                product=product,
                settlement=settlement,
                settled_at=settled_at,
                claim_token=claim_token,
            )

    sender = _Sender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashBeforeUnknownSettlementOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=FailingAcceptancePort(),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)

    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(DailyWorkspaceDeliveryError) as error:
        reopened.dispatch(_prepared(), delivered_at=_DELIVERED)

    assert error.value.code == "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
    assert obligation.settlements[-1][2] == "OUTCOME_UNKNOWN"
    assert len(sender.messages) == 1
    assert (
        reopened.find_delivered_workspace_by_message_id(
            principal_id="finp_daily",
            message_id="message-1",
        )
        is None
    )


# ── 上下文预注入治理 Slice B：跨 renderer/release replay（B4）──────────────


def _renderer_b_projection(payload: object) -> dict[str, str]:
    """模拟新 release 的 renderer：文案不同（绝不等于旧 message）。"""
    del payload
    return {"text": "新渲染器文案：与旧 release 完全不同。"}


def test_cross_renderer_replay_of_delivered_artifact_reuses_stored_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """已落 outbox 的 message/artifact 是 replay owner：新 renderer 不得
    重渲染、不得重发、不得报 conflict。"""
    import fin_analyse.operations.daily_workspace_delivery as delivery_module

    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    first = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    first.dispatch(_prepared(), delivered_at=_DELIVERED)
    assert len(sender.messages) == 1

    monkeypatch.setattr(delivery_module, "project_consultation_presentation", _renderer_b_projection)
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )

    assert receipt.already_delivered is True
    assert receipt.delivered_at == _DELIVERED
    assert len(sender.messages) == 1


def test_renderer_raising_during_replay_of_delivered_artifact_still_recovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DELIVERED 重放绝不依赖当前 renderer：renderer 直接抛错也必须恢复。"""
    import fin_analyse.operations.daily_workspace_delivery as delivery_module

    def _raising_renderer(payload: object) -> dict[str, str]:
        del payload
        raise RuntimeError("renderer B broken")

    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    first = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    first.dispatch(_prepared(), delivered_at=_DELIVERED)

    monkeypatch.setattr(delivery_module, "project_consultation_presentation", _raising_renderer)
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )

    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )

    assert receipt.already_delivered is True
    assert len(sender.messages) == 1


def test_cross_renderer_recovery_of_positive_ack_reuses_stored_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """已 staging（POSITIVE_ACK）恢复同样以 stored message 为 owner。"""

    class _CrashAfterAcceptanceOutbox(SqliteDailyWorkspaceDeliveryOutbox):
        def _record_acceptance_settling(
            self,
            product: PreparedDailyWorkspaceProduct,
            *,
            outcome: DispatchAcceptanceOutcome,
            message_id: str | None,
            observed_at: datetime,
            settlement: str,
            settled_at: datetime,
            claim_token: str,
        ) -> None:
            super()._record_acceptance_settling(
                product,
                outcome=outcome,
                message_id=message_id,
                observed_at=observed_at,
                settlement=settlement,
                settled_at=settled_at,
                claim_token=claim_token,
            )
            raise RuntimeError("simulated process crash after positive ACK")

    import fin_analyse.operations.daily_workspace_delivery as delivery_module

    sender = _Sender()
    obligation = _ObligationPort()
    db_path = tmp_path / "delivery.sqlite3"
    first = _CrashAfterAcceptanceOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.dispatch(_prepared(), delivered_at=_DELIVERED)
    assert len(sender.messages) == 1

    monkeypatch.setattr(delivery_module, "project_consultation_presentation", _renderer_b_projection)
    reopened = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=_Repository(_read()),
        principal_id="finp_daily",
        sender=sender,
        obligation_port=obligation,
        acceptance_port=_AcceptancePort(),
    )

    receipt = reopened.dispatch(
        _prepared(),
        delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
    )

    assert receipt.already_delivered is True
    assert receipt.message_id == "message-1"
    assert len(sender.messages) == 1


def test_stored_empty_message_never_replays_and_conflicts(tmp_path: Path) -> None:
    """旧 message 为空 → 不重渲染、不重发，按既有 conflict 语义。"""
    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)
    import sqlite3 as sqlite3_module

    with sqlite3_module.connect(db_path) as connection:
        connection.execute(
            "UPDATE daily_workspace_delivery_outbox SET message = ''"
        )

    with pytest.raises(
        DailyWorkspaceDeliveryError,
        match="DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT",
    ):
        outbox.dispatch(
            _prepared(),
            delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
        )
    assert len(sender.messages) == 1


def test_stored_hash_drift_never_replays_and_conflicts(tmp_path: Path) -> None:
    """sha256(message) != presentation_hash → conflict，绝不静默复用。"""
    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)
    import sqlite3 as sqlite3_module

    with sqlite3_module.connect(db_path) as connection:
        connection.execute(
            "UPDATE daily_workspace_delivery_outbox SET presentation_hash = 'sha256:deadbeef'"
        )

    with pytest.raises(
        DailyWorkspaceDeliveryError,
        match="DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT",
    ):
        outbox.dispatch(
            _prepared(),
            delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
        )
    assert len(sender.messages) == 1


def test_typed_identity_drift_conflicts_even_when_message_replayable(tmp_path: Path) -> None:
    """typed 身份（checkpoint 等）漂移 → conflict；stored message 可重放
    也不得冒充该身份的既有投递。"""
    repository = _Repository(_read())
    sender = _Sender()
    db_path = tmp_path / "delivery.sqlite3"
    outbox = SqliteDailyWorkspaceDeliveryOutbox(
        db_path=db_path,
        repository=repository,
        principal_id="finp_daily",
        sender=sender,
        obligation_port=_ObligationPort(),
        acceptance_port=_AcceptancePort(),
    )
    outbox.dispatch(_prepared(), delivered_at=_DELIVERED)
    import sqlite3 as sqlite3_module

    with sqlite3_module.connect(db_path) as connection:
        connection.execute(
            "UPDATE daily_workspace_delivery_outbox SET checkpoint = 'midday'"
        )

    with pytest.raises(
        DailyWorkspaceDeliveryError,
        match="DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT",
    ):
        outbox.dispatch(
            _prepared(),
            delivered_at=datetime(2026, 8, 3, 10, 1, tzinfo=_SHANGHAI),
        )
    assert len(sender.messages) == 1
