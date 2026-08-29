"""Private, owner-only diagnostics for failures in the Codex runtime adapter.

The public ``AgentRunResult`` intentionally exposes only stable gap codes.
This module preserves enough private evidence for an operator to distinguish
provider, configuration, transport, and network failures without persisting a
prompt, argv, stdout transcript, environment, or credential.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from fin_analyse.common.owner_only_snapshot import (
    OwnerOnlyJsonSnapshotFile,
    OwnerOnlySnapshotInspectionError,
    OwnerOnlySnapshotReason,
)
from fin_analyse.guo_teacher_research.codex_route_config import is_codex_route_id

_SCHEMA_VERSION = "fin.codex-runtime-failure-diagnostic/v4"
_V3_SCHEMA_VERSION = "fin.codex-runtime-failure-diagnostic/v3"
_INVOCATION_SCHEMA_VERSION = "fin.consultation-diagnostic-event/v2"
_SNAPSHOT_SCHEMA_VERSION = "fin.consultation-diagnostic-snapshot/v2"
_MAX_ARTIFACT_BYTES = 16 * 1024

# A1 stage 闭集: 调用语义决定 stage,不由 route 序号决定。
_STAGES = frozenset(
    {
        "initial_runtime",
        "resume_runtime",
        "fresh_after_resume",
        "state",
        "finalization",
    }
)

_ERROR_ID_PREFIX = "err_"
_ERROR_ID_HEX_BYTES = 16  # 128-bit
_ERROR_ID_PATTERN = re.compile(r"err_[0-9a-f]{32}")


def new_error_id() -> str:
    """Generate one sanitized 128-bit random error id: ``err_`` + 32 hex.

    Ids are never derived from prompt, exception text, principal, request id,
    attempt id, idempotency key, or session id; the same fault reproduced on a
    fresh execution receives a new id, while an exact replay keeps its own.
    """

    return _ERROR_ID_PREFIX + secrets.token_hex(_ERROR_ID_HEX_BYTES)
_MAX_RUNTIME_ERROR_EVENTS = 8
_MAX_JSONL_LINE_BYTES = 64 * 1024
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_ERROR_TYPES = frozenset(
    {
        "api_error",
        "authentication_error",
        "connection_error",
        "invalid_request_error",
        "model_error",
        "network_error",
        "payment_required_error",
        "quota_error",
        "rate_limit_error",
        "server_error",
        "timeout_error",
    }
)
_ALLOWED_ERROR_CODES = frozenset(
    {
        "connection_error",
        "insufficient_credit",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_auth",
        "model_not_found",
        "payment_required",
        "quota_exceeded",
        "rate_limit_exceeded",
        "service_unavailable",
        "timeout",
        "unauthorized",
        "upstream_error",
    }
)
_FAILOVER_DENIED_ERROR_IDENTIFIERS = frozenset(
    {
        "contract_violation",
        "invalid_request",
        "invalid_request_error",
        "policy_violation",
        "product_contract_violation",
        "safety_rejection",
        "schema_validation_error",
        "semantic_rejection",
        "tool_policy_violation",
    }
)
_FAILOVER_DENIED_IDENTIFIER_MARKERS = (
    "capability",
    "config",
    "contract",
    "policy",
    "safety",
    "schema",
    "semantic",
    "tool",
)
_CODEX_CLI_HTTP_FAILURE_PATTERN = re.compile(
    r"^(?:Reconnecting\.\.\. \d+/\d+ \()?unexpected status "
    r"(?P<status>[1-5]\d{2})\b.*\burl:\s*(?:https?|wss?)://\S+",
    re.IGNORECASE,
)
_EVENT_KIND_CLOSED_SET = frozenset(
    {
        "probe_failure",
        "spawn_failure",
        "timeout",
        "stall",
        "exit_failure",
        "parse_failure",
    }
)
# v4 判别式事件闭集(N1: 每类必选/可选/nullable 规则在 _build_artifact/_decode 实现)。
_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "occurred_at",
        "backend",
        "model",
        "event_kind",
        "error_id",
        "elapsed_seconds",
        "route",
        "stage",
        "exit_code",
        "failure_code",
        "failover_class",
        "stderr_sha256",
        "stderr_bytes",
        "stdout_sha256",
        "stdout_bytes",
        "truncated",
        "runtime_errors",
        "probe_origin",
        "http_status",
    }
)


class CodexRuntimeDiagnosticError(RuntimeError):
    """The private diagnostic could not be recorded safely."""


@dataclass(frozen=True, slots=True)
class CodexRuntimeFailureEvent:
    """Adapter-private failure details; raw stderr never enters a public result.

    v4: 判别式 event_kind 闭集,exit_code 按 kind 可空(probe/spawn/timeout/
    stall/parse 无整数退出码);stderr/stdout 仅供内存分类,绝不落盘。
    """

    occurred_at: datetime
    model: str
    stderr: str = field(repr=False)
    exit_code: int | None = None
    stdout: str = field(default="", repr=False)
    event_kind: str = "exit_failure"
    error_id: str = ""
    elapsed_seconds: float = 0.0
    route: str | None = None
    stage: str | None = None
    truncated: bool = False
    probe_origin: str | None = None
    http_status: int | None = None
    # N8:完整流字节数/hash 覆盖值(如 watchdog 异常携带);None 时由
    # stderr/stdout 文本计算。尾部缓冲只进内存分类,绝不落盘。
    stderr_bytes_total: int | None = None
    stderr_sha256_total: str | None = None
    stdout_bytes_total: int | None = None
    stdout_sha256_total: str | None = None


class CodexRuntimeDiagnosticSink(Protocol):
    """Private sink invoked after a Codex child exits non-zero."""

    def record(self, event: CodexRuntimeFailureEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class CodexRuntimeInvocationEvent:
    """Adapter-private invocation trace; no child text ever enters it.

    The last-invocation snapshot records ``started`` before the child runs
    and ``terminated`` after it returns, so a hung call is observable even
    though no failure diagnostic is ever written.

    A1 扩展: stage 闭集区分 initial_runtime / resume_runtime /
    fresh_after_resume / state / finalization;失败事件必须携带稳定
    classifier 与 error_id;started 事件只用于真实 runtime child。
    """

    phase: Literal["started", "terminated"]
    occurred_at: datetime
    model: str
    elapsed_seconds: float = 0.0
    exit_code: int | None = None
    status: str | None = None
    stage: Literal[
        "initial_runtime",
        "resume_runtime",
        "fresh_after_resume",
        "state",
        "finalization",
    ] = "initial_runtime"
    route: str | None = None
    classifier: str | None = None
    failover_classifier: str | None = None
    error_id: str | None = None


class CodexRuntimeInvocationSink(Protocol):
    """Private sink recording one invocation's start and terminal phases."""

    def record(self, event: CodexRuntimeInvocationEvent) -> None: ...


