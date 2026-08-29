"""Transactional state contract for semantic research continuations."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import fin_analyse.guo_teacher_research.semantic_state as semantic_state_module
from fin_analyse.guo_teacher_research.semantic_state import (
    SCHEMA_VERSION,
    JobLease,
    ResearchStateRepository,
    SemanticStateError,
    SemanticStateSnapshotReader,
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
_INPUT = {"question": "Helium 的投资逻辑是否改变？", "context": {}}


def _artifact_hash(product: object) -> str:
    encoded = json.dumps(
        product,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _repo(path: Path) -> ResearchStateRepository:
    return ResearchStateRepository(path, token_secret=_TOKEN_SECRET, epoch=_EPOCH)


def _create_constraint_free_semantic_state_lookalike(path: Path) -> None:
    canonical = path.with_name("canonical-semantic-state.sqlite3")
    _repo(canonical)
    with sqlite3.connect(canonical) as source:
        table_name_query = """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
                ORDER BY name
            """
        table_name_rows = source.execute(table_name_query)
        table_names = [str(row[0]) for row in table_name_rows]
        columns = {
            table_name: [
                (str(row[1]), str(row[2]))
                for row in source.execute(f'PRAGMA table_info("{table_name}")')
            ]
            for table_name in table_names
        }

    with sqlite3.connect(path) as target:
        for table_name in table_names:
            column_sql = ", ".join(
                f'"{column_name}" {column_type}' for column_name, column_type in columns[table_name]
            )
            target.execute(f'CREATE TABLE "{table_name}" ({column_sql})')
        target.execute(
            """
            INSERT INTO semantic_state_meta(id, schema_name, schema_version, epoch)
            VALUES (1, 'semantic-research-v1', 1, ?)
            """,
            (_EPOCH,),
        )
        target.execute(
            "INSERT INTO jobs(job_id, state, coordinated_version_no) "
            "VALUES ('lookalike-terminal-job', 'succeeded', NULL)"
        )
    path.chmod(0o600)


def _install_snapshot_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    child = tmp_path / "_semantic_snapshot_child.py"
    child.write_text(source, encoding="utf-8")
    child.chmod(0o644)
    monkeypatch.setattr(semantic_state_module, "__file__", str(tmp_path / "semantic_state.py"))


def _replace_semantic_table_sql(
    database: Path,
    *,
    table_name: str,
    old: str,
    new: str,
) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        assert row is not None
        sql = str(row[0])
        assert sql.count(old) == 1
        changed_sql = sql.replace(old, new, 1)
        schema_version_row = connection.execute("PRAGMA schema_version").fetchone()
        assert schema_version_row is not None
        schema_version = int(schema_version_row[0])
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (changed_sql, table_name),
        )
        connection.commit()
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")


def _assert_snapshot_unavailable(database: Path) -> None:
    with pytest.raises(SemanticStateError) as error:
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()
    assert error.value.code == "semantic_state_unavailable"
    assert str(error.value) == "semantic_state_unavailable"
    assert error.value.__cause__ is None


def _assert_path_free_snapshot_failure(
    error: BaseException,
    *,
    forbidden_paths: tuple[str, ...],
) -> None:
    assert isinstance(error, SemanticStateError)
    assert error.code == "semantic_state_unavailable"
    assert str(error) == "semantic_state_unavailable"
    assert repr(error) == "SemanticStateError('semantic_state_unavailable')"
    assert error.__cause__ is None
    visible: BaseException | None = error
    while visible is not None:
        rendered = (str(visible), repr(visible))
        assert all(path not in value for path in forbidden_paths for value in rendered)
        if visible.__cause__ is not None:
            visible = visible.__cause__
        elif visible.__context__ is not None and not visible.__suppress_context__:
            visible = visible.__context__
        else:
            visible = None


def _snapshot_store_bytes(
    *paths: Path,
) -> dict[str, tuple[int, int, int, str]]:
    return {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in paths
    }


def _admit(
    repository: ResearchStateRepository,
    *,
    principal_id: str = "principal-a",
    idempotency_key: str = "request-1",
    input_snapshot: object = _INPUT,
):
    return repository.admit_research(
        principal_id=principal_id,
        idempotency_key=idempotency_key,
        contract=_CONTRACT,
        input_snapshot=input_snapshot,
        deadline_at=_NOW + 600,
        now=_NOW,
    )


def _finish_success(
    repository: ResearchStateRepository,
    lease: JobLease,
    *,
    now: float,
) -> None:
    product = {"summary": "thesis remains intact", "confidence": "medium"}
    repository.finalize(
        lease=lease,
        status="succeeded",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=now,
    )


def test_read_only_state_reader_never_provisions_missing_semantic_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing" / "state.sqlite3"
    reader = SemanticStateSnapshotReader(database, epoch=_EPOCH)

    with pytest.raises(SemanticStateError) as error:
        reader.terminal_reconciliation_snapshot()

    assert error.value.code == "semantic_state_unavailable"
    assert str(error.value) == "semantic_state_unavailable"
    assert error.value.__cause__ is None
    assert not database.exists()
    assert not database.parent.exists()


def test_read_only_state_reader_observes_existing_terminal_reconciliation_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)
    database.chmod(0o600)

    snapshot = SemanticStateSnapshotReader(
        database,
        epoch=_EPOCH,
    ).terminal_reconciliation_snapshot()

    assert snapshot.total_jobs == 1
    assert snapshot.terminal_jobs == 1
    assert snapshot.uncoordinated_terminal_jobs == 1
    assert snapshot.reconciled_now == 0


def test_read_only_state_reader_classifies_expired_job_without_mutating_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    database.chmod(0o600)
    before = _snapshot_store_bytes(database)

    snapshot = SemanticStateSnapshotReader(
        database,
        epoch=_EPOCH,
        clock=lambda: _NOW + 600,
    ).terminal_reconciliation_snapshot()

    assert snapshot.total_jobs == 1
    assert snapshot.active_jobs == 0
    assert snapshot.expired_jobs == 1
    assert snapshot.terminal_jobs == 0
    assert snapshot.data_gaps == ("semantic_expired_jobs_pending",)
    assert _snapshot_store_bytes(database) == before


def test_read_only_state_reader_rejects_constraint_free_schema_lookalike(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _create_constraint_free_semantic_state_lookalike(database)

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_rejects_wrong_table_constraint_sql(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    _replace_semantic_table_sql(
        database,
        table_name="chains",
        old="status       TEXT NOT NULL CHECK",
        new="status       TEXT CHECK",
    )
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_rejects_missing_canonical_index(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX claimable_jobs")
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_rejects_missing_canonical_foreign_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    _replace_semantic_table_sql(
        database,
        table_name="jobs",
        old="TEXT NOT NULL REFERENCES chains(chain_id)",
        new="TEXT NOT NULL",
    )
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_rejects_missing_canonical_trigger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER immutable_job_contract")
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_rejects_extra_view(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE VIEW plausible_semantic_jobs AS SELECT job_id, state FROM jobs")
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_rejects_wrong_schema_meta(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE semantic_state_meta SET schema_version=999")
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


@pytest.mark.parametrize(
    ("pragma_name", "foreign_value"),
    (
        ("application_id", 123456),
        ("user_version", 987),
    ),
)
def test_snapshot_reader_rejects_foreign_sqlite_schema_identity(
    tmp_path: Path,
    pragma_name: str,
    foreign_value: int,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA {pragma_name}={foreign_value}")
    database.chmod(0o600)

    _assert_snapshot_unavailable(database)


def test_canonical_snapshot_schema_digest_is_deterministic_and_epoch_bound() -> None:
    first = semantic_state_module._canonical_semantic_snapshot_schema_digest(_EPOCH)
    second = semantic_state_module._canonical_semantic_snapshot_schema_digest(_EPOCH)
    other_epoch = semantic_state_module._canonical_semantic_snapshot_schema_digest(
        f"{_EPOCH}-other"
    )

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64
    assert other_epoch != first


@pytest.mark.parametrize(
    "hard_limit",
    (
        semantic_state_module.resource.RLIM_INFINITY,
        semantic_state_module._SNAPSHOT_CHILD_MAX_OUTPUT_BYTES // 2,
    ),
    ids=("unlimited-parent-hard-limit", "finite-parent-hard-limit"),
)
def test_snapshot_reader_uses_trusted_prlimit_argv_without_a_child_python_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hard_limit: int,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    monkeypatch.setattr(
        semantic_state_module.resource,
        "getrlimit",
        lambda _resource: (0, hard_limit),
    )
    real_popen = semantic_state_module.subprocess.Popen
    popen_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def recording_popen(
        argv: tuple[str, ...],
        *args: object,
        **kwargs: object,
    ):
        popen_calls.append((argv, kwargs))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(semantic_state_module.subprocess, "Popen", recording_popen)

    snapshot = SemanticStateSnapshotReader(
        database,
        epoch=_EPOCH,
    ).terminal_reconciliation_snapshot()

    requested_limit = semantic_state_module._SNAPSHOT_CHILD_MAX_OUTPUT_BYTES + 1
    effective_limit = (
        requested_limit
        if hard_limit == semantic_state_module.resource.RLIM_INFINITY
        else min(requested_limit, hard_limit)
    )
    assert snapshot.total_jobs == 0
    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv[:4] == (
        "/usr/bin/prlimit",
        f"--fsize={effective_limit}:{effective_limit}",
        "--",
        "/usr/bin/bwrap",
    )
    assert "preexec_fn" not in kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["shell"] is False


def test_bounded_snapshot_child_keeps_leader_identity_until_group_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    process_id = 424_242

    class ExitObservedProcess:
        pid = process_id

        def poll(self) -> int:
            raise AssertionError("poll must not reap the leader before process-group cleanup")

        def wait(self) -> int:
            events.append("wait")
            return 7

    def observe_without_reaping(
        id_type: int,
        identity: int,
        options: int,
    ) -> SimpleNamespace:
        assert (id_type, identity) == (os.P_PID, process_id)
        assert options == os.WEXITED | os.WNOHANG | os.WNOWAIT
        events.append("waitid-wnowait")
        return SimpleNamespace(si_pid=process_id)

    def kill_process_group(identity: int, requested_signal: int) -> None:
        assert (identity, requested_signal) == (process_id, signal.SIGKILL)
        events.append("killpg")

    monkeypatch.setattr(
        semantic_state_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: ExitObservedProcess(),
    )
    monkeypatch.setattr(semantic_state_module.os, "waitid", observe_without_reaping)
    monkeypatch.setattr(semantic_state_module.os, "killpg", kill_process_group)

    completed = semantic_state_module._run_bounded_snapshot_child(
        ("ignored",),
        env={"PATH": "/usr/bin:/bin"},
        timeout=1,
        max_output_bytes=64,
    )

    assert completed.returncode == 7
    assert events == ["waitid-wnowait", "killpg", "wait"]


@pytest.mark.parametrize("failure_errno", (errno.EPERM, errno.EIO))
def test_bounded_snapshot_child_fails_closed_after_group_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_errno: int,
) -> None:
    events: list[str] = []
    process_id = 424_243

    class ExitObservedProcess:
        pid = process_id

        def wait(self) -> int:
            events.append("wait")
            return 0

    def observe_without_reaping(
        _id_type: int,
        _identity: int,
        _options: int,
    ) -> SimpleNamespace:
        events.append("waitid-wnowait")
        return SimpleNamespace(si_pid=process_id)

    def deny_group_cleanup(_identity: int, _requested_signal: int) -> None:
        events.append("killpg-denied")
        raise OSError(failure_errno, "/private/semantic-snapshot/cleanup-denied")

    monkeypatch.setattr(
        semantic_state_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: ExitObservedProcess(),
    )
    monkeypatch.setattr(semantic_state_module.os, "waitid", observe_without_reaping)
    monkeypatch.setattr(semantic_state_module.os, "killpg", deny_group_cleanup)

    with pytest.raises(SemanticStateError) as error:
        semantic_state_module._run_bounded_snapshot_child(
            ("ignored",),
            env={"PATH": "/usr/bin:/bin"},
            timeout=1,
            max_output_bytes=64,
        )

    assert error.value.code == "semantic_state_unavailable"
    assert str(error.value) == "semantic_state_unavailable"
    assert "/private/semantic-snapshot" not in str(error.value)
    assert events == ["waitid-wnowait", "killpg-denied", "wait"]


def test_bounded_snapshot_child_never_signals_a_released_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ReapedElsewhereProcess:
        pid = 424_244

        def wait(self) -> int:
            events.append("wait")
            return 0

    def report_released_identity(
        _id_type: int,
        _identity: int,
        _options: int,
    ) -> None:
        events.append("waitid-identity-lost")
        raise ChildProcessError("leader identity was already released")

    def reject_stale_signal(_identity: int, _requested_signal: int) -> None:
        events.append("killpg-stale")
        raise AssertionError("must not signal a released PID/PGID identity")

    monkeypatch.setattr(
        semantic_state_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: ReapedElsewhereProcess(),
    )
    monkeypatch.setattr(semantic_state_module.os, "waitid", report_released_identity)
    monkeypatch.setattr(semantic_state_module.os, "killpg", reject_stale_signal)

    with pytest.raises(SemanticStateError) as error:
        semantic_state_module._run_bounded_snapshot_child(
            ("ignored",),
            env={"PATH": "/usr/bin:/bin"},
            timeout=1,
            max_output_bytes=64,
        )

    assert error.value.code == "semantic_state_unavailable"
    assert events == ["waitid-identity-lost", "wait"]


@pytest.mark.parametrize(
    ("outcome", "output_descriptor"),
    (
        ("timeout", 1),
        ("stdout_limit", 1),
        ("stderr_limit", 2),
        ("success", 1),
        ("invalid_utf8", 1),
    ),
)
def test_bounded_snapshot_child_cleans_grandchildren_and_fds_on_every_outcome(
    tmp_path: Path,
    outcome: str,
    output_descriptor: int,
) -> None:
    marker = f"fin-semantic-snapshot-post-exit-{os.getpid()}-{tmp_path.name}-{outcome}"
    max_output_bytes = 256
    if outcome == "timeout":
        output_source = "time.sleep(30)"
    elif outcome.endswith("_limit"):
        output_source = f"os.write({output_descriptor}, b'x' * {max_output_bytes + 1})"
    elif outcome == "success":
        output_source = "os.write(1, b'bounded\\n')"
    else:
        assert outcome == "invalid_utf8"
        output_source = "os.write(1, b'\\xff')"
    source = "\n".join(
        (
            "import os",
            "import subprocess",
            "import sys",
            "import time",
            "subprocess.Popen(",
            f"    (sys.executable, '-I', '-B', '-c', 'import time; time.sleep(30)', {marker!r}),",
            ")",
            output_source,
        )
    )

    def marked_processes() -> tuple[int, ...]:
        matches: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker.encode() in command:
                matches.append(int(entry.name))
        return tuple(matches)

    before_fds = len(os.listdir("/proc/self/fd"))
    try:
        if outcome == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                semantic_state_module._run_bounded_snapshot_child(
                    (sys.executable, "-I", "-B", "-c", source),
                    env={"PATH": "/usr/bin:/bin"},
                    timeout=0.2,
                    max_output_bytes=max_output_bytes,
                )
        elif outcome.endswith("_limit"):
            with pytest.raises(SemanticStateError):
                semantic_state_module._run_bounded_snapshot_child(
                    (sys.executable, "-I", "-B", "-c", source),
                    env={"PATH": "/usr/bin:/bin"},
                    timeout=1,
                    max_output_bytes=max_output_bytes,
                )
        else:
            completed = semantic_state_module._run_bounded_snapshot_child(
                (sys.executable, "-I", "-B", "-c", source),
                env={"PATH": "/usr/bin:/bin"},
                timeout=1,
                max_output_bytes=max_output_bytes,
            )
            if outcome == "success":
                assert completed.stdout == b"bounded\n"
            else:
                with pytest.raises(UnicodeDecodeError):
                    completed.stdout.decode("utf-8")
    finally:
        for process_id in marked_processes():
            with suppress(ProcessLookupError):
                os.kill(process_id, signal.SIGKILL)

    deadline = time.monotonic() + 2
    while marked_processes() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marked_processes() == ()
    assert len(os.listdir("/proc/self/fd")) <= before_fds


@pytest.mark.parametrize(
    "launcher_attribute",
    ("_SNAPSHOT_LIMIT_LAUNCHER", "_SNAPSHOT_SANDBOX"),
)
def test_snapshot_reader_rejects_untrusted_launchers_without_disclosing_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launcher_attribute: str,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    untrusted_launcher = tmp_path / "private-untrusted-launcher"
    untrusted_launcher.write_bytes(b"not a trusted executable")
    untrusted_launcher.chmod(0o755)
    monkeypatch.setattr(
        semantic_state_module,
        launcher_attribute,
        untrusted_launcher,
    )

    with pytest.raises(SemanticStateError) as error:
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()

    assert error.value.code == "semantic_state_sandbox_unsafe"
    assert str(error.value) == "semantic_state_sandbox_unsafe"
    assert str(untrusted_launcher) not in str(error.value)
    assert error.value.__cause__ is None


def test_snapshot_reader_sanitizes_trusted_launcher_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("/private/runtime/prlimit: raw spawn failure")

    monkeypatch.setattr(semantic_state_module.subprocess, "Popen", fail_spawn)

    _assert_snapshot_unavailable(database)


@pytest.mark.parametrize(
    ("outcome", "file_descriptor"),
    (
        ("success", None),
        ("timeout", None),
        ("output_limit", 1),
        ("output_limit", 2),
    ),
    ids=("success", "timeout", "stdout-limit", "stderr-limit"),
)
def test_snapshot_reader_remains_bounded_with_an_active_background_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    file_descriptor: int | None,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    if outcome == "success":
        source = (
            "import os\n"
            "os.write(1, "
            'b\'{"schema_version":"fin.semantic-terminal-reconciliation/v2",'
            '"status":"ok","total_jobs":0,"active_jobs":0,"expired_jobs":0,'
            '"terminal_jobs":0,'
            '"uncoordinated_terminal_jobs":0}\')\n'
        )
        timeout = 0.5
    elif outcome == "timeout":
        source = "import time\ntime.sleep(30)\n"
        timeout = 0.15
    else:
        assert file_descriptor is not None
        output_bytes = semantic_state_module._SNAPSHOT_CHILD_MAX_OUTPUT_BYTES + 1
        source = "\n".join(
            (
                "import os",
                "import time",
                f"os.write({file_descriptor}, "
                f"b'/private/semantic-state.sqlite3: raw child failure' + b'x' * {output_bytes})",
                "time.sleep(30)",
            )
        )
        timeout = 0.15
    _install_snapshot_child(tmp_path, monkeypatch, source)
    monkeypatch.setattr(
        semantic_state_module,
        "_SNAPSHOT_CHILD_TIMEOUT_SECONDS",
        timeout,
    )
    background_ready = Event()
    release_background = Event()

    def hold_background_thread() -> None:
        background_ready.set()
        assert release_background.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as executor:
        background = executor.submit(hold_background_thread)
        assert background_ready.wait(timeout=1)
        before_fds = len(semantic_state_module.os.listdir("/proc/self/fd"))
        try:
            for _ in range(3):
                started = time.monotonic()
                if outcome == "success":
                    snapshot = SemanticStateSnapshotReader(
                        database,
                        epoch=_EPOCH,
                    ).terminal_reconciliation_snapshot()
                    assert (
                        snapshot.total_jobs,
                        snapshot.active_jobs,
                        snapshot.terminal_jobs,
                        snapshot.uncoordinated_terminal_jobs,
                        snapshot.reconciled_now,
                    ) == (0, 0, 0, 0, 0)
                else:
                    _assert_snapshot_unavailable(database)
                assert time.monotonic() - started < 2
        finally:
            release_background.set()
        assert len(semantic_state_module.os.listdir("/proc/self/fd")) <= before_fds
        background.result(timeout=1)


def test_snapshot_reader_reaps_the_launcher_and_isolated_grandchild_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    marker = (
        "fin-semantic-snapshot-grandchild-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()
    )
    _install_snapshot_child(
        tmp_path,
        monkeypatch,
        "\n".join(
            (
                "import subprocess",
                "import sys",
                "import time",
                "subprocess.Popen(",
                "    (sys.executable, '-I', '-B', '-c', "
                "'import time; time.sleep(30)', "
                f"{marker!r}),",
                "    start_new_session=True,",
                ")",
                "time.sleep(30)",
            )
        ),
    )
    monkeypatch.setattr(
        semantic_state_module,
        "_SNAPSHOT_CHILD_TIMEOUT_SECONDS",
        0.4,
    )
    real_popen = semantic_state_module.subprocess.Popen
    launcher_pids: list[int] = []

    def recording_popen(
        argv: tuple[str, ...],
        *args: object,
        **kwargs: object,
    ):
        process = real_popen(argv, *args, **kwargs)
        launcher_pids.append(process.pid)
        return process

    monkeypatch.setattr(semantic_state_module.subprocess, "Popen", recording_popen)
    background_ready = Event()
    release_background = Event()

    def marked_processes() -> tuple[int, ...]:
        matches: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker.encode() in command:
                matches.append(int(entry.name))
        return tuple(matches)

    grandchild_seen = Event()

    def observe_background_processes() -> None:
        background_ready.set()
        while not release_background.wait(timeout=0.005):
            if marked_processes():
                grandchild_seen.set()

    assert marked_processes() == ()
    with ThreadPoolExecutor(max_workers=1) as executor:
        background = executor.submit(observe_background_processes)
        assert background_ready.wait(timeout=1)
        before_fds = len(semantic_state_module.os.listdir("/proc/self/fd"))
        try:
            _assert_snapshot_unavailable(database)
        finally:
            release_background.set()
        background.result(timeout=1)
        assert len(semantic_state_module.os.listdir("/proc/self/fd")) <= before_fds

    assert grandchild_seen.is_set()
    deadline = time.monotonic() + 2
    while (
        any(Path(f"/proc/{pid}").exists() for pid in launcher_pids) or marked_processes()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(launcher_pids) == 1
    assert not Path(f"/proc/{launcher_pids[0]}").exists()
    assert marked_processes() == ()


@pytest.mark.parametrize("file_descriptor", [1, 2])
def test_snapshot_reader_stops_child_at_output_limit_without_buffering_unboundedly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_descriptor: int,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    output_bytes = semantic_state_module._SNAPSHOT_CHILD_MAX_OUTPUT_BYTES + 1
    _install_snapshot_child(
        tmp_path,
        monkeypatch,
        "\n".join(
            (
                "import os",
                "import time",
                f"os.write({file_descriptor}, b'x' * {output_bytes})",
                "time.sleep(30)",
            )
        ),
    )

    started = time.monotonic()
    _assert_snapshot_unavailable(database)
    elapsed = time.monotonic() - started

    assert elapsed < 5


def test_snapshot_reader_sanitizes_invalid_child_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    _install_snapshot_child(
        tmp_path,
        monkeypatch,
        "import os\nos.write(1, b'\\xff')\n",
    )

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_sanitizes_nonzero_child_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    _install_snapshot_child(
        tmp_path,
        monkeypatch,
        "\n".join(
            (
                "import os",
                "os.write(2, b'/private/state.sqlite3: raw sqlite failure')",
                "raise SystemExit(7)",
            )
        ),
    )

    _assert_snapshot_unavailable(database)


def test_snapshot_reader_kills_timed_out_child_and_sanitizes_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    _install_snapshot_child(
        tmp_path,
        monkeypatch,
        "import time\ntime.sleep(30)\n",
    )
    monkeypatch.setattr(
        semantic_state_module,
        "_SNAPSHOT_CHILD_TIMEOUT_SECONDS",
        0.1,
    )

    started = time.monotonic()
    _assert_snapshot_unavailable(database)
    elapsed = time.monotonic() - started

    assert elapsed < 2


def test_sandboxed_snapshot_reader_does_not_mutate_live_wal_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    database.chmod(0o600)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE jobs SET updated_at=updated_at")
        writer.commit()
        sidecars = (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        )
        assert all(path.is_file() for path in sidecars)

        def identities() -> dict[str, tuple[int, int, int, str]]:
            return {
                path.name: (
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.stat().st_ctime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in sidecars
            }

        before = identities()
        snapshot = SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
            clock=lambda: _NOW + 1,
        ).terminal_reconciliation_snapshot()
        after = identities()
    finally:
        writer.close()

    assert snapshot.total_jobs == 1
    assert snapshot.active_jobs == 1
    assert before == after


def test_snapshot_reader_returns_committed_view_from_hot_rollback_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    database.chmod(0o600)
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()

    crash_writer = "\n".join(
        (
            "import os",
            "import sqlite3",
            "import sys",
            "connection = sqlite3.connect(sys.argv[1])",
            "connection.execute('PRAGMA journal_mode=DELETE')",
            "connection.execute('PRAGMA synchronous=FULL')",
            "connection.execute('PRAGMA cache_size=1')",
            "connection.execute('BEGIN IMMEDIATE')",
            "connection.execute(\"UPDATE semantic_state_meta SET epoch='foreign'\")",
            "connection.execute(",
            '    "INSERT INTO chains("',
            '    "chain_id, principal_id, status, created_at, updated_at"',
            "    \") VALUES ('crash-chain', ?, 'active', 1, 1)\",",
            "    ('x' * 2000,),",
            ")",
            "os._exit(0)",
        )
    )
    subprocess.run(
        (sys.executable, "-I", "-B", "-c", crash_writer, str(database)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=10,
    )
    journal = Path(f"{database}-journal")
    assert journal.is_file()
    assert journal.stat().st_size > 0
    database.chmod(0o600)
    journal.chmod(0o600)
    real_popen = semantic_state_module.subprocess.Popen
    popen_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def recording_popen(
        argv: tuple[str, ...],
        *args: object,
        **kwargs: object,
    ):
        popen_calls.append((argv, kwargs))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(semantic_state_module.subprocess, "Popen", recording_popen)

    def identities() -> dict[str, tuple[int, int, int, str]]:
        return {
            path.name: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in (database, journal)
        }

    before = identities()
    snapshot = SemanticStateSnapshotReader(
        database,
        epoch=_EPOCH,
        clock=lambda: _NOW + 1,
    ).terminal_reconciliation_snapshot()
    after = identities()

    assert snapshot.total_jobs == 1
    assert snapshot.active_jobs == 1
    assert snapshot.terminal_jobs == 0
    assert snapshot.uncoordinated_terminal_jobs == 0
    assert before == after
    assert len(popen_calls) == 2
    materializer_argv, materializer_kwargs = popen_calls[0]
    assert materializer_argv[0] == "/usr/bin/prlimit"
    assert any(argument.startswith("--fsize=") for argument in materializer_argv)
    assert any(argument.startswith("--as=") for argument in materializer_argv)
    assert any(argument.startswith("--nofile=") for argument in materializer_argv)
    assert "--materialize-rollback-destination" in materializer_argv
    assert "--bind" in materializer_argv
    assert materializer_kwargs["stdout"] == subprocess.DEVNULL
    assert materializer_kwargs["stderr"] == subprocess.DEVNULL
    assert materializer_kwargs["start_new_session"] is True
    assert materializer_kwargs["shell"] is False
    observer_argv, observer_kwargs = popen_calls[1]
    requested_limit = semantic_state_module._SNAPSHOT_CHILD_MAX_OUTPUT_BYTES + 1
    _soft_limit, hard_limit = semantic_state_module.resource.getrlimit(
        semantic_state_module.resource.RLIMIT_FSIZE
    )
    effective_limit = (
        requested_limit
        if hard_limit == semantic_state_module.resource.RLIM_INFINITY
        else min(requested_limit, hard_limit)
    )
    assert observer_argv[:4] == (
        "/usr/bin/prlimit",
        f"--fsize={effective_limit}:{effective_limit}",
        "--",
        "/usr/bin/bwrap",
    )
    assert "--bind" not in observer_argv
    assert observer_kwargs["start_new_session"] is True
    assert observer_kwargs["shell"] is False


def test_snapshot_reader_returns_committed_view_during_active_rollback_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    database.chmod(0o600)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("PRAGMA synchronous=FULL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE semantic_state_meta SET epoch='foreign'")
        journal = Path(f"{database}-journal")
        assert journal.is_file()
        assert journal.read_bytes()[:8] == b"\0" * 8
        database.chmod(0o600)
        journal.chmod(0o600)
        before = {
            path.name: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in (database, journal)
        }

        snapshot = SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
            clock=lambda: _NOW + 1,
        ).terminal_reconciliation_snapshot()

        after = {
            path.name: (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in (database, journal)
        }
    finally:
        writer.rollback()
        writer.close()

    assert snapshot.total_jobs == 1
    assert snapshot.active_jobs == 1
    assert snapshot.terminal_jobs == 0
    assert snapshot.uncoordinated_terminal_jobs == 0
    assert before == after


@pytest.mark.parametrize("failure_point", ("creation", "root-lstat"))
def test_snapshot_materializer_sanitizes_private_temporary_directory_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    journal = Path(f"{database}-journal")
    journal.write_bytes(b"rollback-owner-bytes")
    journal.chmod(0o600)
    expected_generation = semantic_state_module._semantic_snapshot_generation(database)
    before = _snapshot_store_bytes(database, journal)
    private_root = tmp_path / "private-materializer-root"
    failure = OSError(errno.EACCES, f"private temporary failure: {private_root}")

    if failure_point == "creation":

        def fail_temporary_directory(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(
            semantic_state_module.tempfile,
            "TemporaryDirectory",
            fail_temporary_directory,
        )
    else:
        temporary = SimpleNamespace(name=str(private_root), cleanup=lambda: None)
        monkeypatch.setattr(
            semantic_state_module.tempfile,
            "TemporaryDirectory",
            lambda **_kwargs: temporary,
        )
        real_lstat = Path.lstat

        def fail_private_root_lstat(path: Path) -> os.stat_result:
            if path == private_root:
                raise failure
            return real_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_private_root_lstat)

    with (
        pytest.raises(BaseException) as captured,
        semantic_state_module._semantic_snapshot_observation_database(
            database,
            expected_generation=expected_generation,
            child=Path("/private/semantic-snapshot-child.py"),
            interpreter=Path("/private/python"),
        ),
    ):
        pytest.fail("temporary-directory failure must fail closed")

    _assert_path_free_snapshot_failure(
        captured.value,
        forbidden_paths=(str(database), str(private_root)),
    )
    assert _snapshot_store_bytes(database, journal) == before


def test_snapshot_reader_rejects_foreign_semantic_schema_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE foreign_owner(value TEXT NOT NULL)")
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_dangling_terminal_coordination_marker(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)
    repository.reconcile_terminal_jobs(now=_NOW + 3)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE jobs SET coordinated_version_no=99 WHERE job_id=?",
            (lease.job_id,),
        )
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_terminal_product_version_mismatch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)
    repository.reconcile_terminal_jobs(now=_NOW + 3)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE chain_versions
            SET payload_json=?
            WHERE job_id=? AND kind='research_terminal'
            """,
            ('{"product_version":999,"status":"completed"}', lease.job_id),
        )
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_unbound_pending_terminal_job(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE continuations SET active_job_id=NULL WHERE chain_id=?",
            (lease.chain_id,),
        )
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_closed_chain_with_active_job(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    admission = _admit(repository)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE continuations SET active_job_id=NULL WHERE chain_id=?",
            (admission.chain_id,),
        )
        connection.execute(
            "UPDATE chains SET status='closed' WHERE chain_id=?",
            (admission.chain_id,),
        )
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_orphan_terminal_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    admission = _admit(repository)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO chain_versions(
                chain_id, version_no, kind, payload_json, created_at
            ) VALUES (?, 2, 'research_terminal', '{}', ?)
            """,
            (admission.chain_id, _NOW + 1),
        )
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


@pytest.mark.parametrize(
    ("product_json", "artifact_hash"),
    (
        (
            '{\n  "confidence": "medium",\n  "summary": "thesis remains intact"\n}',
            _artifact_hash({"summary": "thesis remains intact", "confidence": "medium"}),
        ),
        ('{"action":"BUY"}', _artifact_hash({"action": "BUY"})),
    ),
)
def test_snapshot_reader_rejects_noncanonical_or_forbidden_terminal_product(
    tmp_path: Path,
    product_json: str,
    artifact_hash: str,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE jobs SET product_json=?, artifact_hash=? WHERE job_id=?",
            (product_json, artifact_hash, lease.job_id),
        )
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_cross_principal_idempotency_binding(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(
        repository,
        principal_id="principal-a",
        idempotency_key="request-a",
    )
    second = _admit(
        repository,
        principal_id="principal-b",
        idempotency_key="request-b",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE idempotency
            SET chain_id=?, job_id=?
            WHERE principal_id='principal-a'
            """,
            (second.chain_id, second.job_id),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_feedback_without_matching_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    admitted = _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)
    product = repository.read(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        now=_NOW + 3,
    )
    assert product.product_version == 1
    repository.append_feedback(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        product_version=1,
        feedback_key="feedback-1",
        disposition="useful",
        note="clear boundary",
        now=_NOW + 4,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM chain_versions WHERE kind='feedback'")
    database.chmod(0o600)

    with pytest.raises(SemanticStateError, match="semantic_state_unavailable"):
        SemanticStateSnapshotReader(
            database,
            epoch=_EPOCH,
        ).terminal_reconciliation_snapshot()


def test_snapshot_reader_rejects_sidecar_generation_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fin_analyse.guo_teacher_research.semantic_state as semantic_state_module

    database = tmp_path / "semantic-state.sqlite3"
    repository = _repo(database)
    _admit(repository)
    database.chmod(0o600)
    writer = sqlite3.connect(database)
    real_run = semantic_state_module._run_bounded_snapshot_child
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE jobs SET updated_at=updated_at")
        writer.commit()
        wal = Path(f"{database}-wal")
        assert wal.is_file()

        def run_then_drift(*args, **kwargs):
            completed = real_run(*args, **kwargs)
            with wal.open("ab") as stream:
                stream.write(b"\0")
            return completed

        monkeypatch.setattr(
            semantic_state_module,
            "_run_bounded_snapshot_child",
            run_then_drift,
        )

        with pytest.raises(
            SemanticStateError,
            match="semantic_state_identity_changed",
        ):
            SemanticStateSnapshotReader(
                database,
                epoch=_EPOCH,
            ).terminal_reconciliation_snapshot()
    finally:
        writer.close()


def test_snapshot_database_generation_binds_full_content_on_the_same_inode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    original = database.read_bytes()
    before = semantic_state_module._semantic_snapshot_generation(database)
    before_stat = semantic_state_module._snapshot_stat_identity(database.stat())

    changed = bytearray(original)
    changed[-1] ^= 0x01
    database.write_bytes(changed)
    database.chmod(0o600)
    after = semantic_state_module._semantic_snapshot_generation(database)
    after_stat = semantic_state_module._snapshot_stat_identity(database.stat())

    assert before[0][:-1] == before_stat
    assert after[0][:-1] == after_stat
    assert len(str(before[0][-1])) == 64
    assert before[0][-1] != after[0][-1]
    assert before[0][0:2] == after[0][0:2]
    assert before[0][6] == after[0][6] == len(original)


@pytest.mark.parametrize("failure_point", ("lstat", "open", "read"))
def test_snapshot_generation_sanitizes_private_sidecar_io_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    sidecar = Path(f"{database}-wal")
    sidecar.write_bytes(b"private-sidecar-bytes")
    sidecar.chmod(0o600)
    before = _snapshot_store_bytes(database, sidecar)
    failure = OSError(errno.EIO, f"private sidecar failure: {sidecar}")

    if failure_point == "lstat":
        real_lstat = Path.lstat

        def fail_sidecar_lstat(path: Path) -> os.stat_result:
            if path == sidecar:
                raise failure
            return real_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_sidecar_lstat)
    else:
        real_open = semantic_state_module.os.open
        sidecar_descriptors: set[int] = set()

        def open_with_sidecar_failure(
            path: os.PathLike[str] | str,
            *args: object,
            **kwargs: object,
        ) -> int:
            if Path(path) == sidecar and failure_point == "open":
                raise failure
            descriptor = real_open(path, *args, **kwargs)
            if Path(path) == sidecar:
                sidecar_descriptors.add(descriptor)
            return descriptor

        monkeypatch.setattr(semantic_state_module.os, "open", open_with_sidecar_failure)
        if failure_point == "read":
            real_read = semantic_state_module.os.read

            def fail_sidecar_read(descriptor: int, size: int) -> bytes:
                if descriptor in sidecar_descriptors:
                    raise failure
                return real_read(descriptor, size)

            monkeypatch.setattr(semantic_state_module.os, "read", fail_sidecar_read)

    with pytest.raises(BaseException) as captured:
        semantic_state_module._semantic_snapshot_generation(database)

    _assert_path_free_snapshot_failure(
        captured.value,
        forbidden_paths=(str(database), str(sidecar)),
    )
    assert _snapshot_store_bytes(database, sidecar) == before


