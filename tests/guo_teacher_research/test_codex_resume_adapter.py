"""Phase 3D——CodexCliAgentRuntimeAdapter resume 接线（3D 设计 step 1-6 的单元面）。

覆盖（handoff §6.2/§6.4/§6.5 + 3D 设计验收）：
- initial 路径无 --ephemeral；resume 路径 argv = shared + resume <id> <prompt>
  （-s/-C 位于 resume 之前）
- 成功捕获 thread.started.thread_id（UUIDv7）→ bounded envelope
  （backend/session_id/identity_hash/product_version，版本 = 上一轮 + 1）
- 任何不匹配（backend/identity）视为 fresh，绝不跨 route/identity resume
- resume-before-tool 失败 → 丢弃 handle fresh 重来一次；resume-after-activity
  失败 → fail closed 不重跑
- direct 路径成功后 CodexSessionArtifactStore.capture()

live CLI 行为（resume parity / rollout layout）由 test_codex_resume_parity.py
（3A，CODEX_BIN opt-in）覆盖，本文件全部为单元测试。
"""

from __future__ import annotations

import hashlib
import json as _json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from fin_analyse.guo_teacher_research.agent_runtime import (
    AgentRunRequest,
)
from fin_analyse.guo_teacher_research.codex_runtime import CodexCliAgentRuntimeAdapter

_THREAD_ID = "019fc2fe-7ea6-7e32-a20b-357f21429486"  # UUIDv7 形状


class _FakeMonotonic:
    """可推进的假单调钟（adapter 与 registry 共用同一实例）。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _ScriptedRunner:
    """按序返回剧本 CompletedProcess；剧本耗尽后重复最后一个。"""

    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        index = min(len(self.commands) - 1, len(self._results) - 1)
        return self._results[index]


def _stdout(
    product: dict[str, Any],
    *,
    with_thread: bool = True,
    session_meta: bool = False,
) -> str:
    lines: list[str] = []
    if with_thread:
        lines.append(_json.dumps({"type": "thread.started", "thread_id": _THREAD_ID}))
    if session_meta:
        lines.append(
            _json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": _THREAD_ID},
                }
            )
        )
    lines.append(
        _json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": _json.dumps(product)},
            }
        )
    )
    lines.append(
        _json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
    )
    return "\n".join(lines)


def _stdout_with_thread(thread_id: str) -> str:
    """构造带指定 thread_id 的成功 stdout（A3 交错测试用）。"""
    return "\n".join(
        (
            _json.dumps({"type": "thread.started", "thread_id": thread_id}),
            _json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": _json.dumps({"research_product": {}, "display_product": {}}),
                    },
                }
            ),
            _json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
            ),
        )
    )


def _ok_completed(
    command: list[str], *, session_meta: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=_stdout({"research_product": {}, "display_product": {}}, session_meta=session_meta),
        stderr="",
    )


def _fail_completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")


def _request(**overrides: Any) -> AgentRunRequest:
    base: dict[str, Any] = {
        # generic_research_answer 无 capability bridge 门禁（decision_guidance
        # 要求 allowed_capabilities + bridge），本组测试聚焦 argv/resume 形状。
        "use_case_ref": "generic_research_answer",
        "question": "测试问题",
    }
    base.update(overrides)
    return AgentRunRequest(**base)


def _adapter(
    runner: Any,
    *,
    runtime_route: str | None = "proxy-primary",
    codex_bin: str = "codex",
    model: str = "",
    session_store: Any = None,
    invocation_sink: Any = None,
) -> CodexCliAgentRuntimeAdapter:
    return CodexCliAgentRuntimeAdapter(
        codex_bin=codex_bin,
        model=model,
        workspace_path="/tmp/test-ws",
        runner=runner,
        runtime_route=runtime_route,
        session_store=session_store,
        invocation_sink=invocation_sink,
    )


class _RecordingInvocationSink:
    """记录每个真实 child 的 started/terminated stage 事件（A1 诊断面）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []  # (phase, stage)
        self.failed_error_ids: dict[str, str] = {}  # stage -> error_id
        self.failed_classifiers: dict[str, str] = {}  # stage -> classifier

    def record(self, event: object) -> None:
        phase = getattr(event, "phase", "")
        stage = getattr(event, "stage", "")
        if phase in {"started", "terminated"}:
            self.events.append((phase, stage))
        if phase == "terminated" and getattr(event, "status", "") == "failed":
            error_id = getattr(event, "error_id", None)
            if isinstance(error_id, str):
                self.failed_error_ids[stage] = error_id
            classifier = getattr(event, "classifier", None)
            if isinstance(classifier, str):
                self.failed_classifiers[stage] = classifier


