"""Ephemeral local MCP transport for FIN-owned runtime capabilities.

The Codex subprocess receives only a short-lived stdio MCP server.  That child
forwards bounded JSON calls over an owner-only Unix socket to the parent
process, where the FIN registry and grant-bound capability bridge remain the
sole authorization, accounting, source, and effect gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic as _monotonic
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from fin_analyse.common.stall_watchdog import run_with_stall_watchdog
from fin_analyse.guo_teacher_research.agent_runtime import (
    AgentCapabilityBridge,
    CapabilityResultPublication,
)
from fin_analyse.guo_teacher_research.capability_broker import CapabilityRejectedError

_SOCKET_ENV = "FIN_LOCAL_CAPABILITY_SOCKET"
_TOKEN_ENV = "FIN_LOCAL_CAPABILITY_TOKEN"
_NAMES_ENV = "FIN_LOCAL_CAPABILITY_NAMES"
_DEADLINE_ENV = "FIN_LOCAL_CAPABILITY_DEADLINE"
_TRUSTED_IMPORT_ROOT = str(Path(__file__).resolve().parents[2])
_FORWARDED_ENV_VARS = (
    _SOCKET_ENV,
    _TOKEN_ENV,
    _NAMES_ENV,
    _DEADLINE_ENV,
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
)
_SAFE_CHILD_ENV_VARS = frozenset(
    {
        "CODEX_HOME",
        "COLORTERM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
    }
)
_MAX_FRAME_BYTES = 1_048_576
_MCP_SERVER_NAME = "fin_capabilities"
_DEFAULT_ROUND_TRIP_TIMEOUT_SECONDS = 10.0
# Keep this provider budget aligned with
# production_capability_provider._MARKET_OVERVIEW_BUDGET_SECONDS.  The child
# process only receives the FIN capability name, so transport policy must stay
# FIN-owned and cannot be supplied as an arbitrary caller knob.
_MARKET_OVERVIEW_INNER_BUDGET_SECONDS = 20.0
_LOCAL_TRANSPORT_MARGIN_SECONDS = 2.0
_MARKET_OVERVIEW_ROUND_TRIP_TIMEOUT_SECONDS = (
    _MARKET_OVERVIEW_INNER_BUDGET_SECONDS + _LOCAL_TRANSPORT_MARGIN_SECONDS
)
_MARKET_SNAPSHOT_ROUND_TRIP_TIMEOUT_SECONDS = 32.0
_DEFAULT_READ_TOOL_DESCRIPTION = (
    "Read bounded FIN-owned context for the current research question."
)
_READ_TOOL_DESCRIPTIONS = {
    "fin.read_actual_portfolio": (
        "Read the latest user-confirmed actual portfolio: holdings, quantities, costs, "
        "cash, exposure and as-of. Use for current-portfolio or account-aware advice; "
        "this read never changes the portfolio."
    ),
    # BUG-012：通用默认描述导致 agent 无从判断调用时机（公告探针不触发）。
    # 契约=当天高相关本地参考材料（非 G 非公告）；公告类必须转 read_external_evidence。
    "fin.read_ready_evidence": (
        "Read same-day, highly question-relevant local reference items (recent teacher "
        "Q&A, general-column notes, market observations) as non-G reference context; "
        "an empty result on other days is expected. For official company announcements "
        "or records use read_external_evidence instead."
    ),
}
_SERVER_POLL_INTERVAL_SECONDS = 0.01
_SERVER_THREAD_JOIN_SECONDS = 0.1
_MAX_ADMITTED_SOCKET_REQUESTS = 8
_SOCKET_LISTEN_BACKLOG = 32
_FIRST_FRAME_TIMEOUT_SECONDS = 0.5
_OVER_CAPACITY_REPLY_TIMEOUT_SECONDS = 0.05
_READ_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_OPEN_WORLD_READ_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_DELIBERATION_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


class LocalCapabilityTransportError(RuntimeError):
    """Stable local MCP setup/teardown failure, distinct from child spawn."""


class LocalCapabilityRegistry(Protocol):
    @property
    def names(self) -> tuple[str, ...]: ...

    def invoke(
        self,
        bridge: AgentCapabilityBridge,
        capability: str,
        payload: Mapping[str, object],
        *,
        deadline_at: float | None = None,
        result_publication: CapabilityResultPublication | None = None,
    ) -> dict[str, object]: ...


class _SessionPublicationGate:
    """Linearize short result publication against outer-runtime teardown."""

    def __init__(self, *, deadline_at: float | None) -> None:
        self._deadline_at = deadline_at
        self._active = True
        self._lock = threading.Lock()

    def remaining_seconds(self) -> float | None:
        with self._lock:
            if not self._active:
                return 0.0
            if self._deadline_at is None:
                return None
            return max(0.0, self._deadline_at - _monotonic())

    @contextmanager
    def publication(self) -> Iterator[bool]:
        with self._lock:
            yield self._active and (self._deadline_at is None or _monotonic() < self._deadline_at)

    def close(self) -> None:
        with self._lock:
            self._active = False


def _read_request_frame(
    request: socket.socket,
    *,
    deadline_at: float,
) -> bytes | None:
    """Read exactly one newline-terminated frame under one absolute deadline."""

    frame = bytearray()
    while len(frame) <= _MAX_FRAME_BYTES:
        remaining = deadline_at - _monotonic()
        if remaining <= 0:
            return None
        request.settimeout(remaining)
        try:
            chunk = request.recv(min(65_536, _MAX_FRAME_BYTES + 1 - len(frame)))
        except (OSError, TimeoutError):
            return None
        if not chunk:
            return None
        newline_at = chunk.find(b"\n")
        if newline_at >= 0:
            frame.extend(chunk[:newline_at])
            return bytes(frame) if len(frame) <= _MAX_FRAME_BYTES else None
        frame.extend(chunk)
    return None


class _CapabilityRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _CapabilitySocketServer):  # pragma: no cover
            return
        raw = _read_request_frame(
            self.request,
            deadline_at=server.first_frame_deadline(),
        )
        if raw is None:
            return
        if not raw or len(raw) > _MAX_FRAME_BYTES:
            self._reply_if_current(
                server,
                {"ok": False, "error": "capability_transport_request_invalid"},
            )
            return
        try:
            request = json.loads(raw)
            token = request["token"]
            capability = request["capability"]
            payload = request["payload"]
            if (
                not isinstance(token, str)
                or not secrets.compare_digest(token, server.token)
                or not isinstance(capability, str)
                or capability not in server.allowed_capabilities
                or not isinstance(payload, dict)
            ):
                raise ValueError
            remaining = server.result_gate.remaining_seconds()
            if remaining == 0:
                return
            acquired = (
                server.invocation_lock.acquire()
                if remaining is None
                else server.invocation_lock.acquire(timeout=remaining)
            )
            if not acquired:
                return
            try:
                if server.result_gate.remaining_seconds() == 0:
                    return
                result = server.registry.invoke(
                    server.bridge,
                    capability,
                    payload,
                    deadline_at=server.deadline_at,
                    result_publication=server.result_gate.publication,
                )
            finally:
                server.invocation_lock.release()
        except CapabilityRejectedError as error:
            self._reply_if_current(server, {"ok": False, "error": error.code.value})
            return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._reply_if_current(
                server,
                {"ok": False, "error": "capability_transport_request_invalid"},
            )
            return
        except Exception:
            self._reply_if_current(
                server,
                {"ok": False, "error": "capability_transport_invocation_failed"},
            )
            return
        self._reply_if_current(server, {"ok": True, "result": result})

    def _reply_if_current(
        self,
        server: _CapabilitySocketServer,
        payload: dict[str, object],
    ) -> None:
        with server.result_gate.publication() as publish:
            if publish:
                if server.deadline_at is not None:
                    remaining = server.deadline_at - _monotonic()
                    if remaining <= 0:
                        return
                    self.request.settimeout(remaining)
                self._reply(payload)

    def _reply(self, payload: dict[str, object]) -> None:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(rendered.encode("utf-8") + b"\n")


class _CapabilitySocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False
    request_queue_size = _SOCKET_LISTEN_BACKLOG

    def __init__(
        self,
        path: str,
        *,
        registry: LocalCapabilityRegistry,
        bridge: AgentCapabilityBridge,
        allowed_capabilities: frozenset[str],
        token: str,
        deadline_at: float | None,
    ) -> None:
        self.registry = registry
        self.bridge = bridge
        self.allowed_capabilities = allowed_capabilities
        self.token = token
        self.deadline_at = deadline_at
        self.result_gate = _SessionPublicationGate(deadline_at=deadline_at)
        self.invocation_lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(_MAX_ADMITTED_SOCKET_REQUESTS)
        self._admission_state_lock = threading.Lock()
        self._accepting_requests = True
        self._admitted_sockets: set[socket.socket] = set()
        self._admitted_requests = 0
        self._peak_admitted_requests = 0
        self._handler_threads: set[threading.Thread] = set()
        super().__init__(path, _CapabilityRequestHandler)

    @property
    def admitted_requests(self) -> int:
        with self._admission_state_lock:
            return self._admitted_requests

    @property
    def peak_admitted_requests(self) -> int:
        with self._admission_state_lock:
            return self._peak_admitted_requests

    @property
    def active_handler_threads(self) -> int:
        with self._admission_state_lock:
            return len(self._handler_threads)

    def first_frame_deadline(self) -> float:
        local_deadline = _monotonic() + _FIRST_FRAME_TIMEOUT_SECONDS
        if self.deadline_at is None:
            return local_deadline
        return min(local_deadline, self.deadline_at)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._admission.acquire(blocking=False):
            self._reject_over_capacity(request)
            return
        with self._admission_state_lock:
            if not self._accepting_requests:
                self._admission.release()
                self.shutdown_request(request)
                return
            self._admitted_sockets.add(request)
            self._admitted_requests += 1
            self._peak_admitted_requests = max(
                self._peak_admitted_requests,
                self._admitted_requests,
            )
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_admission(request)
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        current_thread = threading.current_thread()
        with self._admission_state_lock:
            self._handler_threads.add(current_thread)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._admission_state_lock:
                self._handler_threads.discard(current_thread)
            self._release_admission(request)

    def _release_admission(self, request: Any) -> None:
        with self._admission_state_lock:
            if request not in self._admitted_sockets:
                return
            self._admitted_sockets.remove(request)
            self._admitted_requests -= 1
        self._admission.release()

    def stop_accepting_and_close_admitted(self) -> None:
        with self._admission_state_lock:
            self._accepting_requests = False
            admitted = tuple(self._admitted_sockets)
            self._admitted_sockets.clear()
            self._admitted_requests = 0
        for _request in admitted:
            self._admission.release()
        for request in admitted:
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                request.close()

    def _reject_over_capacity(self, request: socket.socket) -> None:
        try:
            request.settimeout(_OVER_CAPACITY_REPLY_TIMEOUT_SECONDS)
            rendered = json.dumps(
                {"ok": False, "error": "capability_transport_busy"},
                separators=(",", ":"),
            )
            request.sendall(rendered.encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


class LocalCapabilitySession:
    """Own one short-lived parent socket for a single Codex run."""

    def __init__(
        self,
        *,
        registry: LocalCapabilityRegistry,
        bridge: AgentCapabilityBridge,
        allowed_capabilities: Sequence[str],
        socket_root: str | Path | None = None,
        deadline_at: float | None = None,
    ) -> None:
        allowed = tuple(allowed_capabilities)
        if not allowed or len(set(allowed)) != len(allowed):
            raise ValueError("local_capability_allowlist_invalid")
        if set(allowed) - set(registry.names):
            raise ValueError("local_capability_allowlist_invalid")
        if deadline_at is not None and (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(deadline_at)
        ):
            raise ValueError("local_capability_deadline_invalid")
        self._registry = registry
        self._bridge = bridge
        self._allowed = allowed
        self._socket_root = Path(socket_root) if socket_root is not None else None
        self._deadline_at = float(deadline_at) if deadline_at is not None else None
        self._tempdir: TemporaryDirectory[str] | None = None
        self._server: _CapabilitySocketServer | None = None
        self._thread: threading.Thread | None = None
        self._token = secrets.token_urlsafe(32)
        self._socket_path: Path | None = None

    def __enter__(self) -> LocalCapabilitySession:
        self._tempdir = TemporaryDirectory(
            prefix="fin-capability-",
            dir=str(self._socket_root) if self._socket_root is not None else None,
        )
        directory = Path(self._tempdir.name)
        directory.chmod(0o700)
        socket_path = directory / "bridge.sock"
        try:
            self._server = _CapabilitySocketServer(
                str(socket_path),
                registry=self._registry,
                bridge=self._bridge,
                allowed_capabilities=frozenset(self._allowed),
                token=self._token,
                deadline_at=self._deadline_at,
            )
        except Exception:
            self._tempdir.cleanup()
            self._tempdir = None
            raise LocalCapabilityTransportError("local_capability_session_setup_failed") from None
        socket_path.chmod(0o600)
        self._socket_path = socket_path
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": _SERVER_POLL_INTERVAL_SECONDS},
            name="fin-local-capability-bridge",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        cleanup_failed = False
        try:
            if self._server is not None:
                self._server.result_gate.close()
                self._server.stop_accepting_and_close_admitted()
                self._server.shutdown()
                self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=_SERVER_THREAD_JOIN_SECONDS)
                cleanup_failed = self._thread.is_alive()
            if self._tempdir is not None:
                self._tempdir.cleanup()
        except Exception:
            cleanup_failed = True
        finally:
            self._server = None
            self._thread = None
            self._tempdir = None
            self._socket_path = None
        if cleanup_failed:
            raise LocalCapabilityTransportError("local_capability_session_cleanup_failed")

    def child_environment(self, *, base: Mapping[str, str] | None = None) -> dict[str, str]:
        if self._socket_path is None:
            raise RuntimeError("local_capability_session_not_started")
        source = os.environ if base is None else base
        environment = {key: value for key, value in source.items() if key in _SAFE_CHILD_ENV_VARS}
        environment.update(
            {
                _SOCKET_ENV: str(self._socket_path),
                _TOKEN_ENV: self._token,
                _NAMES_ENV: json.dumps(self._allowed, separators=(",", ":")),
                _DEADLINE_ENV: (repr(self._deadline_at) if self._deadline_at is not None else ""),
                # The isolated child must import the exact FIN checkout that owns
                # this session.  Never trust an ambient or caller-supplied path.
                "PYTHONPATH": _TRUSTED_IMPORT_ROOT,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        return environment


class LocalMcpCapabilityRunner:
    """Wrap subprocess execution with exactly one ephemeral FIN MCP server."""

    def __init__(
        self,
        *,
        registry: LocalCapabilityRegistry,
        process_runner: Any = subprocess.run,
        socket_root: str | Path | None = None,
        stall_seconds: float | None = None,
        environment_extra: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._process_runner = process_runner
        self._socket_root = socket_root
        # 无输出看门狗：启用时最终子进程走公共 watchdog（任意字节即活动、
        # 进程组清理）。capability 路径因此与 direct/proxy 路径共享同一语义。
        self._stall_seconds = stall_seconds
        # 附加环境变量（session 白名单过滤之后注入）——生产用它绑定
        # attested logical launcher/route identity，避免 capability runner
        # 绕过 launcher 时丢失 route binding。
        self._environment_extra = dict(environment_extra) if environment_extra else None

    def __call__(
        self,
        command: list[str],
        *,
        capability_bridge: AgentCapabilityBridge,
        allowed_capabilities: Sequence[str],
        **process_kwargs: Any,
    ) -> Any:
        if not command:
            raise ValueError("local_capability_command_invalid")
        deadline_at = _process_deadline(process_kwargs.get("timeout"))
        try:
            session = LocalCapabilitySession(
                registry=self._registry,
                bridge=capability_bridge,
                allowed_capabilities=allowed_capabilities,
                socket_root=self._socket_root,
                deadline_at=deadline_at,
            )
        except Exception:
            raise LocalCapabilityTransportError("local_capability_session_setup_failed") from None
        with session:
            try:
                configured_command = _inject_mcp_config(command)
                environment = session.child_environment(base=process_kwargs.pop("env", None))
                if self._environment_extra is not None:
                    environment = {**environment, **self._environment_extra}
            except RuntimeError as error:
                code = (
                    "local_capability_runtime_python_unavailable"
                    if str(error) == "local_capability_runtime_python_unavailable"
                    else "local_capability_session_configuration_failed"
                )
                raise LocalCapabilityTransportError(code) from None
            except Exception:
                raise LocalCapabilityTransportError(
                    "local_capability_session_configuration_failed"
                ) from None
            if self._stall_seconds is not None:
                return run_with_stall_watchdog(
                    configured_command,
                    timeout=process_kwargs.pop("timeout", None),
                    stall_seconds=self._stall_seconds,
                    early_progress_check=process_kwargs.pop(
                        "early_progress_check", None
                    ),
                    early_progress_seconds=process_kwargs.pop(
                        "early_progress_seconds", 0.0
                    ),
                    env=environment,
                    **process_kwargs,
                )
            return self._process_runner(
                configured_command,
                env=environment,
                **process_kwargs,
            )


def _process_deadline(timeout: object) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
    ):
        raise ValueError("local_capability_process_timeout_invalid")
    return _monotonic() + max(0.0, float(timeout))


def _inject_mcp_config(command: list[str]) -> list[str]:
    prompt = command[-1]
    prefix = command[:-1]
    module_args = [
        "-B",
        "-m",
        "fin_analyse.guo_teacher_research.local_capability_transport",
        "serve",
    ]
    config = [
        "-c",
        (f"mcp_servers.{_MCP_SERVER_NAME}.command={json.dumps(_trusted_runtime_python())}"),
        "-c",
        f"mcp_servers.{_MCP_SERVER_NAME}.args={json.dumps(module_args)}",
        "-c",
        (f"mcp_servers.{_MCP_SERVER_NAME}.env_vars={json.dumps(list(_FORWARDED_ENV_VARS))}"),
        "-c",
        f"mcp_servers.{_MCP_SERVER_NAME}.enabled=true",
    ]
    return [*prefix, *config, prompt]


def _trusted_runtime_python() -> str:
    """Return the release-bound interpreter used by the local MCP child.

    ``sys.executable`` preserves the argv path used to start the parent.  A
    gateway launched through the mutable ``current`` alias would therefore
    leak that alias into the child MCP configuration even though PYTHONPATH is
    already bound to the immutable release.  Keep both values under the same
    locked root and deliberately do not resolve the venv symlink to its shared
    base interpreter.
    """

    candidate = Path(_TRUSTED_IMPORT_ROOT) / ".venv" / "bin" / "python"
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError("local_capability_runtime_python_unavailable")
    return str(candidate)


def _child_request(capability: str, payload: dict[str, object]) -> dict[str, object]:
    socket_path = os.environ.get(_SOCKET_ENV, "")
    token = os.environ.get(_TOKEN_ENV, "")
    if not socket_path or not token:
        raise RuntimeError("capability_transport_unconfigured")
    request = json.dumps(
        {"token": token, "capability": capability, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > _MAX_FRAME_BYTES:
        raise RuntimeError("capability_transport_request_invalid")
    deadline_at = _child_round_trip_deadline(capability)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_remaining_round_trip_seconds(deadline_at))
            client.connect(socket_path)
            client.settimeout(_remaining_round_trip_seconds(deadline_at))
            client.sendall(request + b"\n")
            client.settimeout(_remaining_round_trip_seconds(deadline_at))
            with client.makefile("rb") as response_file:
                raw = response_file.readline(_MAX_FRAME_BYTES + 1)
            _remaining_round_trip_seconds(deadline_at)
    except TimeoutError:
        raise RuntimeError("capability_transport_timeout") from None
    if not raw or len(raw) > _MAX_FRAME_BYTES:
        raise RuntimeError("capability_transport_response_invalid")
    response = json.loads(raw)
    if not isinstance(response, dict) or response.get("ok") is not True:
        code = response.get("error") if isinstance(response, dict) else None
        raise RuntimeError(
            code if isinstance(code, str) else "capability_transport_response_invalid"
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("capability_transport_response_invalid")
    return result


def _round_trip_timeout_seconds(capability: str) -> float:
    if capability == "fin.read_market_overview":
        return _MARKET_OVERVIEW_ROUND_TRIP_TIMEOUT_SECONDS
    if capability == "fin.read_market_snapshot":
        return _MARKET_SNAPSHOT_ROUND_TRIP_TIMEOUT_SECONDS
    return _DEFAULT_ROUND_TRIP_TIMEOUT_SECONDS


def _child_round_trip_deadline(capability: str) -> float:
    deadline_at = _monotonic() + _round_trip_timeout_seconds(capability)
    raw_outer_deadline = os.environ.get(_DEADLINE_ENV, "")
    if not raw_outer_deadline:
        return deadline_at
    try:
        outer_deadline = float(raw_outer_deadline)
    except ValueError:
        raise RuntimeError("capability_transport_unconfigured") from None
    if not math.isfinite(outer_deadline):
        raise RuntimeError("capability_transport_unconfigured")
    return min(deadline_at, outer_deadline)


def _remaining_round_trip_seconds(deadline_at: float) -> float:
    remaining = deadline_at - _monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _make_read_tool(capability: str):
    def read(
        question: str,
        instruments: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "result": _child_request(
                capability,
                {"question": question, "instruments": instruments or []},
            )
        }

    read.__doc__ = _READ_TOOL_DESCRIPTIONS.get(capability, _DEFAULT_READ_TOOL_DESCRIPTION)
    return read


def _make_market_overview_tool(capability: str):
    def read(question: str) -> dict[str, object]:
        """Read the bounded broad-market overview without an instrument scope."""

        return {
            "result": _child_request(
                capability,
                {"question": question, "instruments": []},
            )
        }

    return read


def _make_deliberation_tool(capability: str):
    def deliberate(
        task_id: str,
        outcome: str,
        question: str,
        evidence: list[dict[str, Any]],
        trigger_reasons: list[str],
    ) -> dict[str, object]:
        """Run one bounded FIN-owned independent deliberation."""

        return {
            "result": _child_request(
                capability,
                {
                    "task_id": task_id,
                    "outcome": outcome,
                    "question": question,
                    "evidence": evidence,
                    "trigger_reasons": trigger_reasons,
                },
            )
        }

    return deliberate


def _serve_stdio() -> None:
    raw_names = os.environ.get(_NAMES_ENV, "")
    try:
        names = json.loads(raw_names)
    except json.JSONDecodeError as error:
        raise RuntimeError("capability_transport_unconfigured") from error
    if (
        not isinstance(names, list)
        or not names
        or len(set(names)) != len(names)
        or any(not isinstance(name, str) or not name.startswith("fin.") for name in names)
    ):
        raise RuntimeError("capability_transport_unconfigured")

    server = FastMCP("FIN local capabilities")
    for capability in names:
        if capability == "fin.independent_deliberation":
            server.tool(
                name=capability,
                annotations=_DELIBERATION_TOOL_ANNOTATIONS,
                structured_output=True,
            )(_make_deliberation_tool(capability))
            continue
        server.tool(
            name=capability,
            annotations=(
                _OPEN_WORLD_READ_TOOL_ANNOTATIONS
                if capability in {"fin.read_market_overview", "fin.read_market_snapshot"}
                else _READ_TOOL_ANNOTATIONS
            ),
            structured_output=True,
        )(
            _make_market_overview_tool(capability)
            if capability == "fin.read_market_overview"
            else _make_read_tool(capability)
        )
    server.run(transport="stdio")


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve",))
    args = parser.parse_args()
    if args.command == "serve":
        _serve_stdio()


if __name__ == "__main__":
    _main()