class OwnerOnlyCodexRuntimeDiagnosticSink:
    """Atomically replace one owner-only last-failure diagnostic snapshot."""

    def __init__(
        self,
        *,
        target: Path,
        forbidden_root: Path,
        max_bytes: int = _MAX_ARTIFACT_BYTES,
    ) -> None:
        if not target.is_absolute():
            raise ValueError("diagnostic target must be absolute")
        if max_bytes <= 0 or max_bytes > _MAX_ARTIFACT_BYTES:
            raise ValueError("diagnostic max_bytes is invalid")
        canonical_target = Path(os.path.abspath(target))
        canonical_forbidden_root = forbidden_root.resolve()
        if (
            canonical_target == canonical_forbidden_root
            or canonical_forbidden_root in canonical_target.parents
        ):
            raise ValueError("diagnostic target must be outside the project checkout")
        self._snapshot = OwnerOnlyJsonSnapshotFile(
            target=canonical_target,
            forbidden_root=canonical_forbidden_root,
            max_bytes=max_bytes,
        )
        self._max_bytes = max_bytes

    def record(self, event: CodexRuntimeFailureEvent) -> None:
        artifact = _build_artifact(event)
        payload = (
            json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > self._max_bytes:
            raise CodexRuntimeDiagnosticError("diagnostic artifact exceeds its bound")

        with TemporaryDirectory(prefix="fin-codex-runtime-diagnostic-") as temporary:
            temporary_root = Path(temporary)
            os.chmod(temporary_root, 0o700)
            source = temporary_root / "candidate.json"
            _write_owner_only_source(source, payload)
            for _attempt in range(2):
                # 旧 v2 平面快照由 lenient current-decode 精确识别,首个 v3 写入
                # 直接原子替换(不双读、不迁移、不保留兼容层);candidate 恒为 v3 严格。
                try:
                    inspection = self._snapshot.inspect(
                        source=source,
                        decode_candidate=_decode_artifact,
                        decode_current=_decode_artifact_lenient,
                    )
                except OwnerOnlySnapshotInspectionError as error:
                    raise CodexRuntimeDiagnosticError(
                        f"diagnostic inspection rejected:{error.reason.value}"
                    ) from None
                publication = self._snapshot.publish(
                    source=source,
                    candidate_revision=inspection.candidate.revision,
                    expected_current_revision=(
                        "MISSING" if inspection.current is None else inspection.current.revision
                    ),
                    apply=True,
                    decode_candidate=_decode_artifact,
                    decode_current=_decode_artifact_lenient,
                )
                if publication.status in {"PUBLISHED", "EXACT_REPLAY"}:
                    return
                if publication.reason is not OwnerOnlySnapshotReason.CAS_MISMATCH:
                    reason = (
                        publication.reason.value if publication.reason is not None else "UNKNOWN"
                    )
                    raise CodexRuntimeDiagnosticError(f"diagnostic publication rejected:{reason}")
            raise CodexRuntimeDiagnosticError("diagnostic publication raced")


class OwnerOnlyCodexRuntimeInvocationSink:
    """Atomically replace one owner-only last-invocation snapshot.

    Written before the child runs (``started``) and after it returns
    (``terminated``); a hung call leaves the snapshot at ``started`` with the
    elapsed time, making the stall observable without any child text.
    """

    def __init__(
        self,
        *,
        target: Path,
        forbidden_root: Path,
        max_bytes: int = _MAX_ARTIFACT_BYTES,
    ) -> None:
        if not target.is_absolute():
            raise ValueError("diagnostic target must be absolute")
        if max_bytes <= 0 or max_bytes > _MAX_ARTIFACT_BYTES:
            raise ValueError("diagnostic max_bytes is invalid")
        canonical_target = Path(os.path.abspath(target))
        canonical_forbidden_root = forbidden_root.resolve()
        if (
            canonical_target == canonical_forbidden_root
            or canonical_forbidden_root in canonical_target.parents
        ):
            raise ValueError("diagnostic target must be outside the project checkout")
        self._snapshot = OwnerOnlyJsonSnapshotFile(
            target=canonical_target,
            forbidden_root=canonical_forbidden_root,
            max_bytes=max_bytes,
        )
        self._max_bytes = max_bytes

    def record(self, event: CodexRuntimeInvocationEvent) -> None:
        # 事件先独立校验,再组装 v2 envelope(current + last_failure)。
        _build_invocation_event(event)
        envelope = _build_invocation_snapshot(event, previous=None)
        payload = (
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > self._max_bytes:
            raise CodexRuntimeDiagnosticError("diagnostic artifact exceeds its bound")

        with TemporaryDirectory(prefix="fin-consultation-diagnostic-") as temporary:
            temporary_root = Path(temporary)
            os.chmod(temporary_root, 0o700)
            source = temporary_root / "candidate.json"
            _write_owner_only_source(source, payload)
            for _attempt in range(2):
                # CAS 语义: envelope 必须基于与 expected_current_revision 同一
                # 次 inspection 的 current 构造。并发写入在 publish 内被
                # CAS_MISMATCH 拒绝后,循环重新读取并基于新 current 重建;
                # 绝不用新 current 的 revision 发布基于旧 current 的 candidate。
                try:
                    inspection = self._snapshot.inspect(
                        source=source,
                        decode_candidate=_decode_invocation_artifact,
                        decode_current=_decode_invocation_lenient,
                    )
                except OwnerOnlySnapshotInspectionError as error:
                    raise CodexRuntimeDiagnosticError(
                        f"diagnostic inspection rejected:{error.reason.value}"
                    ) from None
                previous = inspection.current.value if inspection.current is not None else None
                envelope = _build_invocation_snapshot(event, previous=previous)
                # 旧 v1 平面快照或 MISSING: 直接以新 envelope 原子替换。
                payload = (
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                _replace_owner_only_source(source, payload)
                candidate_inspection = self._snapshot.inspect(
                    source=source,
                    decode_candidate=_decode_invocation_artifact,
                    decode_current=_decode_invocation_lenient,
                )
                publication = self._snapshot.publish(
                    source=source,
                    candidate_revision=candidate_inspection.candidate.revision,
                    expected_current_revision=(
                        "MISSING" if inspection.current is None else inspection.current.revision
                    ),
                    apply=True,
                    decode_candidate=_decode_invocation_artifact,
                    decode_current=_decode_invocation_lenient,
                )
                if publication.status in {"PUBLISHED", "EXACT_REPLAY"}:
                    return
                if publication.reason is not OwnerOnlySnapshotReason.CAS_MISMATCH:
                    reason = (
                        publication.reason.value if publication.reason is not None else "UNKNOWN"
                    )
                    raise CodexRuntimeDiagnosticError(f"diagnostic publication rejected:{reason}")
            raise CodexRuntimeDiagnosticError("diagnostic publication raced")


_INVOCATION_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "phase",
        "occurred_at",
        "backend",
        "model",
        "elapsed_seconds",
    }
)
# A1 严格闭集: 拒绝任何未列字段,防止 prompt/token/身份等文本越界持久化。
_INVOCATION_ALLOWED_FIELDS = _INVOCATION_REQUIRED_FIELDS | frozenset(
    {
        "status",
        "route",
        "classifier",
        "failover_classifier",
        "exit_code",
        "error_id",
    }
)
# classifier 闭集: 现有 runtime classifier + A1 固定分类 + 未知统一码。
_CLASSIFIER_CLOSED_SET = frozenset(
    {
        "CODEX_CHILD_AUTH",
        "CODEX_CHILD_CONFIG",
        "CODEX_CHILD_MCP",
        "CODEX_CHILD_MODEL",
        "CODEX_CHILD_NETWORK",
        "CODEX_CHILD_NONZERO_UNKNOWN",
        "CODEX_CHILD_RATE_LIMIT",
        "CODEX_CHILD_UNCLASSIFIED_EXIT",
        "CODEX_CHILD_UPSTREAM",
        "CODEX_CHILD_STALL",
        "resume_before_activity_failed",
        "internal_error",
        "codex_probe_failed",
        # A1 state 技术故障 classifier(semantic_service._STATE_CLASSIFIER_ALLOWLIST
        # 的值必须全部落在本闭集内;新增 state code 时两侧同步)。
        "semantic_state_corrupt",
        "semantic_state_epoch_unsupported",
        "semantic_state_schema_unsupported",
        "semantic_state_unavailable",
        "semantic_state_insecure",
        "semantic_state_identity_changed",
        "chain_closed",
        "continuation_conflict",
        "continuation_not_accessible",
        "idempotency_conflict",
        "product_version_not_found",
        "research_in_progress",
        "route_conflict",
        "route_revision_conflict",
        "runtime_handle_invalid",
        "runtime_unavailable",
        "turn_fencing_conflict",
        "turn_fencing_required",
        "turn_lease_expired",
        "turn_lease_held",
        "forbidden_product_field",
    }
)
def _decode_invocation_event(payload: bytes) -> Mapping[str, object]:
    """Decode one v2 consultation-diagnostic event with strict closed-set checks."""

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("diagnostic artifact is invalid") from error
    if (
        not isinstance(decoded, dict)
        or not _INVOCATION_REQUIRED_FIELDS.issubset(decoded)
        or not _INVOCATION_ALLOWED_FIELDS.issuperset(decoded)
    ):
        # 事件键是动态的(可选字段可缺);required 必含、allowed 拒绝额外。
        raise ValueError("diagnostic artifact is invalid")
    occurred_at = decoded.get("occurred_at")
    try:
        parsed_occurred_at = (
            datetime.fromisoformat(occurred_at) if isinstance(occurred_at, str) else None
        )
    except ValueError:
        parsed_occurred_at = None
    if (
        decoded.get("schema_version") != _INVOCATION_SCHEMA_VERSION
        or decoded.get("backend") != "codex"
        or decoded.get("stage") not in _STAGES
        or decoded.get("phase") not in {"started", "terminated"}
        or parsed_occurred_at is None
        or parsed_occurred_at.tzinfo is None
    ):
        raise ValueError("diagnostic artifact is invalid")
    model = decoded.get("model")
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("diagnostic artifact is invalid")
    elapsed = decoded.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        raise ValueError("diagnostic artifact is invalid")
    status = decoded.get("status")
    if status is not None and status not in {"succeeded", "failed"}:
        raise ValueError("diagnostic artifact is invalid")
    for field_name in ("classifier", "failover_classifier"):
        value = decoded.get(field_name)
        if value is not None and value not in _CLASSIFIER_CLOSED_SET:
            raise ValueError("diagnostic artifact is invalid")
    route = decoded.get("route")
    if route is not None and not is_codex_route_id(route):
        raise ValueError("diagnostic artifact is invalid")
    error_id = decoded.get("error_id")
    if error_id is not None and (
        not isinstance(error_id, str) or not _ERROR_ID_PATTERN.fullmatch(error_id)
    ):
        raise ValueError("diagnostic artifact is invalid")
    exit_code = decoded.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -255 <= exit_code <= 255
    ):
        raise ValueError("diagnostic artifact is invalid")
    if decoded.get("phase") == "started":
        # started 仅用于真实 runtime child,失败相关字段必须为 null。
        for field_name in ("status", "classifier", "failover_classifier", "exit_code", "error_id"):
            if decoded.get(field_name) is not None:
                raise ValueError("diagnostic artifact is invalid")
    elif status == "failed":
        # failed event 必须有 classifier 与 error_id;exit_code 可为真实值。
        if not isinstance(decoded.get("classifier"), str) or not decoded.get("classifier"):
            raise ValueError("diagnostic artifact is invalid")
        if not isinstance(decoded.get("error_id"), str) or not decoded.get("error_id"):
            raise ValueError("diagnostic artifact is invalid")
    elif status == "succeeded":
        # succeeded 事件不得携带失败字段;真实 child 成功退出 exit 必须为 0。
        for field_name in ("classifier", "failover_classifier", "error_id"):
            if decoded.get(field_name) is not None:
                raise ValueError("diagnostic artifact is invalid")
        if exit_code not in (0, None):
            raise ValueError("diagnostic artifact is invalid")
    else:
        # terminated 必须带 status;started 已在上方分支处理。
        raise ValueError("diagnostic artifact is invalid")
    return decoded


