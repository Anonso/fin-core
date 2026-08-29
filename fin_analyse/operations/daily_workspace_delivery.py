"""Durable, idempotent delivery for one immutable Daily Workspace artifact."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from fin_analyse.consultation.daily_workspace_product_contracts import (
    is_public_daily_workspace_product,
)
from fin_analyse.consultation.presentation import project_consultation_presentation
from fin_analyse.operations.daily_workspace_runner import (
    DailyWorkspaceDeliveryReceipt,
    PreparedDailyWorkspaceProduct,
)
from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger


class DailyWorkspaceMessageSender(Protocol):
    def send(self, message: str) -> str | None: ...


class DispatchAcceptanceOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class DispatchAcceptanceRecord:
    """B0: dispatch acceptance 事实（平台接受发送，非 delivery 回执）。"""

    platform: str
    message_id: str | None
    observed_at: datetime
    outcome: DispatchAcceptanceOutcome
    claim_token: str


@dataclass(frozen=True, slots=True)
class DeliveredDailyWorkspaceBinding:
    """One exact workspace version accepted by the platform for reply lookup."""

    principal_id: str
    message_id: str
    workspace_ref: str
    trading_day_id: str
    checkpoint: str
    product_version: int
    artifact_hash: str
    delivered_at: datetime


class _DispatchAcceptancePort(Protocol):
    """FIN ledger dispatch-acceptance seam（B0）。

    Repeating the same claim token must record the same acceptance fact
    idempotently.  Recovery may replay only after the platform returned a
    durable message ID; implementations must never treat that replay as a
    second external send.
    """

    def record_dispatch_acceptance(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        acceptance: DispatchAcceptanceRecord,
    ) -> None: ...


class _WorkspaceVersionRepository(Protocol):
    def find_daily_workspace_version_by_key(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> object | None: ...


class _DeliveryObligationPort(Protocol):
    """FIN-owned delivery obligation seam (schema v4)."""

    def claim_delivery(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        claimed_at: float,
        presentation_hash: str,
    ) -> object: ...

    def settle_delivery(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        settlement: str,
        settled_at: float,
        claim_token: str,
    ) -> None: ...


class DailyWorkspaceDeliveryError(RuntimeError):
    """Stable, non-disclosing delivery failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DailyWorkspaceExplicitSendFailureError(DailyWorkspaceDeliveryError):
    """The sender explicitly proved that no message was accepted."""

    def __init__(self) -> None:
        super().__init__("DAILY_WORKSPACE_DELIVERY_SEND_FAILED")


class HermesCliMessageSender:
    """Send one text message through the fixed FIN Hermes profile."""

    def __init__(self, *, target: str, timeout_seconds: float = 30.0) -> None:
        if (
            not isinstance(target, str)
            or target.strip() != target
            or not target.partition(":")[2]
            or not target.startswith("feishu:")
        ):
            raise ValueError("Hermes target must explicitly use feishu:")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("Hermes send timeout must be positive")
        self._target = target
        self._timeout_seconds = float(timeout_seconds)

    def send(self, message: str) -> str | None:
        if not isinstance(message, str) or not message:
            raise ValueError("Hermes message must be non-empty text")
        try:
            completed = subprocess.run(
                [
                    "hermes",
                    "--profile",
                    "fin",
                    "send",
                    "--to",
                    self._target,
                    "--file",
                    "-",
                    "--json",
                ],
                input=message,
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except Exception:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN") from None
        if completed.returncode == 2:
            raise DailyWorkspaceExplicitSendFailureError()
        if completed.returncode != 0:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN") from None
        message_id = payload.get("message_id") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or payload.get("skipped", False) is not False
            or payload.get("platform") != "feishu"
        ):
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")
        # B0: dispatch acceptance 事实——返回平台接受发送的 message_id（可持久化）。
        # 平台接受成功但未返回 message_id：返回 None（outcome 未知），由 outbox
        # 记录 OUTCOME_UNKNOWN 终态，不在此 raise（raise 会绕过 acceptance 记录）。
        return message_id if isinstance(message_id, str) and message_id else None


