"""Daily Decision Workspace facade.

#2 (decision map): the daily workspace is ``fin_consultation`` behind a
per-trading-day semantic chain — premarket/10:00/14:20/review checkpoints and
user follow-ups are versions of the same chain.  The command surface
(open/ask) delegates to the existing consultation chain; ``open`` reads the
latest workspace version from the semantic state repository when one exists.
Scheduled checkpoint generation lands in the next slice.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from fin_analyse.consultation.contracts import (
    AgentContribution,
    AgentContributions,
    ConsultationAnswer,
    ConsultationGeneralScope,
    ConsultationNoAccountContext,
    ConsultationResult,
    ConsultationResultMeta,
    ConsultCommand,
    DailyWorkspaceAskCommand,
    DailyWorkspaceOpenCommand,
)
from fin_analyse.consultation.daily_workspace_context import (
    resolve_scheduled_workspace_context,
)
from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
    DailyWorkspaceInputSnapshot,
    bounded_prior_product_snapshot,
    is_public_daily_workspace_product,
)
from fin_analyse.guo_teacher_research.semantic_contract import (
    DailyWorkspaceContextProjection,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _ConsultationRunner(Protocol):
    """Existing consultation chain surface: handle() 是 consultation 链唯一入口。"""

    def handle(
        self,
        command: ConsultCommand,
        *,
        principal: Any,
        daily_workspace_context: DailyWorkspaceContextProjection | None = None,
        daily_workspace_deadline_at: datetime | None = None,
        daily_checkpoint: str | None = None,
    ) -> ConsultationResult: ...


class _TradingCalendar(Protocol):
    def previous_open_date(self, *, before: date, known_at: datetime) -> object: ...


class _WorkspaceStateRepository(Protocol):
    """Daily workspace state surface (semantic_state repository)."""

    def find_daily_workspace(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
    ) -> Any | None: ...

    def create_daily_workspace_chain(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        now: float,
    ) -> Any: ...

    def append_daily_workspace_version(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        contract: object,
        input_snapshot: object,
        expected_parent_product_version: int,
        status: str,
        product: object,
        now: float,
        data_gaps: tuple[str, ...] = (),
        provenance: object | None = None,
    ) -> Any: ...

    def finalize_scheduled_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        checkpoint: str,
        product: object,
        now: float,
        presentation_hash: str | None = None,
        expected_parent_product_version: int = 0,
    ) -> Any: ...

    def find_daily_workspace_version_by_key(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> Any | None: ...

    def acquire_daily_workspace_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        now: float,
    ) -> bool: ...

    def release_daily_workspace_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> None: ...


class _WorkspaceGenerator(Protocol):
    """Checkpoint product generator.

    Receives the bounded input snapshot (checkpoint identity, previous
    version facts) plus the already-bound principal, and returns the
    immutable workspace product payload.
    """

    def generate(self, *, snapshot: object, principal: Any) -> object: ...


class DailyWorkspaceService:
    """Daily workspace open/ask facade, delegating to the consultation chain.

    V1 semantics:
    - ``open`` reads today's latest workspace version without running the
      Agent; without a per-trading-day version yet, it returns an honest
      unavailable product for the workspace view.
    - ``ask`` continues today's workspace as a consultation turn (origin
      marked accordingly).
    """

    def __init__(
        self,
        consultation_runner: _ConsultationRunner,
        state_repository: _WorkspaceStateRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        calendar: _TradingCalendar | None = None,
        ask_generator: Any | None = None,
    ) -> None:
        self._consultation_runner = consultation_runner
        self._state_repository = state_repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._calendar = calendar
        # on-demand ask generates over the L1 direct lane; injectable for tests.
        self._ask_generator = ask_generator

    def open(
        self,
        command: DailyWorkspaceOpenCommand,
        *,
        principal: Any,
    ) -> ConsultationResult:
        from datetime import UTC, datetime

        trading_day = _effective_trading_day(command.trading_day_id, self._clock())
        if trading_day is None:
            return _unavailable(
                gap="daily_workspace_trading_day_invalid",
                summary="交易日标识格式无效。",
            )

        if self._state_repository is None:
            return _unavailable(
                gap="daily_workspace_state_unavailable",
                summary="Daily workspace 状态层当前不可用。",
            )
        principal_id = getattr(principal, "principal_id", None)
        if not isinstance(principal_id, str) or not principal_id:
            return _unavailable(
                gap="daily_workspace_principal_missing",
                summary="Daily workspace 无法确认请求主体。",
            )
        version = self._state_repository.find_daily_workspace(
            principal_id=principal_id,
            trading_day_id=trading_day,
        )
        if version is None:
            return _unavailable(
                gap="daily_workspace_no_version",
                summary="今日工作区尚无版本，状态层只读。",
            )

        product = getattr(version, "product", None)
        if not isinstance(product, dict):
            product = {}
        if not is_public_daily_workspace_product(product):
            return _unavailable(
                gap="daily_workspace_g_context_unverified",
                summary="既有工作区版本缺少已绑定锅老师认知的证明，未展示其咨询内容。",
            )
        workspace_ref = getattr(version, "workspace_ref", None)
        version_status = getattr(version, "status", None)
        status: Literal["completed", "partial", "unknown"] = (
            version_status if version_status in {"completed", "partial"} else "unknown"
        )
        created_at = getattr(version, "created_at", None)
        as_of = (
            datetime.fromtimestamp(created_at, tz=UTC)
            if isinstance(created_at, (int, float)) and not isinstance(created_at, bool)
            else self._clock()
        )
        stored_gaps = product.get("data_gaps")
        data_gaps = (
            tuple(str(item) for item in stored_gaps)
            if isinstance(stored_gaps, (list, tuple))
            else ()
        )
        return ConsultationResult(
            action="daily_workspace_open",
            origin="on_demand",
            workspace_ref=(workspace_ref if isinstance(workspace_ref, str) else None),
            status=status,
            analysis_profile="EXPLAIN",
            profile_reason="DAILY_WORKSPACE_READ_LATEST_VERSION",
            trigger="on_demand",
            scope=ConsultationGeneralScope(),
            as_of=as_of,
            answer=ConsultationAnswer(
                summary="已读取今日工作区最新版本。",
                disposition="NO_ACTION",
                no_action=True,
            ),
            product=product,
            agent_contributions=AgentContributions(
                guo=AgentContribution(
                    role="GUO_COGNITION",
                    status="UNKNOWN",
                    summary="open 只读工作区版本，不运行 Agent。",
                    source_boundary="G_AND_BOUNDED_EXTERNAL_EVIDENCE",
                )
            ),
            decision_context=ConsultationNoAccountContext(
                mode="NONE",
                status="NOT_REQUIRED",
                risk_status="NOT_EVALUATED",
            ),
            data_gaps=data_gaps,
            result_meta=ConsultationResultMeta(
                agent_runtime_invoked=False,
                agent_output_used=_stored_agent_output_used(product),
            ),
        )

    def scheduled(
        self,
        trading_day_id: str,
        checkpoint: str,
        *,
        principal: Any,
        generator: _WorkspaceGenerator | None = None,
    ) -> ConsultationResult:
        """FIN timer entry: generate one checkpoint version on the daily chain.

        The timer passes only the checkpoint enum; the generator produces the
        immutable product payload from the bounded input snapshot.  Without a
        generator the call honestly returns unavailable and writes nothing.
        """

        if self._state_repository is None:
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gap="daily_workspace_state_unavailable",
                summary="Daily workspace 状态层当前不可用。",
            )
        principal_id = getattr(principal, "principal_id", None)
        if not isinstance(principal_id, str) or not principal_id:
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gap="daily_workspace_principal_missing",
                summary="Daily workspace 无法确认请求主体。",
            )
        try:
            checkpoint_enum = DailyWorkspaceCheckpoint(checkpoint)
        except ValueError:
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gap="daily_workspace_checkpoint_invalid",
                summary="未知的检查点标识。",
            )
        normalized_day = _normalized_trading_day(trading_day_id)
        if normalized_day is None:
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gap="daily_workspace_trading_day_invalid",
                summary="交易日标识格式无效。",
            )
        trading_day_id = normalized_day
        if generator is None:
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gap="daily_workspace_generator_missing",
                summary="检查点生成器未接入，未写入新版本。",
            )

        now = self._clock()
        checkpoint_key = f"daily:{trading_day_id}:{checkpoint_enum.value}"
        existing = self._state_repository.find_daily_workspace_version_by_key(
            principal_id=principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=checkpoint_key,
        )
        if existing is not None:
            # 同 checkpoint 重试 → 幂等返回既有版本，不重新生成。
            return _scheduled_result(existing)
        # 链先就绪（幂等），再原子抢占 checkpoint 生成权。
        self._state_repository.create_daily_workspace_chain(
            principal_id=principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=f"daily:{trading_day_id}:init",
            now=now.timestamp(),
        )
        # B3: import 必须在 acquire 之前完成——acquire 后任何失败（含 import/模块
        # 初始化异常）都要释放 checkpoint，当日链不被永久卡死。
        from fin_analyse.operations.daily_workspace_generator import (
            DailyWorkspaceGenerationUnavailableError,
        )
        from fin_analyse.guo_teacher_research.semantic_state import (
            SemanticStateError,
        )

        if not self._state_repository.acquire_daily_workspace_checkpoint(
            principal_id=principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=checkpoint_key,
            now=now.timestamp(),
        ):
            # 另一实例正在生成同一 checkpoint；不并行合并。
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gap="daily_workspace_generation_in_progress",
                summary="该检查点正在生成中，未写入新版本。",
            )

        state_repository = self._state_repository

        def _release_checkpoint() -> str | None:
            """Best-effort release；失败返回稳定 typed 诊断，不泄露原始异常。"""
            try:
                state_repository.release_daily_workspace_checkpoint(
                    principal_id=principal_id,
                    trading_day_id=trading_day_id,
                    idempotency_key=checkpoint_key,
                )
                return None
            except Exception:
                return "daily_workspace_checkpoint_release_failed"

        # B3 缺陷修复：acquire 后的全部路径（parent 读取/snapshot 构建/生成/
        # finalize）都在保护区——任何异常（含非 SemanticStateError）都释放
        # checkpoint 并返回 typed unavailable，当日链不被永久卡死。
        try:
            previous = self._state_repository.find_daily_workspace(
                principal_id=principal_id,
                trading_day_id=trading_day_id,
            )
            parent_version = previous.product_version if previous is not None else 0
            parent_hash = previous.artifact_hash if previous is not None else None
            snapshot = {
                "schema": "fin.daily-workspace-input-snapshot/v1",
                "trading_day_id": trading_day_id,
                "checkpoint": checkpoint_enum.value,
                "parent_product_version": parent_version,
                "parent_artifact_hash": parent_hash,
            }
            snapshot["daily_workspace_context"] = resolve_scheduled_workspace_context(
                same_day_parent=previous,
                calendar=self._calendar,
                repository=self._state_repository,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
                as_of=now,
            )
            try:
                product = generator.generate(snapshot=snapshot, principal=principal)
            except DailyWorkspaceGenerationUnavailableError as error:
                release_gap = _release_checkpoint()
                return _unavailable(
                    action="daily_workspace_scheduled",
                    origin="scheduled",
                    gaps=(*error.data_gaps, *((release_gap,) if release_gap else ())),
                    summary="咨询链未形成可用检查点版本，未写入新版本。",
                    agent_runtime_invoked=error.agent_runtime_invoked,
                )
            except Exception:
                release_gap = _release_checkpoint()
                return _unavailable(
                    action="daily_workspace_scheduled",
                    origin="scheduled",
                    gaps=(
                        "daily_workspace_generation_failed",
                        *((release_gap,) if release_gap else ()),
                    ),
                    summary="检查点生成失败，未写入新版本。",
                )
            if not isinstance(product, dict):
                release_gap = _release_checkpoint()
                return _unavailable(
                    action="daily_workspace_scheduled",
                    origin="scheduled",
                    gaps=(
                        "daily_workspace_generation_invalid",
                        *((release_gap,) if release_gap else ()),
                    ),
                    summary="检查点生成产物无效，未写入新版本。",
                )
            if not is_public_daily_workspace_product(product):
                release_gap = _release_checkpoint()
                return _unavailable(
                    action="daily_workspace_scheduled",
                    origin="scheduled",
                    gaps=(
                        "daily_workspace_g_context_unverified",
                        *((release_gap,) if release_gap else ()),
                    ),
                    summary="检查点产物缺少已绑定锅老师认知的证明，未写入新版本。",
                    agent_runtime_invoked=_stored_agent_runtime_invoked(product),
                )
            agent_runtime_invoked = _stored_agent_runtime_invoked(product)

            try:
                finalization = self._state_repository.finalize_scheduled_checkpoint(
                    principal_id=principal_id,
                    trading_day_id=trading_day_id,
                    idempotency_key=checkpoint_key,
                    checkpoint=checkpoint_enum.value,
                    product=product,
                    now=now.timestamp(),
                    expected_parent_product_version=parent_version,
                )
                read = getattr(finalization, "read", finalization)
            except SemanticStateError as error:
                release_gap = _release_checkpoint()
                return _unavailable(
                    action="daily_workspace_scheduled",
                    origin="scheduled",
                    gaps=(
                        "daily_workspace_state_conflict",
                        *((release_gap,) if release_gap else ()),
                    ),
                    summary=f"工作区版本写入被拒绝（{error.code}）。",
                    agent_runtime_invoked=agent_runtime_invoked,
                )
            except Exception:
                # B3 缺陷修复：非 SemanticStateError（如 sqlite IntegrityError）
                # 同样释放 checkpoint，返回 typed unavailable。
                release_gap = _release_checkpoint()
                return _unavailable(
                    action="daily_workspace_scheduled",
                    origin="scheduled",
                    gaps=(
                        "daily_workspace_finalize_failed",
                        *((release_gap,) if release_gap else ()),
                    ),
                    summary="检查点版本落库失败，未写入新版本。",
                    agent_runtime_invoked=agent_runtime_invoked,
                )
            return _scheduled_result(
                read,
                agent_runtime_invoked=agent_runtime_invoked,
            )
        except Exception:
            # parent 读取/snapshot 构建等 pre-try 路径的兜底：释放 + typed 失败
            release_gap = _release_checkpoint()
            return _unavailable(
                action="daily_workspace_scheduled",
                origin="scheduled",
                gaps=(
                    "daily_workspace_generation_failed",
                    *((release_gap,) if release_gap else ()),
                ),
                summary="检查点准备失败，未写入新版本。",
            )

    def ask(
        self,
        command: DailyWorkspaceAskCommand,
        *,
        principal: Any,
    ) -> ConsultationResult:
        if self._state_repository is None:
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_state_unavailable",
                summary="Daily workspace 状态层当前不可用。",
            )
        principal_id = getattr(principal, "principal_id", None)
        if not isinstance(principal_id, str) or not principal_id:
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_principal_missing",
                summary="Daily workspace 无法确认请求主体。",
            )
        trading_day_id = _effective_trading_day(command.trading_day_id, self._clock())
        if trading_day_id is None:
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_trading_day_invalid",
                summary="交易日标识格式无效。",
            )
        previous = self._state_repository.find_daily_workspace(
            principal_id=principal_id,
            trading_day_id=trading_day_id,
        )
        if previous is None:
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_no_version",
                summary="今日工作区尚无版本，无法在同一工作区追问。",
            )
        if not is_public_daily_workspace_product(getattr(previous, "product", None)):
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_g_context_unverified",
                summary="既有工作区版本缺少已绑定锅老师认知的证明，未继续其咨询内容。",
            )
        snapshot = _on_demand_snapshot(trading_day_id, previous, command.question)
        from fin_analyse.operations.daily_workspace_generator import (
            L1DirectWorkspaceGenerator,
            DailyWorkspaceGenerationUnavailableError,
        )
        from fin_analyse.guo_teacher_research.semantic_state import (
            SemanticStateError,
        )

        try:
            generator = self._ask_generator
            if generator is None:
                generator = L1DirectWorkspaceGenerator()
            product = generator.generate(
                snapshot=snapshot,
                principal=principal,
            )
        except DailyWorkspaceGenerationUnavailableError as error:
            return _unavailable(
                action="daily_workspace_ask",
                gaps=error.data_gaps,
                summary="咨询链未形成可用工作区版本，未写入新版本。",
                agent_runtime_invoked=error.agent_runtime_invoked,
            )
        except Exception:
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_generation_failed",
                summary="工作区追问生成失败，未写入新版本。",
            )

        now = self._clock()
        gaps = product.get("data_gaps")
        data_gaps = tuple(str(item) for item in gaps) if isinstance(gaps, (list, tuple)) else ()
        agent_runtime_invoked = _stored_agent_runtime_invoked(product)
        try:
            read = self._state_repository.append_daily_workspace_version(
                principal_id=principal_id,
                trading_day_id=trading_day_id,
                idempotency_key=(f"daily:{trading_day_id}:ask:{secrets.token_hex(16)}"),
                contract={
                    "schema": "fin.daily-workspace-contract/v1",
                    "checkpoint": "on_demand",
                    "origin": "on_demand",
                },
                input_snapshot=snapshot,
                expected_parent_product_version=previous.product_version,
                status=(
                    "completed" if product.get("consultation_status") == "completed" else "partial"
                ),
                product=product,
                now=now.timestamp(),
                data_gaps=data_gaps,
            )
        except SemanticStateError as error:
            return _unavailable(
                action="daily_workspace_ask",
                gap="daily_workspace_state_conflict",
                summary=f"工作区版本写入被拒绝（{error.code}）。",
                agent_runtime_invoked=agent_runtime_invoked,
            )
        return _scheduled_result(
            read,
            on_demand=True,
            agent_runtime_invoked=agent_runtime_invoked,
        )


def _on_demand_snapshot(
    trading_day_id: str,
    previous: Any,
    user_question: str,
) -> DailyWorkspaceInputSnapshot:
    return {
        "schema": "fin.daily-workspace-input-snapshot/v1",
        "trading_day_id": trading_day_id,
        "checkpoint": "on_demand",
        "origin": "on_demand",
        "parent_product_version": previous.product_version,
        "parent_artifact_hash": previous.artifact_hash,
        "prior_product": bounded_prior_product_snapshot(
            product_version=previous.product_version,
            artifact_hash=previous.artifact_hash,
            product=previous.product,
        ),
        "user_context": {
            "question": user_question,
            "source_boundary": "NOT_EVIDENCE",
        },
    }


def _normalized_trading_day(trading_day_id: str) -> str | None:
    """Canonicalize an ISO date to ``YYYY-MM-DD``; the trading calendar is
    SchedulePolicy's job.  ``date.fromisoformat`` also accepts compact/week
    forms, so the canonical string (not the caller input) keys the chain."""

    if not isinstance(trading_day_id, str):
        return None
    try:
        parsed = date.fromisoformat(trading_day_id)
    except ValueError:
        return None
    return parsed.isoformat()


def _effective_trading_day(trading_day_id: str | None, now: datetime) -> str | None:
    if trading_day_id is not None:
        return _normalized_trading_day(trading_day_id)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("daily workspace clock must be timezone-aware")
    return now.astimezone(_SHANGHAI).date().isoformat()


def _scheduled_result(
    read: Any,
    *,
    on_demand: bool = False,
    agent_runtime_invoked: bool = False,
) -> ConsultationResult:
    """Project one persisted daily workspace version."""

    from datetime import UTC, datetime

    product = getattr(read, "product", None)
    if not is_public_daily_workspace_product(product):
        return _unavailable(
            action="daily_workspace_ask" if on_demand else "daily_workspace_scheduled",
            origin="on_demand" if on_demand else "scheduled",
            gap="daily_workspace_g_context_unverified",
            summary="工作区版本缺少已绑定锅老师认知的证明，未展示其咨询内容。",
            agent_runtime_invoked=agent_runtime_invoked,
        )
    stored_gaps = read.product.get("data_gaps") if isinstance(read.product, dict) else None
    data_gaps = (
        tuple(str(item) for item in stored_gaps) if isinstance(stored_gaps, (list, tuple)) else ()
    )
    read_status = read.status
    return ConsultationResult(
        action="daily_workspace_ask" if on_demand else "daily_workspace_scheduled",
        origin="on_demand" if on_demand else "scheduled",
        workspace_ref=read.workspace_ref,
        status=(read_status if read_status in {"completed", "partial"} else "unknown"),
        analysis_profile="EXPLAIN",
        profile_reason=(
            "DAILY_WORKSPACE_ON_DEMAND_ASK" if on_demand else "DAILY_WORKSPACE_SCHEDULED_CHECKPOINT"
        ),
        trigger="on_demand",
        scope=ConsultationGeneralScope(),
        as_of=datetime.fromtimestamp(read.as_of, tz=UTC),
        answer=ConsultationAnswer(
            summary=(
                "已在今日工作区追加追问版本。" if on_demand else "已生成今日工作区检查点版本。"
            ),
            disposition="NO_ACTION",
            no_action=True,
        ),
        product=read.product,
        agent_contributions=AgentContributions(
            guo=AgentContribution(
                role="GUO_COGNITION",
                status="UNKNOWN",
                summary=(
                    "on-demand 版本由既有 consultation chain 产出。"
                    if on_demand
                    else "scheduled 版本由 FIN 生成器产出。"
                ),
                source_boundary="G_AND_BOUNDED_EXTERNAL_EVIDENCE",
            )
        ),
        decision_context=ConsultationNoAccountContext(
            mode="NONE",
            status="NOT_REQUIRED",
            risk_status="NOT_EVALUATED",
        ),
        data_gaps=data_gaps,
        result_meta=ConsultationResultMeta(
            agent_runtime_invoked=agent_runtime_invoked,
            agent_output_used=_stored_agent_output_used(read.product),
        ),
    )


def _agent_provenance(product: object) -> dict[str, object]:
    if not isinstance(product, dict):
        return {}
    value = product.get("agent_provenance")
    return value if isinstance(value, dict) else {}


def _stored_agent_runtime_invoked(product: object) -> bool:
    return _agent_provenance(product).get("runtime_invoked_at_generation") is True


def _stored_agent_output_used(product: object) -> bool:
    return _agent_provenance(product).get("output_used") is True


def _unavailable(
    *,
    gap: str | None = None,
    gaps: tuple[str, ...] = (),
    summary: str,
    action: Literal[
        "daily_workspace_open",
        "daily_workspace_ask",
        "daily_workspace_scheduled",
    ] = "daily_workspace_open",
    origin: Literal["on_demand", "scheduled"] = "on_demand",
    agent_runtime_invoked: bool = False,
) -> ConsultationResult:
    from datetime import UTC, datetime

    return ConsultationResult(
        action=action,
        origin=origin,
        status="unavailable",
        analysis_profile="EXPLAIN",
        profile_reason="FIN_DAILY_WORKSPACE_NOT_AVAILABLE",
        trigger="on_demand",
        scope=ConsultationGeneralScope(),
        as_of=datetime.now(UTC),
        answer=ConsultationAnswer(
            summary=summary,
            disposition="NO_ACTION",
            no_action=True,
        ),
        agent_contributions=AgentContributions(
            guo=AgentContribution(
                role="GUO_COGNITION",
                status="UNKNOWN",
                summary=summary,
                source_boundary="G_AND_BOUNDED_EXTERNAL_EVIDENCE",
            )
        ),
        decision_context=ConsultationNoAccountContext(
            mode="NONE",
            status="NOT_REQUIRED",
            risk_status="NOT_EVALUATED",
        ),
        data_gaps=gaps or ((gap,) if gap is not None else ()),
        result_meta=ConsultationResultMeta(
            agent_runtime_invoked=agent_runtime_invoked,
            agent_output_used=False,
        ),
    )
