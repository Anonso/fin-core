"""B2: run ledger — ResearchStateRepository 内 daily_workspace_run_ledger 表。

RED 目标：append_run_ledger / 只读投影 / v5→v6 迁移在实现前不存在或不符合语义。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.semantic_state import (
    DEFAULT_EPOCH,
    SCHEMA_VERSION,
    ResearchStateRepository,
    SemanticStateError,
)

_TOKEN_SECRET = b"b2-run-ledger-test-secret-0123456789abcdef"


def _repo(path: Path) -> ResearchStateRepository:
    return ResearchStateRepository(path, token_secret=_TOKEN_SECRET, epoch=DEFAULT_EPOCH)


def _stages_json() -> str:
    return json.dumps(
        [
            {"stage": "collect", "status": "COLLECT_READY", "degraded": False, "detail": ""},
            {"stage": "prepare", "status": "PREPARED", "degraded": False, "detail": ""},
            {"stage": "deliver", "status": "DELIVERED", "degraded": False, "detail": ""},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_append_run_ledger_records_run_row(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "state.sqlite3")

    repository.append_run_ledger(
        run_id="20260806T141500Z-abc123",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=_stages_json(),
        collect_identity=json.dumps(
            {"completion_status": "ready", "g_generation": "gen-1", "g_source_coverage_sha256": "h" * 64}
        ),
    )

    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT run_id, trading_day_id, checkpoint, trigger, started_at,
                   completed_at, stage_statuses, collect_identity
            FROM daily_workspace_run_ledger
            """
        ).fetchone()
    assert row[0] == "20260806T141500Z-abc123"
    assert row[1] == "2026-08-06"
    assert row[2] == "morning"
    assert row[3] == "manual"
    assert row[4] == 1_720_000_000.0
    assert row[5] == 1_720_000_360.0
    assert json.loads(row[6])[2]["status"] == "DELIVERED"
    assert json.loads(row[7])["g_generation"] == "gen-1"


def test_append_run_ledger_idempotent_then_conflict(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "state.sqlite3")
    payload = {
        "run_id": "run-1",
        "trading_day_id": "2026-08-06",
        "checkpoint": "morning",
        "trigger": "manual",
        "started_at": 1_720_000_000.0,
        "completed_at": 1_720_000_360.0,
        "stage_statuses": _stages_json(),
        "collect_identity": "{}",
    }
    repository.append_run_ledger(**payload)
    # 同 payload 重复 append：幂等返回，不新增行
    repository.append_run_ledger(**payload)
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM daily_workspace_run_ledger WHERE run_id='run-1'"
        ).fetchone()[0]
    assert count == 1
    # 异 payload 同 run_id：conflict，不覆盖
    with pytest.raises(SemanticStateError) as error:
        repository.append_run_ledger(
            **{**payload, "collect_identity": '{"completion_status":"partial"}'}
        )
    assert error.value.code == "run_ledger_conflict"