@pytest.mark.parametrize("sidecar_suffix", ("-wal", "-shm"))
def test_snapshot_reader_rejects_an_unpaired_sidecar(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    sidecar = Path(f"{database}{sidecar_suffix}")
    sidecar.write_bytes(b"orphaned-sqlite-sidecar")
    sidecar.chmod(0o600)

    _assert_snapshot_unavailable(database)


@pytest.mark.parametrize("oversized_part", ("database", "sidecar"))
def test_snapshot_generation_rejects_a_store_part_over_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_part: str,
) -> None:
    database = tmp_path / "semantic-state.sqlite3"
    _repo(database)
    database.chmod(0o600)
    database_size = database.stat().st_size
    if oversized_part == "database":
        limit = database_size - 1
    else:
        limit = database_size
        wal = Path(f"{database}-wal")
        wal.write_bytes(b"x" * (limit + 1))
        wal.chmod(0o600)
    monkeypatch.setattr(
        semantic_state_module,
        "_SNAPSHOT_STORE_PART_MAX_BYTES",
        limit,
    )

    with pytest.raises(SemanticStateError) as error:
        semantic_state_module._semantic_snapshot_generation(database)

    assert error.value.code == "semantic_state_insecure"


def test_terminal_reconciliation_coordinates_retained_jobs_exactly_once(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    admitted = _admit(repository)
    lease = repository.claim_next(
        worker_id="worker-a",
        now=_NOW + 1,
        lease_seconds=30,
    )
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 2)

    before = repository.terminal_reconciliation_snapshot()
    first = repository.reconcile_terminal_jobs(now=_NOW + 3)
    second = repository.reconcile_terminal_jobs(now=_NOW + 4)

    assert before.uncoordinated_terminal_jobs == 1
    assert first.reconciled_now == 1
    assert first.uncoordinated_terminal_jobs == 0
    assert second.reconciled_now == 0
    assert second.uncoordinated_terminal_jobs == 0
    assert (
        repository.read(
            principal_id="principal-a",
            continuation_token=admitted.continuation_token,
            now=_NOW + 5,
        ).status
        == "completed"
    )