def _identity_hash(
    *,
    runtime_route: str = "proxy-primary",
    codex_bin: str = "codex",
    model: str = "",
    auth_identity: str = "",
) -> str:
    material = "\n".join(
        [
            "fin.codex-runtime-identity/v1",
            runtime_route,
            codex_bin,
            model,
            auth_identity,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _valid_continuation(identity_hash: str, product_version: int = 1) -> dict[str, Any]:
    return {
        "backend": "codex-cli",
        "session_id": _THREAD_ID,
        "identity_hash": identity_hash,
        "product_version": product_version,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# argv 形状
# ═══════════════════════════════════════════════════════════════════════════════


def test_initial_command_has_no_ephemeral_and_no_resume() -> None:
    """initial 路径：shared options + prompt；无 --ephemeral、无 resume。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)

    result = adapter.run(_request())
    assert result.status == "ok"
    assert result.continuity_degraded is False
    command = runner.commands[0]
    assert command[0] == "codex"
    assert command[1] == "exec"
    assert "--ephemeral" not in command
    assert "resume" not in command
    assert "测试问题" in command[-1]
    # 共享安全参数在 prompt 之前
    assert "read-only" in command


def test_resume_command_shape_places_shared_options_before_resume() -> None:
    """resume 路径：shared + resume <exact-session-id> <prompt>；无 --last。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash())

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "ok"
    assert result.continuity_degraded is False
    command = runner.commands[0]
    assert "--ephemeral" not in command
    assert "--last" not in command
    resume_index = command.index("resume")
    # A3: exact session id 替代 --last，恢复已持久化的 provider session。
    assert command[resume_index + 1] == _THREAD_ID
    assert "测试问题" in command[resume_index + 2]
    assert "-s" in command[:resume_index]  # read-only 在 resume 之前
    assert "-C" in command[:resume_index]  # workspace 在 resume 之前


def test_interleaved_same_route_resumes_exact_a_session_not_last_b() -> None:
    """A3: 同 route 的 A 首问 → B 首问 → A 追问必须恢复 A，绝不 --last 到 B。"""
    session_a = _THREAD_ID
    session_b = "019fd111-2222-7333-a444-555555555555"  # 第二个 UUIDv7 形状

    class AlternatingRunner:
        """按调用次数返回：A 首问 → B 首问 → A 追问（exact id resume）。"""

        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(command))
            if len(self.commands) == 2:
                returned = session_b
            elif len(self.commands) == 3:
                resume_index = command.index("resume")
                returned = command[resume_index + 1]
            else:
                returned = session_a
            return subprocess.CompletedProcess(
                command, 0, stdout=_stdout_with_thread(returned), stderr=""
            )

    runner = AlternatingRunner()
    adapter = _adapter(runner)

    # A 首问
    first_a = adapter.run(_request())
    assert first_a.status == "ok"
    assert first_a.opaque_runtime_continuation["session_id"] == session_a

    # B 首问（同 route 新 initial，成为最近 session）
    first_b = adapter.run(_request())
    assert first_b.status == "ok"
    assert first_b.opaque_runtime_continuation["session_id"] == session_b

    # A 追问：exact resume A，绝不 --last 到 B
    follow_a = adapter.run(
        _request(opaque_runtime_continuation=first_a.opaque_runtime_continuation)
    )
    assert follow_a.status == "ok"
    assert follow_a.continuity_degraded is False
    follow_command = runner.commands[2]
    assert "--last" not in follow_command
    assert follow_command[follow_command.index("resume") + 1] == session_a
    # 新 envelope 仍是 A，不因 --last 语义漂到 B
    assert follow_a.opaque_runtime_continuation["session_id"] == session_a


# ═══════════════════════════════════════════════════════════════════════════════
# envelope 附着
# ═══════════════════════════════════════════════════════════════════════════════


def test_success_attaches_bounded_envelope() -> None:
    """成功 + thread.started → envelope（backend/session_id/identity_hash/version=1）。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)

    result = adapter.run(_request())
    assert result.status == "ok"
    envelope = result.opaque_runtime_continuation
    assert envelope == {
        "backend": "codex-cli",
        "session_id": _THREAD_ID,
        "identity_hash": _identity_hash(),
        "product_version": 1,
    }


def test_envelope_product_version_increments_from_prior() -> None:
    """续问版本单调：prior version=2 → 新 envelope version=3。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash(), product_version=2)

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "ok"
    assert result.opaque_runtime_continuation["product_version"] == 3


def test_failure_attaches_no_envelope() -> None:
    """失败路径不带 handle（handle 只随成功版本原子推进）。"""
    runner = _ScriptedRunner([_fail_completed([])])
    adapter = _adapter(runner)

    result = adapter.run(_request())
    assert result.status == "error"
    assert result.opaque_runtime_continuation == {}