# v2 envelope 顶层严格字段闭集: 拒绝任何未列字段。
_ENVELOPE_ALLOWED_FIELDS = frozenset({"schema_version", "current", "last_failure"})


_EVIDENCE_MAX_ENTRIES = 20
_EVIDENCE_MAX_TOTAL_BYTES = 5 * 1024 * 1024
_EVIDENCE_TTL_SECONDS = 30 * 24 * 3600
_EVIDENCE_LOCK_NAME = ".evidence.lock"


class EvidenceCollectionSink:
    """多条目脱敏失败证据集合(N2: 20 条 / 5MB / 30 天 / 跨进程锁)。

    条目:``<ts>-<error_id>.json``(0600,目录 0700);写入经临时 staging +
    rename_noreplace(不覆盖);配额/TTL 清理在跨进程文件锁内串行执行,
    只删除通过 owner-regular 校验的过期/超限条目。诊断 best-effort,
    任何失败不影响调用结果。
    """

    def __init__(
        self,
        *,
        root: Path,
        forbidden_root: Path,
        max_entries: int = _EVIDENCE_MAX_ENTRIES,
        max_total_bytes: int = _EVIDENCE_MAX_TOTAL_BYTES,
        ttl_seconds: int = _EVIDENCE_TTL_SECONDS,
    ) -> None:
        if not root.is_absolute():
            raise CodexRuntimeDiagnosticError("evidence root must be absolute")
        canonical_root = root.resolve()
        canonical_forbidden = forbidden_root.resolve()
        if canonical_root == canonical_forbidden or canonical_forbidden in canonical_root.parents:
            raise CodexRuntimeDiagnosticError("evidence root must be outside the project checkout")
        self._root = canonical_root
        self._forbidden_root = canonical_forbidden
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds

    def record(self, event: CodexRuntimeFailureEvent) -> None:
        import fcntl

        from fin_analyse.common.owner_only_collection import (
            OwnerOnlyCollectionError,
            rename_noreplace,
            require_owner_directory,
            verify_owner_regular,
        )

        artifact = _build_artifact(event)
        entry_name = (
            f"{event.occurred_at.astimezone(UTC).strftime('%Y%m%dT%H%M%S%f')}"
            f"-{event.error_id or 'noerrorid'}.json"
        )
        payload = (
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > _MAX_ARTIFACT_BYTES:
            raise CodexRuntimeDiagnosticError("diagnostic artifact exceeds its bound")
        try:
            directory_fd = require_owner_directory(self._root, create=True)
        except (OwnerOnlyCollectionError, OSError):
            return  # best-effort
        staging_name = ""
        try:
            lock_path = self._root / _EVIDENCE_LOCK_NAME
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            os.fchmod(lock_fd, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                now = time.time()
                # 锁内:TTL 清理 + 条目数清理(最旧),再容量准入。
                names = [
                    name
                    for name in os.listdir(directory_fd)
                    if name != _EVIDENCE_LOCK_NAME and name.endswith(".json")
                ]
                stats: dict[str, os.stat_result] = {}
                for name in names:
                    try:
                        stats[name] = verify_owner_regular(
                            directory_fd, name, max_bytes=_MAX_ARTIFACT_BYTES
                        )
                    except (OwnerOnlyCollectionError, OSError):
                        continue
                for name, metadata in sorted(
                    stats.items(), key=lambda item: item[1].st_mtime
                ):
                    if now - metadata.st_mtime > self._ttl_seconds:
                        with suppress(OSError):
                            os.unlink(name, dir_fd=directory_fd)
                        stats.pop(name, None)
                if len(stats) >= self._max_entries:
                    return  # 条目数超限:拒绝新写入,不淘汰历史(audit r3-5)
                if (
                    sum(metadata.st_size for metadata in stats.values()) + len(payload)
                    > self._max_total_bytes
                ):
                    return  # 容量超限:拒绝新写入(design 冻结语义)
                staging_name = f".staging-{os.getpid()}-{secrets.token_hex(4)}"
                staging_fd = os.open(
                    staging_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    dir_fd=directory_fd,
                )
                os.fchmod(staging_fd, 0o600)
                try:
                    with os.fdopen(staging_fd, "wb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    raise
                try:
                    rename_noreplace(
                        directory_fd,
                        staging_name,
                        directory_fd,
                        entry_name,
                    )
                except FileExistsError:
                    return  # 同秒同 error_id 重复发布:幂等跳过
            finally:
                with suppress(OSError):
                    os.close(lock_fd)
        except (OwnerOnlyCollectionError, OSError, ValueError):
            return  # best-effort
        finally:
            if staging_name:
                with suppress(OSError):
                    os.unlink(staging_name, dir_fd=directory_fd)
            with suppress(OSError):
                os.close(directory_fd)


    def _enforce_quota(self, directory_fd, *, read_owner_regular, verify_owner_regular,
                       rename_noreplace, owner_only_collection_error) -> None:
        import fcntl
        import time as _time

        lock_path = self._root / _EVIDENCE_LOCK_NAME
        lock_fd: int | None = None
        try:
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0))
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            entries: list[tuple[str, os.stat_result, float, int]] = []
            total = 0
            for name in os.listdir(self._root):
                if name == _EVIDENCE_LOCK_NAME or not name.endswith(".json"):
                    continue
                try:
                    metadata = verify_owner_regular(directory_fd, name, max_bytes=_MAX_ARTIFACT_BYTES)
                except (owner_only_collection_error, OSError):
                    continue
                size = metadata.st_size
                mtime = metadata.st_mtime
                entries.append((name, metadata, mtime, size))
                total += size
            now = _time.time()
            # TTL 过期优先清理,再按最旧清理超量/超容
            entries.sort(key=lambda item: (item[2], item[0]))
            keep: list[tuple[str, os.stat_result, float, int]] = []
            for name, metadata, mtime, size in entries:
                if now - mtime > self._ttl_seconds:
                    with suppress(OSError):
                        os.unlink(self._root / name)
                    continue
                keep.append((name, metadata, mtime, size))
            while (
                len(keep) > self._max_entries
                or sum(item[3] for item in keep) > self._max_total_bytes
            ):
                name, _, _, _ = keep.pop(0)
                with suppress(OSError):
                    os.unlink(self._root / name)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)


