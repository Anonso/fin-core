"""R1-5: typed cross-generation investment-memory journal contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.investment_memory import (
    AccountReference,
    InvestmentMemoryEventInput,
)
from fin_analyse.guo_teacher_research.semantic_state import (
    SCHEMA_VERSION,
    ResearchStateRepository,
    SemanticStateError,
)

_SECRET = b"investment-memory-test-secret-is-32-bytes!"
_EPOCH = "investment-memory-test"
_NOW = 1_720_000_000.0
_CONTRACT = {
    "schema": "fin.semantic-research-contract/v1",
    "outcome_mode": "answer",
    "scope": "consultation",
    "policy_version": "r1-5-test",
}
_INPUT = {"question": "贵州茅台应继续等待什么信号？", "context": {}}


def _artifact_hash(product: object) -> str:
    encoded = json.dumps(
        product,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _repository(tmp_path: Path) -> ResearchStateRepository:
    return ResearchStateRepository(
        tmp_path / "semantic-state.sqlite3",
        token_secret=_SECRET,
        epoch=_EPOCH,
    )


def test_journal_references_existing_analysis_and_tombstone_blocks_replay(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    product = {
        "headline": "等待量价确认，暂不增加风险。",
        "unknowns": ["融资负债仍未知"],
    }
    answer = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW,
    )

    event = InvestmentMemoryEventInput(
        kind="USER_DECISION",
        statement="我先等待量价确认，不新增仓位。",
        decision="WAIT",
    )
    account_ref = AccountReference(
        snapshot_ref="actual-advisory-snapshot-aaaaaaaaaaaaaaaa",
        revision="sha256:" + "a" * 64,
        as_of=_NOW - 60,
    )

    first = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="hermes-turn-1",
        event=event,
        account_ref=account_ref,
        now=_NOW + 1,
    )
    replay = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="hermes-turn-1",
        event=event,
        account_ref=account_ref,
        now=_NOW + 2,
    )

    assert replay == first
    assert first.analysis_ref is not None
    assert first.analysis_ref.chain_id == answer.chain_id
    assert first.analysis_ref.product_version == answer.product_version
    assert first.analysis_ref.artifact_hash == answer.artifact_hash
    assert first.account_ref == account_ref

    recalled = repository.recall_investment_memory(principal_id="principal-a")
    assert recalled.classification == "investment_memory_not_evidence"
    assert recalled.unresolved_decisions == (first.event,)
    assert recalled.reported_execution == ()
    assert recalled.outcomes == ()
    assert recalled.account_refs == (account_ref,)
    assert recalled.recent_analyses == (first.analysis_ref,)

    repository.tombstone_investment_memory(
        principal_id="principal-a",
        deletion_key="delete-turn-2",
        target_event_id=first.event.event_id,
        now=_NOW + 3,
    )

    assert repository.recall_investment_memory(principal_id="principal-a").is_empty
    assert (
        repository.append_investment_memory_event(
            principal_id="principal-a",
            event_key="hermes-turn-1",
            event=event,
            account_ref=account_ref,
            now=_NOW + 4,
        ).state
        == "TOMBSTONED"
    )
    with pytest.raises(SemanticStateError, match="investment_memory_conflict"):
        repository.append_investment_memory_event(
            principal_id="principal-a",
            event_key="hermes-turn-1",
            event=InvestmentMemoryEventInput(
                kind="USER_DECISION",
                statement="我改变计划。",
                decision="CHANGE_PLAN",
            ),
            account_ref=account_ref,
            now=_NOW + 4,
        )


def test_janitor_redacts_tombstoned_statement_without_reviving_its_key(tmp_path: Path) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = ResearchStateRepository(database, token_secret=_SECRET, epoch=_EPOCH)
    event = InvestmentMemoryEventInput(
        kind="USER_REPORTED_EXECUTION",
        statement="我刚才手动卖出了半仓。",
    )
    receipt = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="hermes-turn-1",
        event=event,
        now=_NOW,
    )
    repository.tombstone_investment_memory(
        principal_id="principal-a",
        deletion_key="delete-turn-2",
        target_event_id=receipt.event.event_id,
        now=_NOW + 1,
    )

    assert repository.purge_tombstoned_investment_memory(
        now=_NOW + 1,
        retention_seconds=0,
    ) == 1
    assert repository.purge_tombstoned_investment_memory(
        now=_NOW + 2,
        retention_seconds=0,
    ) == 0
    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM investment_memory_events WHERE event_id=?",
            (receipt.event.event_id,),
        ).fetchone()
    assert payload is not None
    assert "手动卖出了半仓" not in str(payload[0])
    assert (
        repository.append_investment_memory_event(
            principal_id="principal-a",
            event_key="hermes-turn-1",
            event=event,
            now=_NOW + 3,
        ).state
        == "TOMBSTONED"
    )


def test_outcome_links_prior_facts_without_rewriting_or_recalling_superseded_decision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    decision = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="decision-turn",
        event=InvestmentMemoryEventInput(
            kind="USER_DECISION",
            statement="我先等待量价确认。",
            decision="WAIT",
        ),
        now=_NOW,
    )
    execution = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="execution-turn",
        event=InvestmentMemoryEventInput(
            kind="USER_REPORTED_EXECUTION",
            statement="我已手动降低仓位。",
        ),
        now=_NOW + 1,
    )
    outcome = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="outcome-turn",
        event=InvestmentMemoryEventInput(
            kind="OUTCOME_OBSERVATION",
            statement="两日后量价仍未确认。",
            related_event_ids=(decision.event.event_id, execution.event.event_id),
        ),
        now=_NOW + 2,
    )
    repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="s" * 512,
        event=InvestmentMemoryEventInput(
            kind="USER_DECISION",
            statement="我改为继续等待。",
            decision="CHANGE_PLAN",
            supersedes_event_id=decision.event.event_id,
        ),
        now=_NOW + 3,
    )

    recalled = repository.recall_investment_memory(principal_id="principal-a")

    assert outcome.event.related_event_ids == (
        decision.event.event_id,
        execution.event.event_id,
    )
    assert recalled.outcomes == (outcome.event,)
    assert all(event.event_id != decision.event.event_id for event in recalled.unresolved_decisions)
    old_replay = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="decision-turn",
        event=InvestmentMemoryEventInput(
            kind="USER_DECISION",
            statement="我先等待量价确认。",
            decision="WAIT",
        ),
        now=_NOW + 4,
    )
    assert old_replay.state == "SUPERSEDED"
    assert old_replay.event.statement == "我先等待量价确认。"


def test_global_tombstone_revokes_prior_recall_and_delayed_replay(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    event = InvestmentMemoryEventInput(
        kind="USER_REPORTED_EXECUTION",
        statement="我报告已经完成手动调仓。",
    )
    receipt = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="turn-1",
        event=event,
        now=_NOW,
    )

    repository.tombstone_investment_memory(
        principal_id="principal-a",
        deletion_key="delete-all-turn",
        now=_NOW + 1,
    )

    assert repository.recall_investment_memory(principal_id="principal-a").is_empty
    replay = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="turn-1",
        event=event,
        now=_NOW + 2,
    )
    assert replay.event.event_id == receipt.event.event_id
    assert replay.state == "TOMBSTONED"


def test_stale_route_generation_cannot_append_global_tombstone(tmp_path: Path) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = ResearchStateRepository(database, token_secret=_SECRET, epoch=_EPOCH)
    route_key = "investment-memory-route"
    first_product = {"headline": "第一代咨询"}
    repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-g1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=first_product,
        artifact_hash=_artifact_hash(first_product),
        now=_NOW,
        route_key=route_key,
        route_generation="generation-1",
    )
    repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="memory-g1",
        event=InvestmentMemoryEventInput(
            kind="USER_DECISION",
            statement="我先等待量价确认。",
            decision="WAIT",
        ),
        now=_NOW + 1,
    )

    competing_repository = ResearchStateRepository(
        database,
        token_secret=_SECRET,
        epoch=_EPOCH,
    )
    second_product = {"headline": "第二代咨询"}
    competing_repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-g2",
        contract=_CONTRACT,
        input_snapshot={**_INPUT, "question": "第二代问题"},
        status="completed",
        product=second_product,
        artifact_hash=_artifact_hash(second_product),
        now=_NOW + 2,
        route_key=route_key,
        route_generation="generation-2",
        route_expected_revision=1,
    )

    with pytest.raises(SemanticStateError, match="continuation_not_accessible"):
        repository.tombstone_investment_memory(
            principal_id="principal-a",
            deletion_key="stale-delete-g1",
            now=_NOW + 3,
            route_key=route_key,
            route_generation="generation-1",
            route_expected_revision=1,
        )

    assert not repository.recall_investment_memory(principal_id="principal-a").is_empty
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM investment_memory_events WHERE kind='TOMBSTONE'"
        ).fetchone() == (0,)

    current_route = repository.resolve_route(route_key=route_key)
    assert current_route is not None
    repository.tombstone_investment_memory(
        principal_id="principal-a",
        deletion_key="active-delete-g2",
        now=_NOW + 4,
        route_key=route_key,
        route_generation="generation-2",
        route_expected_revision=int(current_route["active_revision"]),
    )
    assert repository.recall_investment_memory(principal_id="principal-a").is_empty


def test_malformed_route_state_cannot_append_global_tombstone(tmp_path: Path) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = ResearchStateRepository(database, token_secret=_SECRET, epoch=_EPOCH)
    product = {"headline": "咨询"}
    repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-g1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW,
        route_key="malformed-memory-route",
        route_generation="generation-1",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE conversation_routes
            SET seen_generations_json='{"unexpected":"shape"}'
            WHERE route_key='malformed-memory-route'
            """
        )

    with pytest.raises(SemanticStateError, match="semantic_state_corrupt"):
        repository.resolve_route(route_key="malformed-memory-route")
    with pytest.raises(SemanticStateError, match="semantic_state_corrupt"):
        repository.tombstone_investment_memory(
            principal_id="principal-a",
            deletion_key="malformed-route-delete",
            now=_NOW + 1,
            route_key="malformed-memory-route",
            route_generation="generation-1",
            route_expected_revision=1,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM investment_memory_events WHERE kind='TOMBSTONE'"
        ).fetchone() == (0,)