def test_success_attaches_envelope_from_session_meta_without_thread_started() -> None:
    """回归：proxy 路径 codex 0.146 exec 输出无 thread.started，用
    session_meta.session_id 承载会话标识——不解析则追问永远拿不到 resume
    handle，后台 agent 每次新建会话、上下文无法延续。"""
    runner = _ScriptedRunner(
        [_ok_completed([], session_meta=True)]  # type: ignore[arg-type]
    )
    adapter = _adapter(runner)

    result = adapter.run(_request())
    assert result.status == "ok"
    envelope = result.opaque_runtime_continuation
    assert envelope == {
        "backend": "codex-cli",
        "session_id": _THREAD_ID,
        "identity_hash": _identity_hash(),
        "product_version": 1,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 跨 route/identity 永不 resume
# ═══════════════════════════════════════════════════════════════════════════════


def test_foreign_identity_hash_never_resumes() -> None:
    """identity_hash 不匹配（如另一 route 的会话）→ 视为 fresh。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)
    foreign = _identity_hash(runtime_route="proxy-fallback")

    result = adapter.run(_request(opaque_runtime_continuation=_valid_continuation(foreign)))
    assert result.status == "ok"
    assert result.continuity_degraded is True  # 带 handle 但身份不匹配 → 实际转 fresh
    command = runner.commands[0]
    assert "resume" not in command
    # fresh 路径仍捕获新 thread 并附着本 route 身份的新 envelope
    assert result.opaque_runtime_continuation["identity_hash"] == _identity_hash()


def test_foreign_backend_never_resumes() -> None:
    """backend != codex-cli → 视为 fresh。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash())
    continuation["backend"] = "other-backend"

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "ok"
    assert result.continuity_degraded is True  # 带 handle 但 backend 不匹配 → 实际转 fresh
    assert "resume" not in runner.commands[0]


