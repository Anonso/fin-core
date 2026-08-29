from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def test_duplicate_public_entry_attempts_share_one_sanitized_request_identity(
    tmp_path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    current = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)
    ledger = PublicEntryLedger(
        tmp_path / "runtime-truth.sqlite3",
        realm="test",
        clock=lambda: current,
    )
    command = {
        "action": "consult",
        "question": "这是不能写入工程账本的用户问题",
        "idempotency_key": "request-42",
    }

    first = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_test",
        idempotency_key=command["idempotency_key"],
        request_payload=command,
    )
    ledger.finish(first, outcome="completed")

    current += timedelta(seconds=1)
    duplicate = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_test",
        idempotency_key=command["idempotency_key"],
        request_payload=command,
    )
    ledger.finish(duplicate, outcome="completed")

    snapshot = ledger.snapshot()

    assert first.request_id == duplicate.request_id
    assert first.attempt_id != duplicate.attempt_id
    assert first.dedupe_disposition == "new"
    assert duplicate.dedupe_disposition == "duplicate"
    assert snapshot.total_attempts == 2
    assert snapshot.unique_requests == 1
    assert snapshot.duplicate_attempts == 1
    assert snapshot.idempotency_conflicts == 0
    assert snapshot.fin_response_rate == 1.0
    assert snapshot.transport_success_rate is None
    assert snapshot.observation_scope == "fin_mcp_dispatch_only"
    assert snapshot.data_gaps == ("hermes_delivery_observation_missing",)

    with sqlite3.connect(tmp_path / "runtime-truth.sqlite3") as connection:
        persisted = "\n".join(
            str(value)
            for table in ("public_entry_requests", "public_entry_attempts")
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in row
            if value is not None
        )
    assert command["question"] not in persisted
    assert command["idempotency_key"] not in persisted
    assert "finp_test" not in persisted


def test_read_only_snapshot_reader_never_provisions_a_missing_ledger(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedgerError,
        PublicEntrySnapshotReader,
    )

    database = tmp_path / "missing" / "public-entry.sqlite3"
    reader = PublicEntrySnapshotReader(database, realm="production")

    with pytest.raises(
        PublicEntryLedgerError,
        match="public_entry_store_unavailable",
    ):
        reader.snapshot()

    assert not database.exists()
    assert not database.parent.exists()


def test_idempotency_key_reuse_with_different_payload_is_a_counted_conflict(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    ledger = PublicEntryLedger(
        tmp_path / "runtime-truth.sqlite3",
        realm="test",
    )
    first = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_test",
        idempotency_key="same-key",
        request_payload={"action": "consult", "question": "first"},
    )
    ledger.finish(first, outcome="completed")
    conflict = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_test",
        idempotency_key="same-key",
        request_payload={"action": "consult", "question": "different"},
    )
    ledger.finish(
        conflict,
        outcome="unavailable",
        problem_code="idempotency_conflict",
    )

    snapshot = ledger.snapshot()

    assert conflict.request_id == first.request_id
    assert conflict.dedupe_disposition == "conflict"
    assert snapshot.total_attempts == 2
    assert snapshot.unique_requests == 1
    assert snapshot.idempotency_conflicts == 1
    assert snapshot.terminal_attempts == 2
    assert snapshot.fin_response_rate == 1.0


def test_read_only_snapshot_reader_enforces_the_expected_realm(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
        PublicEntrySnapshotReader,
    )

    database = tmp_path / "runtime-truth.sqlite3"
    ledger = PublicEntryLedger(database, realm="test")
    attempt = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_test",
        idempotency_key=None,
        request_payload={"action": "consult"},
    )
    ledger.finish(attempt, outcome="failed", problem_code="internal_error")

    snapshot = PublicEntrySnapshotReader(database, realm="test").snapshot()

    assert snapshot.total_attempts == 1
    assert snapshot.terminal_attempts == 1
    assert snapshot.fin_response_attempts == 0
    assert snapshot.fin_response_rate == 0.0
    with pytest.raises(
        PublicEntryLedgerError,
        match="public_entry_store_realm_mismatch",
    ):
        PublicEntrySnapshotReader(database, realm="production").snapshot()


