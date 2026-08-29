"""Phase 3A：Codex CLI resume parity 与 rollout layout 测试。

把 3A spike（2026-08-02，codex-cli 0.146.0 实测）验证的 CLI 行为固化为测试：
- initial 无 --ephemeral 时捕获 thread.started.thread_id（UUIDv7 格式）
- exact UUID resume 返回同一 thread_id
- missing-session 失败签名：exit 1、stdout 空、stderr 含 "no rollout found" 与 "code -32600"
- 共享安全参数（-s/-C）必须位于 resume 子命令之前，之后则 exit 2 拒绝
- history.persistence="none" 不阻止会话创建与 rollout 落盘，且不生成 history.jsonl
- rollout 最小文件集：sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl

live 测试与 test_helium_fin_codex_runtime 的 canary 相同：CODEX_BIN 显式 opt-in 才跑，
并用 ``llm`` marker 标记（真实调用 LLM）。live 测试使用 pytest tmp_path 作为临时
owner-only CODEX_HOME（运行时从 ~/.codex/auth.json 复制认证快照，不修改任何真实
运行时目录）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})\.jsonl$"
)


def _codex_bin() -> str | None:
    codex_bin = os.environ.get("CODEX_BIN", "")
    if not codex_bin:
        return None
    try:
        subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    return codex_bin


def _require_codex_bin() -> str:
    codex_bin = _codex_bin()
    if codex_bin is None:
        pytest.skip("CODEX_BIN not set — opt in explicitly to the live parity canary")
    return codex_bin


@pytest.fixture
def temp_codex_home(tmp_path: Path) -> Path:
    """pytest tmp_path 下的临时 owner-only CODEX_HOME（auth 快照来自 ~/.codex）。"""
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    auth_src = Path.home() / ".codex/auth.json"
    if auth_src.is_file():
        shutil.copy2(auth_src, home / "auth.json")
        (home / "auth.json").chmod(0o600)
    models_src = Path.home() / ".codex/models.json"
    if models_src.is_file():
        shutil.copy2(models_src, home / "models.json")
    config_src = Path.home() / ".codex/config.toml"
    if config_src.is_file():
        shutil.copy2(config_src, home / "config.toml")
        (home / "config.toml").chmod(0o600)
    return home


def _exec_options() -> list[str]:
    return [
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-s",
        "read-only",
        "--skip-git-repo-check",
    ]


def _thread_id_of(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str):
                return value
    return None


# ── 静态：rollout layout / UUIDv7 ───────────────────────────────────────────


def test_rollout_filename_matches_spike_layout() -> None:
    """rollout 文件名固定为 rollout-<ISOts>-<uuid>.jsonl，uuid 可提取（capture 校验用）。"""
    names = (
        "rollout-2026-08-02T23-01-26-019fc2fe-7ea6-7e32-a20b-357f21429486.jsonl",
        "rollout-2026-08-02T23-10-31-019fc306-d0dd-7ee1-a6ce-1800f304cd1b.jsonl",
    )
    for name in names:
        match = _ROLLOUT_NAME_RE.fullmatch(name)
        assert match is not None
        assert _UUID_RE.fullmatch(match.group(1)) is not None


def test_thread_id_is_uuidv7() -> None:
    """thread.started.thread_id 必须是 UUIDv7（时间有序），不是任意 UUID。"""
    sample = "019fc2fe-7ea6-7e32-a20b-357f21429486"
    parsed = uuid.UUID(sample)
    assert parsed.version == 7


# ── live（CODEX_BIN opt-in + llm marker）：CLI parity ───────────────────────


@pytest.mark.llm
def test_live_initial_captures_canonical_thread_id(temp_codex_home: Path) -> None:
    """initial（无 --ephemeral）在 stdout 给出 canonical thread.started.thread_id（UUIDv7）。"""
    codex_bin = _require_codex_bin()
    env = dict(os.environ, CODEX_HOME=str(temp_codex_home))
    completed = subprocess.run(
        [codex_bin, *_exec_options(), "用一句话回答：1+1等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    thread_id = _thread_id_of(completed.stdout)
    assert thread_id is not None
    assert uuid.UUID(thread_id).version == 7
    # rollout 落盘且文件名含同一 uuid（artifact capture 的证据链）
    rollouts = list((temp_codex_home / "sessions").rglob("rollout-*.jsonl"))
    assert len(rollouts) == 1
    assert thread_id in rollouts[0].name


@pytest.mark.llm
def test_live_resume_returns_same_thread_id(temp_codex_home: Path) -> None:
    """exact UUID resume 返回同一 thread_id，turn 正常完成。"""
    codex_bin = _require_codex_bin()
    env = dict(os.environ, CODEX_HOME=str(temp_codex_home))
    initial = subprocess.run(
        [codex_bin, *_exec_options(), "用一句话回答：1+1等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert initial.returncode == 0, initial.stderr[-500:]
    thread_id = _thread_id_of(initial.stdout)
    assert thread_id is not None

    resumed = subprocess.run(
        [codex_bin, *_exec_options(), "resume", thread_id, "用一句话回答：2+2等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr[-500:]
    assert _thread_id_of(resumed.stdout) == thread_id
    assert '"type":"turn.completed"' in resumed.stdout.replace(" ", "")


@pytest.mark.llm
@pytest.mark.timeout(900)
def test_live_materialized_resume_returns_same_thread_id_in_new_codex_home(
    temp_codex_home: Path,
) -> None:
    """A3: materialize 到新临时 home 后 exact resume 返回同一 thread_id。

    A3-R1-F2/F9: child 以 umask=0o077 运行（与 FIN direct 路径一致），
    rollout 创建即 owner-only，capture 无需手工 chmod。A3_CANARY_EVIDENCE_DIR
    设置时保存 codex binary SHA、route=direct-primary 与两次完整 JSONL/stderr。
    """
    from fin_analyse.guo_teacher_research.codex_session_store import (
        CodexSessionArtifactStore,
    )

    codex_bin = _require_codex_bin()
    store_root = temp_codex_home.parent / "session-store"
    store = CodexSessionArtifactStore(state_root=store_root)
    home_a = temp_codex_home
    env_a = dict(os.environ, CODEX_HOME=str(home_a))
    # 设计 §8.7：initial 与 resume 使用不同临时 workspace（cwd）
    ws_a = home_a.parent / "workspace-a"
    ws_b = home_a.parent / "workspace-b"
    ws_a.mkdir(mode=0o700)
    ws_b.mkdir(mode=0o700)

    initial = subprocess.run(
        [codex_bin, *_exec_options(), "用一句话回答：1+1等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env_a,
        cwd=ws_a,
        check=False,
        umask=0o077,
    )
    assert initial.returncode == 0, initial.stderr[-500:]
    thread_id = _thread_id_of(initial.stdout)
    assert thread_id is not None
    store.capture(
        session_id=thread_id,
        product_version=1,
        runtime_identity_hash="a" * 64,
        codex_executable_sha256="a" * 64,
        source_home=home_a,
    )

    # 新临时 home（跨进程语义）：materialize 后 exact resume 返回同一 ID
    home_b = home_a.parent / "codex-home-b"
    home_b.mkdir(mode=0o700)
    # auth 由 FIN direct 路径注入（materialize 只恢复 sessions/ rollout）
    auth_src = Path.home() / ".codex/auth.json"
    if auth_src.is_file():
        shutil.copy2(auth_src, home_b / "auth.json")
        (home_b / "auth.json").chmod(0o600)
    store.materialize(session_id=thread_id, product_version=1, dest_home=home_b)
    env_b = dict(os.environ, CODEX_HOME=str(home_b))

    resumed = subprocess.run(
        [codex_bin, *_exec_options(), "resume", thread_id, "用一句话回答：2+2等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env_b,
        cwd=ws_b,
        check=False,
        umask=0o077,
    )
    assert resumed.returncode == 0, resumed.stderr[-500:]
    # 显式 argv 断言（AGENTS.md 硬合同）：实际执行形状必须是独立的
    # resume <exact-uuid> <prompt> 三个 positional——"返回同 ID"不能证明
    # exact-id 路径（新 home 只有 A 的 rollout，退化 `resume --last` 也可能
    # 返回 A）。
    assert resumed.args[-3:] == [
        "resume",
        thread_id,
        "用一句话回答：2+2等于几",
    ]
    assert _thread_id_of(resumed.stdout) == thread_id
    assert '"type":"turn.completed"' in resumed.stdout.replace(" ", "")

    evidence_dir = os.environ.get("A3_CANARY_EVIDENCE_DIR")
    if evidence_dir:
        evidence = Path(evidence_dir)
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        (evidence / "route").write_text("direct-primary\n")
        (evidence / "binary.sha256").write_text(
            subprocess.run(
                ["sha256sum", codex_bin],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        snapshot = os.environ.get("A3_CANARY_SNAPSHOT_SHA", "")
        if snapshot:
            (evidence / "snapshot.sha256").write_text(snapshot + "\n")
        for name, proc in (
            ("run-1-initial", initial),
            ("run-2-materialized-resume", resumed),
        ):
            (evidence / f"{name}.stdout.jsonl").write_text(proc.stdout)
            (evidence / f"{name}.stderr").write_text(proc.stderr)
        for file in evidence.iterdir():
            file.chmod(0o600)


_A3_FIN_DISABLED_FEATURES = [
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
]


def _proxy_fin_flags(workspace: str) -> list[str]:
    """FIN runtime 形状 gate 的完整合法参数集（21 项 feature denylist 必填）。"""
    flags = [
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-C",
        workspace,
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        'model_reasoning_effort="max"',
    ]
    for feature in _A3_FIN_DISABLED_FEATURES:
        flags += ["--disable", feature]
    return flags


@pytest.mark.llm
@pytest.mark.timeout(900)
def test_live_proxy_wrapper_route_canary_resumes_a_not_last_b(tmp_path: Path) -> None:
    """A3-R1-F4: fixed A launcher + 三独立进程 exact resume A。

    The canary is opt-in because it performs real provider calls.  The A/B
    launcher binds one route-owned home; no provider selector or cross-provider
    fallback is accepted by the shared runtime.

    A3_CANARY_EVIDENCE_DIR 设置时，把 wrapper/binary SHA、route、snapshot
    （A3_CANARY_SNAPSHOT_SHA）与三次完整 stdout JSONL/stderr 写入证据目录。
    """
    if os.environ.get("FIN_ROUTE_CANARY") != "1":
        pytest.skip("FIN_ROUTE_CANARY=1 is required for the live A/B canary")
    wrapper = Path("scripts/codex_proxy_a.sh")
    assert wrapper.is_file()

    workspace = Path(f"/tmp/fin-codex-runtime-a3canary-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    workspace.mkdir(mode=0o700)
    try:
        env = dict(os.environ)
        route_home = Path("/home/ypk/fin-data/codex-routes/codex-proxy-a")
        auth_path = route_home / "auth.json"
        if not auth_path.is_file():
            pytest.skip("codex-proxy-a route auth is not provisioned")
        # The public launcher supplies the path; the shared binder derives all
        # route/model/auth hashes from the single owner YAML and this home.
        env["FIN_CODEX_ROUTE_LAUNCHER_ID"] = "codex-proxy-a"
        env["CODEX_PROXY_TRACE"] = "1"

        # 记录每次实际发出的 positional（A3 教训：run-3 必须真的携带
        # resume <exact-uuid> <prompt> 三个独立参数，不能漏传或拼成
        # 单个字符串——否则 run-3 只是又一次 initial，测试会误报）。
        invoked_positionals: list[list[str]] = []

        def run(prompt: str, resume_session: str | None = None) -> subprocess.CompletedProcess[str]:
            positional = (
                ["resume", resume_session, prompt] if resume_session is not None else [prompt]
            )
            invoked_positionals.append(list(positional))
            return subprocess.run(
                [
                    "/bin/bash",
                    str(wrapper),
                    *_proxy_fin_flags(str(workspace)),
                    *positional,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                check=False,
            )

        first_a = run("用一句话回答：A3 wrapper canary 3+3 等于几")
        assert first_a.returncode == 0, first_a.stderr[-500:]
        session_a = _thread_id_of(first_a.stdout)
        assert session_a is not None
        assert uuid.UUID(session_a).version == 7
        assert "selected route=codex-proxy-a" in first_a.stderr

        first_b = run("用一句话回答：A3 wrapper canary 4+4 等于几")
        assert first_b.returncode == 0, first_b.stderr[-500:]
        session_b = _thread_id_of(first_b.stdout)
        assert session_b is not None
        assert session_b != session_a

        resumed_a = run("用一句话回答：A3 wrapper canary 5+5 等于几", resume_session=session_a)
        assert resumed_a.returncode == 0, resumed_a.stderr[-500:]
        # 显式断言：run-3 确实发出了 resume <exact-uuid> <prompt>（独立
        # positional，形状 gate 的 3-positional 闭集），而不是漏传/拼串。
        assert invoked_positionals[2] == [
            "resume",
            session_a,
            "用一句话回答：A3 wrapper canary 5+5 等于几",
        ]
        # exact resume A 返回 A，不是最近 session B
        assert _thread_id_of(resumed_a.stdout) == session_a
        assert '"type":"turn.completed"' in resumed_a.stdout.replace(" ", "")

        evidence_dir = os.environ.get("A3_CANARY_EVIDENCE_DIR")
        if evidence_dir:
            evidence = Path(evidence_dir)
            evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
            (evidence / "route").write_text("codex-proxy-a\n")
            (evidence / "wrapper.sha256").write_text(
                subprocess.run(
                    ["sha256sum", str(wrapper)], capture_output=True, text=True, check=True
                ).stdout
            )
            (evidence / "binary.sha256").write_text(
                subprocess.run(
                    [
                        "sha256sum",
                        "/home/ypk/.local/share/fin-analyse/runtime-tools/proxy-codex/bin/codex",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            snapshot = os.environ.get("A3_CANARY_SNAPSHOT_SHA", "")
            if snapshot:
                (evidence / "snapshot.sha256").write_text(snapshot + "\n")
            for name, proc in (
                ("run-1-initial-a", first_a),
                ("run-2-initial-b", first_b),
                ("run-3-resume-a", resumed_a),
            ):
                (evidence / f"{name}.stdout.jsonl").write_text(proc.stdout)
                (evidence / f"{name}.stderr").write_text(proc.stderr)
            for file in evidence.iterdir():
                file.chmod(0o600)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.llm
@pytest.mark.timeout(900)
def test_live_proxy_interleaving_resumes_a_not_last_b_on_same_route(
    temp_codex_home: Path,
) -> None:
    """A3: native CLI 协议基线——同 home 三进程 A→B→A exact resume 返回 A。

    wrapper 端到端 route canary 由 test_live_proxy_wrapper_route_canary_
    resumes_a_not_last_b 覆盖；本测试保留为底层 exact-resume 协议基线
    （不经 wrapper，直接验证 provider 语义）。
    """
    codex_bin = _require_codex_bin()
    env = dict(os.environ, CODEX_HOME=str(temp_codex_home))

    first_a = subprocess.run(
        [codex_bin, *_exec_options(), "用一句话回答：3+3等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert first_a.returncode == 0, first_a.stderr[-500:]
    session_a = _thread_id_of(first_a.stdout)
    assert session_a is not None

    first_b = subprocess.run(
        [codex_bin, *_exec_options(), "用一句话回答：4+4等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert first_b.returncode == 0, first_b.stderr[-500:]
    session_b = _thread_id_of(first_b.stdout)
    assert session_b is not None
    assert session_b != session_a

    resumed_a = subprocess.run(
        [codex_bin, *_exec_options(), "resume", session_a, "用一句话回答：5+5等于几"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert resumed_a.returncode == 0, resumed_a.stderr[-500:]
    # exact resume A 返回 A，不是最近 session B
    assert _thread_id_of(resumed_a.stdout) == session_a


@pytest.mark.llm
def test_live_missing_session_failure_signature(temp_codex_home: Path) -> None:
    """missing-session 的稳定失败签名：exit 1、stdout 空、stderr 含 -32600。"""
    codex_bin = _require_codex_bin()
    env = dict(os.environ, CODEX_HOME=str(temp_codex_home))
    missing = subprocess.run(
        [codex_bin, *_exec_options(), "resume", str(uuid.UUID(int=0)), "x"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert "no rollout found" in missing.stderr
    assert "-32600" in missing.stderr


@pytest.mark.llm
@pytest.mark.parametrize(
    ("option", "value"),
    [("-s", "read-only"), ("-C", "/tmp")],
)
def test_live_rejects_shared_options_after_resume_subcommand(
    temp_codex_home: Path,
    option: str,
    value: str,
) -> None:
    """-s/-C 必须位于 resume 之前；之后 exit 2（0.146.0 实测规则，两个选项都测）。"""
    codex_bin = _require_codex_bin()
    env = dict(os.environ, CODEX_HOME=str(temp_codex_home))
    rejected = subprocess.run(
        [
            codex_bin,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "-s",
            "read-only",
            "resume",
            str(uuid.UUID(int=0)),
            option,
            value,
            "x",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert rejected.returncode != 0
    assert rejected.returncode == 2, rejected.stderr[-300:]


@pytest.mark.llm
def test_live_history_persistence_none_allows_rollout(temp_codex_home: Path) -> None:
    """history.persistence=none 不阻止 thread.started 与 rollout 落盘，且无 history.jsonl。"""
    codex_bin = _require_codex_bin()
    env = dict(os.environ, CODEX_HOME=str(temp_codex_home))
    completed = subprocess.run(
        [
            codex_bin,
            *_exec_options(),
            "-c",
            'history.persistence="none"',
            "用一句话回答：3+3等于几",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    assert _thread_id_of(completed.stdout) is not None
    rollouts = list((temp_codex_home / "sessions").rglob("rollout-*.jsonl"))
    assert len(rollouts) == 1
    assert not (temp_codex_home / "history.jsonl").exists()
