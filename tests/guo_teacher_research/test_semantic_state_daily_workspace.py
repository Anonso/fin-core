"""Daily Decision Workspace state layer: v2 schema migration and chain lifecycle."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.semantic_state import (
    DAILY_WORKSPACE_CHECKPOINT_CLAIM_TTL_SECONDS,
    SCHEMA_VERSION,
    DailyWorkspaceTimingSample,
    ResearchStateRepository,
    SemanticStateError,
    SemanticStateSnapshotReader,
)
from tests.fixtures.daily_workspace import (
    daily_workspace_advisory_product,
    daily_workspace_failure_notice,
)

_TOKEN_SECRET = b"semantic-state-test-secret-is-32-bytes!!"
_EPOCH = "semantic-research-v1-test"
_NOW = 1_720_000_000.0
_CONTRACT = {
    "schema": "fin.semantic-research-contract/v1",
    "outcome_mode": "research",
    "scope": "general",
    "policy_version": "m4-test",
}
_INPUT = {"question": "今天最值得处理什么？", "context": {}}
_PRODUCT = daily_workspace_advisory_product(
    trading_day_id="2026-08-03",
    checkpoint="premarket",
    consultation_as_of="2026-08-03T09:10:00+08:00",
    with_g=True,
)


def _timed_product(
    checkpoint: str,
    *,
    target_at: str,
    prepared_at: str,
    generated_at: str,
    runtime_invoked: bool = True,
    degraded: bool = False,
) -> dict[str, object]:
    trading_day_id = target_at[:10]
    product = (
        daily_workspace_failure_notice(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
            runtime_invoked=runtime_invoked,
        )
        if degraded
        else daily_workspace_advisory_product(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
            consultation_as_of=prepared_at,
            with_g=True,
        )
    )
    product["delivery_timing"] = {
        "schema": "fin.daily-workspace-delivery-timing/v1",
        "target_at": target_at,
        "prepared_at": prepared_at,
        "generated_at": generated_at,
        "evidence_cutoff_at": None if degraded else prepared_at,
    }
    return product


def _finalize_timing_sample(
    repo: ResearchStateRepository,
    *,
    principal_id: str,
    trading_day_id: str,
    checkpoint: str,
    product: dict[str, object],
    now: float,
) -> None:
    repo.create_daily_workspace_chain(
        principal_id=principal_id,
        trading_day_id=trading_day_id,
        idempotency_key=f"daily:{trading_day_id}:init",
        now=now,
    )
    repo.finalize_scheduled_checkpoint(
        principal_id=principal_id,
        trading_day_id=trading_day_id,
        idempotency_key=f"daily:{trading_day_id}:{checkpoint}",
        checkpoint=checkpoint,
        product=product,
        now=now,
    )


def _repo(path: Path) -> ResearchStateRepository:
    return ResearchStateRepository(path, token_secret=_TOKEN_SECRET, epoch=_EPOCH)


def _v1_ddl(table_name: str) -> str:
    """Derive the byte-identical schema-v1 DDL for one table.

    The production v1 database was created by the pre-v2 ``_SCHEMA_DDL``; this
    fixture reproduces that exact text so the migration test matches a real
    v1 owner (including the stored ``sqlite_master.sql`` bytes).
    """

    from fin_analyse.guo_teacher_research.semantic_state import _TABLE_DDL

    ddl = _TABLE_DDL[table_name]
    if table_name == "chains":
        ddl = ddl.replace(
            "        chain_kind   TEXT NOT NULL DEFAULT 'consultation',\n", ""
        ).replace("        business_key TEXT,\n", "")
    elif table_name == "chain_versions":
        ddl = ddl.replace(
            "                                     'closed', 'daily_workspace'\n",
            "                                     'closed'\n",
        )
    elif table_name == "continuations":
        # v3 新增的 runtime handle 与 turn lease 列在 v1 中不存在
        ddl = ddl.replace(
            """        active_job_id       TEXT REFERENCES jobs(job_id),
        runtime_backend     TEXT,
        session_id          TEXT,
        identity_hash       TEXT,
        product_version     INTEGER,
        active_turn_id      TEXT,
        turn_lease_expires_at REAL,
        turn_fencing_token  INTEGER NOT NULL DEFAULT 0 CHECK (turn_fencing_token >= 0),
        created_at          REAL NOT NULL,""",
            """        active_job_id       TEXT REFERENCES jobs(job_id),
        created_at          REAL NOT NULL,""",
        )
    elif table_name == "idempotency":
        ddl = ddl.replace(
            """        CHECK (
            (capability = 'daily_workspace' AND job_id IS NULL AND product_id IS NULL)
            OR (job_id IS NULL) != (product_id IS NULL)
        ),""",
            "        CHECK ((job_id IS NULL) != (product_id IS NULL)),",
        )
    return ddl


def _create_v1_database(path: Path) -> None:
    """Create a schema-v1 owner database with one pre-existing answer chain."""

    from fin_analyse.guo_teacher_research.semantic_state import _SCHEMA_DDL

    connection = sqlite3.connect(path)
    try:
        for ddl in _SCHEMA_DDL:
            if "daily_workspace_chain_per_principal" in ddl:
                continue
            table_row = __import__("re").search(r"CREATE TABLE (\w+)", ddl)
            if table_row is None:
                connection.execute(ddl)
                continue
            table_name = table_row.group(1)
            # runtime_session_gc 是 v3 才有的表，真实 v1 owner 不存在
            if table_name == "runtime_session_gc":
                continue
            if table_name in {"chains", "chain_versions", "idempotency", "continuations"}:
                connection.execute(_v1_ddl(table_name))
            else:
                connection.execute(ddl)
        connection.execute(
            """
            INSERT INTO semantic_state_meta(id, schema_name, schema_version, epoch)
            VALUES (1, 'semantic-research-v1', 1, ?)
            """,
            (_EPOCH,),
        )
        connection.execute(
            """
            INSERT INTO chains(chain_id, principal_id, status, created_at, updated_at)
            VALUES ('old-chain', 'finp_v1_user', 'active', ?, ?)
            """,
            (_NOW, _NOW),
        )
        # 真实 v1 owner 的每条链都有 continuation；token_hash 是 SHA-256。
        connection.execute(
            """
            INSERT INTO continuations(
                token_hash, epoch, principal_id, chain_id, active_job_id,
                created_at, updated_at
            ) VALUES (?, 'semantic-research-v1-test', 'finp_v1_user',
                      'old-chain', NULL, ?, ?)
            """,
            (
                __import__("hashlib").sha256(b"old-chain-token").hexdigest(),
                _NOW,
                _NOW,
            ),
        )
        old_product_json = json.dumps(
            {"question": "v1 既有答案"},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        old_product_hash = (
            "sha256:" + __import__("hashlib").sha256(old_product_json.encode()).hexdigest()
        )
        connection.execute(
            """
            INSERT INTO products(
                product_id, chain_id, job_id, product_version, status,
                product_json, artifact_hash, created_at
            ) VALUES ('old-product', 'old-chain', NULL, 1, 'completed', ?, ?, ?)
            """,
            (old_product_json, old_product_hash, _NOW),
        )
        # 真实 v1 owner 的每个 product 都有版本审计记录；snapshot reader
        # 要求每个 product 恰好一个版本。
        import hashlib

        def sha256(value: str) -> str:
            return hashlib.sha256(value.encode()).hexdigest()

        request_hash = sha256("{}\x00{}")
        # answer product 必有对应 idempotency 记录（create_answer 同事务写入）。
        connection.execute(
            """
            INSERT INTO idempotency(
                principal_id, capability, key_hash, request_hash,
                chain_id, job_id, product_id, created_at
            ) VALUES ('finp_v1_user', 'guo.decision_guidance', ?, ?,
                      'old-chain', NULL, 'old-product', ?)
            """,
            (sha256("old-key"), request_hash, _NOW),
        )
        connection.execute(
            """
            INSERT INTO chain_versions(
                seq, chain_id, version_no, kind, job_id, product_id,
                contract_json, input_json, contract_hash, input_hash,
                payload_json, created_at
            ) VALUES (1, 'old-chain', 1, 'answer', NULL, 'old-product',
                      '{}', '{}', 'c-hash', 'i-hash',
                      ?, ?)
            """,
            (
                json.dumps(
                    {
                        "product_version": 1,
                        "status": "completed",
                        "response_projection": {
                            "as_of": _NOW,
                            "data_gaps": [],
                            "provenance": None,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                _NOW,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_new_repository_creates_latest_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")

    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        row = connection.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id = 1"
        ).fetchone()
        assert int(row[0]) == SCHEMA_VERSION
        daily_index = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'daily_workspace_chain_per_principal'
            """
        ).fetchone()
        assert daily_index is not None
    assert repo is not None