class SqliteDailyWorkspaceDeliveryOutbox:
    """Read the exact FIN product and deliver it through a durable outbox."""

    def __init__(
        self,
        *,
        db_path: Path,
        repository: _WorkspaceVersionRepository,
        principal_id: str,
        sender: DailyWorkspaceMessageSender,
        obligation_port: _DeliveryObligationPort | None = None,
        acceptance_port: _DispatchAcceptancePort | None = None,
    ) -> None:
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("daily workspace delivery principal is invalid")
        if acceptance_port is None:
            # B0: dispatch acceptance 必须持久化（{platform, message_id, observed_at}）——
            # 无 acceptance port 的 outbox 不允许成功派发（fail closed，防事实丢失）。
            raise ValueError("daily workspace delivery acceptance port is required")
        self._db_path = Path(db_path)
        self._repository = repository
        self._principal_id = principal_id
        self._sender = sender
        self._obligation_port = obligation_port
        self._acceptance_port = acceptance_port
        self._db_identity: tuple[int, int] | None = None
        self._initialize()

    def find_delivered_workspace_by_message_id(
        self,
        *,
        principal_id: str,
        message_id: str,
    ) -> DeliveredDailyWorkspaceBinding | None:
        """Resolve only a durably recorded positive-ACK workspace binding.

        No row is returned for a failed, dispatching, or outcome-unknown send.
        The caller must provide the same FIN principal that owns the message;
        this is deliberately not a best-effort cross-principal search.
        """

        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("daily workspace binding principal is invalid")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("daily workspace binding message id is invalid")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT principal_id, message_id, workspace_ref, trading_day_id,
                       checkpoint, product_version, artifact_hash, delivered_at
                FROM daily_workspace_delivery_outbox
                WHERE principal_id = ? AND message_id = ? AND state = 'DELIVERED'
                """,
                (principal_id, message_id),
            ).fetchone()
        if row is None:
            return None
        try:
            delivered_at = datetime.fromisoformat(row[7])
        except (TypeError, ValueError):
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_BINDING_INVALID") from None
        if (
            not isinstance(row[0], str)
            or not row[0]
            or not isinstance(row[1], str)
            or not row[1]
            or not isinstance(row[2], str)
            or not row[2]
            or not isinstance(row[3], str)
            or not row[3]
            or not isinstance(row[4], str)
            or not row[4]
            or not isinstance(row[5], int)
            or row[5] < 1
            or not isinstance(row[6], str)
            or not row[6]
            or delivered_at.tzinfo is None
            or delivered_at.utcoffset() is None
        ):
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_BINDING_INVALID")
        return DeliveredDailyWorkspaceBinding(
            principal_id=row[0],
            message_id=row[1],
            workspace_ref=row[2],
            trading_day_id=row[3],
            checkpoint=row[4],
            product_version=row[5],
            artifact_hash=row[6],
            delivered_at=delivered_at,
        )

    def dispatch(
        self,
        product: PreparedDailyWorkspaceProduct,
        *,
        delivered_at: datetime,
    ) -> DailyWorkspaceDeliveryReceipt:
        if not isinstance(product, PreparedDailyWorkspaceProduct):
            raise TypeError("daily workspace prepared product is invalid")
        if not isinstance(delivered_at, datetime):
            raise TypeError("daily workspace delivery time is invalid")
        if delivered_at.tzinfo is None or delivered_at.utcoffset() is None:
            raise ValueError("daily workspace delivery time must be timezone-aware")

        read = self._repository.find_daily_workspace_version_by_key(
            principal_id=self._principal_id,
            trading_day_id=product.trading_day_id,
            idempotency_key=(
                f"daily:{product.trading_day_id}:{product.checkpoint.value}:delivery-fallback"
                if product.degraded
                else f"daily:{product.trading_day_id}:{product.checkpoint.value}"
            ),
        )
        stored = _verified_stored_product(read, prepared=product)
        delivered_iso = delivered_at.isoformat()
        claimed_at = delivered_at.timestamp()

        # Outbox path/identity validation runs BEFORE any obligation mutation:
        # a replaced or drifted outbox database must fail closed on its own
        # typed code, never be masked by a delivery-outcome error.
        # 上下文预注入治理（B4）：已落 outbox 的 message/artifact 是 replay
        # owner——DELIVERED/DISPATCHING 重放在任何渲染之前完成，绝不依赖
        # 当前 renderer 的重渲染结果。
        with self._connect() as connection:
            prior = connection.execute(
                """
                SELECT delivered_at, principal_id, trading_day_id, checkpoint,
                       workspace_ref, product_version, presentation_hash, message,
                       message_id
                FROM daily_workspace_delivery_outbox
                WHERE artifact_hash = ? AND state = 'DELIVERED'
                """,
                (product.artifact_hash,),
            ).fetchone()
        if prior is not None and isinstance(prior[0], str):
            # Already delivered by an earlier attempt: return the prior
            # receipt without re-claiming ONLY when the typed identity matches
            # and the stored message is self-consistent (codex P1: identity
            # drift or broken stored bytes must fail closed, not be reported
            # as a successful replay).
            typed_identity_match = (
                str(prior[1]) == self._principal_id
                and str(prior[2]) == product.trading_day_id
                and str(prior[3]) == product.checkpoint.value
                and str(prior[4]) == product.workspace_ref
                and int(prior[5]) == product.product_version
            )
            if typed_identity_match and _stored_presentation_is_replayable(
                prior[7],
                prior[6],
            ):
                return DailyWorkspaceDeliveryReceipt(
                    artifact_hash=product.artifact_hash,
                    delivered_at=datetime.fromisoformat(prior[0]),
                    already_delivered=True,
                    message_id=prior[8] if isinstance(prior[8], str) and prior[8] else None,
                )
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT")

        recovered = self._recover_known_dispatch(product=product)
        if recovered is not None:
            return recovered

        message = _render_message(stored, read=read, prepared=product)
        presentation_hash = (
            "sha256:" + hashlib.sha256(message.encode("utf-8", errors="strict")).hexdigest()
        )

        # FIN-owned obligation drives delivery admission (schema v4): claim
        # grants exactly one attempt; settle records the honest outcome.
        # Without an obligation port the delivery fails closed — the sibling
        # outbox alone must never decide whether to send (handoff 11:39).
        if self._obligation_port is None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OBLIGATION_UNAVAILABLE")
        try:
            claim = self._obligation_port.claim_delivery(
                workspace_ref=product.workspace_ref,
                product_version=product.product_version,
                claimed_at=claimed_at,
                presentation_hash=presentation_hash,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "daily_delivery_obligation_not_pending":
                # Already claimed (another sender in flight) or settled: the
                # outcome is genuinely unknown or already delivered.
                raise DailyWorkspaceDeliveryError(
                    "DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN"
                ) from exc
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_OBLIGATION_UNAVAILABLE"
            ) from exc
        claim_token = getattr(claim, "claim_token", None)
        if not isinstance(claim_token, str) or not claim_token:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OBLIGATION_UNAVAILABLE")

        # Freeze the exact message into the sibling outbox (idempotent replay
        # guard), then send.  The outbox no longer owns send authorization.
        # Any staging failure (connection/BEGIN/SELECT/INSERT/UPDATE) must
        # release the claim so the obligation is not left permanently CLAIMED
        # (codex P1).
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT principal_id, trading_day_id, checkpoint, workspace_ref,
                           product_version, presentation_hash, message, state, delivered_at,
                           message_id, claim_token, acceptance_outcome, settlement
                    FROM daily_workspace_delivery_outbox
                    WHERE artifact_hash = ?
                    """,
                    (product.artifact_hash,),
                ).fetchone()
                identity = (
                    self._principal_id,
                    product.trading_day_id,
                    product.checkpoint.value,
                    product.workspace_ref,
                    product.product_version,
                    presentation_hash,
                    message,
                )
                if existing is not None:
                    # FAILED/EXPLICIT_NOT_SENT 重试保持既有全量 identity
                    # 语义（fresh render 与 stored 必须逐字一致）；跨
                    # renderer 漂移时按既有 conflict 语义 fail closed，
                    # 不静默重渲染（B4 冻结：FAILED retry → 既有失败语义）。
                    if existing[:7] != identity:
                        # Outbox conflict: the claim must be released so the
                        # obligation is not left permanently CLAIMED (codex P1).
                        self._settle(
                            product=product,
                            settlement="EXPLICIT_NOT_SENT",
                            settled_at=delivered_at,
                            claim_token=claim_token,
                        )
                        raise DailyWorkspaceDeliveryError(
                            "DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT"
                        )
                    if existing[7] == "DELIVERED" and isinstance(existing[8], str):
                        prior_delivery = datetime.fromisoformat(existing[8])
                        return DailyWorkspaceDeliveryReceipt(
                            artifact_hash=product.artifact_hash,
                            delivered_at=prior_delivery,
                            already_delivered=True,
                            message_id=(
                                existing[9]
                                if isinstance(existing[9], str) and existing[9]
                                else None
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE daily_workspace_delivery_outbox
                        SET state = 'DISPATCHING', attempted_at = ?, claim_token = ?,
                            message_id = NULL, acceptance_outcome = NULL, settlement = NULL
                        WHERE artifact_hash = ?
                        """,
                        (delivered_iso, claim_token, product.artifact_hash),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO daily_workspace_delivery_outbox(
                            artifact_hash, principal_id, trading_day_id, checkpoint,
                            workspace_ref, product_version, presentation_hash, message,
                            state, attempted_at, claim_token
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DISPATCHING', ?, ?)
                        """,
                        (product.artifact_hash, *identity, delivered_iso, claim_token),
                    )
        except Exception as staging_error:
            # Staging failed before send: release the claim so the obligation
            # is not left permanently CLAIMED (codex P1).  This covers typed
            # DailyWorkspaceDeliveryError too (e.g. _connect() path drift) —
            # except the conflict path which settled already (its exception
            # propagates untouched, no double settle).
            if (
                isinstance(staging_error, DailyWorkspaceDeliveryError)
                and staging_error.code == "DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT"
            ):
                raise
            self._settle(
                product=product,
                settlement="EXPLICIT_NOT_SENT",
                settled_at=delivered_at,
                claim_token=claim_token,
            )
            if isinstance(staging_error, DailyWorkspaceDeliveryError):
                raise
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_OUTBOX_STAGING_FAILED"
            ) from None

        try:
            accepted_message_id = self._sender.send(message)
        except DailyWorkspaceExplicitSendFailureError:
            self._record_acceptance_settling(
                product,
                outcome=DispatchAcceptanceOutcome.OUTCOME_UNKNOWN,
                message_id=None,
                observed_at=delivered_at,
                settlement="EXPLICIT_NOT_SENT",
                settled_at=delivered_at,
                claim_token=claim_token,
            )
            self._mark_explicit_send_failure(
                product=product,
                message_id=None,
                claim_token=claim_token,
            )
            raise
        except Exception:
            # Send outcome genuinely unknown: never auto-resend.  The claim is
            # settled as OUTCOME_UNKNOWN so the obligation cannot be re-claimed.
            self._record_settlement_intent(
                product=product,
                message_id=None,
                claim_token=claim_token,
                settlement="OUTCOME_UNKNOWN",
            )
            self._settle(
                product=product,
                settlement="OUTCOME_UNKNOWN",
                settled_at=delivered_at,
                claim_token=claim_token,
            )
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN") from None

        if not isinstance(accepted_message_id, str) or not accepted_message_id:
            # B0: send 返回成功但未取得 message_id——outcome 未知，不得按成功落账。
            self._record_acceptance_settling(
                product,
                outcome=DispatchAcceptanceOutcome.OUTCOME_UNKNOWN,
                message_id=None,
                observed_at=delivered_at,
                settlement="OUTCOME_UNKNOWN",
                settled_at=delivered_at,
                claim_token=claim_token,
            )
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN") from None
        try:
            self._persist_message_id(
                product=product,
                message_id=accepted_message_id,
                claim_token=claim_token,
            )
        except DailyWorkspaceDeliveryError:
            # A platform ACK without a durable local binding is never eligible
            # for a resend.  Fence its semantic obligation as unknown before
            # returning the local binding failure.
            self._settle(
                product=product,
                settlement="OUTCOME_UNKNOWN",
                settled_at=delivered_at,
                claim_token=claim_token,
            )
            raise
        self._record_acceptance_settling(
            product,
            outcome=DispatchAcceptanceOutcome.SUCCEEDED,
            message_id=accepted_message_id,
            observed_at=delivered_at,
            settlement="POSITIVE_ACK",
            settled_at=delivered_at,
            claim_token=claim_token,
        )
        self._finalize_positive_ack_binding(
            product=product,
            presentation_hash=presentation_hash,
            message=message,
            message_id=accepted_message_id,
            claim_token=claim_token,
            delivered_at=delivered_at,
        )
        return DailyWorkspaceDeliveryReceipt(
            artifact_hash=product.artifact_hash,
            delivered_at=delivered_at,
            message_id=accepted_message_id,
        )

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
        """Record acceptance, then settle the already-persisted outcome."""

        self._record_settlement_intent(
            product=product,
            message_id=message_id,
            claim_token=claim_token,
            settlement=settlement,
        )
        try:
            self._record_acceptance(
                product,
                outcome=outcome,
                message_id=message_id,
                observed_at=observed_at,
                claim_token=claim_token,
            )
        except DailyWorkspaceDeliveryError:
            self._replace_settlement_with_unknown(
                product=product,
                message_id=message_id,
                claim_token=claim_token,
                expected_settlement=settlement,
            )
            self._record_acceptance_outcome(
                product=product,
                message_id=message_id,
                claim_token=claim_token,
                outcome=DispatchAcceptanceOutcome.OUTCOME_UNKNOWN,
                settlement="OUTCOME_UNKNOWN",
            )
            self._settle(
                product=product,
                settlement="OUTCOME_UNKNOWN",
                settled_at=settled_at,
                claim_token=claim_token,
            )
            raise
        self._record_acceptance_outcome(
            product=product,
            message_id=message_id,
            claim_token=claim_token,
            outcome=outcome,
            settlement=settlement,
        )
        self._settle(
            product=product,
            settlement=settlement,
            settled_at=settled_at,
            claim_token=claim_token,
        )

    def _record_acceptance(
        self,
        product: PreparedDailyWorkspaceProduct,
        *,
        outcome: DispatchAcceptanceOutcome,
        message_id: str | None,
        observed_at: datetime,
        claim_token: str,
    ) -> None:
        if self._acceptance_port is None:
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_ACCEPTANCE_PORT_UNAVAILABLE"
            )
        try:
            self._acceptance_port.record_dispatch_acceptance(
                workspace_ref=product.workspace_ref,
                product_version=product.product_version,
                acceptance=DispatchAcceptanceRecord(
                    platform="feishu",
                    message_id=message_id,
                    observed_at=observed_at,
                    outcome=outcome,
                    claim_token=claim_token,
                ),
            )
        except Exception:
            # B0: acceptance 事实必须持久化——落账失败 = outcome 未知（fail closed），
            # 由上层按 OUTCOME_UNKNOWN 处理（不自动重发）。
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN") from None

    def _persist_message_id(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
        message_id: str,
        claim_token: str,
    ) -> None:
        """Fence a known platform ACK and its intended settlement together."""

        try:
            with self._connect() as connection:
                update = connection.execute(
                    """
                    UPDATE daily_workspace_delivery_outbox
                    SET message_id = ?, settlement = 'POSITIVE_ACK'
                    WHERE artifact_hash = ? AND state = 'DISPATCHING'
                      AND claim_token = ? AND message_id IS NULL
                      AND settlement IS NULL AND acceptance_outcome IS NULL
                    """,
                    (message_id, product.artifact_hash, claim_token),
                )
                if update.rowcount != 1:
                    raise DailyWorkspaceDeliveryError(
                        "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
                    )
        except DailyWorkspaceDeliveryError:
            raise
        except Exception:
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None

    def _record_settlement_intent(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
        message_id: str | None,
        claim_token: str,
        settlement: str,
    ) -> None:
        """Persist the semantic outcome to recover before settling it."""

        try:
            with self._connect() as connection:
                update = connection.execute(
                    """
                    UPDATE daily_workspace_delivery_outbox
                    SET settlement = ?
                    WHERE artifact_hash = ? AND state = 'DISPATCHING'
                      AND claim_token = ? AND message_id IS ?
                      AND (settlement IS NULL OR settlement = ?)
                    """,
                    (
                        settlement,
                        product.artifact_hash,
                        claim_token,
                        message_id,
                        settlement,
                    ),
                )
                if update.rowcount != 1:
                    raise DailyWorkspaceDeliveryError(
                        "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
                    )
        except DailyWorkspaceDeliveryError:
            raise
        except Exception:
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None

    def _replace_settlement_with_unknown(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
        message_id: str | None,
        claim_token: str,
        expected_settlement: str,
    ) -> None:
        """Terminally downgrade an acceptance-recording failure, once."""

        try:
            with self._connect() as connection:
                update = connection.execute(
                    """
                    UPDATE daily_workspace_delivery_outbox
                    SET settlement = 'OUTCOME_UNKNOWN'
                    WHERE artifact_hash = ? AND state = 'DISPATCHING'
                      AND claim_token = ? AND message_id IS ?
                      AND settlement = ? AND acceptance_outcome IS NULL
                    """,
                    (
                        product.artifact_hash,
                        claim_token,
                        message_id,
                        expected_settlement,
                    ),
                )
                if update.rowcount != 1:
                    raise DailyWorkspaceDeliveryError(
                        "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
                    )
        except DailyWorkspaceDeliveryError:
            raise
        except Exception:
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None

    def _record_acceptance_outcome(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
        message_id: str | None,
        claim_token: str,
        outcome: DispatchAcceptanceOutcome,
        settlement: str,
    ) -> None:
        """Persist the acceptance stage before the semantic settlement.

        A recovered row with a known message id is only eligible to finish
        when this stage is ``succeeded``.  The ordering deliberately makes a
        failed acceptance sticky before its ``OUTCOME_UNKNOWN`` settlement.
        """

        try:
            with self._connect() as connection:
                update = connection.execute(
                    """
                    UPDATE daily_workspace_delivery_outbox
                    SET acceptance_outcome = ?
                    WHERE artifact_hash = ? AND state = 'DISPATCHING'
                      AND claim_token = ? AND message_id IS ?
                      AND settlement = ?
                      AND (acceptance_outcome IS NULL OR acceptance_outcome = ?)
                    """,
                    (
                        outcome.value,
                        product.artifact_hash,
                        claim_token,
                        message_id,
                        settlement,
                        outcome.value,
                    ),
                )
                if update.rowcount != 1:
                    prior = connection.execute(
                        """
                        SELECT state, claim_token, message_id, acceptance_outcome, settlement
                        FROM daily_workspace_delivery_outbox WHERE artifact_hash = ?
                        """,
                        (product.artifact_hash,),
                    ).fetchone()
                    if prior == (
                        "DELIVERED",
                        claim_token,
                        message_id,
                        outcome.value,
                        settlement,
                    ):
                        return
                    raise DailyWorkspaceDeliveryError(
                        "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
                    )
        except DailyWorkspaceDeliveryError:
            raise
        except Exception:
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None

    def _recover_known_dispatch(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
    ) -> DailyWorkspaceDeliveryReceipt | None:
        """Finish a crashed post-send attempt without sending another message.

        B4：stored message/artifact 是 replay owner——typed 身份匹配且
        stored message 自洽（非空 + hash 一致）即复用；绝不依赖当前
        renderer 的重渲染。
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT principal_id, trading_day_id, checkpoint, workspace_ref,
                       product_version, presentation_hash, message, attempted_at,
                       message_id, claim_token, acceptance_outcome, settlement
                FROM daily_workspace_delivery_outbox
                WHERE artifact_hash = ? AND state = 'DISPATCHING'
                """,
                (product.artifact_hash,),
            ).fetchone()
        if row is None:
            return None
        typed_identity_match = (
            str(row[0]) == self._principal_id
            and str(row[1]) == product.trading_day_id
            and str(row[2]) == product.checkpoint.value
            and str(row[3]) == product.workspace_ref
            and int(row[4]) == product.product_version
        )
        if not typed_identity_match or not _stored_presentation_is_replayable(
            row[6],
            row[5],
        ):
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_ARTIFACT_CONFLICT")
        message_id = row[8]
        claim_token = row[9]
        acceptance_outcome = row[10]
        settlement = row[11]
        if not isinstance(claim_token, str) or not claim_token:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")
        try:
            attempted_at = datetime.fromisoformat(row[7])
        except (TypeError, ValueError):
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")

        if settlement is None:
            # This can be a live sender between staging and its first durable
            # post-send fact.  Do not steal or settle its claim; callers fail
            # closed and a process restart may only recover a later persisted
            # outcome stage.
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")

        if settlement == "POSITIVE_ACK":
            if not isinstance(message_id, str) or not message_id:
                raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")
            if acceptance_outcome is None:
                self._record_acceptance_settling(
                    product,
                    outcome=DispatchAcceptanceOutcome.SUCCEEDED,
                    message_id=message_id,
                    observed_at=attempted_at,
                    settlement=settlement,
                    settled_at=attempted_at,
                    claim_token=claim_token,
                )
            elif acceptance_outcome == DispatchAcceptanceOutcome.SUCCEEDED.value:
                self._settle(
                    product=product,
                    settlement=settlement,
                    settled_at=attempted_at,
                    claim_token=claim_token,
                )
            else:
                raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")
            self._finalize_positive_ack_binding(
                product=product,
                presentation_hash=row[5],
                message=row[6],
                message_id=message_id,
                claim_token=claim_token,
                delivered_at=attempted_at,
            )
            return DailyWorkspaceDeliveryReceipt(
                artifact_hash=product.artifact_hash,
                delivered_at=attempted_at,
                already_delivered=True,
                message_id=message_id,
            )

        if settlement not in {"OUTCOME_UNKNOWN", "EXPLICIT_NOT_SENT"}:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")
        if acceptance_outcome is None:
            self._record_acceptance_settling(
                product,
                outcome=DispatchAcceptanceOutcome.OUTCOME_UNKNOWN,
                message_id=message_id,
                observed_at=attempted_at,
                settlement=settlement,
                settled_at=attempted_at,
                claim_token=claim_token,
            )
        elif acceptance_outcome == DispatchAcceptanceOutcome.OUTCOME_UNKNOWN.value:
            if settlement == "EXPLICIT_NOT_SENT":
                self._settle(
                    product=product,
                    settlement=settlement,
                    settled_at=attempted_at,
                    claim_token=claim_token,
                    # An explicit-not-sent settlement clears the semantic
                    # claim. A crash after that write but before this local
                    # row becomes FAILED is recoverable only from this
                    # durable, same-token stage.
                    allow_recovered_explicit_not_sent=True,
                )
            else:
                self._settle(
                    product=product,
                    settlement=settlement,
                    settled_at=attempted_at,
                    claim_token=claim_token,
                )
        else:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")
        if settlement == "EXPLICIT_NOT_SENT":
            self._mark_explicit_send_failure(
                product=product,
                message_id=message_id,
                claim_token=claim_token,
            )
            raise DailyWorkspaceExplicitSendFailureError()
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")

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
        try:
            with self._connect() as connection:
                update = connection.execute(
                    """
                    UPDATE daily_workspace_delivery_outbox
                    SET state = 'DELIVERED', delivered_at = ?
                    WHERE artifact_hash = ? AND state = 'DISPATCHING'
                      AND claim_token = ? AND message_id = ?
                      AND acceptance_outcome = ?
                      AND settlement = 'POSITIVE_ACK'
                    """,
                    (
                        delivered_at.isoformat(),
                        product.artifact_hash,
                        claim_token,
                        message_id,
                        DispatchAcceptanceOutcome.SUCCEEDED.value,
                    ),
                )
                if update.rowcount == 1:
                    return
                prior = connection.execute(
                    """
                    SELECT principal_id, trading_day_id, checkpoint, workspace_ref,
                           product_version, presentation_hash, message, message_id, claim_token,
                           settlement
                    FROM daily_workspace_delivery_outbox
                    WHERE artifact_hash = ? AND state = 'DELIVERED'
                    """,
                    (product.artifact_hash,),
                ).fetchone()
                identity = (
                    self._principal_id,
                    product.trading_day_id,
                    product.checkpoint.value,
                    product.workspace_ref,
                    product.product_version,
                    presentation_hash,
                    message,
                    message_id,
                    claim_token,
                    "POSITIVE_ACK",
                )
                if prior == identity:
                    return
                raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")
        except DailyWorkspaceDeliveryError:
            raise
        except Exception:
            # Platform acceptance and FIN obligation settlement are already
            # durable; a local lookup failure must never cause a resend or a
            # guessed message→workspace association.
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None

    def _mark_explicit_send_failure(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
        message_id: str | None,
        claim_token: str,
    ) -> None:
        try:
            with self._connect() as connection:
                update = connection.execute(
                    """
                    UPDATE daily_workspace_delivery_outbox
                    SET state = 'FAILED'
                    WHERE artifact_hash = ? AND state = 'DISPATCHING'
                      AND claim_token = ? AND message_id IS ?
                      AND acceptance_outcome = ? AND settlement = 'EXPLICIT_NOT_SENT'
                    """,
                    (
                        product.artifact_hash,
                        claim_token,
                        message_id,
                        DispatchAcceptanceOutcome.OUTCOME_UNKNOWN.value,
                    ),
                )
                if update.rowcount == 1:
                    return
                prior = connection.execute(
                    """
                    SELECT state, claim_token, message_id, acceptance_outcome, settlement
                    FROM daily_workspace_delivery_outbox WHERE artifact_hash = ?
                    """,
                    (product.artifact_hash,),
                ).fetchone()
                if prior == (
                    "FAILED",
                    claim_token,
                    message_id,
                    DispatchAcceptanceOutcome.OUTCOME_UNKNOWN.value,
                    "EXPLICIT_NOT_SENT",
                ):
                    return
                raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE")
        except DailyWorkspaceDeliveryError:
            raise
        except Exception:
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_BINDING_UNAVAILABLE"
            ) from None

    def _settle(
        self,
        *,
        product: PreparedDailyWorkspaceProduct,
        settlement: str,
        settled_at: datetime,
        claim_token: str,
        allow_recovered_explicit_not_sent: bool = False,
    ) -> None:
        """Settle one semantic claim, with one narrowly fenced recovery case."""
        if self._obligation_port is None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OBLIGATION_UNAVAILABLE")
        try:
            self._obligation_port.settle_delivery(
                workspace_ref=product.workspace_ref,
                product_version=product.product_version,
                settlement=settlement,
                settled_at=settled_at.timestamp(),
                claim_token=claim_token,
            )
        except Exception as exc:
            if (
                allow_recovered_explicit_not_sent
                and settlement == "EXPLICIT_NOT_SENT"
                and getattr(exc, "code", None) == "daily_delivery_obligation_not_claimed"
            ):
                return
            raise DailyWorkspaceDeliveryError(
                "DAILY_WORKSPACE_DELIVERY_OBLIGATION_UNAVAILABLE"
            ) from exc

    def _initialize(self) -> None:
        if not self._db_path.is_absolute():
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID")
        parent_identity = _secure_outbox_directory(self._db_path.parent)
        try:
            metadata = self._db_path.lstat()
        except FileNotFoundError:
            descriptor = os.open(
                self._db_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
            metadata = self._db_path.lstat()
        except OSError as error:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID") from error
        _validate_outbox_file(metadata)
        self._db_identity = (metadata.st_dev, metadata.st_ino)
        _revalidate_outbox_directory(self._db_path.parent, parent_identity)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_workspace_delivery_outbox(
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
                    delivered_at TEXT,
                    message_id TEXT,
                    claim_token TEXT,
                    acceptance_outcome TEXT,
                    settlement TEXT
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(daily_workspace_delivery_outbox)")
            }
            if "message_id" not in columns:
                connection.execute(
                    "ALTER TABLE daily_workspace_delivery_outbox ADD COLUMN message_id TEXT"
                )
            if "claim_token" not in columns:
                connection.execute(
                    "ALTER TABLE daily_workspace_delivery_outbox ADD COLUMN claim_token TEXT"
                )
            if "acceptance_outcome" not in columns:
                connection.execute(
                    "ALTER TABLE daily_workspace_delivery_outbox ADD COLUMN acceptance_outcome TEXT"
                )
            if "settlement" not in columns:
                connection.execute(
                    "ALTER TABLE daily_workspace_delivery_outbox ADD COLUMN settlement TEXT"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    daily_workspace_delivery_outbox_principal_message_id
                ON daily_workspace_delivery_outbox(principal_id, message_id)
                WHERE message_id IS NOT NULL
                """
            )
        _revalidate_outbox_directory(self._db_path.parent, parent_identity)

    def _connect(self) -> sqlite3.Connection:
        if self._db_identity is None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID")
        parent_identity = _secure_outbox_directory(self._db_path.parent)
        try:
            _revalidate_outbox_file(self._db_path, self._db_identity)
            connection = sqlite3.connect(self._db_path)
            _revalidate_outbox_file(self._db_path, self._db_identity)
            _revalidate_outbox_directory(self._db_path.parent, parent_identity)
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except Exception:
            if "connection" in locals():
                connection.close()
            raise


def _verified_stored_product(
    read: object | None,
    *,
    prepared: PreparedDailyWorkspaceProduct,
) -> dict[str, object]:
    stored = getattr(read, "product", None)
    if not is_public_daily_workspace_product(stored):
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_G_CONTEXT_UNVERIFIED")
    timing = stored.get("delivery_timing") if isinstance(stored, dict) else None
    gaps = stored.get("data_gaps") if isinstance(stored, dict) else None
    expected_timing = {
        "schema": "fin.daily-workspace-delivery-timing/v1",
        "target_at": prepared.target_at.isoformat(),
        "prepared_at": prepared.prepared_at.isoformat(),
        "generated_at": prepared.generated_at.isoformat(),
        "evidence_cutoff_at": (
            None if prepared.evidence_cutoff_at is None else prepared.evidence_cutoff_at.isoformat()
        ),
    }
    if (
        read is None
        or getattr(read, "workspace_ref", None) != prepared.workspace_ref
        or getattr(read, "trading_day_id", None) != prepared.trading_day_id
        or getattr(read, "product_version", None) != prepared.product_version
        or getattr(read, "artifact_hash", None) != prepared.artifact_hash
        or getattr(read, "status", None) not in {"completed", "partial", "unknown"}
        or not isinstance(getattr(read, "as_of", None), (int, float))
        or isinstance(getattr(read, "as_of", None), bool)
        or not math.isfinite(float(getattr(read, "as_of", 0.0)))
        or not isinstance(stored, dict)
        or stored.get("trading_day_id") != prepared.trading_day_id
        or stored.get("checkpoint") != prepared.checkpoint.value
        or not isinstance(timing, dict)
        or any(timing.get(key) != value for key, value in expected_timing.items())
        or (stored.get("degraded") is True) is not prepared.degraded
        or not isinstance(gaps, (list, tuple))
        or tuple(gaps) != prepared.data_gaps
    ):
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_STORED_PRODUCT_MISMATCH")
    return stored


def _stored_presentation_is_replayable(
    stored_message: object,
    stored_hash: object,
) -> bool:
    """已落 outbox 的 message 是否是可信 replay owner。

    旧 message 必须非空且 sha256(message) == 存库 presentation_hash；
    否则不重渲染、不重发，按既有 conflict 语义 fail closed（B4）。
    """
    if not isinstance(stored_message, str) or not stored_message:
        return False
    if not isinstance(stored_hash, str) or not stored_hash:
        return False
    return stored_hash == (
        "sha256:" + hashlib.sha256(stored_message.encode("utf-8", errors="strict")).hexdigest()
    )


def _render_message(
    stored: dict[str, object],
    *,
    read: object,
    prepared: PreparedDailyWorkspaceProduct,
) -> str:
    projection = project_consultation_presentation(
        {
            "schema_version": "fin.consultation/v1",
            "action": "daily_workspace_scheduled",
            "status": getattr(read, "status", "unknown"),
            "as_of": datetime.fromtimestamp(
                float(getattr(read, "as_of", 0.0)),
                tz=UTC,
            ).isoformat(),
            "workspace_ref": prepared.workspace_ref,
            "product": stored,
            "data_gaps": list(prepared.data_gaps),
        }
    )
    message = projection.get("text")
    if not isinstance(message, str) or not message:
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_PRESENTATION_INVALID")
    return message


def _secure_outbox_directory(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            os.mkdir(path, mode=0o700)
            metadata = path.lstat()
        except OSError as error:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID") from error
    except OSError as error:
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID")
    return metadata.st_dev, metadata.st_ino


def _revalidate_outbox_directory(path: Path, expected: tuple[int, int]) -> None:
    if _secure_outbox_directory(path) != expected:
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID")


def _validate_outbox_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID")


def _revalidate_outbox_file(path: Path, expected: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID") from error
    _validate_outbox_file(metadata)
    if (metadata.st_dev, metadata.st_ino) != expected:
        raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_OUTBOX_PATH_INVALID")


class PublicEntryLedgerDispatchAcceptancePort:
    """B0: ledger-backed dispatch-acceptance recording.

    每次成功派发（或 outcome 未知）以独立 attempt 落账：attempt
    (tool_name='daily_workspace_delivery') + delivery event
    (stage='dispatched', status=succeeded|OUTCOME_UNKNOWN, message_id)。
    dispatch acceptance 与 exact delivery/displayed 回执永不混称。
    """

    def __init__(
        self,
        *,
        ledger: PublicEntryLedger,
        principal_id: str,
    ) -> None:
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("dispatch acceptance principal is invalid")
        self._ledger = ledger
        self._principal_id = principal_id

    def record_dispatch_acceptance(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        acceptance: DispatchAcceptanceRecord,
    ) -> None:
        if not isinstance(self._ledger, PublicEntryLedger):
            raise TypeError("dispatch acceptance ledger is invalid")
        if not isinstance(acceptance.claim_token, str) or not acceptance.claim_token:
            raise ValueError("dispatch acceptance claim token is invalid")
        attempt = self._ledger.begin(
            tool_name="daily_workspace_delivery",
            principal_namespace="local",
            principal_id=self._principal_id,
            request_payload={
                "workspace_ref": workspace_ref,
                "product_version": product_version,
            },
            idempotency_key=f"dw-dispatch:{workspace_ref}:{product_version}",
        )
        self._ledger.finish(attempt, outcome="completed")
        claim_scope = hashlib.sha256(
            acceptance.claim_token.encode("utf-8", errors="strict")
        ).hexdigest()[:32]
        self._ledger.record_delivery_event(
            event_id=f"dw-dispatch-{attempt.request_id}-{claim_scope}",
            attempt_id=attempt.attempt_id,
            channel=acceptance.platform,
            stage="dispatched",
            status=acceptance.outcome.value,
            source_contract="fin.dispatch-acceptance/v1",
            message_id=acceptance.message_id,
            allow_same_request_replay=True,
        )