def test_admission_is_atomic_immutable_idempotent_and_token_is_only_hashed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)

    admitted = _admit(repository)
    repeated = _admit(repository)

    assert repeated == admitted
    token_bytes = base64.urlsafe_b64decode(admitted.continuation_token + "==")
    assert len(token_bytes) >= 32
    assert repository.counts().as_tuple() == (1, 1, 1, 1, 0, 0)

    job = repository.get_job(admitted.job_id)
    assert json.loads(job.contract_json) == _CONTRACT
    assert json.loads(job.input_json) == _INPUT
    assert len(job.contract_hash) == 64
    assert len(job.input_hash) == 64
    assert admitted.continuation_token.encode() not in db_path.read_bytes()

    before = repository.counts()
    with pytest.raises(SemanticStateError, match="idempotency_conflict") as error:
        _admit(repository, input_snapshot={**_INPUT, "question": "different"})
    assert error.value.code == "idempotency_conflict"
    assert repository.counts() == before


def test_answer_create_and_continue_append_products_without_jobs_and_replay_exactly(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    first_product = {"summary": "fresh answer", "confidence": "medium"}

    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=first_product,
        artifact_hash=_artifact_hash(first_product),
        now=_NOW,
    )
    repeated = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "must not replace the first product"},
        artifact_hash=_artifact_hash({"summary": "must not replace the first product"}),
        now=_NOW + 1,
    )

    assert created.replayed is False
    assert created.continuity_degraded is False  # 首问无降级事实
    assert repeated.replayed is True
    assert repeated.chain_id == created.chain_id
    assert repeated.product_id == created.product_id
    assert repeated.product_version == 1
    assert repeated.product == first_product
    assert repository.counts().as_tuple() == (1, 1, 0, 1, 1, 0)

    follow_up_product = {"summary": "follow-up answer"}
    continued = repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract={**_CONTRACT, "policy_version": "m4-test-2"},
        input_snapshot={"question": "follow up", "context": {}},
        expected_parent_product_version=1,
        status="partial",
        product=follow_up_product,
        artifact_hash=_artifact_hash(follow_up_product),
        now=_NOW + 2,
        continuity_degraded=True,
    )

    assert continued.chain_id == created.chain_id
    assert continued.product_version == 2
    assert continued.status == "partial"
    assert continued.continuity_degraded is True
    assert repository.counts().as_tuple() == (1, 1, 0, 2, 2, 0)
    read = repository.read(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        now=_NOW + 3,
    )
    assert read.status == "partial"
    assert read.product_version == 2
    assert read.product == {"summary": "follow-up answer"}

    before_stale_parent = repository.counts()
    stale_product = {"summary": "stale-parent answer"}
    with pytest.raises(SemanticStateError, match="continuation_conflict"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-from-stale-parent",
            contract=_CONTRACT,
            input_snapshot={"question": "stale follow up", "context": {}},
            expected_parent_product_version=1,
            status="completed",
            product=stale_product,
            artifact_hash=_artifact_hash(stale_product),
            now=_NOW + 4,
        )
    assert repository.counts() == before_stale_parent

    # A2: degraded 事实随 product 版本原子持久化，repository 重启后
    # exact replay 仍返回 True；不增加表/列或 runtime handle 字段。
    reopened = _repo(tmp_path / "semantic-state.sqlite3")
    replay = reopened.find_answer_replay(
        principal_id="principal-a",
        idempotency_key="answer-2",
        contract={**_CONTRACT, "policy_version": "m4-test-2"},
        input_snapshot={"question": "follow up", "context": {}},
        continuation_token=created.continuation_token,
    )
    assert replay is not None
    assert replay.replayed is True
    assert replay.continuity_degraded is True