def test_idempotency_identity_is_scoped_to_the_authenticated_principal(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    ledger = PublicEntryLedger(
        tmp_path / "runtime-truth.sqlite3",
        realm="test",
    )
    first = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_first",
        idempotency_key="shared-key",
        request_payload={"action": "consult", "question": "first"},
    )
    second = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_second",
        idempotency_key="shared-key",
        request_payload={"action": "consult", "question": "second"},
    )
    ledger.finish(first, outcome="completed")
    ledger.finish(second, outcome="completed")

    snapshot = ledger.snapshot()

    assert first.request_id != second.request_id
    assert first.dedupe_disposition == "new"
    assert second.dedupe_disposition == "new"
    assert snapshot.unique_requests == 2
    assert snapshot.idempotency_conflicts == 0


def test_ledger_refuses_to_add_tables_to_an_unrelated_sqlite_owner(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
    )

    database = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE foreign_owner(value TEXT NOT NULL)")
    database.chmod(0o600)

    with pytest.raises(
        PublicEntryLedgerError,
        match="public_entry_store_schema_mismatch",
    ):
        PublicEntryLedger(database, realm="test")

    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {"foreign_owner"}


def test_ledger_refuses_a_foreign_view_before_any_ddl(tmp_path: Path) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
    )

    database = tmp_path / "foreign-view.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE VIEW foreign_owner AS SELECT 1 AS value")
    database.chmod(0o600)

    with pytest.raises(
        PublicEntryLedgerError,
        match="public_entry_store_schema_mismatch",
    ):
        PublicEntryLedger(database, realm="test")

    with sqlite3.connect(database) as connection:
        objects = {(str(row[0]), str(row[1])) for row in connection.execute("""
                SELECT type, name
                FROM sqlite_master
                WHERE name NOT GLOB 'sqlite_*'
                """)}
    assert objects == {("view", "foreign_owner")}