def _decode_invocation_artifact(payload: bytes) -> Mapping[str, object]:
    """Decode the v2 snapshot envelope (current + last_failure)."""

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("diagnostic artifact is invalid") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema_version") != _SNAPSHOT_SCHEMA_VERSION
        or frozenset(decoded) != _ENVELOPE_ALLOWED_FIELDS
    ):
        # 精确键集合: current/last_failure 均必须存在,拒绝额外字段。
        raise ValueError("diagnostic artifact is invalid")
    current = decoded.get("current")
    if not isinstance(current, dict):
        raise ValueError("diagnostic artifact is invalid")
    # 事件本身以紧凑序列化校验;envelope 不重复存储 schema_version。
    current_payload = json.dumps(
        current,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _decode_invocation_event(current_payload)
    last_failure = decoded.get("last_failure")
    if last_failure is not None:
        if not isinstance(last_failure, dict):
            raise ValueError("diagnostic artifact is invalid")
        failure_payload = json.dumps(
            last_failure,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        # last_failure 必须是 failed 事件,不得是 succeeded/started。
        failure_event = _decode_invocation_event(failure_payload)
        if failure_event.get("status") != "failed":
            raise ValueError("diagnostic artifact is invalid")
    return decoded


# 精确旧 v1 平面 invocation 快照 schema(一次性识别,允许首个 v2 原子替换)。
# 字段为精确闭集: 额外字段(如 prompt)一律拒绝。
_V1_INVOCATION_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "phase",
        "occurred_at",
        "backend",
        "model",
        "elapsed_seconds",
    }
)
_V1_INVOCATION_ALLOWED_FIELDS = _V1_INVOCATION_REQUIRED_FIELDS | frozenset(
    {"exit_code", "status"}
)


def _decode_invocation_lenient(payload: bytes) -> Mapping[str, object]:
    """Lenient current-decode for CAS publication.

    v2 envelope 走严格校验;旧 v1 平面快照用精确 v1 schema 一次性识别,
    允许被首个 v2 写入直接原子替换(设计: 不双读、不迁移、不保留兼容层)。
    其他任意对象一律拒绝,不提供宽松平面解析。
    """

    try:
        return _decode_invocation_artifact(payload)
    except ValueError:
        pass
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("diagnostic artifact is invalid") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema_version") != "fin.codex-runtime-invocation-diagnostic/v1"
        or not _V1_INVOCATION_REQUIRED_FIELDS.issubset(decoded)
        or not _V1_INVOCATION_ALLOWED_FIELDS.issuperset(decoded)
        or decoded.get("backend") != "codex"
        or decoded.get("phase") not in {"started", "terminated"}
        or not isinstance(decoded.get("occurred_at"), str)
    ):
        raise ValueError("diagnostic artifact is invalid")
    model = decoded.get("model")
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("diagnostic artifact is invalid")
    elapsed = decoded.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        raise ValueError("diagnostic artifact is invalid")
    status = decoded.get("status")
    if status is not None and (not isinstance(status, str) or not status):
        raise ValueError("diagnostic artifact is invalid")
    exit_code = decoded.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -255 <= exit_code <= 255
    ):
        raise ValueError("diagnostic artifact is invalid")
    return decoded


def _build_invocation_event(event: CodexRuntimeInvocationEvent) -> dict[str, object]:
    if (
        not isinstance(event.occurred_at, datetime)
        or event.occurred_at.tzinfo is None
        or event.occurred_at.utcoffset() is None
    ):
        raise CodexRuntimeDiagnosticError("diagnostic timestamp is invalid")
    if event.phase not in {"started", "terminated"}:
        raise CodexRuntimeDiagnosticError("diagnostic phase is invalid")
    if event.stage not in _STAGES:
        raise CodexRuntimeDiagnosticError("diagnostic stage is invalid")
    if event.phase == "started" and (
        event.status is not None
        or event.classifier is not None
        or event.failover_classifier is not None
        or event.exit_code is not None
        or event.error_id is not None
    ):
        raise CodexRuntimeDiagnosticError("started event carries failure fields")
    if event.phase == "terminated" and event.status is None:
        raise CodexRuntimeDiagnosticError("terminated event needs status")
    if event.status == "failed" and (
        not isinstance(event.classifier, str) or not event.classifier
        or not isinstance(event.error_id, str) or not event.error_id
    ):
        raise CodexRuntimeDiagnosticError("failed event needs classifier and error id")
    if event.status == "succeeded" and (
        event.classifier is not None
        or event.failover_classifier is not None
        or event.error_id is not None
    ):
        raise CodexRuntimeDiagnosticError("succeeded event carries failure fields")
    elapsed = event.elapsed_seconds
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        raise CodexRuntimeDiagnosticError("diagnostic elapsed time is invalid")
    artifact: dict[str, object] = {
        "schema_version": _INVOCATION_SCHEMA_VERSION,
        "stage": event.stage,
        "phase": event.phase,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "backend": "codex",
        "model": event.model if _MODEL_PATTERN.fullmatch(event.model) else "unknown",
        "elapsed_seconds": elapsed,
    }
    if event.exit_code is not None:
        if (
            not isinstance(event.exit_code, int)
            or isinstance(event.exit_code, bool)
            or not -255 <= event.exit_code <= 255
        ):
            raise CodexRuntimeDiagnosticError("diagnostic exit code is invalid")
        artifact["exit_code"] = event.exit_code
    if event.status is not None:
        if event.status not in {"succeeded", "failed"}:
            raise CodexRuntimeDiagnosticError("diagnostic status is invalid")
        artifact["status"] = event.status
    if event.route is not None:
        if not is_codex_route_id(event.route):
            raise CodexRuntimeDiagnosticError("diagnostic route is invalid")
        artifact["route"] = event.route
    if event.classifier is not None:
        if event.classifier not in _CLASSIFIER_CLOSED_SET:
            raise CodexRuntimeDiagnosticError("diagnostic classifier is invalid")
        artifact["classifier"] = event.classifier
    if event.failover_classifier is not None:
        if event.failover_classifier not in _CLASSIFIER_CLOSED_SET:
            raise CodexRuntimeDiagnosticError("diagnostic failover classifier is invalid")
        artifact["failover_classifier"] = event.failover_classifier
    if event.error_id is not None:
        if not isinstance(event.error_id, str) or not _ERROR_ID_PATTERN.fullmatch(event.error_id):
            raise CodexRuntimeDiagnosticError("diagnostic error id is invalid")
        artifact["error_id"] = event.error_id
    return artifact