def test_answer_replay_rejects_missing_or_invalid_continuity_degraded(
    tmp_path: Path,
) -> None:
    """旧/损坏 projection 缺 continuity_degraded 或非 boolean → fail closed。"""
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    product = {"summary": "degraded answer"}
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="corrupt-degraded",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW,
        continuity_degraded=True,
    )
    assert created.continuity_degraded is True

    # 分别破坏持久化 projection：缺字段、字符串、整数 → replay 全部 fail closed。
    for label, mutate in (
        (
            "missing",
            lambda projection: projection.pop("continuity_degraded"),
        ),
        (
            "string",
            lambda projection: projection.update(continuity_degraded="true"),
        ),
        (
            "integer",
            lambda projection: projection.update(continuity_degraded=1),
        ),
    ):
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT chain_id, version_no, payload_json"
                " FROM chain_versions WHERE kind='answer' ORDER BY version_no"
            ).fetchone()
            assert row is not None
            chain_id, version_no, raw_payload = row
            payload = json.loads(raw_payload)
            mutate(payload["response_projection"])
            connection.execute(
                "UPDATE chain_versions SET payload_json=? WHERE chain_id=? AND version_no=?",
                (json.dumps(payload), chain_id, version_no),
            )
        with pytest.raises(
            SemanticStateError, match="semantic_state_corrupt"
        ):
            reopened = _repo(db_path)
            reopened.find_answer_replay(
                principal_id="principal-a",
                idempotency_key="corrupt-degraded",
                contract=_CONTRACT,
                input_snapshot=_INPUT,
            )
            pytest.fail(f"invalid projection variant {label} must fail closed")
        # 恢复合法值供下一 variant 复用
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT chain_id, version_no, payload_json"
                " FROM chain_versions WHERE kind='answer' ORDER BY version_no"
            ).fetchone()
            chain_id, version_no, raw_payload = row
            payload = json.loads(raw_payload)
            payload["response_projection"]["continuity_degraded"] = True
            connection.execute(
                "UPDATE chain_versions SET payload_json=? WHERE chain_id=? AND version_no=?",
                (json.dumps(payload), chain_id, version_no),
            )


