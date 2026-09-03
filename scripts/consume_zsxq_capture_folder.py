#!/usr/bin/env python3
"""Consume completed ZSXQ Windows captures from one shared folder.

Windows only publishes a per-run artifact and summary.  This WSL one-shot
consumer reads that folder, then delegates all validation, recovery and
exactly-once semantics to the existing capture importer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import rebuild_if_stale
from fin_analyse.guo_teacher_research.mainline_candidates import scan_mainline_candidates
from fin_analyse.runtime.knowledge_root import default_knowledge_base_root
from fin_analyse.scraper.capture_ingest import main as import_capture

_SCHEMA_VERSION = "fin.zsxq-capture-folder-consumer/v1"
_RESULT_SCHEMA_VERSION = "fin.zsxq-capture-folder-consumer-result/v1"
_INGEST_SCHEMA_VERSION = "fin.zsxq-capture-ingest/v1"
_CAPTURE_SCHEMA_VERSION = "fin.zsxq-windows-capture/v4"
_REBUILD_SCHEMA_VERSION = "fin.cognition-mainline-rebuild/v1"
_CANDIDATES_SCHEMA_VERSION = "fin.mainline-candidates/v1"
_CANDIDATES_AUDIT_NAME = "mainline-candidates.v1.jsonl"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{9}-[1-9][0-9]*$")
_DUPLICATE_EXIT = 64
_INTERNAL_ERROR_EXIT = 70
_TEMPFAIL_EXIT = 75
_RETRYABLE_SUMMARY_ERRORS = frozenset(
    {"consumer", "consumer_pending", "consumer_protocol", "wsl_transport"}
)
# Keys the legacy (orchestrating) wrapper used to track its own transport and
# consumer results.  Their presence marks a summary as a legacy/other wrapper;
# the capture-only wrapper never writes them.
_LEGACY_CONSUMER_KEYS = frozenset(
    {
        "consumer_result_sha256",
        "consumer_status",
        "consumer_exit_code",
        "consumer_launcher_exit_code",
        "transport_exit_code",
        "transport_attempts",
    }
)


def _configure_logging() -> None:
    """Send INFO+ module logs to stderr so systemd journal captures them.

    The consumer stdout contract stays a single JSON document; diagnostic
    logging (deep-read, priority, LLM failures) goes to the journal instead.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class _ReadyCaptureCorruptError(RuntimeError):
    pass


_FAILED_ARTIFACT_SCHEMA = "fin.zsxq-capture-artifact/v1"


def _is_verifiable_failed_artifact(artifact: Path) -> bool:
    """True only for a capture artifact that verifiably carries no article data.

    A mid-flight wrapper death leaves ``capture_pending`` summary fields while
    the capture script already published a v1 artifact whose ``final_status``
    is ``failed`` (e.g. transport_unavailable).  Such a run holds no article
    content, so the poller may skip it with an audit instead of blocking the
    whole queue with status 70.  Anything else (succeeded artifact, unreadable
    JSON, missing schema/status) stays fail-closed and requires a human.
    """

    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == _FAILED_ARTIFACT_SCHEMA
        and payload.get("final_status") == "failed"
        and isinstance(payload.get("failure"), dict)
    )