def test_migrated_v1_database_passes_snapshot_reader_contract(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _create_v1_database(path)
    _repo(path)
    path.chmod(0o600)

    from fin_analyse.guo_teacher_research.semantic_state import SemanticStateSnapshotReader

    snapshot = SemanticStateSnapshotReader(path, epoch=_EPOCH).terminal_reconciliation_snapshot()
    assert snapshot.total_jobs == 0


def test_daily_workspace_database_passes_snapshot_reader_contract(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = _repo(path)
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )
    path.chmod(0o600)

    from fin_analyse.guo_teacher_research.semantic_state import SemanticStateSnapshotReader

    snapshot = SemanticStateSnapshotReader(path, epoch=_EPOCH).terminal_reconciliation_snapshot()
    assert snapshot.total_jobs == 0


def test_v1_database_migrates_additively_preserving_existing_data(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _create_v1_database(path)

    repo = _repo(path)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id = 1"
        ).fetchone()
        assert int(row[0]) == SCHEMA_VERSION
        old_product = connection.execute(
            "SELECT product_json FROM products WHERE product_id = 'old-product'"
        ).fetchone()
        assert json.loads(str(old_product[0])) == {"question": "v1 既有答案"}
        daily_index = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'daily_workspace_chain_per_principal'
            """
        ).fetchone()
        assert daily_index is not None
    # 迁移后 daily 能力可用。
    assert (
        repo.find_daily_workspace(principal_id="finp_v1_user", trading_day_id="2026-08-03") is None
    )


def test_daily_chain_lifecycle_create_append_read(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")

    assert repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03") is None

    chain = repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    assert chain.workspace_ref
    assert chain.continuation_token
    assert chain.trading_day_id == "2026-08-03"

    version_1 = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )
    assert version_1.product_version == 1
    assert version_1.workspace_ref == chain.workspace_ref

    read = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert read is not None
    assert read.product_version == 1
    assert read.product["checkpoint"] == "premarket"
    assert read.workspace_ref == chain.workspace_ref

    product_2 = {**_PRODUCT, "product_version": 2, "checkpoint": "morning"}
    version_2 = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:morning:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product=product_2,
        now=_NOW + 100.0,
    )
    assert version_2.product_version == 2

    latest = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert latest is not None
    assert latest.product_version == 2
    assert latest.product["checkpoint"] == "morning"


def test_daily_append_accepts_normal_product_without_g_receipt(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    unverified = {
        **_PRODUCT,
        "agent_provenance": {
            "runtime_invoked_at_generation": True,
            "output_used": True,
        },
    }

    read = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=unverified,
        now=_NOW,
    )
    assert read.product_version == 1


def test_daily_append_accepts_normal_product_that_does_not_claim_g(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    product = daily_workspace_advisory_product(
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        consultation_as_of="2026-08-03T09:10:00+08:00",
        with_g=False,
    )

    read = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=product,
        now=_NOW,
    )

    assert read.product["consultation_product"] == product["consultation_product"]
    assert "product_bound_g_receipt" not in read.product["agent_provenance"]


def test_daily_chain_is_unique_per_principal_and_trading_day(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")

    first = repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    # 同一 idempotency key 重试 → 返回同一链。
    replay = repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW + 1.0,
    )
    assert replay.chain_id == first.chain_id
    assert replay.workspace_ref == first.workspace_ref

    # 不同 idempotency key 撞同一 (principal, day) → 冲突。
    with pytest.raises(SemanticStateError, match="daily_workspace_chain_exists"):
        repo.create_daily_workspace_chain(
            principal_id="finp_daily",
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:other",
            now=_NOW,
        )

    # 另一 principal 同日 → 独立链。
    other = repo.create_daily_workspace_chain(
        principal_id="finp_other",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    assert other.chain_id != first.chain_id


def test_daily_append_enforces_parent_version_and_replay(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )

    # 同 key 重放 → 不新增版本。
    replayed = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW + 1.0,
    )
    assert replayed.product_version == 1

    # parent 漂移 → 冲突，不 fork。
    with pytest.raises(SemanticStateError, match="continuation_conflict"):
        repo.append_daily_workspace_version(
            principal_id="finp_daily",
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:drift:product",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=0,
            status="completed",
            product=_PRODUCT,
            now=_NOW + 2.0,
        )
    # 但正确的 parent 推进成功。
    product_2 = {**_PRODUCT, "product_version": 2}
    repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:morning:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product=product_2,
        now=_NOW + 3.0,
    )
    assert (
        repo.find_daily_workspace(
            principal_id="finp_daily", trading_day_id="2026-08-03"
        ).product_version
        == 2
    )


def test_daily_append_requires_existing_chain(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")

    with pytest.raises(SemanticStateError, match="daily_workspace_chain_missing"):
        repo.append_daily_workspace_version(
            principal_id="finp_daily",
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:premarket:product",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=0,
            status="completed",
            product=_PRODUCT,
            now=_NOW,
        )


def test_daily_append_replay_returns_exact_version_not_latest(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )
    product_2 = {**_PRODUCT, "checkpoint": "morning"}
    repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:morning:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product=product_2,
        now=_NOW + 100.0,
    )

    # 重放 v1 的 key 必须精确返回 v1 版本，而不是链上 latest。
    replayed = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW + 200.0,
    )
    assert replayed.product_version == 1
    assert replayed.product["checkpoint"] == "premarket"


def test_daily_append_binds_fin_owned_identity_and_rejects_internals(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    chain = repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    read = repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )
    # repository 注入身份字段。
    assert read.product["workspace_ref"] == chain.workspace_ref
    assert read.product["trading_day_id"] == "2026-08-03"
    assert read.product["product_version"] == 1
    assert read.product["parent_product_version"] == 0

    # 夹带内部 identity 拒绝。
    with pytest.raises(SemanticStateError, match="forbidden_daily_workspace_identity_field"):
        repo.append_daily_workspace_version(
            principal_id="finp_daily",
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:leak:product",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={**_PRODUCT, "chain_id": "internal-chain"},
            now=_NOW + 1.0,
        )

    # 冲突的 workspace_ref 拒绝。
    with pytest.raises(SemanticStateError, match="daily_workspace_identity_conflict"):
        repo.append_daily_workspace_version(
            principal_id="finp_daily",
            trading_day_id="2026-08-03",
            idempotency_key="daily:2026-08-03:ref:product",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={**_PRODUCT, "workspace_ref": "foreign-ref"},
            now=_NOW + 2.0,
        )


def test_daily_workspace_ref_is_opaque_and_stable(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    chain = repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    assert chain.chain_id not in chain.workspace_ref
    assert chain.continuation_token not in chain.workspace_ref
    read = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert read is None  # 尚无版本
    repo.append_daily_workspace_version(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket:product",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )
    after = repo.find_daily_workspace(principal_id="finp_daily", trading_day_id="2026-08-03")
    assert after is not None
    assert after.workspace_ref == chain.workspace_ref


def test_daily_workspace_timing_samples_are_canonical_scoped_and_agent_attempted(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    principal_id = "finp_timing"
    _finalize_timing_sample(
        repo,
        principal_id=principal_id,
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-03T09:20:00+08:00",
            prepared_at="2026-08-03T08:50:00+08:00",
            generated_at="2026-08-03T08:56:00+08:00",
        ),
        now=_NOW,
    )
    _finalize_timing_sample(
        repo,
        principal_id=principal_id,
        trading_day_id="2026-08-04",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-04T09:20:00+08:00",
            prepared_at="2026-08-04T08:55:00+08:00",
            generated_at="2026-08-04T08:55:00+08:00",
            runtime_invoked=False,
            degraded=True,
        ),
        now=_NOW + 1.0,
    )
    _finalize_timing_sample(
        repo,
        principal_id=principal_id,
        trading_day_id="2026-08-05",
        checkpoint="morning",
        product=_timed_product(
            "morning",
            target_at="2026-08-05T10:00:00+08:00",
            prepared_at="2026-08-05T09:35:00+08:00",
            generated_at="2026-08-05T09:39:00+08:00",
        ),
        now=_NOW + 2.0,
    )
    _finalize_timing_sample(
        repo,
        principal_id=principal_id,
        trading_day_id="2026-08-06",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-06T09:20:00+08:00",
            prepared_at="2026-08-06T08:50:00+08:00",
            generated_at="2026-08-06T09:22:00+08:00",
            degraded=True,
        ),
        now=_NOW + 3.0,
    )
    # 同日附加的非 canonical idempotency version 不能替代 checkpoint sample。
    repo.append_daily_workspace_version(
        principal_id=principal_id,
        trading_day_id="2026-08-06",
        idempotency_key="daily:2026-08-06:premarket:diagnostic",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product=_timed_product(
            "premarket",
            target_at="2026-08-06T09:20:00+08:00",
            prepared_at="2026-08-06T08:00:00+08:00",
            generated_at="2026-08-06T08:01:00+08:00",
        ),
        now=_NOW + 4.0,
    )
    _finalize_timing_sample(
        repo,
        principal_id="finp_other",
        trading_day_id="2026-08-07",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-07T09:20:00+08:00",
            prepared_at="2026-08-07T08:50:00+08:00",
            generated_at="2026-08-07T08:51:00+08:00",
        ),
        now=_NOW + 5.0,
    )

    samples = repo.read_daily_workspace_timing_samples(
        principal_id=principal_id,
        checkpoint="premarket",
        max_samples=10,
    )

    assert all(isinstance(sample, DailyWorkspaceTimingSample) for sample in samples)
    assert [sample.trading_day_id for sample in samples] == ["2026-08-06", "2026-08-03"]
    assert [sample.degraded for sample in samples] == [True, False]
    assert all(sample.agent_runtime_invoked for sample in samples)
    assert samples[0].prepared_at.isoformat() == "2026-08-06T08:50:00+08:00"
    assert samples[0].generated_at.isoformat() == "2026-08-06T09:22:00+08:00"
    assert not hasattr(samples[0], "product")
    assert not hasattr(samples[0], "workspace_ref")
    assert repo.read_daily_workspace_timing_samples(
        principal_id=principal_id,
        checkpoint="premarket",
        max_samples=1,
    ) == (samples[0],)


def test_daily_workspace_timing_samples_skip_legacy_or_invalid_timing_and_bound_input(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "state.sqlite3")
    repo.create_daily_workspace_chain(
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:init",
        now=_NOW,
    )
    repo.append_daily_workspace_version(
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=0,
        status="completed",
        product=_PRODUCT,
        now=_NOW,
    )

    assert (
        repo.read_daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="premarket",
            max_samples=1,
        )
        == ()
    )
    with pytest.raises(ValueError, match="checkpoint"):
        repo.read_daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="invalid",
            max_samples=1,
        )
    with pytest.raises(ValueError, match="max_samples"):
        repo.read_daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="premarket",
            max_samples=0,
        )


def test_daily_workspace_timing_samples_verify_artifact_hash(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = _repo(path)
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-03T09:20:00+08:00",
            prepared_at="2026-08-03T08:50:00+08:00",
            generated_at="2026-08-03T08:55:00+08:00",
        ),
        now=_NOW,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE products SET product_json = '{}' ")

    with pytest.raises(SemanticStateError, match="semantic_state_corrupt"):
        repo.read_daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="premarket",
            max_samples=1,
        )


def test_read_only_snapshot_reader_projects_timing_samples_without_mutating_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repo = _repo(path)
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-03T09:20:00+08:00",
            prepared_at="2026-08-03T08:50:00+08:00",
            generated_at="2026-08-03T08:56:00+08:00",
        ),
        now=_NOW,
    )
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-04",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-04T09:20:00+08:00",
            prepared_at="2026-08-04T08:50:00+08:00",
            generated_at="2026-08-04T08:50:00+08:00",
            runtime_invoked=False,
            degraded=True,
        ),
        now=_NOW + 1.0,
    )
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-05",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-05T09:20:00+08:00",
            prepared_at="2026-08-05T08:50:00+08:00",
            generated_at="2026-08-05T09:22:00+08:00",
            degraded=True,
        ),
        now=_NOW + 2.0,
    )
    path.chmod(0o600)
    parts = (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal"))
    before = {part.name: part.read_bytes() for part in parts if part.exists()}

    samples = SemanticStateSnapshotReader(
        path,
        epoch=_EPOCH,
    ).daily_workspace_timing_samples(
        principal_id="finp_timing",
        checkpoint="premarket",
        max_samples=10,
    )

    assert [sample.trading_day_id for sample in samples] == ["2026-08-05", "2026-08-03"]
    assert [sample.degraded for sample in samples] == [True, False]
    assert all(sample.agent_runtime_invoked for sample in samples)
    assert not hasattr(samples[0], "product")
    assert not hasattr(samples[0], "workspace_ref")
    assert {part.name: part.read_bytes() for part in parts if part.exists()} == before


def test_read_only_timing_snapshot_does_not_require_unrelated_terminal_reconciliation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repo = _repo(path)
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-03T09:20:00+08:00",
            prepared_at="2026-08-03T08:50:00+08:00",
            generated_at="2026-08-03T08:55:00+08:00",
        ),
        now=_NOW,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO chains(
                chain_id, principal_id, chain_kind, business_key,
                status, created_at, updated_at
            ) VALUES ('unrelated-chain', 'finp_unrelated', 'consultation', NULL, 'active', ?, ?)
            """,
            (_NOW, _NOW),
        )
        connection.execute(
            """
            INSERT INTO continuations(
                token_hash, epoch, principal_id, chain_id, active_job_id,
                created_at, updated_at
            ) VALUES ('unrelated-token', 'foreign-epoch', 'finp_unrelated', 'unrelated-chain', NULL, ?, ?)
            """,
            (_NOW, _NOW),
        )
    path.chmod(0o600)

    samples = SemanticStateSnapshotReader(path, epoch=_EPOCH).daily_workspace_timing_samples(
        principal_id="finp_timing",
        checkpoint="premarket",
        max_samples=1,
    )

    assert [sample.trading_day_id for sample in samples] == ["2026-08-03"]


