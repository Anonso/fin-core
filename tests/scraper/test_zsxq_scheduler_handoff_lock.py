from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _ready_completion():
    from fin_analyse.scraper.cdp_runtime import (
        GWorkingSetPublicationReceipt,
        ProductionCdpCompletionReceipt,
    )
    from fin_analyse.scraper.contracts import ZsxqRunResult

    now = datetime(2026, 7, 29, 6, 0, tzinfo=UTC).isoformat()
    return ProductionCdpCompletionReceipt(
        run=ZsxqRunResult(
            status="no_change",
            request_id="request-1",
            intent="sync",
            trigger="schedule",
            run_id="run-1",
            changed_count=0,
            attempt=1,
            started_at=now,
            finished_at=now,
        ),
        completion_status="ready",
        g_working_set=GWorkingSetPublicationReceipt(
            published=True,
            status="READY",
            generation="a" * 64,
            evaluated_at=now,
            source_refs=("zsxq-1",),
            data_gaps=(),
            freshness="FRESH",
            source_coverage_sha256="b" * 64,
            producer_id="fin.zsxq-production-cdp/v1",
            producer_run_id="run-1",
            producer_run_status="no_change",
            publication_mode="NO_CHANGE",
            prior_generation="a" * 64,
            prior_source_refs=("zsxq-1",),
            prior_source_coverage_sha256="b" * 64,
            prior_evaluated_at=now,
            prior_freshness="FRESH",
        ),
    )


def test_scheduled_run_returns_75_before_runtime_or_knowledge_access_when_handoff_locked(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.scheduler_handoff_lock import (
        HandoffLockMode,
        hold_scheduler_handoff_lock,
        scheduler_handoff_lock_path,
    )

    runtime_db = (tmp_path / "state" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    lock_path = scheduler_handoff_lock_path(runtime_db)

    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("run must not start")),
    )

    with hold_scheduler_handoff_lock(lock_path, mode=HandoffLockMode.EXCLUSIVE):
        exit_code = scheduled_run.main(
            [
                "--runtime-db",
                str(runtime_db),
                "--knowledge-base-root",
                str(knowledge_base_root),
            ]
        )

    assert exit_code == 75
    assert json.loads(capsys.readouterr().out) == {
        "coalesced": True,
        "completion_status": "coalesced",
        "completion_data_gaps": [],
        "error_code": "scheduler_handoff_locked",
        "intent": "sync",
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "coalesced",
        "trigger": "schedule",
    }
    assert not runtime_db.exists()
    assert not knowledge_base_root.exists()