def _build_invocation_snapshot(
    event: CodexRuntimeInvocationEvent,
    *,
    previous: Mapping[str, object] | None,
) -> dict[str, object]:
    """Build the v2 envelope; a failed event also becomes the last_failure."""

    current = _build_invocation_event(event)
    last_failure: object = None
    if previous is not None:
        previous_failure = previous.get("last_failure")
        # 只保留合法 failed 事件;恶意/非 failed 内容绝不原样透传。
        if isinstance(previous_failure, dict) and previous_failure.get("status") == "failed":
            last_failure = previous_failure
    if event.status == "failed":
        last_failure = current
    return {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "current": current,
        "last_failure": last_failure,
    }




def _build_artifact(event: CodexRuntimeFailureEvent) -> dict[str, object]:
    """v4 判别式事件序列化(N1 字段矩阵)。

    - event_kind 闭集校验;
    - exit_code: exit_failure 必填 int;其他 kind 可为 None(非 None 也校验范围);
    - probe_origin/http_status: probe_failure 时至少一个非 None,其他 kind 必须
      为 None(跨字段约束);
    - error_id/elapsed_seconds/route/stage/truncated 按闭集写入;
    - stderr/stdout 只进内存分类,摘要只存 sha256/bytes(N1/F1)。
    """

    if (
        not isinstance(event.occurred_at, datetime)
        or event.occurred_at.tzinfo is None
        or event.occurred_at.utcoffset() is None
    ):
        raise CodexRuntimeDiagnosticError("diagnostic timestamp is invalid")
    if event.event_kind not in _EVENT_KIND_CLOSED_SET:
        raise CodexRuntimeDiagnosticError("diagnostic event kind is invalid")
    if event.event_kind == "exit_failure":
        if (
            not isinstance(event.exit_code, int)
            or isinstance(event.exit_code, bool)
            or not -255 <= event.exit_code <= 255
        ):
            raise CodexRuntimeDiagnosticError("diagnostic exit code is invalid")
    elif event.exit_code is not None and (
        not isinstance(event.exit_code, int)
        or isinstance(event.exit_code, bool)
        or not -255 <= event.exit_code <= 255
    ):
        raise CodexRuntimeDiagnosticError("diagnostic exit code is invalid")
    resolved_error_id = event.error_id
    if not isinstance(resolved_error_id, str):
        raise CodexRuntimeDiagnosticError("diagnostic error id is invalid")
    if not resolved_error_id:
        resolved_error_id = new_error_id()
    elif not _ERROR_ID_PATTERN.fullmatch(resolved_error_id):
        raise CodexRuntimeDiagnosticError("diagnostic error id is invalid")
    if (
        not isinstance(event.elapsed_seconds, (int, float))
        or isinstance(event.elapsed_seconds, bool)
        or not math.isfinite(event.elapsed_seconds)
        or event.elapsed_seconds < 0
    ):
        raise CodexRuntimeDiagnosticError("diagnostic elapsed seconds is invalid")
    if event.route is not None and not is_codex_route_id(event.route):
        raise CodexRuntimeDiagnosticError("diagnostic route is invalid")
    if event.stage is not None and event.stage not in _STAGES:
        raise CodexRuntimeDiagnosticError("diagnostic stage is invalid")
    if not isinstance(event.truncated, bool):
        raise CodexRuntimeDiagnosticError("diagnostic truncated is invalid")
    if event.event_kind == "probe_failure":
        if event.probe_origin is None and event.http_status is None:
            raise CodexRuntimeDiagnosticError(
                "diagnostic probe failure requires origin or http status"
            )
    elif event.probe_origin is not None or event.http_status is not None:
        raise CodexRuntimeDiagnosticError("diagnostic probe fields only for probe_failure")
    if event.probe_origin is not None and (
        not isinstance(event.probe_origin, str)
        or len(event.probe_origin) > 128
    ):
        raise CodexRuntimeDiagnosticError("diagnostic probe origin is invalid")
    if event.http_status is not None and (
        not isinstance(event.http_status, int)
        or isinstance(event.http_status, bool)
        or not 100 <= event.http_status <= 599
    ):
        raise CodexRuntimeDiagnosticError("diagnostic http status is invalid")
    stderr = event.stderr if isinstance(event.stderr, str) else ""
    stdout = event.stdout if isinstance(event.stdout, str) else ""
    stderr_bytes = stderr.encode("utf-8", errors="replace")
    stdout_bytes = stdout.encode("utf-8", errors="replace")
    runtime_errors = _extract_runtime_errors(stdout)
    stderr_total = (
        event.stderr_bytes_total
        if event.stderr_bytes_total is not None and event.stderr_bytes_total >= 0
        else len(stderr_bytes)
    )
    stdout_total = (
        event.stdout_bytes_total
        if event.stdout_bytes_total is not None and event.stdout_bytes_total >= 0
        else len(stdout_bytes)
    )
    stderr_digest = (
        event.stderr_sha256_total
        if isinstance(event.stderr_sha256_total, str) and _SHA256_PATTERN.fullmatch(event.stderr_sha256_total)
        else hashlib.sha256(stderr_bytes).hexdigest()
    )
    stdout_digest = (
        event.stdout_sha256_total
        if isinstance(event.stdout_sha256_total, str) and _SHA256_PATTERN.fullmatch(event.stdout_sha256_total)
        else hashlib.sha256(stdout_bytes).hexdigest()
    )
    if event.event_kind == "exit_failure":
        failure_code = classify_codex_runtime_failure(
            stderr,
            stdout,
            exit_code=event.exit_code,
        )
        failover_class = classify_codex_failover_failure(
            exit_code=event.exit_code,  # type: ignore[arg-type]
            stdout=stdout,
        )
    else:
        failure_code = classify_codex_runtime_failure(
            stderr,
            stdout,
            exit_code=event.exit_code,
        )
        failover_class = None
    return {
        "schema_version": _SCHEMA_VERSION,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "backend": "codex",
        "model": event.model if _MODEL_PATTERN.fullmatch(event.model) else "unknown",
        "event_kind": event.event_kind,
        "error_id": resolved_error_id,
        "elapsed_seconds": event.elapsed_seconds,
        "route": event.route,
        "stage": event.stage,
        "exit_code": event.exit_code,
        "failure_code": failure_code,
        "failover_class": failover_class,
        "stderr_sha256": stderr_digest,
        "stderr_bytes": stderr_total,
        "stdout_sha256": stdout_digest,
        "stdout_bytes": stdout_total,
        "truncated": event.truncated,
        "runtime_errors": runtime_errors,
        "probe_origin": event.probe_origin,
        "http_status": event.http_status,
    }


def classify_codex_runtime_failure(
    stderr: str,
    stdout: str,
    *,
    exit_code: int | None = None,
) -> str:
    # Classification is deliberately in-memory only.  Child text can contain
    # the user's complete prompt, account or portfolio, so no substring of it
    # may enter the persisted diagnostic.
    # A child may have already emitted a structured FIN denial before a
    # supervisor kills it.  Preserve that fail-closed semantic result instead
    # of mislabelling it as a retryable process failure.
    structured_denial = _classify_structured_failover_denial(stdout)
    if structured_denial is not None:
        return structured_denial
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code < 0:
        return "CODEX_CHILD_UNCLASSIFIED_EXIT"
    combined = stderr + "\n" + _runtime_error_text(stdout)
    text_class = _classify_failure_text(combined)
    if text_class in {"CODEX_CHILD_MCP", "CODEX_CHILD_CONFIG"}:
        return text_class
    structured_class = _classify_structured_runtime_error(stdout)
    if structured_class != "CODEX_CHILD_NONZERO_UNKNOWN":
        return structured_class
    return text_class