def test_read_only_timing_snapshot_rejects_related_product_chain_drift(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = _repo(path)
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-03T09:20:00+08:00",
            prepared_at="2026-08-03T08:50:00+08:00",
            generated_at="2026-08-03T08:55:00+08:00",
        ),
        now=_NOW,
    )
    unrelated = repo.create_daily_workspace_chain(
        principal_id="finp_other",
        trading_day_id="2026-08-04",
        idempotency_key="daily:2026-08-04:init",
        now=_NOW + 1.0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE products SET chain_id=?",
            (unrelated.chain_id,),
        )
    path.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(path, epoch=_EPOCH).daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="premarket",
            max_samples=1,
        )


def test_read_only_snapshot_reader_timing_projection_fails_closed_on_artifact_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repo = _repo(path)
    _finalize_timing_sample(
        repo,
        principal_id="finp_timing",
        trading_day_id="2026-08-03",
        checkpoint="premarket",
        product=_timed_product(
            "premarket",
            target_at="2026-08-03T09:20:00+08:00",
            prepared_at="2026-08-03T08:50:00+08:00",
            generated_at="2026-08-03T08:55:00+08:00",
        ),
        now=_NOW,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE products SET product_json = '{}'")
    path.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(path, epoch=_EPOCH).daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="premarket",
            max_samples=1,
        )