def test_malformed_session_id_never_resumes() -> None:
    """session_id 非 canonical UUID → 视为 fresh。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash())
    continuation["session_id"] = "not-a-uuid"

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "ok"
    assert result.continuity_degraded is True  # 带 handle 但 session id 非法 → 实际转 fresh
    assert "resume" not in runner.commands[0]


# ═══════════════════════════════════════════════════════════════════════════════
# 失败边界（handoff §6.4）
# ═══════════════════════════════════════════════════════════════════════════════


def test_resume_before_tool_failure_retries_fresh_once() -> None:
    """resume-before-tool 失败 → 丢弃 handle，fresh 重来一次。"""
    runner = _ScriptedRunner([_fail_completed([]), _ok_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash())

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "ok"
    assert result.continuity_degraded is True  # resume 失败后 fresh 成功 → 显式降级
    assert len(runner.commands) == 2
    assert runner.commands[0][1] == "exec"
    assert "resume" in runner.commands[0]
    assert "resume" not in runner.commands[1]  # 第二次为 fresh
    # fresh 会话的新 envelope
    assert result.opaque_runtime_continuation["session_id"] == _THREAD_ID


def test_resume_before_activity_then_fresh_failure_is_explicitly_degraded() -> None:
    """resume-before-activity 失败、随后 fresh 也失败 → 仍显式标记 degraded。"""
    sink = _RecordingInvocationSink()
    runner = _ScriptedRunner([_fail_completed([]), _fail_completed([])])
    adapter = _adapter(runner, invocation_sink=sink)
    continuation = _valid_continuation(_identity_hash())

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "error"
    assert result.continuity_degraded is True  # fresh 也失败不撤销降级事实
    assert len(runner.commands) == 2
    assert "resume" in runner.commands[0]
    assert "resume" not in runner.commands[1]
    # A1 诊断面：真实 child 成对记录，stage 顺序为 resume_runtime →
    # fresh_after_resume，不得压成一个 invocation。
    assert sink.events == [
        ("started", "resume_runtime"),
        ("terminated", "resume_runtime"),
        ("started", "fresh_after_resume"),
        ("terminated", "fresh_after_resume"),
    ]
    # A1：最终 error id 只由失败 origin 生成一次（本场景为 fresh child 的
    # 失败终态回填），格式为 err_ + 32 hex，且与 fresh child 的 terminated
    # 事件同源、不同于 resume child 的 ID——防止复用 resume child 的 ID。
    assert result.error_id is not None
    assert result.error_id.startswith("err_")
    assert len(result.error_id) == 4 + 32
    assert sink.failed_error_ids["resume_runtime"] != result.error_id
    assert sink.failed_error_ids["fresh_after_resume"] == result.error_id


class _ActiveBridge:
    def __init__(self) -> None:
        self.rejected = False
        self.activity_started = True
        self.trace: list[dict[str, Any]] = [
            {"capability": "fin.read_market_snapshot", "status": "ok"}
        ]


class _PassiveBridge:
    def __init__(self) -> None:
        self.rejected = False
        self.activity_started = False
        self.trace: list[dict[str, Any]] = []


def test_resume_after_activity_failure_fails_closed() -> None:
    """resume-after-activity 不确定 → fail closed，不静默重跑。"""
    runner = _ScriptedRunner([_fail_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash())

    result = adapter.run(
        _request(
            opaque_runtime_continuation=continuation,
            capability_bridge=_ActiveBridge(),
        )
    )
    assert result.status == "error"
    assert result.continuity_degraded is False  # after-activity fail closed，无 fresh 事实
    assert len(runner.commands) == 1  # 无 fresh 重试


# ═══════════════════════════════════════════════════════════════════════════════
# session artifact capture（direct 路径）
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeStore:
    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.captures: list[dict[str, Any]] = []
        self.materializations: list[dict[str, Any]] = []
        self._events = events

    def capture(self, **kwargs: Any) -> dict[str, Any]:
        self.captures.append(dict(kwargs))
        if self._events is not None:
            self._events.append(("store.capture", str(kwargs["session_id"])))
        return {"captured": True}

    def materialize(self, **kwargs: Any) -> int:
        self.materializations.append(dict(kwargs))
        if self._events is not None:
            self._events.append(("store.materialize", str(kwargs["session_id"])))
        return 0


class _DirectIdentity:
    executable = Path("/pinned/codex")
    expected_sha256 = "a" * 64
    expected_auth_identity_sha256 = "b" * 64
    bindings = 0

    @staticmethod
    def validate() -> None:
        return None

    def spawn_binding(self):
        self.bindings += 1
        return _nullcontext(("/proc/self/fd/42", (), "/pinned/auth", lambda: None))


def _nullcontext(value):
    class _Ctx:
        def __enter__(self):
            return value

        def __exit__(self, *exc):
            return False

    return _Ctx()


def test_direct_success_captures_session_artifact() -> None:
    """direct 路径成功后把 rollout 捕获为不可覆盖快照（临时 home 销毁前）。"""
    runner = _ScriptedRunner([_ok_completed([])])
    store = _FakeStore()
    identity = _DirectIdentity()
    adapter = CodexCliAgentRuntimeAdapter(
        codex_bin="/pinned/codex",
        model="",
        workspace_path="/tmp/test-ws",
        runner=runner,
        runtime_identity=identity,  # type: ignore[arg-type]
        runtime_route="direct-primary",
        session_store=store,  # type: ignore[arg-type]
    )

    result = adapter.run(_request())
    assert result.status == "ok"
    assert len(store.captures) == 1
    captured = store.captures[0]
    assert captured["session_id"] == _THREAD_ID
    assert captured["product_version"] == 1
    assert captured["runtime_identity_hash"] == _identity_hash(
        runtime_route="direct-primary",
        codex_bin="a" * 64,  # direct：executable 组件 = 可执行文件 sha（非路径）
        auth_identity="b" * 64,
    )
    assert captured["codex_executable_sha256"] == "a" * 64
    assert Path(captured["source_home"]) == Path("/pinned/auth")


def test_direct_resume_without_session_store_never_starts_exact_child() -> None:
    """A3-R1-F6: direct 路径缺 session store → exact child 0，直接 fresh。"""
    runner = _ScriptedRunner([_ok_completed([]), _ok_completed([])])
    identity = _DirectIdentity()
    adapter = CodexCliAgentRuntimeAdapter(
        codex_bin="/pinned/codex",
        model="",
        workspace_path="/tmp/test-ws",
        runner=runner,
        runtime_identity=identity,  # type: ignore[arg-type]
        runtime_route="direct-primary",
        # session_store 缺省（None）——direct 路径仍可能误配
    )
    first = adapter.run(_request())
    assert first.status == "ok"

    follow = adapter.run(_request(opaque_runtime_continuation=first.opaque_runtime_continuation))
    assert follow.status == "ok"
    assert follow.continuity_degraded is True  # 缺 store → materialize 失败 → fresh
    # 首问 + fresh：exact child 因缺 store 从未启动（materialize 前置失败）
    assert len(runner.commands) == 2
    assert "resume" not in runner.commands[1]  # 追问是 fresh（无 resume child）


class _KwargRecordingRunner:
    """记录每次调用的 kwargs（含 umask），返回固定成功 stdout。"""

    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.kwarg_list: list[dict[str, Any]] = []
        self._events = events

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        self.kwarg_list.append(dict(kwargs))
        if self._events is not None:
            kind = "runner.child.resume" if "resume" in command else "runner.child.initial"
            self._events.append((kind, _THREAD_ID))
        return subprocess.CompletedProcess(
            command, 0, stdout=_stdout_with_thread(_THREAD_ID), stderr=""
        )


def test_direct_resume_materializes_exact_prior_artifact_before_invocation() -> None:
    """A3: direct resume 在 child 启动前 materialize 已捕获的 exact session。"""
    # F8: store 与 runner 共享顺序日志——证明 materialize 严格先于 child 启动，
    # 不是"只检查最终计数"。
    events: list[tuple[str, str]] = []
    runner = _KwargRecordingRunner(events)
    store = _FakeStore(events)
    identity = _DirectIdentity()
    adapter = CodexCliAgentRuntimeAdapter(
        codex_bin="/pinned/codex",
        model="",
        workspace_path="/tmp/test-ws",
        runner=runner,
        runtime_identity=identity,  # type: ignore[arg-type]
        runtime_route="direct-primary",
        session_store=store,  # type: ignore[arg-type]
    )

    # 首问捕获 version 1
    first = adapter.run(_request())
    assert first.status == "ok"
    assert len(store.captures) == 1

    # 追问：exact resume 前必须 materialize prior artifact 到本轮 auth home
    follow = adapter.run(_request(opaque_runtime_continuation=first.opaque_runtime_continuation))
    assert follow.status == "ok"
    assert follow.continuity_degraded is False
    assert len(store.materializations) == 1
    materialized = store.materializations[0]
    assert materialized["session_id"] == _THREAD_ID
    assert materialized["product_version"] == 1
    assert Path(materialized["dest_home"]) == Path("/pinned/auth")
    # 共享顺序日志：首问 child → capture(v1) → materialize(v1) → resume child
    # → capture(v2)；materialize 严格位于 resume child 之前。
    assert events == [
        ("runner.child.initial", _THREAD_ID),
        ("store.capture", _THREAD_ID),
        ("store.materialize", _THREAD_ID),
        ("runner.child.resume", _THREAD_ID),
        ("store.capture", _THREAD_ID),
    ], f"materialize 必须发生在 resume child 启动前，实际顺序：{events}"
    # 新 envelope 使用 prior + 1
    assert follow.opaque_runtime_continuation["product_version"] == 2
    # A3-R1-F2: direct child 必须以 owner-only umask 运行（rollout 可被 capture）
    assert all(kwargs.get("umask") == 0o077 for kwargs in runner.kwarg_list)


class _FailingMaterializeStore(_FakeStore):
    def materialize(self, **kwargs: Any) -> int:
        self.materializations.append(dict(kwargs))
        raise RuntimeError("artifact missing or corrupt")


def test_direct_materialize_failure_falls_back_fresh_before_activity() -> None:
    """A3: direct materialize 失败 → exact child 不启动，仅 fresh 一次并显式降级。"""
    sink = _RecordingInvocationSink()
    runner = _ScriptedRunner([_ok_completed([]), _ok_completed([])])
    store = _FailingMaterializeStore()
    identity = _DirectIdentity()
    adapter = CodexCliAgentRuntimeAdapter(
        codex_bin="/pinned/codex",
        model="",
        workspace_path="/tmp/test-ws",
        runner=runner,
        runtime_identity=identity,  # type: ignore[arg-type]
        runtime_route="direct-primary",
        session_store=store,  # type: ignore[arg-type]
        invocation_sink=sink,
    )

    first = adapter.run(_request())
    assert first.status == "ok"
    assert len(store.captures) == 1

    # 追问：materialize 失败 → exact child 未启动 → 直接 fresh 一次
    follow = adapter.run(_request(opaque_runtime_continuation=first.opaque_runtime_continuation))
    assert follow.status == "ok"
    assert follow.continuity_degraded is True  # materialize 失败转 fresh → 显式降级
    assert len(store.materializations) == 1
    # 调用序列：首问 + 追问（追问仅有 fresh child；exact resume child 因
    # materialize 失败未启动，不是"resume 启动后再 fresh"的二次调用）。
    assert len(runner.commands) == 2
    assert "resume" not in runner.commands[1]  # 追问是 fresh（无 resume）
    # A1 stage：resume_runtime 尝试后 materialize 失败 → fresh_after_resume
    assert sink.events == [
        ("started", "initial_runtime"),
        ("terminated", "initial_runtime"),
        ("started", "resume_runtime"),
        ("terminated", "resume_runtime"),
        ("started", "fresh_after_resume"),
        ("terminated", "fresh_after_resume"),
    ]


class _TempAuthIdentity(_DirectIdentity):
    """direct 路径身份：spawn_binding 返回真实临时 auth home（capture 需要真实目录）。"""

    def __init__(self, root: Path) -> None:
        self.bindings: list[Path] = []
        self._root = root

    def spawn_binding(self):
        home = self._root / f"auth-home-{len(self.bindings)}"
        home.mkdir(mode=0o700, exist_ok=False)
        self.bindings.append(home)
        return _nullcontext(("/proc/self/fd/42", (), str(home), lambda: None))


def _write_owner_rollout(home: Path, session_id: str) -> Path:
    """模拟 codex child 落盘：rollout 按 child 的 umask=0o077 语义写为 owner-only。

    注意：这是 scripted provider 对真实 child 落盘行为的模拟（adapter 传给
    child 的 umask=0o077 由 kwargs 断言证明）；真实 OS umask 链路由 live
    direct canary（subprocess umask=0o077）证明。
    """
    day = home / "sessions" / "2026" / "08" / "05"
    day.mkdir(parents=True, exist_ok=True)
    for parent in (day, day.parent, day.parent.parent, day.parent.parent.parent):
        parent.chmod(0o700)
    rollout = day / f"rollout-2026-08-05T08-00-00-{session_id}.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{}}\n')
    rollout.chmod(0o600)
    return rollout


def test_direct_exact_resume_survives_new_adapter_and_codex_home(tmp_path: Path) -> None:
    """A3-R1-F2: 真实 store + 两个真实 adapter/auth home 的 capture→materialize→resume。

    冻结测试名：test_direct_exact_resume_survives_new_adapter_and_codex_home。
    每个 adapter 使用同 root 下新建的 CodexSessionArtifactStore 独立实例
    （磁盘是唯一共享面）；第一个 adapter/home capture A（child 收到
    umask=0o077 由 kwargs 断言；真实 OS umask 链路由 live direct canary
    证明）；第二个全新 adapter/store/home materialize 后 exact resume A。
    第二个 runner 启动时只能看到 A 的 rollout——证明不依赖内存中的
    adapter/store 实例。
    """
    from fin_analyse.guo_teacher_research.codex_session_store import (
        CodexSessionArtifactStore,
    )

    session_a = "019fc2fe-7ea6-7e32-a20b-357f21429486"

    calls: list[list[str]] = []
    umasks: list[int | None] = []
    home_rollouts_at_call: list[list[str]] = []

    def scripted_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        umasks.append(kwargs.get("umask"))
        resumed = "resume" in command
        session = command[command.index("resume") + 1] if resumed else session_a
        home = Path(kwargs["env"]["CODEX_HOME"])
        sessions = home / "sessions"
        home_rollouts_at_call.append(
            sorted(p.name for p in sessions.rglob("rollout-*.jsonl")) if sessions.exists() else []
        )
        _write_owner_rollout(home, session)
        stdout = "\n".join(
            (
                _json.dumps({"type": "thread.started", "thread_id": session}),
                _json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": _json.dumps({"research_product": {}, "display_product": {}}),
                        },
                    }
                ),
                _json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
                ),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    build_state = {"n": 0}

    def build_adapter() -> tuple[CodexCliAgentRuntimeAdapter, _TempAuthIdentity]:
        build_state["n"] += 1
        identity = _TempAuthIdentity(tmp_path / f"identities-{build_state['n']}")
        identity._root.mkdir(mode=0o700, exist_ok=True)  # noqa: SLF001
        adapter = CodexCliAgentRuntimeAdapter(
            codex_bin="/pinned/codex",
            model="",
            workspace_path="/tmp/test-ws",
            runner=scripted_runner,
            runtime_identity=identity,  # type: ignore[arg-type]
            runtime_route="direct-primary",
            # 每个 adapter 使用同 root 下新建的独立 store 实例（磁盘持久化
            # 是唯一共享面）——证明不依赖 adapter/store 内存实例。
            session_store=CodexSessionArtifactStore(state_root=tmp_path / "session-store"),  # type: ignore[arg-type]
        )
        return adapter, identity

    adapter1, identity1 = build_adapter()
    first = adapter1.run(_request())
    assert first.status == "ok"
    envelope_a = first.opaque_runtime_continuation
    assert envelope_a["session_id"] == session_a
    assert envelope_a["product_version"] == 1
    # 真实 store 自动 capture；child 收到 umask=0o077（kwargs 断言；真实
    # OS umask 链路由 live direct canary 证明；scripted rollout 的 owner-only
    # 写盘是 child 行为模拟，见 _write_owner_rollout 注释）
    assert umasks == [0o077]
    session_dir = (
        tmp_path / "session-store" / "runtime-sessions" / "codex-cli" / "v1"
    ) / uuid.UUID(session_a).hex
    assert (session_dir / "versions" / "1").is_dir()

    # 第二个全新 adapter + 全新 auth home（不依赖内存实例）
    adapter2, identity2 = build_adapter()
    assert identity2.bindings == []
    follow = adapter2.run(_request(opaque_runtime_continuation=envelope_a))
    assert follow.status == "ok"
    assert follow.opaque_runtime_continuation["session_id"] == session_a
    assert follow.opaque_runtime_continuation["product_version"] == 2
    # resume child argv 携带 A；第二个 runner 启动时只能看到 A 的 rollout
    assert len(calls) == 2
    assert "resume" in calls[1]
    assert calls[1][calls[1].index("resume") + 1] == session_a
    assert umasks == [0o077, 0o077]
    assert home_rollouts_at_call[0] == []  # 首问时 home 为空
    assert home_rollouts_at_call[1], "materialize 必须在 resume child 启动前恢复 rollout"
    assert all(session_a in name for name in home_rollouts_at_call[1])
    assert len(identity2.bindings) == 1  # 第二个 adapter 用的是自己的新 home


@pytest.mark.parametrize(
    "returned_id",
    (None, "019fd111-2222-7333-a444-555555555555"),
)
def test_exact_resume_rejects_missing_or_different_returned_session_before_activity(
    returned_id: str | None,
) -> None:
    """A3: exact resume 返回缺失/不同 session id → 不发布 payload，走 fresh 并降级。

    参数化覆盖两类失败回报：无 thread id（缺失）与合法但不同的 session id（B）。
    F8: 缺失与不同都必须走同一 fail-closed 边界——不发布、仅 fresh 一次、显式降级。
    """
    session_b = "019fd111-2222-7333-a444-555555555555"

    def no_thread_stdout() -> str:
        return "\n".join(
            (
                _json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": _json.dumps({"research_product": {}, "display_product": {}}),
                        },
                    }
                ),
                _json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
                ),
            )
        )

    class ReturnedIdRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(command))
            if len(self.commands) == 1:
                return subprocess.CompletedProcess(
                    command, 0, stdout=_stdout_with_thread(_THREAD_ID), stderr=""
                )
            if len(self.commands) == 2:
                # 追问：resume 后返回缺失（None）或不同（B）的 session id
                stdout = (
                    no_thread_stdout() if returned_id is None else _stdout_with_thread(returned_id)
                )
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
            # fresh child：正常返回 A
            return subprocess.CompletedProcess(
                command, 0, stdout=_stdout_with_thread(_THREAD_ID), stderr=""
            )

    runner = ReturnedIdRunner()
    adapter = _adapter(runner)
    first = adapter.run(_request())
    assert first.status == "ok"
    envelope_a = first.opaque_runtime_continuation

    follow = adapter.run(_request(opaque_runtime_continuation=envelope_a))
    # resume 返回 id 缺失/不同 → 不发布 → 走 fresh 一次 → 显式降级
    assert follow.status == "ok"
    assert follow.continuity_degraded is True
    assert len(runner.commands) == 3  # 首问 + resume 尝试 + fresh
    assert "resume" in runner.commands[1]
    assert "resume" not in runner.commands[2]
    # 错误 session id（B）不得泄漏进后续 continuation
    assert follow.opaque_runtime_continuation.get("session_id") != session_b


def test_exact_resume_conflicting_thread_and_session_events_fail_closed() -> None:
    """A3-R1-F1: thread.started=A 后 session_meta=B 的冲突回报 → 校验失败不发布。"""
    session_b = "019fd111-2222-7333-a444-555555555555"

    def conflicting_stdout() -> str:
        return "\n".join(
            (
                _json.dumps({"type": "thread.started", "thread_id": _THREAD_ID}),
                _json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"session_id": session_b},
                    }
                ),
                _json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": _json.dumps({"research_product": {}, "display_product": {}}),
                        },
                    }
                ),
                _json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
                ),
            )
        )

    class ConflictingRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(command))
            if len(self.commands) == 1:
                return subprocess.CompletedProcess(
                    command, 0, stdout=_stdout_with_thread(_THREAD_ID), stderr=""
                )
            # 追问：冲突回报（thread=A 但 session_meta=B）
            return subprocess.CompletedProcess(command, 0, stdout=conflicting_stdout(), stderr="")

    runner = ConflictingRunner()
    adapter = _adapter(runner)
    first = adapter.run(_request())
    assert first.status == "ok"

    follow = adapter.run(_request(opaque_runtime_continuation=first.opaque_runtime_continuation))
    # 冲突回报 → 校验失败 → 走 fresh 并显式降级（不发布冲突 payload）
    assert follow.status == "ok"
    assert follow.continuity_degraded is True
    assert len(runner.commands) == 3  # 首问 + resume 尝试 + fresh
    assert "resume" in runner.commands[1]
    assert "resume" not in runner.commands[2]


def test_exact_resume_contract_violation_with_wrong_id_never_publishes() -> None:
    """A3-R1-F1: resume 返回 wrong ID 且 product 不合法 → 立即失败。"""
    session_b = "019fd111-2222-7333-a444-555555555555"

    class WrongIdViolationRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(command))
            if len(self.commands) == 1:
                return subprocess.CompletedProcess(
                    command, 0, stdout=_stdout_with_thread(_THREAD_ID), stderr=""
                )
            # 追问：返回 B（wrong id）+ contract violation（无合法 product）
            stdout = "\n".join(
                (
                    _json.dumps({"type": "thread.started", "thread_id": session_b}),
                    _json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": _json.dumps({"answer": "", "bogus": True}),
                            },
                        }
                    ),
                    _json.dumps({"type": "turn.completed"}),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    runner = WrongIdViolationRunner()
    adapter = _adapter(runner)
    first = adapter.run(_request())
    assert first.status == "ok"

    follow = adapter.run(_request(opaque_runtime_continuation=first.opaque_runtime_continuation))
    # wrong id + contract violation：identity gate 先触发。
    assert follow.continuity_degraded is True
    assert len(runner.commands) == 3  # 首问 + resume 尝试 + disclosed fresh
    assert "resume" in runner.commands[1]
    assert "resume" not in runner.commands[2]


def test_exact_resume_session_mismatch_after_activity_fails_closed() -> None:
    """A3: exact resume 返回不同 session 且已有 activity → fail closed，无 fresh。"""
    session_b = "019fd111-2222-7333-a444-555555555555"

    class WrongSessionRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(command))
            returned = session_b if len(self.commands) == 2 else _THREAD_ID
            return subprocess.CompletedProcess(
                command, 0, stdout=_stdout_with_thread(returned), stderr=""
            )

    runner = WrongSessionRunner()
    sink = _RecordingInvocationSink()
    adapter = _adapter(runner, invocation_sink=sink)
    first = adapter.run(_request())
    assert first.status == "ok"

    follow = adapter.run(
        _request(
            opaque_runtime_continuation=first.opaque_runtime_continuation,
            capability_bridge=_ActiveBridge(),
        )
    )
    # after-activity + session mismatch → error，无 fresh，仅一个 child
    assert follow.status == "error"
    assert len(runner.commands) == 2  # 首问 + resume（无 fresh）
    assert "resume" in runner.commands[1]
    assert sink.failed_error_ids.get("resume_runtime") == follow.error_id
    # A3-R1-F7: after-activity mismatch 的 classifier 不得误记为 before-activity
    assert sink.failed_classifiers.get("resume_runtime") != "resume_before_activity_failed"


@pytest.mark.parametrize(
    "bad_version",
    (None, "1", 0, -1, True),
    ids=("missing", "string", "zero", "negative", "bool"),
)
def test_malformed_product_version_never_resumes(bad_version: Any) -> None:
    """A3: product_version 非严格正整数 → 不接受 exact resume，实际转 fresh。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)
    continuation = _valid_continuation(_identity_hash())
    continuation["product_version"] = bad_version

    result = adapter.run(_request(opaque_runtime_continuation=continuation))
    assert result.status == "ok"
    assert result.continuity_degraded is True  # 带 handle 但 version 非法 → fresh
    assert "resume" not in runner.commands[0]


