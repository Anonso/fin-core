from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.consume_zsxq_capture_folder import main as _consumer_main

_RELEASE_SHA = "a" * 40
_NOT_BEFORE_RUN_ID = "20260820T000000000-1"


@pytest.fixture(autouse=True)
def _isolate_cognition_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let consume tests touch the real production read-model artifact."""

    def fake_rebuild() -> dict[str, object]:
        return {
            "schema_version": "fin.cognition-mainline-rebuild/v1",
            "disposition": "ALREADY_CURRENT",
        }

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder._rebuild_cognition_mainline",
        fake_rebuild,
    )


def main(argv: list[str]) -> int:
    if "--not-before-run-id" not in argv:
        argv = [*argv, "--not-before-run-id", _NOT_BEFORE_RUN_ID]
    return _consumer_main(argv)


def test_dry_run_none_path_selects_the_oldest_pending_in_time_order(
    tmp_path: Path, capsys
) -> None:
    _write_run(tmp_path, "20260820T070000000-0", capture_exit=0, published=True)
    oldest_pending = _write_run(
        tmp_path, "20260820T110000000-1", capture_exit=0, published=True
    )
    _write_run(tmp_path, "20260820T130000000-2", capture_exit=0, published=True)
    _write_run(tmp_path, "20260820T142000000-3", capture_exit=1, published=True)
    latest = _write_run(tmp_path, "20260820T160000000-4", capture_exit=0, published=True)
    _write_consumer_result(
        latest,
        status="ready",
        exit_code=0,
        completed_artifacts=1,
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--not-before-run-id",
                "20260820T100000000-1",
                "--dry-run",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "schema_version": "fin.zsxq-capture-folder-consumer/v1",
        "status": "pending",
        "artifact": str(oldest_pending),
    }


def test_none_path_accepts_a_capture_only_summary_as_pending(
    tmp_path: Path, capsys
) -> None:
    artifact = _write_run(
        tmp_path,
        "20260820T130000000-2",
        capture_exit=0,
        published=True,
        capture_only=True,
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["artifact"] == str(artifact)


def test_none_path_drains_two_pending_artifacts_in_time_order(
    tmp_path: Path, monkeypatch
) -> None:
    older = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
    )
    newer = _write_run(
        tmp_path,
        "20260820T130000000-2",
        capture_exit=0,
        published=True,
    )
    imported: list[Path] = []

    def succeed(argv: list[str]) -> int:
        artifact = Path(argv[argv.index("--artifact") + 1])
        imported.append(artifact)
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    assert imported == [older]
    assert json.loads(
        (older.parents[1] / "consumer.result.json").read_text()
    )["status"] == "ready"

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    assert imported == [older, newer]
    assert json.loads(
        (newer.parents[1] / "consumer.result.json").read_text()
    )["status"] == "ready"


def test_none_path_consumes_an_older_capture_release_backlog(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    old_sha = "b" * 40
    artifact = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit=old_sha,
    )

    def succeed(argv: list[str]) -> int:
        imported = Path(argv[argv.index("--artifact") + 1])
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(imported),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    result = json.loads(
        (artifact.parents[1] / "consumer.result.json").read_text()
    )
    assert result["status"] == "ready"
    assert result["source_commit"] == _RELEASE_SHA
    assert result["capture_source_commit"] == old_sha


def test_none_path_reuses_an_old_executor_terminal_result_and_skips_import(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    old_sha = "b" * 40
    artifact = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit=old_sha,
    )
    expected = _write_consumer_result(
        artifact,
        status="ready",
        exit_code=0,
        completed_artifacts=1,
        source_commit=old_sha,
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not restart")),
    )

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert json.loads(
        (artifact.parents[1] / "consumer.result.json").read_text()
    ) == expected


def test_none_path_retries_an_old_executor_retryable_result_and_overwrites_it(
    tmp_path: Path, monkeypatch
) -> None:
    old_sha = "b" * 40
    artifact = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit=old_sha,
    )
    _write_consumer_result(
        artifact,
        status="retryable",
        exit_code=70,
        completed_artifacts=0,
        source_commit=old_sha,
    )

    def succeed(argv: list[str]) -> int:
        imported = Path(argv[argv.index("--artifact") + 1])
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(imported),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    result = json.loads(
        (artifact.parents[1] / "consumer.result.json").read_text()
    )
    assert result["status"] == "ready"
    assert result["source_commit"] == _RELEASE_SHA


def test_none_path_capture_pending_race_returns_70_without_writing_a_result(
    tmp_path: Path, monkeypatch
) -> None:
    # capture_pending summary + artifact already visible: transient window.
    _write_run(
        tmp_path,
        "20260820T130000000-2",
        capture_exit=0,
        published=True,
        error_code="capture_pending",
        exit_code=75,
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 70
    )
    assert not (
        tmp_path / "20260820T130000000-2" / "consumer.result.json"
    ).exists()


def test_none_path_skips_verifiable_failed_artifact_pending_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Wrapper died mid-flight: summary stayed capture_pending (identity invalid)
    # while the capture script left a verifiably failed artifact.  This must not
    # block the poller forever (status 70); it is skipped with an audit line.
    run_dir = tmp_path / "20260820T130000000-2"
    handoff = run_dir / "handoff"
    handoff.mkdir(parents=True)
    artifact = handoff / "capture.latest.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "fin.zsxq-capture-artifact/v1",
                "run_id": "ec58420a-0b07-46f2-a438-907ba312fd4f",
                "final_status": "failed",
                "failure": {"reason": "transport_unavailable"},
                "content_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "fin.zsxq-windows-capture/v4",
                "run_id": "20260820T130000000-2",
                "capture_exit_code": None,
                "capture_ready": False,
                "artifact_published": False,
                "source_commit": _RELEASE_SHA,
                "error_code": "capture_pending",
                "exit_code": 75,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    out = capsys.readouterr().out
    assert '"status": "idle"' in out
    assert not (run_dir / "consumer.result.json").exists()
    audit = (
        tmp_path
        / "state"
        / "fin-analyse"
        / "zsxq-scraper"
        / "poller-skip-audit.v1.jsonl"
    )
    assert audit.exists()
    audit_line = json.loads(audit.read_text().splitlines()[0])
    assert audit_line["run_id"] == "20260820T130000000-2"
    assert audit_line["reason"] == "verifiable_failed_artifact"


def test_duplicate_import_is_a_successful_noop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    artifact = _write_run(
        tmp_path, "20260820T130000000-2", capture_exit=0, published=True
    )

    def duplicate(_argv: list[str]) -> int:
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "duplicate",
                    "original_status": "succeeded",
                    "original_completion_status": "ready",
                    "original_exit_code": 0,
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 64

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture", duplicate
    )

    assert main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "duplicate"


def test_partial_duplicate_preserves_the_original_terminal_exit(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260820T130000000-2"
    artifact = _write_run(tmp_path, run_id, capture_exit=0, published=True)

    def duplicate_partial(_argv: list[str]) -> int:
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "duplicate",
                    "original_status": "succeeded",
                    "original_completion_status": "partial",
                    "original_exit_code": 4,
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 64

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture", duplicate_partial
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
            ]
        )
        == 4
    )
    result = json.loads((tmp_path / run_id / "consumer.result.json").read_text())
    assert result["status"] == "partial"
    assert result["exit_code"] == 4


def test_duplicate_must_match_the_exact_artifact_identity(
    tmp_path: Path, monkeypatch
) -> None:
    artifact = _write_run(
        tmp_path, "20260820T130000000-2", capture_exit=0, published=True
    )

    def wrong_duplicate(_argv: list[str]) -> int:
        identity = _ingest_identity(artifact)
        identity["run_id"] = "different-run"
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "duplicate",
                    "original_status": "succeeded",
                    "original_completion_status": "ready",
                    "original_exit_code": 0,
                    "artifact": identity,
                }
            )
        )
        return 64

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture", wrong_duplicate
    )

    assert main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 70


def test_invalid_request_is_not_misclassified_as_duplicate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _write_run(tmp_path, "20260820T130000000-2", capture_exit=0, published=True)

    def invalid_request(_argv: list[str]) -> int:
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "invalid_request",
                }
            )
        )
        return 64

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture", invalid_request
    )

    assert main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 64
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_request"


def test_zero_exit_without_a_typed_ingest_success_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    _write_run(tmp_path, "20260820T130000000-2", capture_exit=0, published=True)

    def malformed_success(_argv: list[str]) -> int:
        print("not-json")
        return 0

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture", malformed_success
    )

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA])
        == 70
    )


def test_requested_run_never_falls_back_to_a_different_completed_capture(
    tmp_path: Path, capsys
) -> None:
    requested = "20260820T130000000-2"
    expected = _write_run(tmp_path, requested, capture_exit=0, published=True)
    _write_run(tmp_path, "20260820T142000000-3", capture_exit=0, published=True)

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                requested,
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["artifact"] == str(expected)


def test_requested_run_with_drift_is_unavailable_instead_of_falling_back(
    tmp_path: Path, capsys
) -> None:
    requested = "20260820T130000000-2"
    artifact = _write_run(tmp_path, requested, capture_exit=0, published=True)
    artifact.write_text('{"drifted":true}', encoding="utf-8")
    _write_run(tmp_path, "20260820T142000000-3", capture_exit=0, published=True)

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                requested,
                "--dry-run",
            ]
        )
        == 70
    )
    assert json.loads(capsys.readouterr().out)["status"] == "corrupt"


def test_requested_run_rejects_a_summary_bound_to_another_run(
    tmp_path: Path, capsys
) -> None:
    requested = "20260820T130000000-2"
    _write_run(
        tmp_path,
        requested,
        capture_exit=0,
        published=True,
        summary_run_id="20260820T110000000-1",
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                requested,
                "--dry-run",
            ]
        )
        == 70
    )
    assert json.loads(capsys.readouterr().out)["status"] == "corrupt"


def test_requested_run_drains_older_completed_captures_in_order(
    tmp_path: Path, monkeypatch
) -> None:
    older = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit="b" * 40,
    )
    requested = _write_run(
        tmp_path,
        "20260820T130000000-2",
        capture_exit=0,
        published=True,
    )
    imported: list[Path] = []

    def succeed(argv: list[str]) -> int:
        artifact = Path(argv[argv.index("--artifact") + 1])
        imported.append(artifact)
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                "20260820T130000000-2",
            ]
        )
        == 0
    )
    assert imported == [older, requested]
    result = json.loads(
        (tmp_path / "20260820T130000000-2" / "consumer.result.json").read_text()
    )
    assert result == {
        "capture_source_commit": _RELEASE_SHA,
        "completed_artifacts": 2,
        "exit_code": 0,
        "run_id": "20260820T130000000-2",
        "schema_version": "fin.zsxq-capture-folder-consumer-result/v1",
        "source_commit": _RELEASE_SHA,
        "status": "ready",
        "through_artifact_sha256": sha256(requested.read_bytes()).hexdigest(),
    }
    older_result = json.loads(
        (older.parents[1] / "consumer.result.json").read_text()
    )
    assert older_result["status"] == "ready"
    assert older_result["source_commit"] == _RELEASE_SHA
    assert older_result["capture_source_commit"] == "b" * 40
    assert older_result["through_artifact_sha256"] == sha256(older.read_bytes()).hexdigest()


def test_old_partial_is_bound_to_old_run_and_new_capture_remains_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    older = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit="b" * 40,
    )
    requested_id = "20260820T130000000-2"
    requested = _write_run(
        tmp_path,
        requested_id,
        capture_exit=0,
        published=True,
    )
    imported: list[Path] = []

    requested_attempts = 0

    def partial_then_retryable_then_ready(argv: list[str]) -> int:
        nonlocal requested_attempts
        artifact = Path(argv[argv.index("--artifact") + 1])
        imported.append(artifact)
        if artifact == requested:
            requested_attempts += 1
            if requested_attempts == 1:
                print(
                    json.dumps(
                        {
                            "schema_version": "fin.zsxq-capture-ingest/v1",
                            "status": "internal_error",
                        }
                    )
                )
                return 70
        completion_status = "partial" if artifact == older else "ready"
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": completion_status,
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 4 if artifact == older else 0

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        partial_then_retryable_then_ready,
    )
    argv = [
        "--runs-root",
        str(tmp_path),
        "--source-commit",
        _RELEASE_SHA,
        "--run-id",
        requested_id,
    ]

    assert main(argv) == 70
    assert imported == [older]
    older_result = json.loads(
        (older.parents[1] / "consumer.result.json").read_text()
    )
    requested_result = json.loads(
        (requested.parents[1] / "consumer.result.json").read_text()
    )
    assert (older_result["run_id"], older_result["status"], older_result["exit_code"]) == (
        older.parents[1].name,
        "partial",
        4,
    )
    assert requested_result["status"] == "retryable"
    assert requested_result["blocked_by_run_id"] == older.parents[1].name
    assert requested_result["blocked_exit_code"] == 4

    result_path = requested.parents[1] / "consumer.result.json"
    summary_path = requested.parents[1] / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary.update(
        {
            "error_code": "consumer",
            "exit_code": 70,
            "consumer_status": "retryable",
            "consumer_result_sha256": sha256(result_path.read_bytes()).hexdigest(),
        }
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    assert main(argv) == 70
    assert imported == [older, requested]
    requested_result = json.loads(result_path.read_text())
    assert requested_result["status"] == "retryable"
    assert "blocked_by_run_id" not in requested_result

    assert main(argv) == 0
    assert imported == [older, requested, requested]
    assert json.loads(result_path.read_text())["status"] == "ready"


def test_consumer_failure_keeps_the_completed_capture_retryable(
    tmp_path: Path, capsys
) -> None:
    run_id = "20260820T130000000-2"
    artifact = _write_run(
        tmp_path,
        run_id,
        capture_exit=0,
        published=True,
        error_code="consumer",
        exit_code=70,
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["artifact"] == str(artifact)


def test_wsl_transport_failure_keeps_the_completed_capture_retryable(
    tmp_path: Path, capsys
) -> None:
    run_id = "20260820T130000000-2"
    artifact = _write_run(
        tmp_path,
        run_id,
        capture_exit=0,
        published=True,
        error_code="wsl_transport",
        exit_code=75,
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["artifact"] == str(artifact)


def test_corrupt_older_ready_capture_is_visible_and_blocks_newer_import(
    tmp_path: Path, monkeypatch
) -> None:
    older = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit="b" * 40,
    )
    older.write_text('{"drifted":true}', encoding="utf-8")
    requested_id = "20260820T130000000-2"
    _write_run(tmp_path, requested_id, capture_exit=0, published=True)
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                requested_id,
            ]
        )
        == 70
    )
    result = json.loads(
        (tmp_path / requested_id / "consumer.result.json").read_text()
    )
    assert result["status"] == "retryable"
    assert result["exit_code"] == 70


def test_post_cutover_artifact_with_legacy_summary_blocks_newer_import(
    tmp_path: Path, monkeypatch
) -> None:
    older = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit="b" * 40,
    )
    summary_path = older.parents[1] / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["schema_version"] = "fin.zsxq-windows-capture/v3"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    requested_id = "20260820T130000000-2"
    requested = _write_run(
        tmp_path,
        requested_id,
        capture_exit=0,
        published=True,
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                requested_id,
            ]
        )
        == 70
    )
    assert json.loads(
        (requested.parents[1] / "consumer.result.json").read_text()
    )["status"] == "retryable"


def test_existing_terminal_result_is_reused_without_restarting_import(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    run_id = "20260820T130000000-2"
    artifact = _write_run(tmp_path, run_id, capture_exit=0, published=True)
    expected = _write_consumer_result(
        artifact,
        status="partial",
        exit_code=4,
        completed_artifacts=1,
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not restart")),
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
            ]
        )
        == 4
    )
    assert json.loads(capsys.readouterr().out) == expected


def test_atomic_result_failure_preserves_the_previous_retryable_result(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260820T130000000-2"
    artifact = _write_run(tmp_path, run_id, capture_exit=0, published=True)
    previous = _write_consumer_result(
        artifact,
        status="retryable",
        exit_code=70,
        completed_artifacts=0,
    )

    def succeed(argv: list[str]) -> int:
        imported = Path(argv[argv.index("--artifact") + 1])
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(imported),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.os.replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("injected")),
    )

    with pytest.raises(OSError, match="injected"):
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
            ]
        )
    assert json.loads(
        (tmp_path / run_id / "consumer.result.json").read_text()
    ) == previous


def test_retryable_result_can_advance_without_stale_summary_digest_corruption(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260820T130000000-2"
    artifact = _write_run(tmp_path, run_id, capture_exit=0, published=True)

    def retryable(_argv: list[str]) -> int:
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "internal_error",
                }
            )
        )
        return 70

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture", retryable
    )
    argv = [
        "--runs-root",
        str(tmp_path),
        "--source-commit",
        _RELEASE_SHA,
        "--run-id",
        run_id,
    ]
    assert main(argv) == 70
    result_path = artifact.parents[1] / "consumer.result.json"
    summary_path = artifact.parents[1] / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary.update(
        {
            "error_code": "consumer",
            "exit_code": 70,
            "consumer_status": "retryable",
            "consumer_result_sha256": sha256(result_path.read_bytes()).hexdigest(),
        }
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    def succeed(call_argv: list[str]) -> int:
        imported = Path(call_argv[call_argv.index("--artifact") + 1])
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(imported),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)
    assert main(argv) == 0
    assert json.loads(result_path.read_text())["status"] == "ready"

    next_id = "20260820T142000000-3"
    expected = _write_run(tmp_path, next_id, capture_exit=0, published=True)
    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                next_id,
                "--dry-run",
            ]
        )
        == 0
    )
    assert expected.is_file()


def test_published_artifact_without_summary_is_visible_corruption(
    tmp_path: Path, monkeypatch
) -> None:
    old = _write_run(
        tmp_path,
        "20260820T110000000-1",
        capture_exit=0,
        published=True,
        source_commit="b" * 40,
    )
    (old.parents[1] / "summary.json").unlink()
    requested_id = "20260820T130000000-2"
    requested = _write_run(
        tmp_path,
        requested_id,
        capture_exit=0,
        published=True,
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                requested_id,
            ]
        )
        == 70
    )
    result = json.loads(
        (requested.parents[1] / "consumer.result.json").read_text()
    )
    assert result["status"] == "retryable"


def test_explicit_cutover_preserves_but_ignores_legacy_summaryless_artifact(
    tmp_path: Path, capsys
) -> None:
    legacy = _write_run(
        tmp_path,
        "20260819T123754458-24680",
        capture_exit=0,
        published=True,
        source_commit="b" * 40,
    )
    (legacy.parents[1] / "summary.json").unlink()
    requested_id = "20260820T202000000-2"
    requested = _write_run(
        tmp_path,
        requested_id,
        capture_exit=0,
        published=True,
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--not-before-run-id",
                "20260820T160000000-1",
                "--run-id",
                requested_id,
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["artifact"] == str(requested)
    assert legacy.is_file()


def test_run_directory_symlink_cannot_write_a_result_outside_runs_root(
    tmp_path: Path, monkeypatch
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    outside_root = tmp_path / "outside"
    run_id = "20260820T202000000-2"
    _write_run(outside_root, run_id, capture_exit=0, published=True)
    (runs_root / run_id).symlink_to(outside_root / run_id, target_is_directory=True)
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert (
        main(
            [
                "--runs-root",
                str(runs_root),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
            ]
        )
        == 70
    )
    assert not (outside_root / run_id / "consumer.result.json").exists()


def test_handoff_directory_symlink_is_rejected_before_import(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "20260820T202000000-2"
    artifact = _write_run(tmp_path, run_id, capture_exit=0, published=True)
    outside_handoff = tmp_path / "outside-handoff"
    artifact.parent.rename(outside_handoff)
    artifact.parent.symlink_to(outside_handoff, target_is_directory=True)
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.import_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert (
        main(
            [
                "--runs-root",
                str(tmp_path),
                "--source-commit",
                _RELEASE_SHA,
                "--run-id",
                run_id,
            ]
        )
        == 70
    )
    assert not (tmp_path / run_id / "consumer.result.json").exists()


def test_runs_root_symlink_is_rejected(tmp_path: Path) -> None:
    real_runs_root = tmp_path / "real-runs"
    real_runs_root.mkdir()
    linked_runs_root = tmp_path / "linked-runs"
    linked_runs_root.symlink_to(real_runs_root, target_is_directory=True)

    with pytest.raises(SystemExit):
        main(
            [
                "--runs-root",
                str(linked_runs_root),
                "--source-commit",
                _RELEASE_SHA,
            ]
        )


def _write_run(
    root: Path,
    run_id: str,
    *,
    capture_exit: int,
    published: bool,
    source_commit: str = _RELEASE_SHA,
    error_code: str | None = "consumer_pending",
    exit_code: int | None = None,
    summary_run_id: str | None = None,
    capture_only: bool = False,
) -> Path:
    handoff = root / run_id / "handoff"
    handoff.mkdir(parents=True)
    artifact = handoff / "capture.latest.json"
    artifact.write_text(
        json.dumps(
            {
                "run_id": f"capture-{run_id}",
                "content_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    summary: dict[str, object] = {
        "schema_version": "fin.zsxq-windows-capture/v4",
        "run_id": summary_run_id or run_id,
        "capture_exit_code": capture_exit,
        "capture_ready": capture_exit == 0 and published,
        "artifact_published": published,
        "source_commit": source_commit,
        "artifact_sha256": sha256(artifact.read_bytes()).hexdigest(),
        "error_code": None if capture_only else error_code,
        "exit_code": (
            0
            if capture_only
            else (
                exit_code
                if exit_code is not None
                else (75 if capture_exit == 0 and published else 70)
            )
        ),
    }
    if not capture_only:
        summary["consumer_result_sha256"] = None
    (handoff.parent / "summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )
    return artifact


def _ingest_identity(artifact: Path) -> dict[str, str]:
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return {
        "run_id": payload["run_id"],
        "file": artifact.name,
        "content_sha256": payload["content_sha256"],
    }


def _write_consumer_result(
    artifact: Path,
    *,
    status: str,
    exit_code: int,
    completed_artifacts: int,
    source_commit: str = _RELEASE_SHA,
) -> dict[str, object]:
    run_id = artifact.parents[1].name
    payload: dict[str, object] = {
        "schema_version": "fin.zsxq-capture-folder-consumer-result/v1",
        "run_id": run_id,
        "source_commit": source_commit,
        "capture_source_commit": source_commit,
        "status": status,
        "exit_code": exit_code,
        "completed_artifacts": completed_artifacts,
        "through_artifact_sha256": sha256(artifact.read_bytes()).hexdigest(),
    }
    (artifact.parents[1] / "consumer.result.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return payload


def test_ingest_success_triggers_cognition_rebuild_without_blocking(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    older = _write_run(
        tmp_path, "20260820T070000000-1", capture_exit=0, published=True
    )
    imported: list[Path] = []

    def succeed(argv: list[str]) -> int:
        artifact = Path(argv[argv.index("--artifact") + 1])
        imported.append(artifact)
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)
    calls: list[int] = []

    def fake_rebuild() -> dict[str, object]:
        calls.append(1)
        return {
            "schema_version": "fin.cognition-mainline-rebuild/v1",
            "disposition": "PUBLISHED",
            "candidate_identity": "b" * 64,
            "generation": 2,
        }

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder._rebuild_cognition_mainline",
        fake_rebuild,
    )

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    assert imported == [older]
    assert calls
    assert (
        json.loads(
            (older.parents[1] / "consumer.result.json").read_text(encoding="utf-8")
        )["status"]
        == "ready"
    )


def test_ingest_success_survives_rebuild_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _write_run(tmp_path, "20260820T070000000-1", capture_exit=0, published=True)

    def succeed(argv: list[str]) -> int:
        artifact = Path(argv[argv.index("--artifact") + 1])
        print(
            json.dumps(
                {
                    "schema_version": "fin.zsxq-capture-ingest/v1",
                    "status": "succeeded",
                    "completion_status": "ready",
                    "artifact": _ingest_identity(artifact),
                }
            )
        )
        return 0

    monkeypatch.setattr("scripts.consume_zsxq_capture_folder.import_capture", succeed)

    def broken_rebuild() -> dict[str, object]:
        raise RuntimeError("rebuild exploded")

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder._rebuild_cognition_mainline",
        broken_rebuild,
    )

    assert (
        main(["--runs-root", str(tmp_path), "--source-commit", _RELEASE_SHA]) == 0
    )
    assert (
        json.loads(
            (tmp_path / "20260820T070000000-1" / "consumer.result.json").read_text(
                encoding="utf-8"
            )
        )["status"]
        == "ready"
    )