def test_ledger_rejects_same_column_schema_without_owned_constraints(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
    )

    database = tmp_path / "lookalike.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE runtime_truth_meta(key TEXT, value TEXT);
            CREATE TABLE public_entry_requests(
                request_id TEXT,
                tool_name TEXT,
                principal_scope_hash TEXT,
                idempotency_key_hash TEXT,
                request_hash TEXT,
                first_seen_at TEXT
            );
            CREATE TABLE public_entry_attempts(
                attempt_id TEXT,
                request_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                outcome TEXT,
                problem_code TEXT,
                dedupe_disposition TEXT
            );
            INSERT INTO runtime_truth_meta(key, value) VALUES
                ('schema_name', 'fin-public-entry-ledger'),
                ('schema_version', '1'),
                ('realm', 'test');
            """)
    database.chmod(0o600)

    with pytest.raises(
        PublicEntryLedgerError,
        match="public_entry_store_schema_mismatch",
    ):
        PublicEntryLedger(database, realm="test")


def test_snapshot_connection_is_bound_to_the_validated_inode_during_path_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fin_analyse.runtime.public_entry_ledger as ledger_module
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntrySnapshotReader,
    )

    original = tmp_path / "original.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    backup = tmp_path / "original-backup.sqlite3"
    PublicEntryLedger(original, realm="test")
    replacement_ledger = PublicEntryLedger(replacement, realm="test")
    attempt = replacement_ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local-installation",
        principal_id="finp_replacement",
        idempotency_key=None,
        request_payload={"action": "consult"},
    )
    replacement_ledger.finish(attempt, outcome="completed")
    original_identity = (original.stat().st_dev, original.stat().st_ino)
    real_connect = sqlite3.connect
    swapped = False

    def swapping_connect(database, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(original, backup)
            os.rename(replacement, original)
            try:
                return real_connect(database, *args, **kwargs)
            finally:
                os.rename(original, replacement)
                os.rename(backup, original)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", swapping_connect)

    with pytest.raises(
        ledger_module.PublicEntryLedgerError,
        match="public_entry_store_generation_changed",
    ):
        PublicEntrySnapshotReader(original, realm="test").snapshot()

    assert swapped is True
    assert (original.stat().st_dev, original.stat().st_ino) == original_identity


def test_snapshot_rejects_existing_foreign_key_violations(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
        PublicEntrySnapshotReader,
    )

    database = tmp_path / "runtime-truth.sqlite3"
    PublicEntryLedger(database, realm="test")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("""
            INSERT INTO public_entry_attempts(
                attempt_id, request_id, started_at, finished_at,
                outcome, problem_code, dedupe_disposition
            ) VALUES (
                'att_orphan', 'req_missing', '2026-07-27T09:30:00+00:00',
                '2026-07-27T09:30:01+00:00', 'completed', NULL, 'new'
            )
            """)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is not None

    with pytest.raises(
        PublicEntryLedgerError,
        match="public_entry_store_integrity_mismatch",
    ):
        PublicEntrySnapshotReader(database, realm="test").snapshot()


def test_snapshot_rejects_wal_mode_without_mutating_sidecars(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
        PublicEntrySnapshotReader,
    )

    database = tmp_path / "runtime-truth.sqlite3"
    PublicEntryLedger(database, realm="test")
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE runtime_truth_meta SET value=value")
        writer.commit()
        sidecars = (Path(f"{database}-wal"), Path(f"{database}-shm"))
        assert all(path.is_file() for path in sidecars)
        before = {
            path.name: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in sidecars
        }

        with pytest.raises(
            PublicEntryLedgerError,
            match="public_entry_store_generation_unsupported",
        ):
            PublicEntrySnapshotReader(database, realm="test").snapshot()

        after = {
            path.name: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in sidecars
        }
    finally:
        writer.close()

    assert after == before


def test_snapshot_holds_one_read_transaction_across_schema_and_aggregate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fin_analyse.runtime.public_entry_ledger as ledger_module
    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntrySnapshotReader,
    )

    database = tmp_path / "runtime-truth.sqlite3"
    PublicEntryLedger(database, realm="test")
    real_snapshot = ledger_module._snapshot_from_connection
    writer_errors: list[str] = []

    def snapshot_while_writer_waits(connection):
        def write_same_inode() -> None:
            try:
                with sqlite3.connect(database, timeout=0.05) as writer:
                    writer.execute(
                        "UPDATE runtime_truth_meta SET value='changed' WHERE key='realm'"
                    )
            except sqlite3.OperationalError as error:
                writer_errors.append(str(error))

        thread = threading.Thread(target=write_same_inode)
        thread.start()
        result = real_snapshot(connection)
        thread.join(timeout=2)
        assert not thread.is_alive()
        return result

    monkeypatch.setattr(
        ledger_module,
        "_snapshot_from_connection",
        snapshot_while_writer_waits,
    )

    snapshot = PublicEntrySnapshotReader(database, realm="test").snapshot()

    assert snapshot.total_attempts == 0
    assert writer_errors and "locked" in writer_errors[0].lower()
    with sqlite3.connect(database) as connection:
        realm = connection.execute(
            "SELECT value FROM runtime_truth_meta WHERE key='realm'"
        ).fetchone()[0]
    assert realm == "test"


def test_record_delivery_event_and_snapshot_projection(tmp_path: Path) -> None:
    """delivery 事件记录后 snapshot 投影 transport 事实并消除 gap。"""
    from datetime import UTC, datetime

    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
    )

    clock = [datetime(2026, 8, 1, 10, 0, tzinfo=UTC)]

    def _clock() -> datetime:
        return clock[0]

    ledger = PublicEntryLedger(
        db_path=tmp_path / "runtime-truth.sqlite3",
        realm="production",
        clock=_clock,
    )
    attempt = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local",
        principal_id="principal-1",
        request_payload={"action": "consult", "question": "q"},
        idempotency_key="key-1",
    )
    ledger.finish(attempt, outcome="completed")

    # 无 delivery 事件：gap 保留、transport_rate None、scope 不变
    snapshot = ledger.snapshot()
    assert snapshot.data_gaps == ("hermes_delivery_observation_missing",)
    assert snapshot.transport_success_rate is None
    assert snapshot.observation_scope == "fin_mcp_dispatch_only"

    # 记录 delivery 事件（幂等）
    ledger.record_delivery_event(
        event_id="hermes-obl-1",
        attempt_id=attempt.attempt_id,
        channel="feishu",
        stage="delivered",
        status="succeeded",
        source_contract="hermes.delivery_obligations/v0.19.0",
    )
    ledger.record_delivery_event(
        event_id="hermes-obl-1",
        attempt_id=attempt.attempt_id,
        channel="feishu",
        stage="delivered",
        status="succeeded",
        source_contract="hermes.delivery_obligations/v0.19.0",
    )

    snapshot = ledger.snapshot()
    assert snapshot.data_gaps == ()
    assert snapshot.transport_success_rate == 1.0
    assert snapshot.observation_scope == "fin_mcp_dispatch_plus_hermes_delivery"

    # 未知 attempt 拒绝
    import pytest as _pytest

    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedgerError

    with _pytest.raises(PublicEntryLedgerError):
        ledger.record_delivery_event(
            event_id="hermes-x",
            attempt_id="missing",
            channel="feishu",
            stage="delivered",
            status="succeeded",
        )

    # 幂等冲突：同 event_id 不同 payload 拒绝（不静默 first-win）
    with _pytest.raises(PublicEntryLedgerError):
        ledger.record_delivery_event(
            event_id="hermes-obl-1",
            attempt_id=attempt.attempt_id,
            channel="cli",
            stage="delivered",
            status="succeeded",
        )


def test_claim_scoped_delivery_event_replay_requires_opt_in_and_same_message_id(
    tmp_path: Path,
) -> None:
    """A recovery retry has a new attempt but not a new acceptance fact."""

    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
        PublicEntryLedgerError,
    )

    ledger = PublicEntryLedger(
        db_path=tmp_path / "runtime-truth.sqlite3",
        realm="production",
    )
    first = ledger.begin(
        tool_name="daily_workspace_delivery",
        principal_namespace="local",
        principal_id="finp_daily",
        idempotency_key="dw-dispatch:dw:opaque:2",
        request_payload={"workspace_ref": "dw:opaque", "product_version": 2},
    )
    ledger.finish(first, outcome="completed")
    ledger.record_delivery_event(
        event_id="dw-dispatch-claim-1",
        attempt_id=first.attempt_id,
        channel="feishu",
        stage="dispatched",
        status="succeeded",
        source_contract="fin.dispatch-acceptance/v1",
        message_id="message-1",
    )
    ledger.record_delivery_event(
        event_id="dw-dispatch-empty-id",
        attempt_id=first.attempt_id,
        channel="feishu",
        stage="dispatched",
        status="OUTCOME_UNKNOWN",
        source_contract="fin.dispatch-acceptance/v1",
        message_id="",
    )
    with pytest.raises(PublicEntryLedgerError) as empty_id_error:
        ledger.record_delivery_event(
            event_id="dw-dispatch-empty-id",
            attempt_id=first.attempt_id,
            channel="feishu",
            stage="dispatched",
            status="OUTCOME_UNKNOWN",
            source_contract="fin.dispatch-acceptance/v1",
            message_id=None,
        )
    assert empty_id_error.value.args == ("public_entry_delivery_event_conflict",)

    replay = ledger.begin(
        tool_name="daily_workspace_delivery",
        principal_namespace="local",
        principal_id="finp_daily",
        idempotency_key="dw-dispatch:dw:opaque:2",
        request_payload={"workspace_ref": "dw:opaque", "product_version": 2},
    )
    ledger.finish(replay, outcome="completed")
    with pytest.raises(PublicEntryLedgerError) as default_error:
        ledger.record_delivery_event(
            event_id="dw-dispatch-claim-1",
            attempt_id=replay.attempt_id,
            channel="feishu",
            stage="dispatched",
            status="succeeded",
            source_contract="fin.dispatch-acceptance/v1",
            message_id="message-1",
        )
    assert default_error.value.args == ("public_entry_delivery_event_conflict",)

    ledger.record_delivery_event(
        event_id="dw-dispatch-claim-1",
        attempt_id=replay.attempt_id,
        channel="feishu",
        stage="dispatched",
        status="succeeded",
        source_contract="fin.dispatch-acceptance/v1",
        message_id="message-1",
        allow_same_request_replay=True,
    )

    with pytest.raises(PublicEntryLedgerError) as error:
        ledger.record_delivery_event(
            event_id="dw-dispatch-claim-1",
            attempt_id=replay.attempt_id,
            channel="feishu",
            stage="dispatched",
            status="succeeded",
            source_contract="fin.dispatch-acceptance/v1",
            message_id="message-2",
            allow_same_request_replay=True,
        )
    assert error.value.args == ("public_entry_delivery_event_conflict",)

    conflict = ledger.begin(
        tool_name="daily_workspace_delivery",
        principal_namespace="local",
        principal_id="finp_daily",
        idempotency_key="dw-dispatch:dw:opaque:2",
        request_payload={"workspace_ref": "dw:opaque", "product_version": 3},
    )
    ledger.finish(conflict, outcome="completed")
    with pytest.raises(PublicEntryLedgerError) as conflict_error:
        ledger.record_delivery_event(
            event_id="dw-dispatch-claim-1",
            attempt_id=conflict.attempt_id,
            channel="feishu",
            stage="dispatched",
            status="succeeded",
            source_contract="fin.dispatch-acceptance/v1",
            message_id="message-1",
            allow_same_request_replay=True,
        )
    assert conflict_error.value.args == ("public_entry_delivery_attempt_conflict",)


def test_claim_scoped_event_accepts_new_attempt_after_duplicate_wins_the_race(
    tmp_path: Path,
) -> None:
    """The first `new` attempt can lose the event-insert race to its retry."""

    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    ledger = PublicEntryLedger(
        db_path=tmp_path / "runtime-truth.sqlite3",
        realm="production",
    )
    new_attempt = ledger.begin(
        tool_name="daily_workspace_delivery",
        principal_namespace="local",
        principal_id="finp_daily",
        idempotency_key="dw-dispatch:dw:opaque:2",
        request_payload={"workspace_ref": "dw:opaque", "product_version": 2},
    )
    ledger.finish(new_attempt, outcome="completed")
    duplicate_attempt = ledger.begin(
        tool_name="daily_workspace_delivery",
        principal_namespace="local",
        principal_id="finp_daily",
        idempotency_key="dw-dispatch:dw:opaque:2",
        request_payload={"workspace_ref": "dw:opaque", "product_version": 2},
    )
    ledger.finish(duplicate_attempt, outcome="completed")

    for attempt in (duplicate_attempt, new_attempt):
        ledger.record_delivery_event(
            event_id="dw-dispatch-claim-1",
            attempt_id=attempt.attempt_id,
            channel="feishu",
            stage="dispatched",
            status="succeeded",
            source_contract="fin.dispatch-acceptance/v1",
            message_id="message-1",
            allow_same_request_replay=True,
        )

    with sqlite3.connect(tmp_path / "runtime-truth.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM public_entry_delivery_events"
        ).fetchone() == (1,)


def test_dispatch_acceptance_and_outcome_unknown_are_distinct_stages(tmp_path: Path) -> None:
    """B0: dispatch acceptance（平台接受发送）与 delivery/displayed 回执分离。

    stage='dispatched' 是可记录闭集值；status='OUTCOME_UNKNOWN' 是可记录闭集值；
    message_id 作为 dispatch acceptance 事实随事件持久化。
    """
    from datetime import UTC, datetime

    from fin_analyse.runtime.public_entry_ledger import (
        PublicEntryLedger,
    )

    clock = [datetime(2026, 8, 1, 10, 0, tzinfo=UTC)]

    def _clock() -> datetime:
        return clock[0]

    ledger = PublicEntryLedger(
        db_path=tmp_path / "runtime-truth.sqlite3",
        realm="production",
        clock=_clock,
    )
    attempt = ledger.begin(
        tool_name="fin_consultation",
        principal_namespace="local",
        principal_id="principal-1",
        request_payload={"action": "consult", "question": "q"},
        idempotency_key="key-1",
    )
    ledger.finish(attempt, outcome="completed")

    ledger.record_delivery_event(
        event_id="dispatch-1",
        attempt_id=attempt.attempt_id,
        channel="feishu",
        stage="dispatched",
        status="succeeded",
        source_contract="fin.dispatch-acceptance/v1",
        message_id="om_x100b6803e2134ca0b1625a231c3855a",
    )
    ledger.record_delivery_event(
        event_id="dispatch-2",
        attempt_id=attempt.attempt_id,
        channel="feishu",
        stage="dispatched",
        status="OUTCOME_UNKNOWN",
        source_contract="fin.dispatch-acceptance/v1",
        message_id=None,
    )

    # 持久化断言：dispatch acceptance 事件带 message_id 落账（直接读库核对）
    import sqlite3 as _sqlite3

    con = _sqlite3.connect(tmp_path / "runtime-truth.sqlite3")
    rows = con.execute(
        "SELECT event_id, stage, status, message_id FROM public_entry_delivery_events "
        "WHERE event_id IN ('dispatch-1', 'dispatch-2') ORDER BY event_id"
    ).fetchall()
    con.close()
    assert rows == [
        ("dispatch-1", "dispatched", "succeeded", "om_x100b6803e2134ca0b1625a231c3855a"),
        ("dispatch-2", "dispatched", "OUTCOME_UNKNOWN", None),
    ]
    # 诚实语义：dispatched 不是 exact delivery——snapshot 不得据此声称 delivery
    snapshot = ledger.snapshot()
    assert snapshot.transport_success_rate is None
    assert snapshot.observation_scope == "fin_mcp_dispatch_only"


def test_v2_ledger_migrates_to_v3_with_relabel(tmp_path: Path) -> None:
    """B0: 既有 v2 ledger 经正常初始化升级到 v3，Hermes obligation 观察重标 dispatched。"""
    import sqlite3 as _sqlite3
    from datetime import UTC, datetime

    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger

    db = tmp_path / "runtime-truth-v2.sqlite3"
    con = _sqlite3.connect(db)
    con.execute("PRAGMA application_id=0x46494E50")
    con.execute("PRAGMA user_version=2")
    con.execute(
        "CREATE TABLE runtime_truth_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    from fin_analyse.runtime.public_entry_ledger import _SCHEMA_SQL

    con.execute(_SCHEMA_SQL["public_entry_requests"])
    con.execute(_SCHEMA_SQL["public_entry_attempts"])
    con.execute(
        'CREATE TABLE "public_entry_delivery_events" (event_id TEXT PRIMARY KEY, '
        "attempt_id TEXT NOT NULL REFERENCES public_entry_attempts(attempt_id), "
        "channel TEXT NOT NULL, stage TEXT NOT NULL CHECK "
        "(stage IN ('rendered', 'delivered', 'displayed', 'acknowledged')), "
        "status TEXT NOT NULL CHECK "
        "(status IN ('pending', 'succeeded', 'failed', 'abandoned', 'unknown', 'unobservable')), "
        "source_contract TEXT, observed_at TEXT NOT NULL)"
    )
    con.executemany(
        "INSERT INTO runtime_truth_meta(key, value) VALUES (?, ?)",
        (("schema_name", "fin-public-entry-ledger"), ("schema_version", "2"), ("realm", "production")),
    )
    con.execute(
        "INSERT INTO public_entry_requests VALUES "
        "('req-1', 'fin_consultation', 'h', 'k', 'r', '2026-08-01T10:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO public_entry_attempts VALUES "
        "('att-1', 'req-1', '2026-08-01T10:00:00+00:00', '2026-08-01T10:05:00+00:00', "
        "'completed', NULL, 'new')"
    )
    con.execute(
        "INSERT INTO public_entry_delivery_events VALUES "
        "('hermes-obl-1', 'att-1', 'feishu', 'delivered', 'succeeded', "
        "'hermes.delivery_obligations/v0.19.0', '2026-08-01T10:05:00+00:00')"
    )
    con.commit()
    con.close()
    db.chmod(0o600)

    clock = [datetime(2026, 8, 1, 10, 0, tzinfo=UTC)]

    def _clock() -> datetime:
        return clock[0]

    PublicEntryLedger(db_path=db, realm="production", clock=_clock)

    con = _sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    assert con.execute(
        "SELECT value FROM runtime_truth_meta WHERE key='schema_version'"
    ).fetchone()[0] == "3"
    row = con.execute(
        "SELECT stage, status FROM public_entry_delivery_events WHERE event_id='hermes-obl-1'"
    ).fetchone()
    assert row == ("dispatched", "succeeded")
    assert "message_id" in [
        r[1] for r in con.execute("PRAGMA table_info(public_entry_delivery_events)").fetchall()
    ]
    con.close()