def classify_codex_failover_failure(
    *,
    exit_code: int,
    stdout: str,
) -> str:
    """Classify a failed child for FIN's availability-only route transition.

    Arbitrary child text cannot establish a semantic/safety denial or an
    availability category.  The narrow exception is Codex CLI's own JSONL
    error envelope when reconnect and terminal events consistently identify
    HTTP 429 or 5xx from a provider URL without conflicting denial text.  A
    non-zero child with no such stable outcome is `CODEX_CHILD_UNCLASSIFIED_EXIT`
    and remains on its original route.  Structured safety, semantic, contract,
    tool, configuration and capability failures remain fail-closed.
    """

    denial_class = _classify_structured_failover_denial(stdout)
    if denial_class is not None:
        return denial_class
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code < 0:
        return "CODEX_CHILD_UNCLASSIFIED_EXIT"
    cli_http_class = _classify_codex_cli_http_failure(stdout)
    if cli_http_class != "CODEX_CHILD_NONZERO_UNKNOWN":
        return cli_http_class
    return "CODEX_CHILD_UNCLASSIFIED_EXIT"


def _classify_codex_cli_http_failure(stdout: str) -> str:
    """Recognize Codex CLI's typed event envelope for provider HTTP failures."""

    classifications: list[str] = []
    terminal_class: str | None = None
    saw_reconnect = False
    for line in stdout.splitlines():
        if not line.strip() or len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "item.failed":
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        if record.get("type") not in {"error", "turn.failed"}:
            continue
        error = record.get("error")
        if isinstance(error, dict) and any(key in error for key in ("type", "code", "status")):
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        message = error.get("message") if isinstance(error, dict) else record.get("message")
        if not isinstance(message, str):
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        event_type = record["type"]
        is_reconnect = message.casefold().startswith("reconnecting... ")
        # 只有 turn.failed 携带 reconnect 前缀才是矛盾形态。真实 Codex CLI
        # 在重连耗尽后会打印无前缀的 terminal error；它仍须通过下方严格的
        # status/url/denial 检查，且必须有至少一个 reconnect 事件与一致的
        # terminal 分类才会授权 failover。
        if event_type == "turn.failed" and is_reconnect:
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        saw_reconnect |= is_reconnect
        match = _CODEX_CLI_HTTP_FAILURE_PATTERN.match(message)
        if match is None:
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        status = int(match.group("status"))
        if status != 429 and not 500 <= status <= 599:
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        text_class = _classify_failure_text(message)
        normalized_message = message.casefold().replace("_", " ")
        if text_class not in {
            "CODEX_CHILD_AUTH",
            "CODEX_CHILD_CONFIG",
            "CODEX_CHILD_MCP",
        } and (
            "invalid request" in normalized_message
            or any(marker in normalized_message for marker in _FAILOVER_DENIED_IDENTIFIER_MARKERS)
        ):
            return "CODEX_CHILD_NONZERO_UNKNOWN"
        if text_class != "CODEX_CHILD_NONZERO_UNKNOWN":
            classification = text_class
        elif status == 429:
            classification = "CODEX_CHILD_RATE_LIMIT"
        else:
            classification = "CODEX_CHILD_UPSTREAM"
        classifications.append(classification)
        if event_type == "turn.failed":
            terminal_class = classification

    if (
        not saw_reconnect
        or terminal_class is None
        or any(item != terminal_class for item in classifications)
    ):
        return "CODEX_CHILD_NONZERO_UNKNOWN"
    return terminal_class


def _classify_failure_text(text: str) -> str:
    sample = (text[:32_768] + "\n" + text[-32_768:]).casefold().replace("_", " ")
    if "mcp" in sample or "capability transport" in sample:
        return "CODEX_CHILD_MCP"
    if any(
        marker in sample
        for marker in (
            "strict config",
            "configuration error",
            "config error",
            "unknown feature",
            "invalid config",
        )
    ):
        return "CODEX_CHILD_CONFIG"
    if any(
        marker in sample
        for marker in (
            "unauthorized",
            "authentication error",
            "authentication failed",
            "invalid auth",
            "invalid api key",
            "login required",
            "http 401",
            "status 401",
            "http 403",
            "status 403",
            "forbidden",
            "permission denied",
        )
    ):
        return "CODEX_CHILD_AUTH"
    if any(
        marker in sample
        for marker in (
            "rate limit",
            "quota exceeded",
            "insufficient credit",
            "usage limit",
            "payment required",
            "http 402",
            "status 402",
            "http 429",
            "status 429",
        )
    ):
        return "CODEX_CHILD_RATE_LIMIT"
    if any(
        marker in sample
        for marker in (
            "model not found",
            "unknown model",
            "unsupported model",
            "model unavailable",
        )
    ):
        return "CODEX_CHILD_MODEL"
    if any(
        marker in sample
        for marker in (
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "upstream error",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    ):
        return "CODEX_CHILD_UPSTREAM"
    if any(
        marker in sample
        for marker in (
            "connection error",
            "connection refused",
            "connection reset",
            "network error",
            "dns",
            "tls",
            "temporary failure in name resolution",
        )
    ):
        return "CODEX_CHILD_NETWORK"
    return "CODEX_CHILD_NONZERO_UNKNOWN"


def _classify_structured_runtime_error(stdout: str) -> str:
    classified: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        if not line.strip() or len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") not in {
            "error",
            "turn.failed",
            "item.failed",
        }:
            continue
        error = record.get("error")
        if not isinstance(error, dict):
            continue
        classification = _classify_structured_error_record(error)
        classified.append((record["type"], classification))

    # Configuration and local capability failures are never provider
    # availability failures.  A conflicting earlier network event must not
    # authorize failover when the same child later reports either condition.
    for denied in ("CODEX_CHILD_MCP", "CODEX_CHILD_CONFIG"):
        if any(classification == denied for _event_type, classification in classified):
            return denied

    # Failover requires a terminal, unambiguous availability outcome.  Any
    # unknown/safety/tool/semantic event, or disagreement between structured
    # availability events, makes the whole child failure non-authoritative.
    terminal = [
        classification for event_type, classification in classified if event_type == "turn.failed"
    ]
    if not terminal:
        return "CODEX_CHILD_NONZERO_UNKNOWN"
    terminal_class = terminal[-1]
    if terminal_class == "CODEX_CHILD_NONZERO_UNKNOWN" or any(
        classification != terminal_class for _event_type, classification in classified
    ):
        return "CODEX_CHILD_NONZERO_UNKNOWN"
    return terminal_class


def _classify_structured_failover_denial(stdout: str) -> str | None:
    """Prioritize structured FIN denials over conflicting availability status."""

    denial_class: str | None = None
    saw_authentication_rejection = False
    for line in stdout.splitlines():
        if not line.strip() or len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") not in {
            "error",
            "turn.failed",
            "item.failed",
        }:
            continue
        error = record.get("error")
        if not isinstance(error, dict):
            continue
        identifiers = {
            value.casefold()
            for value in (error.get("type"), error.get("code"))
            if isinstance(value, str)
        }
        if any("config" in identifier for identifier in identifiers):
            return "CODEX_CHILD_CONFIG"
        if any("mcp" in identifier or "capability" in identifier for identifier in identifiers):
            denial_class = "CODEX_CHILD_MCP"
            continue
        if (
            identifiers & _FAILOVER_DENIED_ERROR_IDENTIFIERS
            or any(
                marker in identifier
                for identifier in identifiers
                for marker in _FAILOVER_DENIED_IDENTIFIER_MARKERS
            )
        ) and denial_class is None:
            denial_class = "CODEX_CHILD_NONZERO_UNKNOWN"
            continue
        # Authentication itself remains fail-closed, but it cannot override a
        # known FIN product/safety/semantic denial merely because a provider
        # attached an HTTP 401 to that denial.
        if error.get("status") == 401 or identifiers & {
            "authentication_error",
            "invalid_api_key",
            "invalid_auth",
            "unauthorized",
        }:
            saw_authentication_rejection = True
    return denial_class or ("CODEX_CHILD_AUTH" if saw_authentication_rejection else None)