def test_unicode_answer_product_replays_and_reads_after_repository_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    product = {
        "summary": "组合风险仍需复核",
        "details": {"主线": "半导体材料"},
    }
    created = _repo(db_path).create_answer(
        principal_id="principal-a",
        idempotency_key="unicode-answer",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW,
    )

    reopened = _repo(db_path)
    replay = reopened.find_answer_replay(
        principal_id="principal-a",
        idempotency_key="unicode-answer",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
    )
    read = reopened.read(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        now=_NOW + 1,
    )

    assert replay is not None
    assert replay.replayed is True
    assert replay.product == product
    assert read.product == product


def test_answer_idempotency_conflict_and_forbidden_advice_are_zero_write(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    first_product = {"summary": "fresh answer"}
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=first_product,
        artifact_hash=_artifact_hash(first_product),
        now=_NOW,
    )
    before = repository.counts()

    with pytest.raises(SemanticStateError, match="idempotency_conflict"):
        repository.create_answer(
            principal_id="principal-a",
            idempotency_key="answer-1",
            contract=_CONTRACT,
            input_snapshot={**_INPUT, "question": "different"},
            status="completed",
            product={"summary": "different"},
            artifact_hash=_artifact_hash({"summary": "different"}),
            now=_NOW + 1,
        )
    with pytest.raises(SemanticStateError, match="forbidden_product_field"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "unsafe", "current_advice": {"action": "buy"}},
            artifact_hash="sha256:unsafe",
            now=_NOW + 2,
        )

    assert repository.counts() == before
    assert b"current_advice" not in (tmp_path / "semantic-state.sqlite3").read_bytes()


def test_repository_rejects_mismatched_product_hash_before_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    product = {"summary": "canonical product"}

    with pytest.raises(ValueError, match="canonical product"):
        repository.create_answer(
            principal_id="principal-a",
            idempotency_key="answer-1",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            status="completed",
            product=product,
            artifact_hash="sha256:" + "0" * 64,
            now=_NOW,
        )

    admitted = _admit(repository)
    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None
    with pytest.raises(ValueError, match="canonical product"):
        repository.finalize(
            lease=lease,
            status="succeeded",
            product=product,
            artifact_hash="sha256:" + "0" * 64,
            now=_NOW + 1,
        )
    with pytest.raises(ValueError, match="product identity"):
        repository.finalize(
            lease=lease,
            status="failed",
            product=None,
            artifact_hash="sha256:" + "0" * 64,
            now=_NOW + 1,
        )

    assert repository.counts().as_tuple() == (1, 1, 1, 1, 0, 0)
    assert repository.get_job(admitted.job_id).state == "running"


def test_repository_rejects_corrupt_stored_product_hash_on_read(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    product = {"summary": "fresh answer"}
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE products SET artifact_hash=? WHERE product_id=?",
            ("sha256:" + "0" * 64, created.product_id),
        )

    with pytest.raises(SemanticStateError, match="semantic_state_corrupt"):
        repository.read(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            now=_NOW + 1,
        )


def test_repository_rejects_noncanonical_stored_product_json(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    product = {"summary": "fresh answer", "details": {"verified": True}}
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE products SET product_json=? WHERE product_id=?",
            (json.dumps(product, indent=2, sort_keys=True), created.product_id),
        )

    with pytest.raises(SemanticStateError, match="semantic_state_corrupt"):
        repository.read(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            now=_NOW + 1,
        )


def test_repository_rejects_product_state_without_coordinated_product(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    admitted = _admit(repository)
    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 1)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET coordinated_version_no=99 WHERE job_id=?",
            (admitted.job_id,),
        )

    with pytest.raises(SemanticStateError, match="semantic_state_corrupt"):
        repository.read(
            principal_id="principal-a",
            continuation_token=admitted.continuation_token,
            now=_NOW + 2,
        )


