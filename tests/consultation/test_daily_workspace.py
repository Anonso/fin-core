"""Tests for Daily Decision Workspace V1 (command surface + facade + dispatch)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from fin_analyse.consultation.contracts import (
    ConsultationCommand,
    ConsultationFreshness,
    DailyWorkspaceAskCommand,
    DailyWorkspaceOpenCommand,
    GContextConsumption,
)
from fin_analyse.consultation.daily_workspace import DailyWorkspaceService
from fin_analyse.consultation.daily_workspace_product_contracts import (
    MAX_PRIOR_PRODUCT_CONTEXT_CHARS,
)


class _RecordingGenerator:
    def __init__(self, product: dict[str, object]) -> None:
        self.product = product
        self.snapshots: list[dict[str, object]] = []

    def generate(self, *, snapshot: object, principal: object) -> object:
        self.snapshots.append(snapshot)  # type: ignore[arg-type]
        result = dict(self.product)
        if isinstance(snapshot, dict):
            if isinstance(snapshot.get("trading_day_id"), str):
                result["trading_day_id"] = snapshot["trading_day_id"]
            if isinstance(snapshot.get("checkpoint"), str):
                result["checkpoint"] = snapshot["checkpoint"]
        consultation_product = result.get("consultation_product")
        first_screen = result.get("first_screen")
        if (
            isinstance(consultation_product, dict)
            and isinstance(consultation_product.get("answer_text"), str)
            and first_screen == _WORKSPACE_PRODUCT["first_screen"]
        ):
            result["first_screen"] = {
                **first_screen,
                "top_items": [
                    {
                        "item": consultation_product["answer_text"],
                        "disposition": "OBSERVE",
                    }
                ],
            }
        return result


class _ThrowingGenerator:
    def generate(self, *, snapshot: object, principal: object) -> object:
        raise RuntimeError("generator boom")


def _real_repo(tmp_path) -> object:
    from fin_analyse.guo_teacher_research.semantic_state import ResearchStateRepository

    return ResearchStateRepository(
        tmp_path / "state.sqlite3",
        token_secret=b"daily-workspace-test-secret-is-32-bytes!!",
    )


class _RecordingWorkspaceRepository:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.input_snapshots: list[dict[str, object]] = []

    def append_daily_workspace_version(self, **kwargs: Any) -> Any:
        snapshot = kwargs["input_snapshot"]
        assert isinstance(snapshot, dict)
        self.input_snapshots.append(snapshot)
        return self.delegate.append_daily_workspace_version(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


_G_REFERENCES = [{"generation": "g-generation-1", "source_ref": "g:1"}]


def _bound_consultation_product(**fields: object) -> dict[str, object]:
    answer_text = fields.pop(
        "answer_text",
        fields.pop("headline", fields.pop("answer_summary", "处理 A")),
    )
    return {
        "contract_id": "consultation_product",
        "contract_version": "v1",
        "answer_text": answer_text,
    }


_WORKSPACE_PRODUCT: dict[str, object] = {
    "schema_version": "fin.daily_workspace_product/v1",
    "checkpoint": "premarket",
    "trading_day_id": "2026-08-03",
    "origin": "scheduled",
    "generated_via": "consultation-chain-v1",
    "consultation_status": "completed",
    "agent_provenance": {
        "runtime_invoked_at_generation": True,
        "output_used": True,
        "product_bound_g_receipt": None,
    },
    "consultation_product": _bound_consultation_product(),
    "context_boundaries": {
        "prior_product": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
        "user_question": "NOT_EVIDENCE",
    },
    "input_snapshot_receipt": {
        "schema": "fin.daily-workspace-input-receipt/v1",
        "consultation_as_of": "2026-08-03T09:10:00+08:00",
    },
    "first_screen": {
        "top_items": [{"item": "处理 A", "disposition": "OBSERVE"}],
        "rationale": [],
        "changes_vs_previous": [],
        "unknowns": [],
        "portfolio_review": [],
    },
    "data_gaps": [],
}


class _FakeConsultationRunner:
    def __init__(self, data_gaps: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self.data_gaps = data_gaps

    def handle(
        self,
        command,
        *,
        principal: object,
        daily_workspace_context: object | None = None,
        daily_checkpoint: object | None = None,
    ) -> object:
        del daily_workspace_context, daily_checkpoint
        self.calls.append(command.question)
        from fin_analyse.consultation.contracts import (
            AgentContribution,
            AgentContributions,
            ConsultationAnswer,
            ConsultationGeneralScope,
            ConsultationNoAccountContext,
            ConsultationResult,
            ConsultationResultMeta,
        )

        return ConsultationResult(
            action="consult",
            origin="scheduled",
            status="unavailable" if self.data_gaps else "completed",
            analysis_profile="EXPLAIN",
            profile_reason="TEST",
            trigger="on_demand",
            scope=ConsultationGeneralScope(),
            as_of=datetime.now(UTC),
            answer=ConsultationAnswer(
                summary="ok",
                disposition="OBSERVE",
                no_action=True,
            ),
            product=_bound_consultation_product(headline="ok", answer_summary="ok"),
            agent_contributions=AgentContributions(
                guo=AgentContribution(
                    role="GUO_COGNITION",
                    status="READY",
                    summary="ok",
                    source_boundary="G_AND_BOUNDED_EXTERNAL_EVIDENCE",
                )
            ),
            decision_context=ConsultationNoAccountContext(
                mode="NONE",
                status="NOT_REQUIRED",
                risk_status="NOT_EVALUATED",
            ),
            data_gaps=self.data_gaps,
            freshness=ConsultationFreshness(
                g_context=GContextConsumption(
                    status="CONSUMED",
                    generation="g-generation-1",
                    source_refs=("g:1",),
                    references=tuple(_G_REFERENCES),
                )
            ),
            result_meta=ConsultationResultMeta(
                agent_runtime_invoked=True,
                agent_output_used=True,
            ),
        )


def test_consultation_command_accepts_daily_actions() -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(ConsultationCommand)
    open_command = adapter.validate_python(
        {"action": "daily_workspace_open", "trading_day_id": "2026-08-03"}
    )
    assert isinstance(open_command, DailyWorkspaceOpenCommand)
    ask_command = adapter.validate_python(
        {"action": "daily_workspace_ask", "question": "今天最值得处理什么？"}
    )
    assert isinstance(ask_command, DailyWorkspaceAskCommand)


class _FakeWorkspaceStateRepository:
    def __init__(self, version: object | None = None) -> None:
        self.version = version
        self.lookups: list[tuple[str, str]] = []

    def find_daily_workspace(self, *, principal_id: str, trading_day_id: str) -> object | None:
        self.lookups.append((principal_id, trading_day_id))
        return self.version


class _FakePrincipal:
    def __init__(self, principal_id: str) -> None:
        self.principal_id = principal_id


def test_daily_open_returns_honest_unavailable_without_state_layer() -> None:
    service = DailyWorkspaceService(consultation_runner=_FakeConsultationRunner())
    result = service.open(
        DailyWorkspaceOpenCommand(trading_day_id="2026-08-03"),
        principal=_FakePrincipal("finp_daily"),
    )
    assert result.action == "daily_workspace_open"
    assert result.status == "unavailable"
    assert "daily_workspace_state_unavailable" in result.data_gaps


def test_daily_open_returns_unavailable_when_no_version_exists() -> None:
    repository = _FakeWorkspaceStateRepository(version=None)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repository,
    )
    result = service.open(
        DailyWorkspaceOpenCommand(trading_day_id="2026-08-03"),
        principal=_FakePrincipal("finp_daily"),
    )
    assert result.status == "unavailable"
    assert "daily_workspace_no_version" in result.data_gaps
    assert repository.lookups == [("finp_daily", "2026-08-03")]


def test_daily_open_returns_latest_version_from_state_repository() -> None:
    from types import SimpleNamespace

    repository = _FakeWorkspaceStateRepository(
        version=SimpleNamespace(
            workspace_ref="opaque-ref-1",
            status="completed",
            created_at=1_720_000_000.0,
            product={
                **_WORKSPACE_PRODUCT,
                "trading_day_id": "2026-08-04",
                "first_screen": {
                    "top_items": [{"item": "处理 A", "disposition": "OBSERVE"}],
                    "unknowns": [],
                },
            },
        )
    )
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repository,
        clock=lambda: datetime(2026, 8, 3, 16, 30, tzinfo=UTC),
    )
    result = service.open(
        DailyWorkspaceOpenCommand(),
        principal=_FakePrincipal("finp_daily"),
    )
    assert result.status == "completed"
    assert result.workspace_ref == "opaque-ref-1"
    assert result.product is not None
    assert result.product["checkpoint"] == "premarket"
    assert result.result_meta.agent_runtime_invoked is False
    assert result.result_meta.agent_output_used is True
    # A2: Daily Workspace 的版本延续不是 provider continuity，不得误标。
    assert result.result_meta.continuity == "NEW_CHAIN"
    assert result.answer.no_action is True
    assert repository.lookups == [("finp_daily", "2026-08-04")]


def test_daily_open_hides_a_legacy_normal_product_without_bound_g_receipt() -> None:
    from types import SimpleNamespace

    repository = _FakeWorkspaceStateRepository(
        version=SimpleNamespace(
            workspace_ref="opaque-ref-gless",
            status="completed",
            created_at=1_720_000_000.0,
            product={
                "schema_version": "fin.daily_workspace_product/v1",
                "trading_day_id": "2026-08-04",
                "checkpoint": "premarket",
                "origin": "scheduled",
                "generated_via": "consultation-chain-v1",
                "consultation_status": "completed",
                "agent_provenance": {
                    "runtime_invoked_at_generation": True,
                    "output_used": True,
                },
                "consultation_product": {
                    "shared_brain_references": [
                        {"generation": "g-generation-1", "source_ref": "g:1"}
                    ]
                },
                "first_screen": {"top_items": [{"item": "不得展示"}], "unknowns": []},
            },
        )
    )
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repository,
        clock=lambda: datetime(2026, 8, 3, 16, 30, tzinfo=UTC),
    )

    result = service.open(
        DailyWorkspaceOpenCommand(),
        principal=_FakePrincipal("finp_daily"),
    )

    assert result.status == "unavailable"
    assert result.product is None
    assert result.data_gaps == ("daily_workspace_g_context_unverified",)


def test_daily_open_projects_partial_status_and_version_timestamp() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    version_created_at = 1_720_000_000.0
    repository = _FakeWorkspaceStateRepository(
        version=SimpleNamespace(
            workspace_ref="opaque-ref-2",
            status="partial",
            created_at=version_created_at,
            product={
                **_WORKSPACE_PRODUCT,
                "trading_day_id": "2026-08-03",
                "checkpoint": "close",
                "consultation_status": "partial",
                "data_gaps": ["paper_close_unavailable"],
                "first_screen": {"top_items": [], "unknowns": ["收盘 PAPER 未终态"]},
            },
        )
    )
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repository,
    )
    result = service.open(
        DailyWorkspaceOpenCommand(trading_day_id="2026-08-03"),
        principal=_FakePrincipal("finp_daily"),
    )
    # 诚实投影：partial 不伪装成 completed，as_of 用版本真实生成时间。
    assert result.status == "partial"
    assert result.as_of == datetime.fromtimestamp(version_created_at, tz=UTC)
    assert result.data_gaps == ("paper_close_unavailable",)


def test_daily_ask_requires_an_existing_workspace() -> None:
    runner = _FakeConsultationRunner()
    repository = _FakeWorkspaceStateRepository(version=None)
    service = DailyWorkspaceService(
        consultation_runner=runner,
        state_repository=repository,
    )
    result = service.ask(
        DailyWorkspaceAskCommand(
            question="今天最值得处理什么？",
            trading_day_id="2026-08-03",
        ),
        principal=_FakePrincipal("finp_daily"),
    )

    assert result.action == "daily_workspace_ask"
    assert result.origin == "on_demand"
    assert result.status == "unavailable"
    assert result.data_gaps == ("daily_workspace_no_version",)
    assert repository.lookups == [("finp_daily", "2026-08-03")]
    assert runner.calls == []


def test_daily_workspace_actions_remain_new_chain(tmp_path) -> None:
    """open/ask/scheduled 都不是 provider continuation，公开 meta 保持 NEW_CHAIN。"""
    from types import SimpleNamespace

    principal = _FakePrincipal("finp_daily")
    runner = _FakeConsultationRunner()

    # open：读取已有 workspace 版本。
    open_service = DailyWorkspaceService(
        consultation_runner=runner,
        state_repository=_FakeWorkspaceStateRepository(
            version=SimpleNamespace(
                workspace_ref="opaque-ref-3",
                status="completed",
                created_at=1_720_000_000.0,
                product={
                    "schema_version": "fin.daily_workspace_product/v1",
                    "trading_day_id": "2026-08-04",
                    "checkpoint": "premarket",
                    "agent_provenance": {
                        "runtime_invoked_at_generation": True,
                        "output_used": True,
                    },
                    "first_screen": {"top_items": [{"item": "处理 C"}], "unknowns": []},
                },
            )
        ),
    )
    opened = open_service.open(
        DailyWorkspaceOpenCommand(),
        principal=principal,
    )
    assert opened.result_meta.continuity == "NEW_CHAIN"

    # scheduled + ask：走真实 repository（fake 缺 scheduled 所需 seam）。
    repository = _real_repo(tmp_path)
    service = DailyWorkspaceService(
        consultation_runner=runner,
        state_repository=repository,
        clock=lambda: datetime(2026, 8, 2, 16, 30, tzinfo=UTC),
    )
    scheduled = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=principal,
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert scheduled.result_meta.continuity == "NEW_CHAIN"
    asked = service.ask(
        DailyWorkspaceAskCommand(question="今天最值得处理什么？"),
        principal=principal,
    )
    assert asked.result_meta.continuity == "NEW_CHAIN"


def test_daily_ask_appends_an_on_demand_version_to_the_same_workspace(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    principal = _FakePrincipal("finp_daily")
    runner = _FakeConsultationRunner()
    initial = DailyWorkspaceService(
        consultation_runner=runner,
        state_repository=repo,
    ).scheduled(
        "2026-08-03",
        "premarket",
        principal=principal,
        generator=_RecordingGenerator(
            {
                **_WORKSPACE_PRODUCT,
                "prior_context_padding": "x" * (MAX_PRIOR_PRODUCT_CONTEXT_CHARS + 1),
            }
        ),
    )
    before = repo.find_daily_workspace(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
    )
    assert before is not None
    recording_repository = _RecordingWorkspaceRepository(repo)

    class _RecordingAskGenerator:
        def __init__(self) -> None:
            self.snapshots: list[object] = []

        def generate(self, *, snapshot: object, principal: object) -> object:
            self.snapshots.append(snapshot)
            product = {
                **_WORKSPACE_PRODUCT,
                "origin": "on_demand",
                # generator-owned on-demand honesty gap (mirrors L1 projector)
                "data_gaps": ["daily_workspace_prior_context_not_consumed"],
            }
            if isinstance(snapshot, dict) and snapshot.get("parent_artifact_hash") is not None:
                product["parent_artifact_hash"] = snapshot["parent_artifact_hash"]
            return product

    ask_generator = _RecordingAskGenerator()
    service = DailyWorkspaceService(
        consultation_runner=runner,
        state_repository=recording_repository,
        clock=lambda: datetime(2026, 8, 2, 16, 30, tzinfo=UTC),
        ask_generator=ask_generator,
    )

    result = service.ask(
        DailyWorkspaceAskCommand(question="今天最值得处理什么？"),
        principal=principal,
    )

    assert result.action == "daily_workspace_ask"
    assert result.origin == "on_demand"
    assert result.status == "completed"
    assert result.workspace_ref == initial.workspace_ref
    assert result.product is not None
    assert result.product["origin"] == "on_demand"
    assert result.product["product_version"] == 2
    assert result.product["parent_product_version"] == 1
    assert result.product["parent_artifact_hash"] == before.artifact_hash
    assert "daily_workspace_prior_context_not_consumed" in result.data_gaps

    [snapshot] = recording_repository.input_snapshots
    assert snapshot["trading_day_id"] == "2026-08-03"
    prior = snapshot["prior_product"]
    assert isinstance(prior, dict)
    assert prior["artifact_hash"] == before.artifact_hash
    assert len(prior["content"]) == MAX_PRIOR_PRODUCT_CONTEXT_CHARS
    assert prior["truncated"] is True
    assert prior["consumption_status"] == "NOT_CONSUMED"
    assert prior["source_boundary"] == "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE"
    assert snapshot["user_context"] == {
        "question": "今天最值得处理什么？",
        "source_boundary": "NOT_EVIDENCE",
    }

    after = repo.find_daily_workspace(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
    )
    assert after is not None
    assert after.workspace_ref == before.workspace_ref
    assert after.product_version == before.product_version + 1


def test_daily_ask_preserves_unavailable_gaps_without_appending() -> None:
    from types import SimpleNamespace

    from fin_analyse.operations.daily_workspace_generator import (
        DailyWorkspaceGenerationUnavailableError,
    )

    repo = _FakeWorkspaceStateRepository(
        version=SimpleNamespace(
            product_version=1,
            artifact_hash="sha256:existing",
            product=_WORKSPACE_PRODUCT,
        )
    )
    principal = _FakePrincipal("finp_daily")

    class _UnavailableAskGenerator:
        def generate(self, *, snapshot: object, principal: object) -> object:
            raise DailyWorkspaceGenerationUnavailableError(
                ("daily_workspace_l1_all_backends_failed",)
            )

    result = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
        ask_generator=_UnavailableAskGenerator(),
    ).ask(
        DailyWorkspaceAskCommand(
            question="本轮为什么不可用？",
            trading_day_id="2026-08-03",
        ),
        principal=principal,
    )

    assert result.status == "unavailable"
    assert result.data_gaps == ("daily_workspace_l1_all_backends_failed",)
    assert repo.lookups == [("finp_daily", "2026-08-03")]


def test_scheduled_without_state_layer_is_unavailable() -> None:
    service = DailyWorkspaceService(consultation_runner=_FakeConsultationRunner())
    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
    )
    assert result.action == "daily_workspace_scheduled"
    assert result.status == "unavailable"
    assert "daily_workspace_state_unavailable" in result.data_gaps


def test_scheduled_rejects_invalid_checkpoint_and_trading_day(tmp_path) -> None:
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=_real_repo(tmp_path),
    )

    bad_checkpoint = service.scheduled(
        "2026-08-03",
        "lunch",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert bad_checkpoint.status == "unavailable"
    assert "daily_workspace_checkpoint_invalid" in bad_checkpoint.data_gaps

    bad_day = service.scheduled(
        "2026/08/03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert bad_day.status == "unavailable"
    assert "daily_workspace_trading_day_invalid" in bad_day.data_gaps


def test_scheduled_without_generator_writes_nothing(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )

    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
    )

    assert result.status == "unavailable"
    assert "daily_workspace_generator_missing" in result.data_gaps
    assert repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03") is None


def test_scheduled_generates_first_checkpoint_version(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    generator = _RecordingGenerator(_WORKSPACE_PRODUCT)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )

    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=generator,
    )

    assert result.action == "daily_workspace_scheduled"
    assert result.origin == "scheduled"
    assert result.status == "completed"
    assert result.workspace_ref is not None
    assert result.product is not None
    assert result.product["checkpoint"] == "premarket"
    assert result.result_meta.agent_runtime_invoked is True
    assert result.result_meta.agent_output_used is True
    assert generator.snapshots[0]["parent_product_version"] == 0
    assert generator.snapshots[0]["parent_artifact_hash"] is None

    read = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert read is not None
    assert read.product_version == 1
    assert read.workspace_ref == result.workspace_ref


def test_scheduled_second_checkpoint_appends_next_version(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )

    premarket = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "premarket"})
    service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=premarket,
    )
    morning = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "morning"})
    result = service.scheduled(
        "2026-08-03",
        "morning",
        principal=_FakePrincipal("finp_daily"),
        generator=morning,
    )

    assert result.status == "completed"
    assert result.product["checkpoint"] == "morning"
    assert morning.snapshots[0]["parent_product_version"] == 1
    assert morning.snapshots[0]["parent_artifact_hash"] is not None
    read = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert read is not None
    assert read.product_version == 2

    # 同一 checkpoint 重试 → exact replay，不新增版本。
    replay = service.scheduled(
        "2026-08-03",
        "morning",
        principal=_FakePrincipal("finp_daily"),
        generator=morning,
    )
    assert replay.status == "completed"
    assert replay.result_meta.agent_runtime_invoked is False
    assert replay.result_meta.agent_output_used is True
    assert (
        repo.find_daily_workspace(
            principal_id="finp_daily", trading_day_id="2026-08-03"
        ).product_version
        == 2
    )


def test_scheduled_second_checkpoint_injects_bounded_same_day_parent_context(tmp_path) -> None:
    """Later checkpoints receive the exact prior version, never its raw product."""

    repo = _real_repo(tmp_path)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    principal = _FakePrincipal("finp_daily")
    premarket = _RecordingGenerator(
        {
            **_WORKSPACE_PRODUCT,
            "checkpoint": "premarket",
            "consultation_product": _bound_consultation_product(
                headline="开盘先观察主线 A。",
                decision_basis=["量能仍未确认。"],
                watch_conditions=["午前成交量放大。"],
                invalidation_conditions=["主线跌破关键支撑。"],
                unknowns=["北交所来源缺口。"],
            ),
        }
    )
    service.scheduled(
        "2026-08-03",
        "premarket",
        principal=principal,
        generator=premarket,
    )
    first = repo.find_daily_workspace(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
    )
    assert first is not None

    morning = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "morning"})
    result = service.scheduled(
        "2026-08-03",
        "morning",
        principal=principal,
        generator=morning,
    )

    assert result.status == "completed"
    context = morning.snapshots[0]["daily_workspace_context"]
    assert context == {
        "schema_version": "fin.daily-workspace-context/v1",
        "classification": "prior_daily_workspace_context_not_evidence",
        "relationship": "same_trading_day_parent",
        "source": {
            "trading_day_id": "2026-08-03",
            "checkpoint": "premarket",
            "product_version": 1,
            "artifact_hash": first.artifact_hash,
        },
        "carry_over": {
            "answer_text": "开盘先观察主线 A。",
        },
        "data_gaps": [],
    }


def test_scheduled_verified_parent_projects_complete_typed_context(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    principal = _FakePrincipal("finp_daily")
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    service.scheduled(
        "2026-08-03",
        "premarket",
        principal=principal,
        generator=_RecordingGenerator(
            {
                **_WORKSPACE_PRODUCT,
                "checkpoint": "premarket",
                "consultation_product": _bound_consultation_product(
                    headline="缺少其他 canonical 字段。"
                ),
            }
        ),
    )
    morning = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "morning"})

    result = service.scheduled(
        "2026-08-03",
        "morning",
        principal=principal,
        generator=morning,
    )

    assert result.status == "completed"
    context = morning.snapshots[0]["daily_workspace_context"]
    assert context["carry_over"] == {
        "answer_text": "缺少其他 canonical 字段。",
    }
    assert context["data_gaps"] == []


def test_unverified_legacy_parent_never_enters_same_or_previous_day_context() -> None:
    from types import SimpleNamespace

    from fin_analyse.consultation.daily_workspace_context import (
        resolve_scheduled_workspace_context,
    )

    parent = SimpleNamespace(
        trading_day_id="2026-08-03",
        product_version=1,
        artifact_hash="sha256:" + "a" * 64,
        status="completed",
        product={
            "checkpoint": "postmarket",
            "generated_via": "consultation-chain-v1",
            "consultation_product": {
                "shared_brain_references": [{"generation": "g-generation-1", "source_ref": "g:1"}],
                "headline": "不得注入下一轮。",
                "decision_basis": ["无 G receipt 的内容。"],
                "watch_conditions": [],
                "invalidation_conditions": [],
                "unknowns": [],
            },
        },
    )

    same_day = resolve_scheduled_workspace_context(
        same_day_parent=parent,
        calendar=None,
        repository=object(),  # type: ignore[arg-type]
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        as_of=datetime(2026, 8, 3, 2, 0, tzinfo=UTC),
    )

    class _Calendar:
        def previous_open_date(self, *, before: date, known_at: datetime) -> object:
            return SimpleNamespace(previous_open_date=date(2026, 8, 3))

    class _Repository:
        def find_daily_workspace_version_by_key(self, **_kwargs: object) -> object:
            return parent

    previous_day = resolve_scheduled_workspace_context(
        same_day_parent=None,
        calendar=_Calendar(),
        repository=_Repository(),
        principal_id="finp_daily",
        trading_day_id="2026-08-04",
        as_of=datetime(2026, 8, 4, 2, 0, tzinfo=UTC),
    )

    for context in (same_day, previous_day):
        assert context["carry_over"]["answer_text"] == ""
        assert context["data_gaps"] == ["daily_workspace_parent_g_context_unverified"]


def test_scheduled_four_checkpoints_form_one_exact_parent_chain(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    principal = _FakePrincipal("finp_daily")
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    checkpoints = ("premarket", "morning", "close", "postmarket")
    generators: list[_RecordingGenerator] = []
    for index, checkpoint in enumerate(checkpoints, start=1):
        generator = _RecordingGenerator(
            {
                **_WORKSPACE_PRODUCT,
                "checkpoint": checkpoint,
                "consultation_product": _bound_consultation_product(
                    headline=f"第 {index} 版。",
                    decision_basis=[f"依据 {index}。"],
                    watch_conditions=[f"观察 {index}。"],
                    invalidation_conditions=[f"失效 {index}。"],
                    unknowns=[f"缺口 {index}。"],
                ),
            }
        )
        result = service.scheduled(
            "2026-08-03",
            checkpoint,
            principal=principal,
            generator=generator,
        )
        assert result.status == "completed"
        generators.append(generator)

    for parent_version, generator in enumerate(generators):
        snapshot = generator.snapshots[0]
        assert snapshot["parent_product_version"] == parent_version
        if parent_version == 0:
            continue
        context = snapshot["daily_workspace_context"]
        assert context["relationship"] == "same_trading_day_parent"
        assert context["source"]["product_version"] == parent_version
        assert context["source"]["checkpoint"] == checkpoints[parent_version - 1]

    latest = repo.find_daily_workspace(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
    )
    assert latest is not None
    assert latest.product_version == 4


def test_scheduled_first_checkpoint_uses_only_previous_real_trading_day_postmarket(
    tmp_path,
) -> None:
    """A new day does not guess weekdays or jump over a missing postmarket version."""

    from types import SimpleNamespace

    class _Calendar:
        def __init__(self) -> None:
            self.calls: list[date] = []

        def previous_open_date(self, *, before: date, known_at: datetime) -> object:
            assert known_at.tzinfo is not None
            self.calls.append(before)
            return SimpleNamespace(previous_open_date=date(2026, 8, 3))

    repo = _real_repo(tmp_path)
    principal = _FakePrincipal("finp_daily")
    prior_service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    prior = _RecordingGenerator(
        {
            **_WORKSPACE_PRODUCT,
            "checkpoint": "postmarket",
            "consultation_product": _bound_consultation_product(
                headline="盘后保留主线 A 的观察。",
                decision_basis=["今日量能未确认。"],
                watch_conditions=["明日开盘量价。"],
                invalidation_conditions=["主线持续走弱。"],
                unknowns=["两融负债未知。"],
            ),
        }
    )
    prior_service.scheduled(
        "2026-08-03",
        "postmarket",
        principal=principal,
        generator=prior,
    )
    previous = repo.find_daily_workspace(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
    )
    assert previous is not None

    calendar = _Calendar()
    current = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "premarket"})
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
        calendar=calendar,
        clock=lambda: datetime(2026, 8, 4, 1, 20, tzinfo=UTC),
    )
    result = service.scheduled(
        "2026-08-04",
        "premarket",
        principal=principal,
        generator=current,
    )

    assert result.status == "completed"
    assert calendar.calls == [date(2026, 8, 4)]
    assert current.snapshots[0]["daily_workspace_context"] == {
        "schema_version": "fin.daily-workspace-context/v1",
        "classification": "prior_daily_workspace_context_not_evidence",
        "relationship": "previous_trading_day",
        "source": {
            "trading_day_id": "2026-08-03",
            "checkpoint": "postmarket",
            "product_version": 1,
            "artifact_hash": previous.artifact_hash,
        },
        "carry_over": {
            "answer_text": "盘后保留主线 A 的观察。",
        },
        "data_gaps": [],
    }


def test_scheduled_missing_previous_postmarket_never_skips_to_an_older_day(tmp_path) -> None:
    from types import SimpleNamespace

    class _Calendar:
        def previous_open_date(self, *, before: date, known_at: datetime) -> object:
            assert before == date(2026, 8, 4)
            assert known_at.tzinfo is not None
            return SimpleNamespace(previous_open_date=date(2026, 8, 3))

    repo = _real_repo(tmp_path)
    principal = _FakePrincipal("finp_daily")
    # A successful 8/2 review exists, but the calendar-selected 8/3 review does not.
    DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    ).scheduled(
        "2026-08-02",
        "postmarket",
        principal=principal,
        generator=_RecordingGenerator(
            {
                **_WORKSPACE_PRODUCT,
                "checkpoint": "postmarket",
                "consultation_product": _bound_consultation_product(headline="不得被跨日跳读。"),
            }
        ),
    )
    current = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "premarket"})

    result = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
        calendar=_Calendar(),
        clock=lambda: datetime(2026, 8, 4, 1, 20, tzinfo=UTC),
    ).scheduled(
        "2026-08-04",
        "premarket",
        principal=principal,
        generator=current,
    )

    assert result.status == "completed"
    context = current.snapshots[0]["daily_workspace_context"]
    assert context["relationship"] == "previous_trading_day"
    assert context["source"] is None
    assert context["data_gaps"] == ["daily_workspace_previous_trading_day_postmarket_missing"]


def test_scheduled_partial_previous_postmarket_is_gap_not_context(tmp_path) -> None:
    from types import SimpleNamespace

    class _Calendar:
        def previous_open_date(self, *, before: date, known_at: datetime) -> object:
            assert before == date(2026, 8, 4)
            assert known_at.tzinfo is not None
            return SimpleNamespace(previous_open_date=date(2026, 8, 3))

    repo = _real_repo(tmp_path)
    principal = _FakePrincipal("finp_daily")
    partial = _RecordingGenerator(
        {
            **_WORKSPACE_PRODUCT,
            "checkpoint": "postmarket",
            "consultation_status": "partial",
            "first_screen": {
                "top_items": [],
                "rationale": [],
                "changes_vs_previous": [],
                "unknowns": [],
                "portfolio_review": [],
            },
            "consultation_product": _bound_consultation_product(
                headline="这段 partial 内容不得成为次日上下文。"
            ),
        }
    )
    DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    ).scheduled(
        "2026-08-03",
        "postmarket",
        principal=principal,
        generator=partial,
    )
    prior = repo.find_daily_workspace(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
    )
    assert prior is not None
    assert prior.status == "partial"
    current = _RecordingGenerator({**_WORKSPACE_PRODUCT, "checkpoint": "premarket"})

    result = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
        calendar=_Calendar(),
        clock=lambda: datetime(2026, 8, 4, 1, 20, tzinfo=UTC),
    ).scheduled(
        "2026-08-04",
        "premarket",
        principal=principal,
        generator=current,
    )

    assert result.status == "completed"
    context = current.snapshots[0]["daily_workspace_context"]
    assert context["source"] == {
        "trading_day_id": "2026-08-03",
        "checkpoint": "postmarket",
        "product_version": 1,
        "artifact_hash": prior.artifact_hash,
    }
    assert context["carry_over"] == {
        "answer_text": "",
    }
    assert context["data_gaps"] == ["daily_workspace_previous_trading_day_partial"]


def test_scheduled_preserves_typed_unavailable_and_generic_failure_writes_nothing(
    tmp_path,
) -> None:
    from fin_analyse.operations.daily_workspace_generator import (
        DailyWorkspaceGenerationUnavailableError,
    )

    class _UnavailableGenerator:
        def generate(self, *, snapshot: object, principal: object) -> object:
            raise DailyWorkspaceGenerationUnavailableError(
                ("daily_workspace_l1_all_backends_failed",)
            )

    repo = _real_repo(tmp_path)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )

    unavailable = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_UnavailableGenerator(),
    )
    failed = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_ThrowingGenerator(),
    )
    assert repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03") is None
    recovered = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )

    assert unavailable.data_gaps == ("daily_workspace_l1_all_backends_failed",)
    assert failed.data_gaps == ("daily_workspace_generation_failed",)
    assert recovered.status == "completed"


def test_scheduled_state_conflict_preserves_that_runtime_was_invoked(tmp_path) -> None:
    from fin_analyse.guo_teacher_research.semantic_state import SemanticStateError

    delegate = _real_repo(tmp_path)

    class ConflictRepository:
        def finalize_scheduled_checkpoint(self, **_kwargs: object) -> object:
            raise SemanticStateError("continuation_conflict")

        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

    result = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=ConflictRepository(),
    ).scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )

    assert result.status == "unavailable"
    assert result.result_meta.agent_runtime_invoked is True
    assert result.result_meta.agent_output_used is False


def test_scheduled_concurrent_second_caller_reports_in_progress(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    # 时钟钉在与占位 claim 同一 epoch：TTL 回收按「相对 service 时钟的年龄」
    # 判僵尸，假 epoch + 真时钟会把占位误判成超时遗留。
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
        clock=lambda: datetime.fromtimestamp(1_720_000_000.0, tz=UTC),
    )

    # 第一调用方占位（acquire 赢家）——用 acquire 直接模拟并发中的持有者。
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:init",
        now=1_720_000_000.0,
    )
    assert (
        repo.acquire_daily_workspace_checkpoint(
            principal_id="finp_daily",
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:premarket",
            now=1_720_000_000.0,
        )
        is True
    )

    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )

    assert result.status == "unavailable"
    assert "daily_workspace_generation_in_progress" in result.data_gaps
    assert repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03") is None

    # 占位释放后可正常生成。
    repo.release_daily_workspace_checkpoint(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
    )
    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert result.status == "completed"


def test_scheduled_non_dict_product_is_rejected_and_released(tmp_path) -> None:
    repo = _real_repo(tmp_path)

    class _NonDictGenerator:
        def generate(self, *, snapshot: object, principal: object) -> object:
            return "not-a-product"

    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_NonDictGenerator(),
    )

    assert result.status == "unavailable"
    assert "daily_workspace_generation_invalid" in result.data_gaps
    # 占位已释放，可重试。
    retry = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert retry.status == "completed"


def test_scheduled_retry_does_not_invoke_generator_again(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    generator = _RecordingGenerator(_WORKSPACE_PRODUCT)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    first = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=generator,
    )
    assert first.status == "completed"
    assert len(generator.snapshots) == 1

    retry = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=generator,
    )
    assert retry.status == "completed"
    assert len(generator.snapshots) == 1  # 幂等返回，不再生成


def test_scheduled_normalizes_compact_trading_day_identity(tmp_path) -> None:
    repo = _real_repo(tmp_path)
    generator = _RecordingGenerator(_WORKSPACE_PRODUCT)
    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )

    compact = service.scheduled(
        "20260803",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=generator,
    )
    assert compact.status == "completed"
    assert generator.snapshots[0]["trading_day_id"] == "2026-08-03"

    # compact 与标准形式代表同一天 → 同一链（open 读同一版本）。
    opened = service.open(
        DailyWorkspaceOpenCommand(trading_day_id="2026-08-03"),
        principal=_FakePrincipal("finp_daily"),
    )
    assert opened.status == "completed"
    assert opened.workspace_ref == compact.workspace_ref


def test_scheduled_rejects_partial_generator_product_without_bound_g(tmp_path) -> None:
    repo = _real_repo(tmp_path)

    class _PartialProductGenerator:
        def generate(self, *, snapshot: object, principal: object) -> object:
            return {
                "schema_version": "fin.daily_workspace_product/v1",
                "checkpoint": "premarket",
                "trading_day_id": "2026-08-03",
                "generated_via": "consultation-chain-v1",
                "consultation_status": "partial",
                "first_screen": {
                    "top_items": [],
                    "rationale": [],
                    "changes_vs_previous": [],
                    "unknowns": ["paper_close_unavailable"],
                    "portfolio_review": [],
                },
                "data_gaps": ["paper_close_unavailable"],
                "consultation_product": None,
            }

    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=repo,
    )
    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_PartialProductGenerator(),
    )

    assert result.status == "unavailable"
    assert result.data_gaps == ("daily_workspace_g_context_unverified",)
    read = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert read is None


def test_scheduled_releases_checkpoint_on_non_semantic_finalize_failure(tmp_path) -> None:
    """B3 缺陷: finalize 抛非 SemanticStateError 时 checkpoint 必须释放——
    当日链不被 generation_in_progress 永久卡死。"""
    delegate = _real_repo(tmp_path)

    class CrashFinalizeRepository:
        def __init__(self) -> None:
            self.failures_left = 1

        def finalize_scheduled_checkpoint(self, **_kwargs: object) -> object:
            if self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("obligation not null constraint failed")
            return delegate.finalize_scheduled_checkpoint(**_kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=CrashFinalizeRepository(),
    )
    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert result.status == "unavailable"
    # checkpoint 已释放：下一次调用可重新 acquire 并成功
    retry = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert retry.status == "completed"


def test_scheduled_releases_checkpoint_when_parent_read_fails(tmp_path) -> None:
    """B3 缺陷: acquire 后 parent 读取失败也必须释放 checkpoint。"""
    delegate = _real_repo(tmp_path)

    class CrashParentReadRepository:
        def __init__(self) -> None:
            self.failures_left = 1

        def find_daily_workspace(self, **_kwargs: object) -> object:
            if self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("parent read failed")
            return delegate.find_daily_workspace(**_kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

    service = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=CrashParentReadRepository(),
    )
    result = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert result.status == "unavailable"
    retry = service.scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert retry.status == "completed"


def test_scheduled_release_failure_surfaces_typed_diagnostic(tmp_path) -> None:
    """B3 (Spec-B3-04): release 自身失败返回稳定 typed 诊断，且不掩盖主失败阶段。"""
    delegate = _real_repo(tmp_path)

    class CrashEverythingRepository:
        def finalize_scheduled_checkpoint(self, **_kwargs: object) -> object:
            raise RuntimeError("obligation not null constraint failed")

        def release_daily_workspace_checkpoint(self, **_kwargs: object) -> None:
            raise RuntimeError("release exploded")

        def __getattr__(self, name: str) -> object:
            return getattr(delegate, name)

    result = DailyWorkspaceService(
        consultation_runner=_FakeConsultationRunner(),
        state_repository=CrashEverythingRepository(),
    ).scheduled(
        "2026-08-03",
        "premarket",
        principal=_FakePrincipal("finp_daily"),
        generator=_RecordingGenerator(_WORKSPACE_PRODUCT),
    )
    assert result.status == "unavailable"
    gaps = tuple(result.data_gaps)
    # 两个失败阶段都保留：主失败 + release 失败（不互相掩盖）
    assert "daily_workspace_finalize_failed" in gaps
    assert "daily_workspace_checkpoint_release_failed" in gaps