def test_scheduled_run_holds_shared_handoff_lock_across_the_production_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.scheduler_handoff_lock import (
        HandoffLockMode,
        SchedulerHandoffLockBusyError,
        hold_scheduler_handoff_lock,
        scheduler_handoff_lock_path,
    )

    runtime_db = (tmp_path / "state" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles":[]}', encoding="utf-8")
    observed = {"exclusive_blocked": False}

    def run_once(**_kwargs):
        with (
            pytest.raises(SchedulerHandoffLockBusyError),
            hold_scheduler_handoff_lock(
                scheduler_handoff_lock_path(runtime_db),
                mode=HandoffLockMode.EXCLUSIVE,
            ),
        ):
            raise AssertionError("exclusive handoff lock must not be acquired")
        observed["exclusive_blocked"] = True
        return _ready_completion()

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", run_once)

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    assert exit_code == 0
    assert observed == {"exclusive_blocked": True}


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_handoff_lock_rejects_foreign_link_without_touching_its_target(
    tmp_path: Path,
    kind: str,
) -> None:
    from fin_analyse.scraper.scheduler_handoff_lock import (
        HandoffLockMode,
        SchedulerHandoffLockError,
        hold_scheduler_handoff_lock,
        scheduler_handoff_lock_path,
    )

    runtime_db = (tmp_path / "state" / "runtime.sqlite3").resolve()
    lock_path = scheduler_handoff_lock_path(runtime_db)
    lock_path.parent.mkdir(mode=0o700)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign-bytes")
    foreign.chmod(0o600)
    if kind == "symlink":
        lock_path.symlink_to(foreign)
    else:
        lock_path.hardlink_to(foreign)

    with (
        pytest.raises(SchedulerHandoffLockError),
        hold_scheduler_handoff_lock(lock_path, mode=HandoffLockMode.SHARED),
    ):
        raise AssertionError("unsafe lock must not be acquired")

    assert foreign.read_bytes() == b"foreign-bytes"
    assert lock_path.exists()


def test_scheduled_run_fails_closed_when_lock_name_is_replaced_during_run(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.scheduler_handoff_lock import scheduler_handoff_lock_path

    runtime_db = (tmp_path / "state" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles":[]}', encoding="utf-8")
    lock_path = scheduler_handoff_lock_path(runtime_db)
    foreign_bytes = b"foreign-lock"

    def replace_lock_name(**_kwargs):
        replacement = lock_path.parent / "foreign-replacement"
        replacement.write_bytes(foreign_bytes)
        replacement.chmod(0o600)
        os.replace(replacement, lock_path)
        return _ready_completion()

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", replace_lock_name)

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    assert exit_code == 70
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "scheduler_handoff_lock_unsafe",
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "internal_error",
    }
    assert lock_path.read_bytes() == foreign_bytes


@pytest.mark.parametrize("drift", ["chmod", "rename_to_symlink"])
def test_scheduled_run_fails_closed_when_lock_parent_drifts_during_run(
    monkeypatch,
    tmp_path: Path,
    capsys,
    drift: str,
) -> None:
    from fin_analyse.scraper import scheduled_run

    runtime_db = (tmp_path / "state" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles":[]}', encoding="utf-8")
    original_parent = runtime_db.parent

    def drift_parent(**_kwargs):
        if drift == "chmod":
            original_parent.chmod(0o755)
        else:
            renamed = original_parent.with_name("state-original")
            original_parent.rename(renamed)
            original_parent.symlink_to(renamed, target_is_directory=True)
        return _ready_completion()

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", drift_parent)

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    assert exit_code == 70
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "scheduler_handoff_lock_parent_unsafe",
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "internal_error",
    }


def test_scheduled_run_coalesces_against_an_exclusive_lock_in_another_process(
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.scheduler_handoff_lock import scheduler_handoff_lock_path

    runtime_db = (tmp_path / "state" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    lock_path = scheduler_handoff_lock_path(runtime_db)
    child_source = """
import sys
from pathlib import Path
from fin_analyse.scraper.scheduler_handoff_lock import (
    HandoffLockMode,
    hold_scheduler_handoff_lock,
)
with hold_scheduler_handoff_lock(
    Path(sys.argv[1]),
    mode=HandoffLockMode.EXCLUSIVE,
):
    print("READY", flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_source, str(lock_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"

        exit_code = scheduled_run.main(
            [
                "--runtime-db",
                str(runtime_db),
                "--knowledge-base-root",
                str(knowledge_base_root),
            ]
        )

        assert exit_code == 75
        assert json.loads(capsys.readouterr().out)["error_code"] == ("scheduler_handoff_locked")
        assert not runtime_db.exists()
    finally:
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=5)
        if child.returncode != 0:
            assert child.stderr is not None
            raise AssertionError(child.stderr.read())


def test_handoff_lock_rejects_an_untyped_mode_before_creating_the_lock(
    tmp_path: Path,
) -> None:
    from fin_analyse.scraper.scheduler_handoff_lock import hold_scheduler_handoff_lock

    lock_path = (tmp_path / "state" / "scheduler-handoff.lock").resolve()

    with (
        pytest.raises(ValueError, match="scheduler_handoff_lock_mode_invalid"),
        hold_scheduler_handoff_lock(
            lock_path,
            mode="SHARED",  # type: ignore[arg-type]
        ),
    ):
        raise AssertionError("untyped lock mode must not be accepted")

    assert not lock_path.exists()


def test_scheduled_run_rejects_a_noncanonical_runtime_path_before_lock_creation(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    noncanonical = tmp_path / "state" / ".." / "runtime.sqlite3"
    knowledge_base_root = tmp_path / "knowledge-base"
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("run must not start")),
    )

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(noncanonical),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    assert exit_code == 64
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "runtime_db_must_be_canonical",
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "invalid_request",
    }
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "scheduler-handoff.lock").exists()
    assert not knowledge_base_root.exists()