def _classify_structured_error_record(error: Mapping[str, object]) -> str:
    """Classify one structured runtime error without consulting free text."""

    error_type = error.get("type")
    error_code = error.get("code")
    status = error.get("status")
    identifiers = {value for value in (error_type, error_code) if isinstance(value, str)}
    if any("mcp" in value.casefold() or "capability" in value.casefold() for value in identifiers):
        return "CODEX_CHILD_MCP"
    if any("config" in value.casefold() for value in identifiers):
        return "CODEX_CHILD_CONFIG"
    if identifiers & _FAILOVER_DENIED_ERROR_IDENTIFIERS or any(
        marker in identifier.casefold()
        for identifier in identifiers
        for marker in _FAILOVER_DENIED_IDENTIFIER_MARKERS
    ):
        return "CODEX_CHILD_NONZERO_UNKNOWN"
    if status == 401 or identifiers & {
        "authentication_error",
        "invalid_api_key",
        "invalid_auth",
        "unauthorized",
    }:
        return "CODEX_CHILD_AUTH"
    if status in {402, 429} or identifiers & {
        "insufficient_credit",
        "insufficient_quota",
        "payment_required",
        "payment_required_error",
        "quota_error",
        "quota_exceeded",
        "rate_limit_error",
        "rate_limit_exceeded",
    }:
        return "CODEX_CHILD_RATE_LIMIT"
    if identifiers & {"model_error", "model_not_found"}:
        return "CODEX_CHILD_MODEL"
    if (
        isinstance(status, int) and not isinstance(status, bool) and 500 <= status <= 599
    ) or identifiers & {"api_error", "server_error", "service_unavailable", "upstream_error"}:
        return "CODEX_CHILD_UPSTREAM"
    if identifiers & {
        "connection_error",
        "network_error",
        "timeout",
        "timeout_error",
    }:
        return "CODEX_CHILD_NETWORK"
    return "CODEX_CHILD_NONZERO_UNKNOWN"


def _runtime_error_text(stdout: str) -> str:
    """Project only runtime error records for in-memory classification.

    Successful/partial agent messages can contain the same words as provider
    failures.  Excluding them prevents user content from changing failover
    eligibility when the child later exits non-zero for an unrelated reason.
    """

    projected: list[str] = []
    projected_bytes = 0
    for line in stdout.splitlines():
        if projected_bytes >= 64 * 1024:
            break
        if not line.strip() or len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") not in {
            "error",
            "turn.failed",
            "item.failed",
        }:
            continue
        values: list[object] = [record.get("message")]
        error = record.get("error")
        if isinstance(error, dict):
            values.extend(
                (
                    error.get("type"),
                    error.get("code"),
                    error.get("status"),
                    error.get("message"),
                )
            )
        for value in values:
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                continue
            text = str(value)[:4_096]
            projected.append(text)
            projected_bytes += len(text.encode("utf-8", errors="replace"))
    return "\n".join(projected)


def _extract_runtime_errors(stdout: str) -> list[dict[str, object]]:
    if not stdout:
        return []
    extracted: list[dict[str, object]] = []
    for line in reversed(stdout.splitlines()):
        if len(extracted) >= _MAX_RUNTIME_ERROR_EVENTS:
            break
        if not line.strip() or len(line.encode("utf-8", errors="replace")) > _MAX_JSONL_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        event_type = record.get("type")
        if event_type not in {"error", "turn.failed", "item.failed"}:
            continue
        event: dict[str, object] = {"event_type": event_type}
        error = record.get("error")
        if isinstance(error, dict):
            _copy_error_identifier(
                event,
                "error_type",
                error.get("type"),
                allowed=_ALLOWED_ERROR_TYPES,
            )
            _copy_error_identifier(
                event,
                "error_code",
                error.get("code"),
                allowed=_ALLOWED_ERROR_CODES,
            )
            status = error.get("status")
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                event["http_status"] = status
        extracted.append(event)
    extracted.reverse()
    return extracted


def _copy_error_identifier(
    event: dict[str, object],
    key: str,
    value: object,
    *,
    allowed: frozenset[str],
) -> None:
    if isinstance(value, str) and value in allowed:
        event[key] = value


_V3_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "occurred_at",
        "backend",
        "model",
        "exit_code",
        "failure_code",
        "failover_class",
        "stderr_sha256",
        "stderr_bytes",
        "stdout_sha256",
        "stdout_bytes",
        "runtime_errors",
    }
)


def _decode_artifact(payload: bytes) -> Mapping[str, object]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("diagnostic artifact is invalid") from error
    if not isinstance(decoded, dict):
        raise ValueError("diagnostic artifact is invalid")
    if decoded.get("schema_version") == _SCHEMA_VERSION:
        return _decode_artifact_v4(decoded)
    if decoded.get("schema_version") == _V3_SCHEMA_VERSION:
        return _decode_artifact_v3(decoded)
    raise ValueError("diagnostic artifact is invalid")


def _decode_artifact_v3(decoded: Mapping[str, object]) -> Mapping[str, object]:
    """旧 v3 平面快照严格解码(兼容,N1: 不因新 union 拒绝历史诊断)。"""

    if frozenset(decoded) != _V3_ARTIFACT_FIELDS or decoded.get("backend") != "codex":
        raise ValueError("diagnostic artifact is invalid")
    if not isinstance(decoded.get("occurred_at"), str):
        raise ValueError("diagnostic artifact is invalid")
    model = decoded.get("model")
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("diagnostic artifact is invalid")
    exit_code = decoded.get("exit_code")
    stderr_bytes = decoded.get("stderr_bytes")
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -255 <= exit_code <= 255
        or not isinstance(stderr_bytes, int)
        or isinstance(stderr_bytes, bool)
        or stderr_bytes < 0
    ):
        raise ValueError("diagnostic artifact is invalid")
    failure_code = decoded.get("failure_code")
    if not isinstance(failure_code, str) or not failure_code.startswith("CODEX_CHILD_"):
        raise ValueError("diagnostic artifact is invalid")
    stderr_sha256 = decoded.get("stderr_sha256")
    if not isinstance(stderr_sha256, str) or not _SHA256_PATTERN.fullmatch(stderr_sha256):
        raise ValueError("diagnostic artifact is invalid")
    stdout_sha256 = decoded.get("stdout_sha256")
    stdout_bytes = decoded.get("stdout_bytes")
    runtime_errors = decoded.get("runtime_errors")
    if (
        not isinstance(stdout_sha256, str)
        or not _SHA256_PATTERN.fullmatch(stdout_sha256)
        or not isinstance(stdout_bytes, int)
        or isinstance(stdout_bytes, bool)
        or stdout_bytes < 0
        or not isinstance(runtime_errors, list)
        or len(runtime_errors) > _MAX_RUNTIME_ERROR_EVENTS
    ):
        raise ValueError("diagnostic artifact is invalid")
    for event in runtime_errors:
        _validate_runtime_error(event)
    return decoded


# 精确旧 v2 平面 failure 快照 schema(一次性识别,允许首个 v3 写入直接原子替换;
# 与 invocation lenient 同一设计: 不双读、不迁移、不保留兼容层)。
# 字段为精确闭集: 额外字段(如 prompt)一律拒绝。