def _record_skipped_failed_artifact(
    run_dir: Path,
    artifact: Path,
    *,
    reason: str,
) -> None:
    """Append one content-free audit line; never blocks or changes exit code."""

    home = Path.home()
    state_root = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
    audit_path = state_root / "fin-analyse" / "zsxq-scraper" / "poller-skip-audit.v1.jsonl"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(
                descriptor,
                (
                    json.dumps(
                        {
                            "schema_version": "fin.poller-skip-audit/v1",
                            "run_id": run_dir.name,
                            "reason": reason,
                            "artifact_sha256": sha256(artifact.read_bytes()).hexdigest(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        finally:
            os.close(descriptor)
    except (OSError, json.JSONDecodeError):
        return


@dataclass(frozen=True)
class _CaptureCandidate:
    run_id: str
    artifact: Path
    source_commit: str
    artifact_sha256: str
    ingest_run_id: str
    ingest_content_sha256: str


@dataclass(frozen=True)
class _BoundResult:
    payload: dict[str, object]
    content_sha256: str


@dataclass(frozen=True)
class _InspectedRun:
    candidate: _CaptureCandidate
    result: _BoundResult | None


@dataclass(frozen=True)
class _Selection:
    artifacts: tuple[_CaptureCandidate, ...]
    requested: _CaptureCandidate | None = None
    existing_result: dict[str, object] | None = None
    corrupt: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--not-before-run-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _completed_artifact(
    run_dir: Path,
    *,
    expected_source_commit: str | None,
) -> _InspectedRun | None:
    summary_path = run_dir / "summary.json"
    handoff_dir = run_dir / "handoff"
    artifact = handoff_dir / "capture.latest.json"
    if _RUN_ID.fullmatch(run_dir.name) is None:
        return None
    if run_dir.is_symlink():
        raise _ReadyCaptureCorruptError
    if not run_dir.is_dir():
        return None
    if handoff_dir.is_symlink():
        raise _ReadyCaptureCorruptError
    if summary_path.is_symlink() or not summary_path.is_file():
        if artifact.exists():
            raise _ReadyCaptureCorruptError
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if artifact.exists():
            raise _ReadyCaptureCorruptError from None
        return None
    if not isinstance(summary, dict) or summary.get("schema_version") != _CAPTURE_SCHEMA_VERSION:
        if artifact.exists():
            raise _ReadyCaptureCorruptError
        return None
    summary_identity_valid = (
        summary.get("run_id") == run_dir.name
        and type(summary.get("capture_exit_code")) is int
        and isinstance(summary.get("source_commit"), str)
        and _FULL_SHA.fullmatch(summary["source_commit"]) is not None
        and (expected_source_commit is None or summary["source_commit"] == expected_source_commit)
    )
    if not summary_identity_valid:
        if artifact.exists() and not _is_verifiable_failed_artifact(artifact):
            raise _ReadyCaptureCorruptError
        if artifact.exists():
            _record_skipped_failed_artifact(
                run_dir,
                artifact,
                reason="verifiable_failed_artifact",
            )
        return None
    if (
        summary.get("capture_exit_code") != 0
        or summary.get("capture_ready") is not True
        or summary.get("artifact_published") is not True
    ):
        if summary.get("error_code") == "capture_pending" and artifact.exists():
            if not _is_verifiable_failed_artifact(artifact):
                raise _ReadyCaptureCorruptError
            _record_skipped_failed_artifact(
                run_dir,
                artifact,
                reason="verifiable_failed_artifact",
            )
        return None
    if artifact.is_symlink() or not artifact.is_file():
        raise _ReadyCaptureCorruptError
    try:
        artifact_raw = artifact.read_bytes()
        artifact_sha256 = sha256(artifact_raw).hexdigest()
        artifact_payload = json.loads(artifact_raw)
    except OSError as error:
        raise _ReadyCaptureCorruptError from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ReadyCaptureCorruptError from error
    if summary.get("artifact_sha256") != artifact_sha256:
        raise _ReadyCaptureCorruptError
    if (
        not isinstance(artifact_payload, dict)
        or not isinstance(artifact_payload.get("run_id"), str)
        or not isinstance(artifact_payload.get("content_sha256"), str)
        or _SHA256.fullmatch(artifact_payload["content_sha256"]) is None
    ):
        raise _ReadyCaptureCorruptError

    candidate = _CaptureCandidate(
        run_id=run_dir.name,
        artifact=artifact,
        source_commit=summary["source_commit"],
        artifact_sha256=artifact_sha256,
        ingest_run_id=artifact_payload["run_id"],
        ingest_content_sha256=artifact_payload["content_sha256"],
    )
    result = _read_result(candidate)
    result_sha256 = summary.get("consumer_result_sha256")
    if result is not None:
        if result_sha256 is not None and result_sha256 != result.content_sha256:
            retryable_result_was_superseded = (
                summary.get("error_code") == "consumer"
                and summary.get("consumer_status") == "retryable"
                and summary.get("exit_code") in {_INTERNAL_ERROR_EXIT, _TEMPFAIL_EXIT}
            )
            if not retryable_result_was_superseded:
                raise _ReadyCaptureCorruptError
        return _InspectedRun(candidate=candidate, result=result)

    error_code = summary.get("error_code")
    exit_code = summary.get("exit_code")
    legacy_keys_present = any(key in summary for key in _LEGACY_CONSUMER_KEYS)
    new_pending = (
        not legacy_keys_present
        and error_code is None
        and type(exit_code) is int
        and exit_code == 0
        and result_sha256 is None
    )
    legacy_pending = (
        error_code in _RETRYABLE_SUMMARY_ERRORS
        and type(exit_code) is int
        and exit_code != 0
        and result_sha256 is None
    )
    if new_pending or legacy_pending:
        return _InspectedRun(candidate=candidate, result=None)
    raise _ReadyCaptureCorruptError


def _pending_artifacts(
    runs_root: Path,
    *,
    source_commit: str,
    not_before_run_id: str,
    run_id: str | None,
) -> _Selection:
    """Return eligible artifacts through one requested run in replay order."""

    if not runs_root.is_dir():
        return _Selection(())
    if run_id is not None and run_id < not_before_run_id:
        return _Selection(())
    if run_id is None:
        # Poller mode: consume the OLDEST pending/retryable eligible capture so
        # the backlog drains in time order.  Pre-cutover directories are skipped
        # (never break), and any legal post-cutover capture SHA is eligible; the
        # executor SHA from the current poller release binds the result instead.
        for run_dir in sorted(runs_root.iterdir()):
            if run_dir.name < not_before_run_id:
                continue
            try:
                inspected = _completed_artifact(
                    run_dir,
                    expected_source_commit=None,
                )
            except _ReadyCaptureCorruptError:
                return _Selection((), corrupt=True)
            if inspected is not None and (
                inspected.result is None or inspected.result.payload["status"] == "retryable"
            ):
                return _Selection((inspected.candidate,))
        return _Selection(())

    try:
        requested = _completed_artifact(
            runs_root / run_id,
            expected_source_commit=source_commit,
        )
    except _ReadyCaptureCorruptError:
        return _Selection((), corrupt=True)
    if requested is None:
        return _Selection(())
    if requested.result is not None and requested.result.payload["status"] != "retryable":
        return _Selection(
            (),
            requested=requested.candidate,
            existing_result=requested.result.payload,
        )
    artifacts: list[_CaptureCandidate] = []
    # ponytail: reuse the importer ledger instead of creating a second checkpoint.
    for run_dir in sorted(runs_root.iterdir()):
        if run_dir.name < not_before_run_id:
            continue
        if run_dir.name >= run_id:
            break
        try:
            inspected = _completed_artifact(
                run_dir,
                expected_source_commit=None,
            )
        except _ReadyCaptureCorruptError:
            return _Selection((), requested=requested.candidate, corrupt=True)
        if inspected is not None and (
            inspected.result is None or inspected.result.payload["status"] == "retryable"
        ):
            artifacts.append(inspected.candidate)
    artifacts.append(requested.candidate)
    return _Selection(tuple(artifacts), requested=requested.candidate)


def _read_result(candidate: _CaptureCandidate) -> _BoundResult | None:
    result_path = candidate.artifact.parents[1] / "consumer.result.json"
    if not result_path.exists():
        return None
    if result_path.is_symlink() or not result_path.is_file():
        raise _ReadyCaptureCorruptError
    try:
        raw = result_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ReadyCaptureCorruptError from error
    if not isinstance(payload, dict):
        raise _ReadyCaptureCorruptError
    status = payload.get("status")
    exit_code = payload.get("exit_code")
    valid_status = type(exit_code) is int and (
        (status == "ready" and exit_code == 0)
        or (status == "partial" and exit_code == 4)
        or (status == "retryable" and exit_code in {_INTERNAL_ERROR_EXIT, _TEMPFAIL_EXIT})
        or (status == "failed" and exit_code not in {0, 4, _INTERNAL_ERROR_EXIT, _TEMPFAIL_EXIT})
    )
    if (
        payload.get("schema_version") != _RESULT_SCHEMA_VERSION
        or payload.get("run_id") != candidate.run_id
        or not isinstance(payload.get("source_commit"), str)
        or _FULL_SHA.fullmatch(payload["source_commit"]) is None
        or payload.get("capture_source_commit") != candidate.source_commit
        or payload.get("through_artifact_sha256") != candidate.artifact_sha256
        or type(payload.get("completed_artifacts")) is not int
        or payload["completed_artifacts"] < 0
        or not valid_status
    ):
        raise _ReadyCaptureCorruptError
    return _BoundResult(payload=payload, content_sha256=sha256(raw).hexdigest())


def _atomic_write_result(path: Path, payload: dict[str, object]) -> None:
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_candidate_result(
    *,
    candidate: _CaptureCandidate,
    source_commit: str,
    exit_code: int,
    completed_artifacts: int,
    blocked_by: _CaptureCandidate | None = None,
    blocked_exit_code: int | None = None,
) -> int:
    if exit_code == 0:
        status = "ready"
    elif exit_code == 4:
        status = "partial"
    elif exit_code in {_INTERNAL_ERROR_EXIT, _TEMPFAIL_EXIT}:
        status = "retryable"
    else:
        status = "failed"
    payload: dict[str, object] = {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "run_id": candidate.run_id,
        "source_commit": source_commit,
        "capture_source_commit": candidate.source_commit,
        "status": status,
        "exit_code": exit_code,
        "completed_artifacts": completed_artifacts,
        "through_artifact_sha256": candidate.artifact_sha256,
    }
    if blocked_by is not None:
        payload["blocked_by_run_id"] = blocked_by.run_id
        payload["blocked_exit_code"] = blocked_exit_code
    _atomic_write_result(
        candidate.artifact.parents[1] / "consumer.result.json",
        payload,
    )
    return exit_code


def _rebuild_cognition_mainline() -> dict[str, object]:
    """Follow a moved G Working Set identity after a successful ingest.

    Non-blocking reliability repair: the v1 read-model pins the Working Set
    identity at build time, so every content ingest moves the manifest identity
    and fails the cognition PIT gate closed.  Rebuild generation+1 when READY
    and drifted; any failure keeps the current artifact untouched and stays
    typed.  The result goes to an owner-only audit line, never to stdout (the
    consumer stdout contract stays a single JSON document), and never changes
    the ingest exit code.
    """

    home = Path.home()
    state_root = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
    data_root = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    readmodel_root = state_root / "fin-analyse" / "cognition-mainline-readmodel-v1"
    manifest = (
        data_root
        / "fin-analyse"
        / "shared"
        / "knowledge-base"
        / "runtime"
        / "operations"
        / "g_working_set"
        / "manifest.v1.json"
    )
    try:
        # 批注文档 = owner durable 数据，读 canonical KB 根（knowledge_root 缝），
        # 不再依赖仓布局（knowledge-base/ 被 .gitignore，fresh checkout 会丢）。
        # 解析留在 try 内：缝 fail-closed（KnowledgeRootConfigurationError）时
        # 仍走下方 typed 审计行，不在 wrapper 层丢可观测性。
        annotation = (
            default_knowledge_base_root()
            / "manual-annotations"
            / "g-cognition-mainline.md"
        )
        result = rebuild_if_stale(
            annotation_path=annotation,
            readmodel_root=readmodel_root,
            manifest_path=manifest,
        )
    except Exception as exc:  # noqa: BLE001 - typed, never blocks ingest
        result_dict: dict[str, object] = {
            "schema_version": _REBUILD_SCHEMA_VERSION,
            "disposition": "FAILED",
            "reason": f"rebuild_invocation_failed:{type(exc).__name__}",
        }
    else:
        result_dict = result.to_dict()
    # the audit sink must never block ingest
    with suppress(Exception):
        _append_rebuild_audit(result_dict, state_root / "fin-analyse")

    # 设计门 g-mainline-growth-v1 部件1：候选扫描与 rebuild 同位触发；纯读
    # index/KB，唯一写出是 state 下的候选草稿（0600 幂等重写）；typed、
    # 永不阻断 ingest、不改 rebuild 结果。
    try:
        scan = scan_mainline_candidates(
            annotation_path=annotation,
            readmodel_root=readmodel_root,
            index_path=default_knowledge_base_root() / "index.json",
        )
    except Exception as exc:  # noqa: BLE001 - typed, never blocks ingest
        scan_dict: dict[str, object] = {
            "schema_version": _CANDIDATES_SCHEMA_VERSION,
            "disposition": "FAILED",
            "reason": f"scan_invocation_failed:{type(exc).__name__}",
        }
    else:
        scan_dict = scan.to_dict()
    with suppress(Exception):
        _append_rebuild_audit(
            scan_dict,
            state_root / "fin-analyse",
            filename=_CANDIDATES_AUDIT_NAME,
        )
    return result_dict


def _append_rebuild_audit(
    payload: dict[str, object],
    state_root: Path,
    *,
    filename: str = "cognition-mainline-rebuild.v1.jsonl",
) -> None:
    """Append one content-free typed audit line (owner-only, mode 0600)."""

    log_path = state_root / filename
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(
                descriptor,
                (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            )
        finally:
            os.close(descriptor)
    except OSError:
        # The rebuild already stays typed; a missing audit sink must never
        # change the ingest exit code or hide the underlying rebuild result.
        return


def _stop_after_candidate(
    *,
    candidate: _CaptureCandidate,
    requested: _CaptureCandidate | None,
    source_commit: str,
    exit_code: int,
    completed_artifacts: int,
) -> int:
    _write_candidate_result(
        candidate=candidate,
        source_commit=source_commit,
        exit_code=exit_code,
        completed_artifacts=completed_artifacts,
    )
    if requested is not None and candidate.run_id != requested.run_id:
        return _write_candidate_result(
            candidate=requested,
            source_commit=source_commit,
            exit_code=_INTERNAL_ERROR_EXIT,
            completed_artifacts=completed_artifacts,
            blocked_by=candidate,
            blocked_exit_code=exit_code,
        )
    return exit_code


def _ingest_identity(candidate: _CaptureCandidate) -> dict[str, str]:
    return {
        "run_id": candidate.ingest_run_id,
        "file": candidate.artifact.name,
        "content_sha256": candidate.ingest_content_sha256,
    }


def _effective_ingest_exit(
    *,
    exit_code: int,
    payload: object,
    candidate: _CaptureCandidate,
) -> int:
    if not isinstance(payload, dict) or payload.get("schema_version") != _INGEST_SCHEMA_VERSION:
        return exit_code if exit_code != 0 else _INTERNAL_ERROR_EXIT
    exact_artifact = payload.get("artifact") == _ingest_identity(candidate)
    if exit_code == 0:
        if (
            exact_artifact
            and payload.get("status") in {"succeeded", "no_change"}
            and payload.get("completion_status") == "ready"
        ):
            return 0
        return _INTERNAL_ERROR_EXIT
    if exit_code == 4:
        if (
            exact_artifact
            and payload.get("status") in {"succeeded", "no_change"}
            and payload.get("completion_status") == "partial"
        ):
            return 4
        return _INTERNAL_ERROR_EXIT
    if exit_code != _DUPLICATE_EXIT or payload.get("status") != "duplicate":
        return exit_code
    original_exit = payload.get("original_exit_code")
    if not exact_artifact or type(original_exit) is not int:
        return _INTERNAL_ERROR_EXIT
    original_status = payload.get("original_status")
    original_completion = payload.get("original_completion_status")
    if original_exit == 0 and not (
        original_status in {"succeeded", "no_change"} and original_completion == "ready"
    ):
        return _INTERNAL_ERROR_EXIT
    if original_exit == 4 and not (
        original_status in {"succeeded", "no_change"} and original_completion == "partial"
    ):
        return _INTERNAL_ERROR_EXIT
    if original_exit == _DUPLICATE_EXIT:
        return _INTERNAL_ERROR_EXIT
    return original_exit


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = _parser()
    args = parser.parse_args(argv)
    runs_root = args.runs_root.expanduser()
    if not runs_root.is_absolute():
        raise SystemExit("--runs-root must be absolute")
    if runs_root.is_symlink():
        parser.error("--runs-root must not be a symlink")
    if _FULL_SHA.fullmatch(args.source_commit) is None:
        parser.error("--source-commit must be a lowercase 40-character SHA")
    if _RUN_ID.fullmatch(args.not_before_run_id) is None:
        parser.error("--not-before-run-id has invalid format")
    if args.run_id is not None and _RUN_ID.fullmatch(args.run_id) is None:
        parser.error("--run-id has invalid format")
    selection = _pending_artifacts(
        runs_root,
        source_commit=args.source_commit,
        not_before_run_id=args.not_before_run_id,
        run_id=args.run_id,
    )
    if selection.existing_result is not None:
        print(json.dumps(selection.existing_result, sort_keys=True))
        exit_code = selection.existing_result.get("exit_code")
        return exit_code if isinstance(exit_code, int) else _INTERNAL_ERROR_EXIT
    if selection.corrupt:
        print(json.dumps({"schema_version": _SCHEMA_VERSION, "status": "corrupt"}))
        if selection.requested is None:
            return _INTERNAL_ERROR_EXIT
        return _write_candidate_result(
            candidate=selection.requested,
            source_commit=args.source_commit,
            exit_code=_INTERNAL_ERROR_EXIT,
            completed_artifacts=0,
        )
    if not selection.artifacts:
        status = "unavailable" if args.run_id is not None else "idle"
        print(json.dumps({"schema_version": _SCHEMA_VERSION, "status": status}))
        return _TEMPFAIL_EXIT if args.run_id is not None else 0
    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "status": "pending",
                    "artifact": str(selection.artifacts[-1].artifact),
                }
            )
        )
        return 0
    completed_artifacts = 0
    for candidate in selection.artifacts:
        artifact = candidate.artifact
        try:
            artifact_unchanged = (
                sha256(artifact.read_bytes()).hexdigest() == candidate.artifact_sha256
            )
        except OSError:
            artifact_unchanged = False
        if not artifact_unchanged:
            return _stop_after_candidate(
                candidate=candidate,
                requested=selection.requested,
                source_commit=args.source_commit,
                exit_code=_INTERNAL_ERROR_EXIT,
                completed_artifacts=completed_artifacts,
            )
        captured_stdout = StringIO()
        with redirect_stdout(captured_stdout):
            exit_code = import_capture(
                [
                    "--artifact",
                    str(artifact),
                    "--trigger",
                    "schedule",
                    "--deadline-seconds",
                    "1200",
                ]
            )
        output = captured_stdout.getvalue()
        print(output, end="")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = None
        effective_exit = _effective_ingest_exit(
            exit_code=exit_code,
            payload=payload,
            candidate=candidate,
        )
        completed_artifacts += 1  # noqa: SIM113 — advance only after importer returns
        if effective_exit != 0:
            return _stop_after_candidate(
                candidate=candidate,
                requested=selection.requested,
                source_commit=args.source_commit,
                exit_code=effective_exit,
                completed_artifacts=completed_artifacts,
            )
        _write_candidate_result(
            candidate=candidate,
            source_commit=args.source_commit,
            exit_code=0,
            completed_artifacts=completed_artifacts,
        )
        # ingest 已成功；rebuild 失败只保持 cognition 掉线（typed gap），不改变消费结果。
        with suppress(Exception):
            _rebuild_cognition_mainline()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