def test_mid_admission_failure_rolls_back_chain_binding_job_and_version(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TRIGGER force_job_insert_failure
            BEFORE INSERT ON jobs
            BEGIN
                SELECT RAISE(ABORT, 'forced admission failure');
            END
            """)

    with pytest.raises(sqlite3.IntegrityError, match="forced admission failure"):
        _admit(repository)

    assert repository.counts().as_tuple() == (0, 0, 0, 0, 0, 0)


def test_foreign_and_unknown_continuations_are_indistinguishable_and_zero_write(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    admitted = _admit(repository)
    before = repository.counts()

    failures: list[SemanticStateError] = []
    for principal_id, token in (
        ("principal-b", admitted.continuation_token),
        ("principal-a", "A" * 43),
    ):
        with pytest.raises(SemanticStateError) as error:
            repository.read(
                principal_id=principal_id,
                continuation_token=token,
                now=_NOW,
            )
        failures.append(error.value)

    assert [error.code for error in failures] == [
        "continuation_not_accessible",
        "continuation_not_accessible",
    ]
    assert [str(error) for error in failures] == [
        "continuation_not_accessible",
        "continuation_not_accessible",
    ]
    assert repository.counts() == before


def test_read_is_runtime_free_and_continue_obeys_active_and_closed_truth_table(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    admitted = _admit(repository)
    before = repository.counts()

    queued = repository.read(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        now=_NOW,
    )
    assert queued.status == "queued"
    assert queued.allowed_actions == ("read", "close")
    assert repository.counts() == before

    with pytest.raises(SemanticStateError, match="research_in_progress"):
        repository.continue_research(
            principal_id="principal-a",
            continuation_token=admitted.continuation_token,
            idempotency_key="request-2",
            contract=_CONTRACT,
            input_snapshot={"question": "follow up", "context": {}},
            deadline_at=_NOW + 900,
            now=_NOW,
        )
    assert repository.counts() == before

    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 1)
    completed = repository.read(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        now=_NOW + 2,
    )
    assert completed.status == "completed"

    continued = repository.continue_research(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        idempotency_key="request-2",
        contract=_CONTRACT,
        input_snapshot={"question": "follow up", "context": {}},
        deadline_at=_NOW + 900,
        now=_NOW + 3,
    )
    assert continued.chain_id == admitted.chain_id
    assert continued.job_id != admitted.job_id
    assert continued.continuation_token == admitted.continuation_token

    closed = repository.close(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        now=_NOW + 4,
    )
    assert closed.status == "closed"
    with pytest.raises(SemanticStateError, match="chain_closed"):
        repository.continue_research(
            principal_id="principal-a",
            continuation_token=admitted.continuation_token,
            idempotency_key="request-3",
            contract=_CONTRACT,
            input_snapshot={"question": "too late", "context": {}},
            deadline_at=_NOW + 900,
            now=_NOW + 5,
        )


def test_lease_reclaim_fences_stale_worker_and_restart_never_reclaims_terminal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    admitted = _admit(repository)

    first = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=10)
    assert first is not None
    assert (first.attempt, first.fencing_token) == (1, 1)
    assert repository.claim_next(worker_id="worker-b", now=_NOW + 9, lease_seconds=10) is None

    restarted = _repo(db_path)
    second = restarted.claim_next(worker_id="worker-b", now=_NOW + 11, lease_seconds=10)
    assert second is not None
    assert second.job_id == first.job_id == admitted.job_id
    assert (second.attempt, second.fencing_token) == (2, 2)

    with pytest.raises(SemanticStateError, match="lease_lost"):
        repository.heartbeat(lease=first, now=_NOW + 12, lease_seconds=10)
    with pytest.raises(SemanticStateError, match="lease_lost"):
        _finish_success(repository, first, now=_NOW + 12)

    renewed = restarted.heartbeat(lease=second, now=_NOW + 12, lease_seconds=10)
    assert renewed.lease_expires_at == _NOW + 22
    _finish_success(restarted, renewed, now=_NOW + 13)

    after_second_restart = _repo(db_path)
    assert (
        after_second_restart.claim_next(worker_id="worker-c", now=_NOW + 100, lease_seconds=10)
        is None
    )


def test_unicode_worker_id_can_renew_and_finalize_its_lease(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    _admit(repository)

    lease = repository.claim_next(
        worker_id="研究工人🚀",
        now=_NOW,
        lease_seconds=10,
    )

    assert lease is not None
    renewed = repository.heartbeat(
        lease=lease,
        now=_NOW + 1,
        lease_seconds=10,
    )
    _finish_success(repository, renewed, now=_NOW + 2)


def test_queued_job_is_claimable_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    admitted = _admit(_repo(db_path))

    claimed = _repo(db_path).claim_next(worker_id="worker-a", now=_NOW, lease_seconds=10)

    assert claimed is not None
    assert claimed.job_id == admitted.job_id


def test_concurrent_terminal_reads_append_exactly_one_stable_product_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    admitted = _admit(repository)
    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 1)

    def _read() -> object:
        return _repo(db_path).read(
            principal_id="principal-a",
            continuation_token=admitted.continuation_token,
            now=_NOW + 2,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _unused: _read(), range(2)))

    assert results[0] == results[1]
    result = results[0]
    assert result.status == "completed"
    assert result.product_version == 1
    assert result.product == {"confidence": "medium", "summary": "thesis remains intact"}
    assert repository.counts().as_tuple() == (1, 1, 1, 2, 1, 0)


def test_close_cancels_pending_job_and_fences_late_result(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    admitted = _admit(repository)
    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None

    closed = repository.close(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        now=_NOW + 1,
    )

    assert closed.status == "closed"
    assert repository.get_job(admitted.job_id).state == "cancelled"
    assert repository.get_job(admitted.job_id).fencing_token == lease.fencing_token + 1
    before = repository.counts()
    with pytest.raises(SemanticStateError, match="lease_lost"):
        repository.heartbeat(lease=lease, now=_NOW + 2, lease_seconds=30)
    with pytest.raises(SemanticStateError, match="lease_lost"):
        _finish_success(repository, lease, now=_NOW + 2)
    assert (
        repository.read(
            principal_id="principal-a",
            continuation_token=admitted.continuation_token,
            now=_NOW + 3,
        ).status
        == "closed"
    )
    assert repository.counts() == before


def test_feedback_is_idempotent_product_metadata_and_never_cognition(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    admitted = _admit(repository)
    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None
    _finish_success(repository, lease, now=_NOW + 1)
    product = repository.read(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        now=_NOW + 2,
    )
    assert product.product_version == 1

    first = repository.append_feedback(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        product_version=1,
        feedback_key="feedback-1",
        disposition="useful",
        note="The evidence boundary was clear.",
        now=_NOW + 3,
    )
    repeated = repository.append_feedback(
        principal_id="principal-a",
        continuation_token=admitted.continuation_token,
        product_version=1,
        feedback_key="feedback-1",
        disposition="useful",
        note="The evidence boundary was clear.",
        now=_NOW + 4,
    )

    assert repeated == first
    assert repository.counts().as_tuple() == (1, 1, 1, 3, 1, 1)
    assert "cognition" not in repository.table_names()


def test_forbidden_current_advice_is_rejected_before_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    _admit(repository)
    lease = repository.claim_next(worker_id="worker-a", now=_NOW, lease_seconds=30)
    assert lease is not None

    with pytest.raises(SemanticStateError, match="forbidden_product_field"):
        repository.finalize(
            lease=lease,
            status="succeeded",
            product={"summary": "ok", "current_advice": {"action": "buy"}},
            artifact_hash="sha256:result-unsafe",
            now=_NOW + 1,
        )

    assert b"current_advice" not in db_path.read_bytes()


# ── Phase 3C：turn lease 与 runtime handle（schema v3）───────────────────────


def _runtime_handle(version: int = 1) -> dict[str, object]:
    return {
        "backend": "codex-cli",
        "session_id": "019fc2fe-7ea6-7e32-a20b-357f21429486",
        "identity_hash": "a" * 64,
        "product_version": version,
    }


def test_begin_answer_turn_acquires_lease_and_release_clears(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    fencing = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    assert fencing >= 1
    # 同链并发第二个 turn：lease 未到期 → turn_lease_held
    with pytest.raises(SemanticStateError, match="turn_lease_held"):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-2",
            lease_seconds=30.0,
            now=_NOW + 1,
        )
    repository.release_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        fencing_token=fencing,
        now=_NOW + 2,
    )
    # release 后新 turn 可获取，fencing token 单调递增
    fencing2 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-3",
        lease_seconds=30.0,
        now=_NOW + 3,
    )
    assert fencing2 > fencing


def test_turn_lease_expiry_allows_takeover_and_stale_release_is_noop(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    fencing1 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    # lease 到期（超时/崩溃残留）→ 新 turn 可接管
    fencing2 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-2",
        lease_seconds=30.0,
        now=_NOW + 31.0,
    )
    assert fencing2 > fencing1
    # 旧 turn 的 release 是 stale no-op：不释放新 holder 的 lease
    repository.release_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        fencing_token=fencing1,
        now=_NOW + 32.0,
    )
    with pytest.raises(SemanticStateError, match="turn_lease_held"):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-3",
            lease_seconds=30.0,
            now=_NOW + 33.0,
        )


def test_create_answer_persists_initial_runtime_handle(tmp_path: Path) -> None:
    # 回归：initial 的 handle 必须持久化——否则追问的 prior_read.runtime_handle
    # 为空 → 后台 agent 每次 fresh 新建会话，上下文无法延续。
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    handle = _runtime_handle(version=1)
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
        runtime_handle=handle,
    )
    assert created.product_version == 1
    with sqlite3.connect(tmp_path / "semantic-state.sqlite3") as connection:
        row = connection.execute(
            "SELECT runtime_backend, session_id, identity_hash, product_version "
            "FROM continuations"
        ).fetchone()
    assert row is not None
    assert str(row[0]) == "codex-cli"
    assert str(row[1]) == handle["session_id"]
    assert str(row[2]) == "a" * 64
    assert int(row[3]) == 1


def test_create_answer_rejects_invalid_initial_runtime_handle(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    with pytest.raises(SemanticStateError, match="runtime_handle_invalid"):
        repository.create_answer(
            principal_id="principal-a",
            idempotency_key="answer-1",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            status="completed",
            product={"summary": "first"},
            artifact_hash=_artifact_hash({"summary": "first"}),
            now=_NOW,
            runtime_handle=_runtime_handle(version=2),  # initial 必须指向版本 1
        )


def test_append_answer_with_runtime_handle_cas(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    handle = _runtime_handle(version=2)
    continued = repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract={**_CONTRACT, "policy_version": "m4-test-2"},
        input_snapshot={"question": "follow up", "context": {}},
        expected_parent_product_version=1,
        status="completed",
        product={"summary": "second"},
        artifact_hash=_artifact_hash({"summary": "second"}),
        now=_NOW + 2,
        runtime_handle=handle,
    )
    assert continued.product_version == 2
    # handle 持久化到 continuations
    with sqlite3.connect(tmp_path / "semantic-state.sqlite3") as connection:
        row = connection.execute(
            "SELECT runtime_backend, session_id, identity_hash, product_version "
            "FROM continuations"
        ).fetchone()
    assert row is not None
    assert str(row[0]) == "codex-cli"
    assert str(row[1]) == handle["session_id"]
    assert str(row[2]) == "a" * 64
    assert int(row[3]) == 2  # handle 必须指向本轮新 product version


def test_append_answer_rejects_invalid_runtime_handle(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    for bad_handle in (
        {"backend": "", "session_id": "s", "identity_hash": "a" * 64, "product_version": 2},
        {
            "backend": "codex-cli",
            "session_id": "s",
            "identity_hash": "not-hex",
            "product_version": 2,
        },
        {
            "backend": "codex-cli",
            "session_id": "s",
            "identity_hash": "a" * 64,
            "product_version": 0,
        },
        {"backend": "codex-cli", "session_id": "", "identity_hash": "a" * 64, "product_version": 2},
    ):
        with pytest.raises(SemanticStateError, match="runtime_handle_invalid"):
            repository.append_answer(
                principal_id="principal-a",
                continuation_token=created.continuation_token,
                idempotency_key=f"answer-bad-{bad_handle.get('session_id')}",
                contract=_CONTRACT,
                input_snapshot=_INPUT,
                expected_parent_product_version=1,
                status="completed",
                product={"summary": "second"},
                artifact_hash=_artifact_hash({"summary": "second"}),
                now=_NOW + 2,
                runtime_handle=bad_handle,
            )


def test_append_answer_requires_live_turn_lease_when_turn_supplied(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    # 未持有 lease 直接带 turn_id append → turn_lease_expired
    with pytest.raises(SemanticStateError, match="turn_lease_expired"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot={"question": "follow up", "context": {}},
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "second"},
            artifact_hash=_artifact_hash({"summary": "second"}),
            now=_NOW + 2,
            runtime_handle=_runtime_handle(),
            turn_id="turn-1",
            fencing_token=1,
        )
    # 持有 lease 且 fencing token 匹配 → 成功推进
    fencing = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-2",
        lease_seconds=30.0,
        now=_NOW,
    )
    continued = repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-3",
        contract=_CONTRACT,
        input_snapshot={"question": "follow up", "context": {}},
        expected_parent_product_version=1,
        status="completed",
        product={"summary": "second"},
        artifact_hash=_artifact_hash({"summary": "second"}),
        now=_NOW + 1,
        runtime_handle=_runtime_handle(version=2),
        turn_id="turn-2",
        fencing_token=fencing,
    )
    assert continued.product_version == 2
    # 错误 fencing token → turn_fencing_conflict
    repository.release_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-2",
        fencing_token=fencing,
        now=_NOW + 2,
    )
    fencing3 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-3",
        lease_seconds=30.0,
        now=_NOW + 3,
    )
    with pytest.raises(SemanticStateError, match="turn_fencing_conflict"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-4",
            contract=_CONTRACT,
            input_snapshot={"question": "follow up", "context": {}},
            expected_parent_product_version=2,
            status="completed",
            product={"summary": "third"},
            artifact_hash=_artifact_hash({"summary": "third"}),
            now=_NOW + 4,
            runtime_handle=_runtime_handle(version=2),
            turn_id="turn-3",
            fencing_token=fencing3 + 1,
        )


def test_v2_database_migrates_to_latest_preserving_continuations(tmp_path: Path) -> None:
    """v2 DB（无新列）打开后自动迁移到最新 schema，旧 continuation 数据保留。"""
    db_path = tmp_path / "semantic-state.sqlite3"
    repository = _repo(db_path)
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    # 手工降级为 v2：去掉新列，把 schema_version 改为 2
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE continuations_v2 (
                token_hash    TEXT PRIMARY KEY,
                epoch         TEXT NOT NULL,
                principal_id  TEXT NOT NULL,
                chain_id      TEXT NOT NULL UNIQUE REFERENCES chains(chain_id),
                active_job_id TEXT REFERENCES jobs(job_id),
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            )
            """)
        connection.execute("""
            INSERT INTO continuations_v2(token_hash, epoch, principal_id, chain_id,
                                         active_job_id, created_at, updated_at)
            SELECT token_hash, epoch, principal_id, chain_id,
                   active_job_id, created_at, updated_at
            FROM continuations
            """)
        connection.execute("DROP TABLE continuations")
        connection.execute("""
            CREATE TABLE continuations (
                token_hash    TEXT PRIMARY KEY,
                epoch         TEXT NOT NULL,
                principal_id  TEXT NOT NULL,
                chain_id      TEXT NOT NULL UNIQUE REFERENCES chains(chain_id),
                active_job_id TEXT REFERENCES jobs(job_id),
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            )
            """)
        connection.execute("""
            INSERT INTO continuations(token_hash, epoch, principal_id, chain_id,
                                      active_job_id, created_at, updated_at)
            SELECT token_hash, epoch, principal_id, chain_id,
                   active_job_id, created_at, updated_at
            FROM continuations_v2
            """)
        connection.execute("DROP TABLE continuations_v2")
        # B2 (S2): 真实历史 v2 物理形状——v3 起的所有表都不存在
        # （runtime_session_gc/v4 obligations/v5 routes/v6 run ledger）
        connection.execute("DROP TABLE runtime_session_gc")
        connection.execute("DROP TABLE daily_workspace_obligations")
        connection.execute("DROP TABLE conversation_routes")
        connection.execute("DROP TABLE daily_workspace_run_ledger")
        connection.execute("UPDATE semantic_state_meta SET schema_version=2 WHERE id=1")
        connection.execute("COMMIT")
        connection.execute("PRAGMA foreign_keys=ON")

    # 重新打开：自动迁移 v2→v3→…→v6，旧 token 仍可继续
    reopened = _repo(db_path)
    follow_up_product = {"summary": "follow-up after migration"}
    continued = reopened.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract=_CONTRACT,
        input_snapshot={"question": "follow up", "context": {}},
        expected_parent_product_version=1,
        status="completed",
        product=follow_up_product,
        artifact_hash=_artifact_hash(follow_up_product),
        now=_NOW + 5,
        runtime_handle=_runtime_handle(version=2),
    )
    assert continued.product_version == 2
    # B2 (S2): 迁移后所有 v3–v6 表以 canonical DDL 重建（非空壳 fixture）
    with sqlite3.connect(db_path) as connection:
        tables = {
            str(r[0])
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            )
        }
    assert {"runtime_session_gc", "daily_workspace_obligations",
            "conversation_routes", "daily_workspace_run_ledger"} <= tables
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id=1"
        ).fetchone()
        assert int(row[0]) == SCHEMA_VERSION