def test_v5_database_migrates_to_latest_preserving_obligations(tmp_path: Path) -> None:
    """v5 库（无 run ledger 表）打开后自动迁移到最新（v8），既有 obligation 数据保留。"""
    db_path = tmp_path / "state.sqlite3"
    _repo(db_path)
    assert SCHEMA_VERSION == 8
    # 写入一条真实 obligation（PENDING 状态，迁移后必须原样保留）
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO daily_workspace_obligations(
                workspace_ref, product_version, artifact_hash, state,
                created_at, updated_at
            ) VALUES ('dw:obligation-1', 2, 'sha256:' || 'b' * 64, 'PENDING', 1.0, 1.0)
            """
        )
    # 手工降级为 v5：删掉 run ledger 表，schema_version 改 5
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE daily_workspace_run_ledger")
        connection.execute("UPDATE semantic_state_meta SET schema_version=5 WHERE id=1")
    # 重新打开：自动迁移 v5→v6，表恢复
    reopened = _repo(db_path)
    assert reopened is not None
    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id=1"
        ).fetchone()[0]
        tables = {
            str(r[0])
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            )
        }
    assert version == SCHEMA_VERSION
    assert "daily_workspace_run_ledger" in tables
    # 迁移后 obligation 数据原样保留（D2 数据保留证据，非空壳断言）
    with sqlite3.connect(db_path) as connection:
        obligation = connection.execute(
            "SELECT workspace_ref, state FROM daily_workspace_obligations"
        ).fetchone()
    assert obligation == ("dw:obligation-1", "PENDING")
    reopened.append_run_ledger(
        run_id="post-migration-run",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="schedule",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=_stages_json(),
        collect_identity="{}",
    )


def test_last_run_ledger_success_excludes_degraded_and_failed(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "state.sqlite3")
    failed = json.dumps(
        [{"stage": "collect", "status": "COLLECT_FAILED", "degraded": False, "detail": "gap"}]
    )
    degraded = json.dumps(
        [
            {"stage": "collect", "status": "COLLECT_READY", "degraded": False, "detail": ""},
            {"stage": "prepare", "status": "PREPARED", "degraded": True, "detail": "degraded"},
            {"stage": "deliver", "status": "DELIVERED", "degraded": True, "detail": "degraded"},
        ]
    )
    ok = _stages_json()
    for i, stages in enumerate((failed, degraded, ok)):
        repository.append_run_ledger(
            run_id=f"run-{i}",
            trading_day_id="2026-08-06",
            checkpoint="morning",
            trigger="manual",
            started_at=1_720_000_000.0 + i,
            completed_at=1_720_000_360.0 + i,
            stage_statuses=stages,
            collect_identity="{}",
        )
    last = repository.last_run_ledger_success("2026-08-06")
    assert last == "run-2"
    assert repository.last_run_ledger_success("2026-08-07") is None


def test_recent_run_ledger_returns_bounded_rows_most_recent_first(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "state.sqlite3")
    for i in range(3):
        repository.append_run_ledger(
            run_id=f"run-{i}",
            trading_day_id="2026-08-06",
            checkpoint="morning",
            trigger="manual",
            started_at=1_720_000_000.0 + i,
            completed_at=1_720_000_360.0 + i,
            stage_statuses=_stages_json(),
            collect_identity="{}",
        )
    recent = repository.recent_run_ledger("2026-08-06", limit=2)
    assert [row["run_id"] for row in recent] == ["run-2", "run-1"]
    assert [row["completed_at"] for row in recent] == [1_720_000_362.0, 1_720_000_361.0]




def test_append_run_ledger_rejects_invalid_input(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "state.sqlite3")
    base = {
        "run_id": "run-x",
        "trading_day_id": "2026-08-06",
        "checkpoint": "morning",
        "trigger": "manual",
        "started_at": 1_720_000_000.0,
        "completed_at": 1_720_000_360.0,
        "stage_statuses": _stages_json(),
        "collect_identity": "{}",
    }
    with pytest.raises(ValueError):
        repository.append_run_ledger(**{**base, "trigger": "cron"})
    with pytest.raises(ValueError):
        repository.append_run_ledger(**{**base, "checkpoint": "lunch"})
    with pytest.raises(ValueError):
        repository.append_run_ledger(**{**base, "completed_at": 1_719_999_999.0})
    with pytest.raises(ValueError):
        repository.append_run_ledger(**{**base, "stage_statuses": "not-json"})
    with pytest.raises(ValueError):
        repository.append_run_ledger(**{**base, "collect_identity": "not-json"})
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM daily_workspace_run_ledger"
        ).fetchone()[0]
    assert count == 0


def test_last_run_ledger_success_returns_most_recent_success_after_failure(
    tmp_path: Path,
) -> None:
    """B2 (R2): 成功后再失败——最近一次成功仍可查（失败不抹掉历史成功）。"""
    repository = _repo(tmp_path / "state.sqlite3")
    ok = _stages_json()
    failed = json.dumps(
        [{"stage": "collect", "status": "COLLECT_FAILED", "degraded": False, "detail": "gap"}]
    )
    repository.append_run_ledger(
        run_id="run-ok",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=ok,
        collect_identity="{}",
    )
    repository.append_run_ledger(
        run_id="run-fail",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_361.0,
        completed_at=1_720_000_400.0,
        stage_statuses=failed,
        collect_identity="{}",
    )
    assert repository.last_run_ledger_success("2026-08-06") == "run-ok"


def test_latest_run_ledger_freshness_projection(tmp_path: Path) -> None:
    """B2 (R2+用户约束): freshness 只在真实采集（succeeded/partial）时呈现。"""
    repository = _repo(tmp_path / "state.sqlite3")
    assert repository.latest_run_ledger_freshness("2026-08-06") is None
    repository.append_run_ledger(
        run_id="run-1",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=_stages_json(),
        collect_identity='{"run_status": "succeeded", "g_freshness": "STALE"}',
    )
    assert repository.latest_run_ledger_freshness("2026-08-06") == "STALE"
    repository.append_run_ledger(
        run_id="run-2",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_400.0,
        completed_at=1_720_000_500.0,
        stage_statuses=_stages_json(),
        collect_identity='{"run_status": "succeeded", "g_freshness": "FRESH"}',
    )
    assert repository.latest_run_ledger_freshness("2026-08-06") == "FRESH"


def test_latest_run_ledger_freshness_honors_no_change_real_collection(
    tmp_path: Path,
) -> None:
    """B slice：capture 链已验证后，ingest 的 no_change 是真实采集（窗口重采集 +
    G 重发布）——其 g_freshness 如实反查（旧前提「ZSXQ 未验证采集源」已推翻）。"""
    repository = _repo(tmp_path / "state.sqlite3")
    repository.append_run_ledger(
        run_id="run-old-success",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=_stages_json(),
        collect_identity='{"run_status": "succeeded", "g_freshness": "FRESH"}',
    )
    # 最新 run 是 no_change（capture 链 ingest 的幂等重采集）——如实投影其 freshness
    repository.append_run_ledger(
        run_id="run-no-change",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_400.0,
        completed_at=1_720_000_500.0,
        stage_statuses=_stages_json(),
        collect_identity='{"run_status": "no_change", "g_freshness": "FRESH"}',
    )
    assert repository.latest_run_ledger_freshness("2026-08-06") == "FRESH"


def test_append_conflict_on_any_payload_field_difference(tmp_path: Path) -> None:
    """B2 (R3): 幂等 = 完整 payload 逐字段一致——trigger 不同即 conflict。"""
    repository = _repo(tmp_path / "state.sqlite3")
    base = {
        "run_id": "run-x",
        "trading_day_id": "2026-08-06",
        "checkpoint": "morning",
        "trigger": "manual",
        "started_at": 1_720_000_000.0,
        "completed_at": 1_720_000_360.0,
        "stage_statuses": _stages_json(),
        "collect_identity": "{}",
    }
    repository.append_run_ledger(**base)
    with pytest.raises(SemanticStateError) as error:
        repository.append_run_ledger(**{**base, "trigger": "schedule"})
    assert error.value.code == "run_ledger_conflict"


def test_closed_set_rejects_duplicate_stage(tmp_path: Path) -> None:
    """B2 (S3): 重复 collect 不得判成功——每 stage exactly once。"""
    repository = _repo(tmp_path / "state.sqlite3")
    dup = json.dumps(
        [
            {"stage": "collect", "status": "COLLECT_READY", "degraded": False, "detail": ""},
            {"stage": "collect", "status": "COLLECT_READY", "degraded": False, "detail": ""},
            {"stage": "prepare", "status": "PREPARED", "degraded": False, "detail": ""},
            {"stage": "deliver", "status": "DELIVERED", "degraded": False, "detail": ""},
        ]
    )
    repository.append_run_ledger(
        run_id="run-dup",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=dup,
        collect_identity="{}",
    )
    assert repository.last_run_ledger_success("2026-08-06") is None


def test_non_ascii_payload_idempotent_replay(tmp_path: Path) -> None:
    """B2 (R3): 含中文 reason_code 的同 payload 第二次 append 幂等返回。"""
    repository = _repo(tmp_path / "state.sqlite3")
    stages = json.dumps(
        [
            {"stage": "collect", "status": "COLLECT_FAILED", "degraded": False,
             "detail": "采集不可用"},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = {
        "run_id": "run-cn",
        "trading_day_id": "2026-08-06",
        "checkpoint": "morning",
        "trigger": "manual",
        "started_at": 1_720_000_000.0,
        "completed_at": 1_720_000_360.0,
        "stage_statuses": stages,
        "collect_identity": '{"g_freshness": "未知"}',
    }
    repository.append_run_ledger(**payload)
    repository.append_run_ledger(**payload)  # 幂等：不抛、不新增
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM daily_workspace_run_ledger WHERE run_id='run-cn'"
        ).fetchone()[0]
    assert count == 1


def test_last_run_ledger_success_not_capped_at_100_failures(tmp_path: Path) -> None:
    """B2 (R2): 成功后有 100+ 失败 run——最近一次成功仍可查（回扫不被截断）。"""
    repository = _repo(tmp_path / "state.sqlite3")
    ok = _stages_json()
    failed = json.dumps(
        [{"stage": "collect", "status": "COLLECT_FAILED", "degraded": False, "detail": "gap"}]
    )
    repository.append_run_ledger(
        run_id="run-ok",
        trading_day_id="2026-08-06",
        checkpoint="morning",
        trigger="manual",
        started_at=1_720_000_000.0,
        completed_at=1_720_000_360.0,
        stage_statuses=ok,
        collect_identity="{}",
    )
    for i in range(105):
        repository.append_run_ledger(
            run_id=f"run-fail-{i}",
            trading_day_id="2026-08-06",
            checkpoint="morning",
            trigger="manual",
            started_at=1_720_000_400.0 + i,
            completed_at=1_720_000_500.0 + i,
            stage_statuses=failed,
            collect_identity="{}",
        )
    assert repository.last_run_ledger_success("2026-08-06") == "run-ok"


def test_v6_old_obligation_shape_migrates_to_v7_normalizing_pending_hash(
    tmp_path: Path,
) -> None:
    """B3 缺陷: v6 库 obligations 表为旧 NOT NULL 形状时，重开归一为 canonical
    DDL（v7）：PENDING 行废弃预计算 hash 归一 NULL、CLAIMED/SETTLED 行语义保留。"""
    db_path = tmp_path / "state.sqlite3"
    _repo(db_path)
    assert SCHEMA_VERSION == 8
    with sqlite3.connect(db_path) as connection:
        # 构造 401e1dd5 前的旧形状 obligations 表（presentation_hash NOT NULL）
        connection.execute("DROP TABLE daily_workspace_obligations")
        connection.execute(
            """
            CREATE TABLE daily_workspace_obligations (
                workspace_ref     TEXT NOT NULL,
                product_version   INTEGER NOT NULL,
                artifact_hash     TEXT NOT NULL,
                presentation_hash TEXT NOT NULL,
                state             TEXT NOT NULL
                                      CHECK (state IN ('PENDING', 'CLAIMED', 'SETTLED')),
                claim_token       TEXT,
                claimed_at        REAL,
                settled_at        REAL,
                settlement        TEXT CHECK (settlement IN (
                                       'POSITIVE_ACK', 'EXPLICIT_NOT_SENT', 'OUTCOME_UNKNOWN'
                                   )),
                created_at        REAL NOT NULL,
                updated_at        REAL NOT NULL,
                PRIMARY KEY (workspace_ref, product_version)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO daily_workspace_obligations VALUES
            ('dw:pending-1', 1, 'sha256:' || 'a' * 64, 'sha256:old-precomputed',
             'PENDING', NULL, NULL, NULL, NULL, 1.0, 1.0),
            ('dw:claimed-1', 2, 'sha256:' || 'b' * 64, 'sha256:rendered',
             'CLAIMED', 'token-1', 2.0, NULL, NULL, 1.0, 2.0),
            ('dw:notsent-1', 3, 'sha256:' || 'c' * 64, 'sha256:rendered',
             'SETTLED', 'token-2', 3.0, 4.0, 'EXPLICIT_NOT_SENT', 1.0, 4.0)
            """
        )
        connection.execute("UPDATE semantic_state_meta SET schema_version=6 WHERE id=1")
    # 重开：v6 经 v7 obligations 归一后迁移到最新
    reopened = _repo(db_path)
    assert reopened is not None
    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id=1"
        ).fetchone()[0]
        cols = {r[1]: r[3] for r in connection.execute("PRAGMA table_info(daily_workspace_obligations)")}
        rows = connection.execute(
            "SELECT workspace_ref, presentation_hash, state, claim_token, settlement "
            "FROM daily_workspace_obligations ORDER BY workspace_ref"
        ).fetchall()
    assert version == SCHEMA_VERSION
    assert cols["presentation_hash"] == 0  # 已归一 nullable
    by_ref = {r[0]: r for r in rows}
    # PENDING：废弃预计算 hash 归一 NULL（新 CHECK 语义）
    assert by_ref["dw:pending-1"][1] is None
    assert by_ref["dw:pending-1"][2] == "PENDING"
    # CLAIMED：claim-time 绑定 hash 语义保留
    assert by_ref["dw:claimed-1"][1] == "sha256:rendered"
    assert by_ref["dw:claimed-1"][3] == "token-1"
    # SETTLED+EXPLICIT_NOT_SENT（新 CHECK 不可能态）：归一回 PENDING 可重试
    assert by_ref["dw:notsent-1"][2] == "PENDING"
    assert by_ref["dw:notsent-1"][1] is None
    assert by_ref["dw:notsent-1"][3] is None
    assert by_ref["dw:notsent-1"][4] is None