def test_proxy_success_skips_capture() -> None:
    """proxy 路径（无 auth_home）不捕获——wrapper 持久持有 CODEX_HOME。"""
    runner = _ScriptedRunner([_ok_completed([])])
    store = _FakeStore()
    adapter = _adapter(runner, session_store=store)

    result = adapter.run(_request())
    assert result.status == "ok"
    assert store.captures == []


def test_capture_skipped_on_failure() -> None:
    """失败不捕获。"""
    runner = _ScriptedRunner([_fail_completed([])])
    store = _FakeStore()
    identity = _DirectIdentity()
    adapter = CodexCliAgentRuntimeAdapter(
        codex_bin="/pinned/codex",
        model="",
        workspace_path="/tmp/test-ws",
        runner=runner,
        runtime_identity=identity,  # type: ignore[arg-type]
        runtime_route="direct-primary",
        session_store=store,  # type: ignore[arg-type]
    )

    result = adapter.run(_request())
    assert result.status == "error"
    assert store.captures == []


def test_envelope_session_id_never_leaks_to_public_surface() -> None:
    """session_id 只进私有 seam，不进 payload/provenance/data_gaps。"""
    runner = _ScriptedRunner([_ok_completed([])])
    adapter = _adapter(runner)

    result = adapter.run(_request())
    assert result.status == "ok"
    assert _THREAD_ID not in result.payload
    assert _THREAD_ID not in _json.dumps(result.provenance)
    assert uuid.UUID(result.opaque_runtime_continuation["session_id"]) is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 sticky 路由（handoff §6.3/§7，必测 6/7）
# ═══════════════════════════════════════════════════════════════════════════════


class _UnserializableSchemaRunner:
    """child 前 schema 序列化失败：证明没有任何 child 被调用。"""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        raise AssertionError("pre-child schema failure must prevent child invocation")