def test_journal_rows_are_append_only_except_for_retention_redaction(tmp_path: Path) -> None:
    database = tmp_path / "append-only.sqlite3"
    repository = ResearchStateRepository(database, token_secret=_SECRET, epoch=_EPOCH)
    receipt = repository.append_investment_memory_event(
        principal_id="principal-a",
        event_key="turn-1",
        event=InvestmentMemoryEventInput(
            kind="USER_REPORTED_EXECUTION",
            statement="用户报告已执行。",
        ),
        now=_NOW,
    )

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable investment memory event"),
    ):
        connection.execute(
            "UPDATE investment_memory_events SET payload_json='{}' WHERE event_id=?",
            (receipt.event.event_id,),
        )
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only investment memory event"),
    ):
        connection.execute(
            "DELETE FROM investment_memory_events WHERE event_id=?",
            (receipt.event.event_id,),
        )

    repository.tombstone_investment_memory(
        principal_id="principal-a",
        deletion_key="delete-turn",
        target_event_id=receipt.event.event_id,
        now=_NOW + 1,
    )
    assert repository.purge_tombstoned_investment_memory(
        now=_NOW + 1,
        retention_seconds=0,
    ) == 1


def test_v7_database_adds_empty_memory_journal_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "published-v7.sqlite3"
    repository = ResearchStateRepository(database, token_secret=_SECRET, epoch=_EPOCH)
    assert repository.recall_investment_memory(principal_id="principal-a").is_empty
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE investment_memory_events")
        connection.execute("UPDATE semantic_state_meta SET schema_version=7 WHERE id=1")

    reopened = ResearchStateRepository(database, token_secret=_SECRET, epoch=_EPOCH)

    assert reopened.recall_investment_memory(principal_id="principal-a").is_empty
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id=1"
        ).fetchone()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_memory_events)")
        }
        triggers = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='trigger' AND tbl_name='investment_memory_events'
                """
            )
        }
    assert version == (SCHEMA_VERSION,)
    assert {"event_id", "event_key_hash", "target_event_id", "payload_json", "purged_at"} <= columns
    assert triggers == {
        "append_only_investment_memory_event",
        "immutable_investment_memory_event",
    }