def test_read_only_timing_snapshot_never_provisions_missing_state(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "state.sqlite3"

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(path, epoch=_EPOCH).daily_workspace_timing_samples(
            principal_id="finp_timing",
            checkpoint="premarket",
            max_samples=1,
        )

    assert not path.exists()
    assert not path.parent.exists()


def test_checkpoint_claim_reclaims_stale_unbound_claim_after_ttl(tmp_path: Path) -> None:
    """SIGTERM/OOM 遗留的未绑定 claim（finally 不执行）超 TTL 后自动回收重取；
    新鲜未绑定 claim 仍拒绝第二个 caller，绑定行永不回收。"""
    repo = _repo(tmp_path / "state.sqlite3")
    repo.create_daily_workspace_chain(
        principal_id="finp_daily",
        trading_day_id="2026-08-03",
        idempotency_key="daily:2026-08-03:premarket",
        now=_NOW,
    )
    claim = {
        "principal_id": "finp_daily",
        "trading_day_id": "2026-08-03",
        "idempotency_key": "daily:2026-08-03:premarket:prepare",
    }

    assert repo.acquire_daily_workspace_checkpoint(**claim, now=_NOW)
    # TTL 内的未绑定 claim = 可能仍在生成，保持 generation-in-progress 语义。
    assert not repo.acquire_daily_workspace_checkpoint(**claim, now=_NOW + 60.0)
    # 超 TTL 的僵尸 claim 回收并重取成功。
    assert repo.acquire_daily_workspace_checkpoint(
        **claim, now=_NOW + DAILY_WORKSPACE_CHECKPOINT_CLAIM_TTL_SECONDS + 60.0
    )