def test_read_exposes_runtime_handle_after_append(tmp_path: Path) -> None:
    """append 写入 handle 后，read() 通过 continuation 读回同一 handle。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    # 未 append handle 前 read 无 handle
    before = repository.read(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        now=_NOW + 1,
    )
    assert before.runtime_handle is None

    handle = _runtime_handle(version=2)
    repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract=_CONTRACT,
        input_snapshot={"question": "follow up", "context": {}},
        expected_parent_product_version=1,
        status="completed",
        product={"summary": "second"},
        artifact_hash=_artifact_hash({"summary": "second"}),
        now=_NOW + 2,
        runtime_handle=handle,
    )
    after = repository.read(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        now=_NOW + 3,
    )
    assert after.runtime_handle == handle


def test_append_answer_requires_turn_fencing_pairing(tmp_path: Path) -> None:
    """turn_id 与 fencing_token 必须成对；缺一即拒绝（fencing 形同虚设的修复）。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    # 只给 turn_id 不给 fencing_token → 拒绝
    with pytest.raises(SemanticStateError, match="turn_fencing_required"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "second"},
            artifact_hash=_artifact_hash({"summary": "second"}),
            now=_NOW + 1,
            runtime_handle=_runtime_handle(version=2),
            turn_id="turn-1",
        )
    # 只给 fencing_token 不给 turn_id → 拒绝
    with pytest.raises(SemanticStateError, match="turn_fencing_required"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-3",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "second"},
            artifact_hash=_artifact_hash({"summary": "second"}),
            now=_NOW + 1,
            runtime_handle=_runtime_handle(version=2),
            fencing_token=1,
        )


def test_append_answer_rejects_non_mapping_handle(tmp_path: Path) -> None:
    """非 Mapping handle（字符串/list）→ runtime_handle_invalid，而非 AttributeError。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    for bad in ("codex-cli", ["backend"]):
        with pytest.raises(SemanticStateError, match="runtime_handle_invalid"):
            repository.append_answer(
                principal_id="principal-a",
                continuation_token=created.continuation_token,
                idempotency_key=f"answer-bad-{type(bad).__name__}",
                contract=_CONTRACT,
                input_snapshot=_INPUT,
                expected_parent_product_version=1,
                status="completed",
                product={"summary": "second"},
                artifact_hash=_artifact_hash({"summary": "second"}),
                now=_NOW + 1,
                runtime_handle=bad,  # type: ignore[arg-type]
            )


def test_append_answer_rejects_handle_version_mismatch(tmp_path: Path) -> None:
    """handle product_version 必须等于本轮新 product version；指向任意版本拒绝。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    with pytest.raises(SemanticStateError, match="runtime_handle_invalid"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "second"},
            artifact_hash=_artifact_hash({"summary": "second"}),
            now=_NOW + 1,
            runtime_handle=_runtime_handle(version=999),
        )


def test_turn_lease_takeover_at_exact_expiry_boundary(tmp_path: Path) -> None:
    """expires_at == now 时允许接管（<= now），旧 holder 不能再 append。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    fencing1 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    # 隔离精确到期边界（now == expires_at，无 takeover）：
    # 原 holder 以 now == expires_at append 必须失效（<= now，不能是 < now）
    with pytest.raises(SemanticStateError, match="turn_lease_expired"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "second"},
            artifact_hash=_artifact_hash({"summary": "second"}),
            now=_NOW + 30.0,
            runtime_handle=_runtime_handle(version=2),
            turn_id="turn-1",
            fencing_token=fencing1,
        )
    # 精确到期（now == expires_at）→ 接管成功（begin 用 <= now）
    fencing2 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-2",
        lease_seconds=30.0,
        now=_NOW + 30.0,
    )
    assert fencing2 > fencing1


def test_turn_lease_rejects_infinite_and_boolean_inputs(tmp_path: Path) -> None:
    """now=inf/lease_seconds=True 等非有限/布尔输入一律拒绝，防永久 lease。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    with pytest.raises(ValueError):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            lease_seconds=float("inf"),
            now=_NOW,
        )
    with pytest.raises(ValueError):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            lease_seconds=True,
            now=_NOW,
        )
    with pytest.raises(ValueError):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            lease_seconds=30.0,
            now=float("inf"),
        )


def test_turn_lease_rejects_nan_and_empty_turn_id_and_stale_release(tmp_path: Path) -> None:
    """NaN lease、空 turn_id、stale release 的回归覆盖。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    # NaN lease_seconds
    with pytest.raises(ValueError):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            lease_seconds=float("nan"),
            now=_NOW,
        )
    # NaN now
    with pytest.raises(ValueError):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            lease_seconds=30.0,
            now=float("nan"),
        )
    # 空 turn_id
    with pytest.raises(ValueError):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="",
            lease_seconds=30.0,
            now=_NOW,
        )
    # release 空 turn_id
    with pytest.raises(ValueError):
        repository.release_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="",
            fencing_token=1,
            now=_NOW,
        )
    # release NaN now
    with pytest.raises(ValueError):
        repository.release_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            fencing_token=1,
            now=float("nan"),
        )
    # stale release（错误 turn_id）是 no-op：不抛、不改变 lease
    fencing = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    repository.release_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="wrong-turn",
        fencing_token=fencing,
        now=_NOW + 1,
    )
    with pytest.raises(SemanticStateError, match="turn_lease_held"):
        repository.begin_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-2",
            lease_seconds=30.0,
            now=_NOW + 2,
        )


def test_replay_path_obeys_turn_fencing_contract(tmp_path: Path) -> None:
    """idempotency replay 同样受 turn/fencing 契约约束，不能绕过。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    fencing = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    product = {"summary": "second"}
    write = repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW + 1,
        runtime_handle=_runtime_handle(version=2),
        turn_id="turn-1",
        fencing_token=fencing,
    )
    assert write.product_version == 2
    # 同 key replay：只给 turn_id 不给 fencing_token → 成对校验先拒绝
    with pytest.raises(SemanticStateError, match="turn_fencing_required"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=2,
            status="completed",
            product=product,
            artifact_hash=_artifact_hash(product),
            now=_NOW + 2,
            runtime_handle=_runtime_handle(version=3),
            turn_id="turn-1",
        )
    # 同 key replay：错误 fencing token → turn_fencing_conflict
    with pytest.raises(SemanticStateError, match="turn_fencing_conflict"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=2,
            status="completed",
            product=product,
            artifact_hash=_artifact_hash(product),
            now=_NOW + 2,
            runtime_handle=_runtime_handle(version=3),
            turn_id="turn-1",
            fencing_token=fencing + 1,
        )
    # 同 key replay：成对且正确 → 返回已存 replay
    replay = repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=2,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW + 2,
        runtime_handle=_runtime_handle(version=3),
        turn_id="turn-1",
        fencing_token=fencing,
    )
    assert replay.replayed is True
    assert replay.product_version == 2