def _decode_artifact_v4(decoded: Mapping[str, object]) -> Mapping[str, object]:
    """v4 判别式事件严格解码(N1 字段矩阵 + 跨字段约束)。"""

    if frozenset(decoded) != _ARTIFACT_FIELDS or decoded.get("backend") != "codex":
        raise ValueError("diagnostic artifact is invalid")
    occurred_at_text = decoded.get("occurred_at")
    if not isinstance(occurred_at_text, str):
        raise ValueError("diagnostic artifact is invalid")
    try:
        parsed_at = datetime.fromisoformat(occurred_at_text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError("diagnostic artifact is invalid") from None
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise ValueError("diagnostic artifact is invalid")
    model = decoded.get("model")
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("diagnostic artifact is invalid")
    event_kind = decoded.get("event_kind")
    if event_kind not in _EVENT_KIND_CLOSED_SET:
        raise ValueError("diagnostic artifact is invalid")
    error_id = decoded.get("error_id")
    if not isinstance(error_id, str) or not _ERROR_ID_PATTERN.fullmatch(error_id):
        raise ValueError("diagnostic artifact is invalid")
    elapsed = decoded.get("elapsed_seconds")
    if (
        not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError("diagnostic artifact is invalid")
    route = decoded.get("route")
    stage = decoded.get("stage")
    if (route is not None and not is_codex_route_id(route)) or (
        stage is not None and stage not in _STAGES
    ):
        raise ValueError("diagnostic artifact is invalid")
    truncated = decoded.get("truncated")
    if not isinstance(truncated, bool):
        raise ValueError("diagnostic artifact is invalid")
    exit_code = decoded.get("exit_code")
    if event_kind == "exit_failure":
        if (
            not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not -255 <= exit_code <= 255
        ):
            raise ValueError("diagnostic artifact is invalid")
    elif exit_code is not None and (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -255 <= exit_code <= 255
    ):
        raise ValueError("diagnostic artifact is invalid")
    probe_origin = decoded.get("probe_origin")
    http_status = decoded.get("http_status")
    if event_kind == "probe_failure":
        if probe_origin is None and http_status is None:
            raise ValueError("diagnostic artifact is invalid")
    elif probe_origin is not None or http_status is not None:
        raise ValueError("diagnostic artifact is invalid")
    if probe_origin is not None and (
        not isinstance(probe_origin, str) or len(probe_origin) > 128
    ):
        raise ValueError("diagnostic artifact is invalid")
    if http_status is not None and (
        not isinstance(http_status, int)
        or isinstance(http_status, bool)
        or not 100 <= http_status <= 599
    ):
        raise ValueError("diagnostic artifact is invalid")
    stderr_bytes = decoded.get("stderr_bytes")
    stdout_bytes = decoded.get("stdout_bytes")
    if (
        not isinstance(stderr_bytes, int)
        or isinstance(stderr_bytes, bool)
        or stderr_bytes < 0
        or not isinstance(stdout_bytes, int)
        or isinstance(stdout_bytes, bool)
        or stdout_bytes < 0
    ):
        raise ValueError("diagnostic artifact is invalid")
    failure_code = decoded.get("failure_code")
    if not isinstance(failure_code, str) or not failure_code.startswith("CODEX_CHILD_"):
        raise ValueError("diagnostic artifact is invalid")
    failover_class = decoded.get("failover_class")
    if failover_class is not None and failover_class not in _CLASSIFIER_CLOSED_SET:
        raise ValueError("diagnostic artifact is invalid")
    for key in ("stderr_sha256", "stdout_sha256"):
        digest = decoded.get(key)
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError("diagnostic artifact is invalid")
    runtime_errors = decoded.get("runtime_errors")
    if (
        not isinstance(runtime_errors, list)
        or len(runtime_errors) > _MAX_RUNTIME_ERROR_EVENTS
    ):
        raise ValueError("diagnostic artifact is invalid")
    for event in runtime_errors:
        _validate_runtime_error(event)
    return decoded


_V2_FAILURE_SCHEMA_VERSION = "fin.codex-runtime-failure-diagnostic/v2"
_V2_FAILURE_FIELDS = frozenset(
    {
        "schema_version",
        "occurred_at",
        "backend",
        "model",
        "exit_code",
        "failure_code",
        "stderr_sha256",
        "stderr_bytes",
        "stdout_sha256",
        "stdout_bytes",
        "runtime_errors",
    }
)


def _decode_artifact_lenient(payload: bytes) -> Mapping[str, object]:
    """Lenient current-decode for CAS publication (failure snapshot).

    v3 artifact 走严格校验;旧 v2 平面快照用精确 v2 schema 一次性识别,
    允许被首个 v3 写入直接原子替换(设计: 不双读、不迁移、不保留兼容层)。
    其他任意对象一律拒绝,不提供宽松平面解析。
    """

    try:
        return _decode_artifact(payload)
    except ValueError:
        pass
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("diagnostic artifact is invalid") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema_version") != _V2_FAILURE_SCHEMA_VERSION
        or frozenset(decoded) != _V2_FAILURE_FIELDS
        or decoded.get("backend") != "codex"
    ):
        raise ValueError("diagnostic artifact is invalid")
    if not isinstance(decoded.get("occurred_at"), str):
        raise ValueError("diagnostic artifact is invalid")
    model = decoded.get("model")
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("diagnostic artifact is invalid")
    exit_code = decoded.get("exit_code")
    stderr_bytes = decoded.get("stderr_bytes")
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -255 <= exit_code <= 255
        or not isinstance(stderr_bytes, int)
        or isinstance(stderr_bytes, bool)
        or stderr_bytes < 0
    ):
        raise ValueError("diagnostic artifact is invalid")
    failure_code = decoded.get("failure_code")
    if not isinstance(failure_code, str) or not failure_code.startswith("CODEX_CHILD_"):
        raise ValueError("diagnostic artifact is invalid")
    stderr_sha256 = decoded.get("stderr_sha256")
    if not isinstance(stderr_sha256, str) or not _SHA256_PATTERN.fullmatch(stderr_sha256):
        raise ValueError("diagnostic artifact is invalid")
    stdout_sha256 = decoded.get("stdout_sha256")
    stdout_bytes = decoded.get("stdout_bytes")
    runtime_errors = decoded.get("runtime_errors")
    if (
        not isinstance(stdout_sha256, str)
        or not _SHA256_PATTERN.fullmatch(stdout_sha256)
        or not isinstance(stdout_bytes, int)
        or isinstance(stdout_bytes, bool)
        or stdout_bytes < 0
        or not isinstance(runtime_errors, list)
        or len(runtime_errors) > _MAX_RUNTIME_ERROR_EVENTS
    ):
        raise ValueError("diagnostic artifact is invalid")
    for event in runtime_errors:
        _validate_runtime_error(event)
    return decoded


def _validate_runtime_error(event: object) -> None:
    if not isinstance(event, dict):
        raise ValueError("diagnostic artifact is invalid")
    allowed = {"event_type", "error_type", "error_code", "http_status"}
    if not set(event) <= allowed or event.get("event_type") not in {
        "error",
        "turn.failed",
        "item.failed",
    }:
        raise ValueError("diagnostic artifact is invalid")
    error_type = event.get("error_type")
    if error_type is not None and error_type not in _ALLOWED_ERROR_TYPES:
        raise ValueError("diagnostic artifact is invalid")
    error_code = event.get("error_code")
    if error_code is not None and error_code not in _ALLOWED_ERROR_CODES:
        raise ValueError("diagnostic artifact is invalid")
    status = event.get("http_status")
    if status is not None and (
        not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599
    ):
        raise ValueError("diagnostic artifact is invalid")


def _write_owner_only_source(path: Path, payload: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("diagnostic source write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_owner_only_source(path: Path, payload: bytes) -> None:
    """覆盖写入已有 candidate source(循环内 CAS 重试时使用)。

    source 只存在于调用方 owner-only 临时目录中;覆盖写不改变其
    0600/euid 不变量,允许 CAS 重试时刷新 envelope 内容。
    """

    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("diagnostic source write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


__all__ = [
    "CodexRuntimeDiagnosticError",
    "CodexRuntimeDiagnosticSink",
    "CodexRuntimeFailureEvent",
    "OwnerOnlyCodexRuntimeDiagnosticSink",
    "classify_codex_failover_failure",
    "classify_codex_runtime_failure",
]