def test_append_rejects_omitted_turn_proof_when_lease_held(tmp_path: Path) -> None:
    """链上已有活动 lease 时完全省略 turn proof → 拒绝（只有 lease holder 能推进）。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    # 活动 lease 存在但完全省略 turn_id/fencing_token → 拒绝
    with pytest.raises(SemanticStateError, match="turn_fencing_required"):
        repository.append_answer(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            idempotency_key="answer-2",
            contract=_CONTRACT,
            input_snapshot=_INPUT,
            expected_parent_product_version=1,
            status="completed",
            product={"summary": "second"},
            artifact_hash=_artifact_hash({"summary": "second"}),
            now=_NOW + 1,
            runtime_handle=_runtime_handle(version=2),
        )
    # lease 到期后省略 proof 允许（无活动 lease）→ 正常推进
    write = repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-3",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product={"summary": "second"},
        artifact_hash=_artifact_hash({"summary": "second"}),
        now=_NOW + 31.0,
        runtime_handle=_runtime_handle(version=2),
    )
    assert write.product_version == 2


def test_close_clears_runtime_handle_and_turn_fence(tmp_path: Path) -> None:
    """close 清空 runtime handle 并失效 turn fence。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    fencing = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    product = {"summary": "second"}
    repository.append_answer(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        idempotency_key="answer-2",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        expected_parent_product_version=1,
        status="completed",
        product=product,
        artifact_hash=_artifact_hash(product),
        now=_NOW + 1,
        runtime_handle=_runtime_handle(version=2),
        turn_id="turn-1",
        fencing_token=fencing,
    )
    repository.close(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        now=_NOW + 2,
    )
    with sqlite3.connect(tmp_path / "semantic-state.sqlite3") as connection:
        row = connection.execute(
            "SELECT runtime_backend, session_id, identity_hash, product_version, "
            "active_turn_id, turn_lease_expires_at "
            "FROM continuations"
        ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] is None and row[2] is None and row[3] is None
    assert row[4] is None and row[5] is None


def test_renew_answer_turn_guards_fencing(tmp_path: Path) -> None:
    """renew 只有持有 lease 的 turn 可续期；stale holder 不能续期。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    fencing1 = repository.begin_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        lease_seconds=30.0,
        now=_NOW,
    )
    # 正确 holder 续期 → 新 fencing token
    fencing2 = repository.renew_answer_turn(
        principal_id="principal-a",
        continuation_token=created.continuation_token,
        turn_id="turn-1",
        fencing_token=fencing1,
        lease_seconds=30.0,
        now=_NOW + 10,
    )
    assert fencing2 > fencing1
    # stale fencing token 续期 → 拒绝
    with pytest.raises(SemanticStateError, match="turn_lease_expired"):
        repository.renew_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            fencing_token=fencing1,
            lease_seconds=30.0,
            now=_NOW + 11,
        )
    # 到期后续期 → 拒绝
    with pytest.raises(SemanticStateError, match="turn_lease_expired"):
        repository.renew_answer_turn(
            principal_id="principal-a",
            continuation_token=created.continuation_token,
            turn_id="turn-1",
            fencing_token=fencing2,
            lease_seconds=30.0,
            now=_NOW + 41.0,
        )


def test_append_rejects_non_codex_backend_and_non_uuid_session(tmp_path: Path) -> None:
    """handle 必须 backend=codex-cli 且 session_id 为 UUID。"""
    repository = _repo(tmp_path / "semantic-state.sqlite3")
    created = repository.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "first"},
        artifact_hash=_artifact_hash({"summary": "first"}),
        now=_NOW,
    )
    for bad in (
        {
            "backend": "not-codex",
            "session_id": "019fc2fe-7ea6-7e32-a20b-357f21429486",
            "identity_hash": "a" * 64,
            "product_version": 2,
        },
        {
            "backend": "codex-cli",
            "session_id": "not-a-uuid",
            "identity_hash": "a" * 64,
            "product_version": 2,
        },
        {
            "backend": "codex-cli",
            "session_id": "019fc2fe-7ea6-7e32-a20b-357f21429486",
            "identity_hash": "a" * 64,
            "product_version": 2,
            "extra": 1,
        },
    ):
        with pytest.raises(SemanticStateError, match="runtime_handle_invalid"):
            repository.append_answer(
                principal_id="principal-a",
                continuation_token=created.continuation_token,
                idempotency_key=f"answer-bad-{bad.get('backend')}-{bad.get('session_id')}",
                contract=_CONTRACT,
                input_snapshot=_INPUT,
                expected_parent_product_version=1,
                status="completed",
                product={"summary": "second"},
                artifact_hash=_artifact_hash({"summary": "second"}),
                now=_NOW + 1,
                runtime_handle=bad,
            )

# ── A5L-2（A-minus rebaseline）：真实已发布 v4 数据库 → v5 迁移 ────────────


def test_routed_answer_replay_requires_its_exact_post_route_state(tmp_path: Path) -> None:
    database = tmp_path / "routed-answer-replay.sqlite3"
    repository = _repo(database)
    route_key = "routed-answer-replay"
    first_product = {"summary": "generation one"}
    repository.create_answer(
        principal_id="principal-a",
        idempotency_key="generation-one-turn",
        contract=_CONTRACT,
        input_snapshot={**_INPUT, "question": "generation one"},
        status="completed",
        product=first_product,
        artifact_hash=_artifact_hash(first_product),
        now=_NOW,
        route_key=route_key,
        route_generation="generation-1",
    )
    second_product = {"summary": "generation two"}
    second_kwargs = {
        "principal_id": "principal-a",
        "idempotency_key": "generation-two-turn",
        "contract": _CONTRACT,
        "input_snapshot": {**_INPUT, "question": "generation two"},
        "status": "completed",
        "product": second_product,
        "artifact_hash": _artifact_hash(second_product),
        "now": _NOW + 1,
        "route_key": route_key,
        "route_generation": "generation-2",
        "route_expected_revision": 1,
    }
    winner = repository.create_answer(**second_kwargs)
    bound = repository.find_answer_replay_bound(
        principal_id="principal-a",
        idempotency_key="generation-two-turn",
        route_key=route_key,
        route_generation="generation-2",
        route_expected_revision=1,
    )
    assert bound is not None
    assert bound[0].chain_id == winner.chain_id
    replay = repository.create_answer(**second_kwargs)
    assert replay.replayed is True
    assert replay.chain_id == winner.chain_id

    third_product = {"summary": "generation three"}
    repository.create_answer(
        principal_id="principal-a",
        idempotency_key="generation-three-turn",
        contract=_CONTRACT,
        input_snapshot={**_INPUT, "question": "generation three"},
        status="completed",
        product=third_product,
        artifact_hash=_artifact_hash(third_product),
        now=_NOW + 2,
        route_key=route_key,
        route_generation="generation-3",
        route_expected_revision=2,
    )
    with pytest.raises(SemanticStateError, match="continuation_not_accessible"):
        repository.find_answer_replay_bound(
            principal_id="principal-a",
            idempotency_key="generation-two-turn",
            route_key=route_key,
            route_generation="generation-2",
            route_expected_revision=1,
        )
    with pytest.raises(SemanticStateError, match="continuation_not_accessible"):
        repository.create_answer(**second_kwargs)


def test_expected_absent_answer_replay_rejects_an_orphaned_route(tmp_path: Path) -> None:
    database = tmp_path / "expected-absent-replay.sqlite3"
    repository = _repo(database)
    product = {"summary": "same turn winner"}
    kwargs = {
        "principal_id": "principal-a",
        "idempotency_key": "same-turn",
        "contract": _CONTRACT,
        "input_snapshot": _INPUT,
        "status": "completed",
        "product": product,
        "artifact_hash": _artifact_hash(product),
        "now": _NOW,
        "route_key": "expected-absent-route",
        "route_generation": "generation-1",
    }
    winner = repository.create_answer(**kwargs)
    replay = repository.create_answer(**kwargs)
    assert replay.replayed is True
    assert replay.chain_id == winner.chain_id
    bound = repository.find_answer_replay_bound(
        principal_id="principal-a",
        idempotency_key="same-turn",
        route_key="expected-absent-route",
        route_generation="generation-1",
    )
    assert bound is not None
    assert bound[0].chain_id == winner.chain_id

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM conversation_routes WHERE route_key='expected-absent-route'"
        )
    with pytest.raises(SemanticStateError, match="route_revision_conflict"):
        repository.find_answer_replay_bound(
            principal_id="principal-a",
            idempotency_key="same-turn",
            route_key="expected-absent-route",
            route_generation="generation-1",
        )
    with pytest.raises(SemanticStateError, match="route_revision_conflict"):
        repository.create_answer(**kwargs)


def test_real_published_v4_database_migrates_to_latest_preserving_data(
    tmp_path: Path,
) -> None:
    """真实 v4 库文件（已发布 SCHEMA_VERSION=4 形状）经真实重启迁移到 v5。

    v5 相对 v4 仅 additive 新增 conversation_routes，因此"v5 建库后删除该表
    并降级 meta 版本"得到与已发布 v4 库文件字节级等价的 schema 形状；重开
    repository 必须触发真实 _migrate_v4_to_v5（及后续链）：表以 canonical DDL 重建、既有
    语义数据（chain/product）原样保留。
    """
    import sqlite3

    database = tmp_path / "published-v4.sqlite3"
    repo = _repo(database)
    # 写入真实 v4 语义数据：一条已发布 answer chain
    created = repo.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-1",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "v4 era product"},
        artifact_hash=_artifact_hash({"summary": "v4 era product"}),
        now=_NOW,
    )
    # 降级为已发布 v4 库文件形状（B2 S2: 真实 v4 没有 v5 routes / v6 ledger）
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE conversation_routes")
        conn.execute("DROP TABLE daily_workspace_run_ledger")
        conn.execute("UPDATE semantic_state_meta SET schema_version=4 WHERE id=1")
        conn.commit()
    # 真实重启：repository 重开触发 v4→v5 迁移
    reopened = _repo(database)
    with sqlite3.connect(database) as conn:
        version = conn.execute(
            "SELECT schema_version FROM semantic_state_meta WHERE id=1"
        ).fetchone()[0]
        assert version == SCHEMA_VERSION
        # conversation_routes 以 canonical DDL 重建（列齐全）
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_routes)")
        }
        assert {"route_key", "active_generation", "active_chain_id",
                "active_revision", "seen_generations_json"} <= columns
        # B2 (S2): v6 run ledger 以 canonical DDL 重建
        ledger_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(daily_workspace_run_ledger)")
        }
        assert {"run_id", "trading_day_id", "checkpoint", "trigger",
                "started_at", "completed_at", "stage_statuses",
                "collect_identity"} <= ledger_columns
        # 既有 v4 语义数据原样保留
        assert conn.execute(
            "SELECT COUNT(*) FROM chains WHERE chain_id=?",
            (created.chain_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM products WHERE chain_id=?",
            (created.chain_id,),
        ).fetchone()[0] == 1
    # 迁移后的库可被新路由语义直接使用（表可读可写）
    assert reopened.resolve_route(route_key="no-such-route") is None
    reopened.create_answer(
        principal_id="principal-a",
        idempotency_key="answer-2",
        contract=_CONTRACT,
        input_snapshot=_INPUT,
        status="completed",
        product={"summary": "v5 era product"},
        artifact_hash=_artifact_hash({"summary": "v5 era product"}),
        now=_NOW + 1,
        route_key="route-1",
        route_generation="gen-1",
    )
    assert reopened.resolve_route(route_key="route-1")["active_generation"] == "gen-1"
