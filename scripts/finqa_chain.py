#!/usr/bin/env python3
"""finqa-chain: config-driven headless consultation chain with automatic fallback.

设计：docs/design/finqa-chain-v1.md（§2.2 施工基线；设计稿随合入归档，git
bbd92db）。节点表 config/finqa_nodes.yaml：声明序 = 优先序，每节点独立
enabled，增删/排序/开关只改 yaml。语义出处：声明序+enabled = 提取链
llm.yaml；precheck/横幅/tsv/exit78 = 设计门 codex_open.sh（横幅一律 stderr
——先例横幅走 stdout 是已知缺陷，不继承）。

调用：finqa_chain.py ["问题"...]        # 走链（无位置参数且 stdin 非 TTY 时读 stdin）
      finqa_chain.py --node <id> [...]  # 单腿钉定（探针/对照/评估钉腿），跳过链
      FINQA_NODES_FILE=<yaml> 覆盖节点表（演练用）；FINQA_NODE_TIMEOUT=秒

退出码：0 = 有腿答出（stdout=答案，stderr 末行 served-by 元信息）；78 = 全部
可用腿失败（stderr 列各腿 rc/阶段）；2 = 用法/配置错误。答案只走 stdout，
横幅/元信息只走 stderr。tsv（$XDG_STATE_HOME/fin-analyse/finqa-chain/
fallback.tsv）六字段 ts/event/node/rc/stage/detail：event ∈ fallback|success|
exhausted，success 行即使用证据与 provenance 数据源；detail 只记阶段级 token，
不落问题文本与上游输出（家规 3）。session 可丢：各腿照原样留档但从不 resume。
无熔断：每次调用从首位真试起（人频次，现探现走）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_NODES_FILE = _PROJECT_ROOT / "config" / "finqa_nodes.yaml"
_CONSULT_WORKSPACE = Path.home() / "fin-data" / "consult-agent"
_LLM_ENV_FILE = Path.home() / ".config" / "fin-analyse" / "llm.env"
_EXIT_USAGE = 2
_EXIT_EXHAUSTED = 78
_DEFAULT_TIMEOUT_SECONDS = 900
_USAGE = "usage: finqa_chain.py [--node <id>] [question ...]"


def _state_tsv_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "fin-analyse" / "finqa-chain" / "fallback.tsv"


def _tsv_row(event: str, node: str, rc: int | str, stage: str, detail: str, *, call_id: str) -> None:
    """Append one ledger row. detail carries stage-level tokens only (家规 3)."""

    path = _state_tsv_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = f"{int(time.time())}\t{event}\t{node}\t{rc}\t{stage}\t{detail or '-'}\t{call_id}\n"
        newly = not path.exists()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(line)
        if newly:
            os.chmod(path, 0o600)
    except OSError as error:
        print(f"finqa-chain: tsv write failed: {error}", file=sys.stderr)


def _banner(message: str) -> None:
    print(f"finqa-chain: {message}", file=sys.stderr)


def _llm_env_key(key_name: str) -> str | None:
    """Read one KEY=value from the owner-only llm.env (no values ever printed)."""

    try:
        for line in _LLM_ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key_name}="):
                value = line.split("=", 1)[1].strip()
                return value or None
    except OSError:
        return None
    return None


def _precheck_claude() -> str | None:
    binary = Path.home() / ".local" / "bin" / "claude"
    if not binary.is_file():
        return "claude binary missing"
    config_dir = _CONSULT_WORKSPACE / ".claude-home"
    if not config_dir.is_dir():
        return "CLAUDE_CODE_CONFIG_DIR missing"
    if not (_CONSULT_WORKSPACE / ".mcp.json").is_file():
        return ".mcp.json missing"
    return None


def _precheck_commandcode() -> str | None:
    binary = Path.home() / ".local" / "bin" / "cmd"
    if not binary.is_file():
        return "cmd binary missing"
    return None


def _precheck_codex() -> str | None:
    if shutil.which("codex") is None:
        return "codex binary missing"
    codex_home = _CONSULT_WORKSPACE / ".codex"
    if not codex_home.is_dir():
        return "CODEX_HOME missing"
    auth_json = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if not auth_json.is_file():
        return "opencode auth.json missing"
    try:
        key = json.loads(auth_json.read_text(encoding="utf-8")).get("opencode-go", {}).get("key")
    except (OSError, ValueError):
        return "opencode auth.json unreadable"
    if not key:
        return "opencode-go key missing"
    if _llm_env_key("GLM_API_KEY") is None:
        return "GLM_API_KEY missing in llm.env"
    return None


_PRECHECKS = {
    "claude": _precheck_claude,
    "commandcode": _precheck_commandcode,
    "codex": _precheck_codex,
}


def _launch_argv(harness: str, questions: list[str]) -> list[str]:
    """Verbatim launch recipes — bashrc 三腿函数体（设计稿附录 B）为权威基线。"""

    if harness == "claude":
        return [
            str(Path.home() / ".local" / "bin" / "claude"),
            "-p",
            *questions,
            "--strict-mcp-config",
            "--mcp-config",
            str(_CONSULT_WORKSPACE / ".mcp.json"),
        ]
    if harness == "commandcode":
        return [
            str(Path.home() / ".local" / "bin" / "cmd"),
            "--skip-onboarding",
            "--no-auto-update",
            "--effort",
            "max",
            "-m",
            "deepseek/deepseek-v4-pro",
            "-p",
            *questions,
        ]
    if harness == "codex":
        return [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            *questions,
        ]
    raise ValueError(f"unknown harness: {harness}")


def _launch_env(harness: str) -> dict[str, str]:
    environment = dict(os.environ)
    if harness == "claude":
        environment["CLAUDE_CODE_CONFIG_DIR"] = str(_CONSULT_WORKSPACE / ".claude-home")
    elif harness == "codex":
        glm_key = _llm_env_key("GLM_API_KEY")
        if glm_key:
            environment["GLM_API_KEY"] = glm_key
        try:
            auth_json = Path.home() / ".local" / "share" / "opencode" / "auth.json"
            environment["OPENCODE_GO_API_KEY"] = json.loads(
                auth_json.read_text(encoding="utf-8")
            )["opencode-go"]["key"]
        except (OSError, ValueError, KeyError):
            pass  # precheck 已拦；此处兜底保持进程不崩
        environment["CODEX_HOME"] = str(_CONSULT_WORKSPACE / ".codex")
    return environment


def _run_node(node: dict, questions: list[str], timeout_seconds: float) -> tuple[int | str, str]:
    """Run one node. Returns (rc, stage); rc int=process code, str=failure token."""

    harness = node["harness"]
    precheck_failure = _PRECHECKS[harness]()
    if precheck_failure is not None:
        return precheck_failure, "precheck"
    try:
        completed = subprocess.run(
            _launch_argv(harness, questions),
            cwd=str(_CONSULT_WORKSPACE),
            env=_launch_env(harness),
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return "timeout", "timeout"
    except OSError as error:
        return type(error).__name__, "spawn"
    if completed.returncode != 0:
        return completed.returncode, "run"
    if not completed.stdout.strip():
        return "empty stdout", "run"
    sys.stdout.buffer.write(completed.stdout)
    sys.stdout.flush()
    return 0, "ok"


def _load_nodes() -> list[dict]:
    import yaml

    nodes_file = Path(os.environ.get("FINQA_NODES_FILE") or _DEFAULT_NODES_FILE)
    try:
        payload = yaml.safe_load(nodes_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"finqa-chain: nodes config unreadable ({nodes_file}): {error}")
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise SystemExit(f"finqa-chain: nodes config malformed: {nodes_file}")
    nodes = []
    for entry in payload["nodes"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("harness"):
            raise SystemExit(f"finqa-chain: node entry malformed: {entry!r}")
        if entry["harness"] not in _PRECHECKS:
            raise SystemExit(f"finqa-chain: unknown harness: {entry['harness']!r}")
        nodes.append(entry)
    return nodes


def main() -> int:
    arguments = sys.argv[1:]
    pinned_node: str | None = None
    if arguments and arguments[0] == "--node":
        if len(arguments) < 2:
            print(_USAGE, file=sys.stderr)
            return _EXIT_USAGE
        pinned_node, arguments = arguments[1], arguments[2:]
    questions = arguments
    if not questions and not sys.stdin.isatty():
        questions = [sys.stdin.read().strip()]
    if not questions or not questions[0]:
        print(_USAGE, file=sys.stderr)
        return _EXIT_USAGE

    call_id = f"{int(time.time())}-{os.getpid()}-{os.urandom(2).hex()}"
    try:
        timeout_seconds = float(os.environ.get("FINQA_NODE_TIMEOUT") or _DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        timeout_seconds = _DEFAULT_TIMEOUT_SECONDS

    nodes = _load_nodes()
    if pinned_node is not None:
        nodes = [node for node in nodes if node["id"] == pinned_node]
        if not nodes:
            print(f"finqa-chain: unknown node: {pinned_node}", file=sys.stderr)
            return _EXIT_USAGE
    else:
        nodes = [node for node in nodes if node.get("enabled")]

    attempts: list[str] = []
    for index, node in enumerate(nodes):
        node_id = node["id"]
        rc, stage = _run_node(node, questions, timeout_seconds)
        if rc == 0 and stage == "ok":
            _tsv_row("success", node_id, 0, stage, "-", call_id=call_id)
            _banner(f"served-by {node_id} call_id={call_id} rc=0")
            return 0
        detail = stage if stage in {"precheck", "timeout", "spawn"} else "empty stdout"
        attempts.append(f"{node_id}:{stage}:rc={rc}")
        next_node = nodes[index + 1]["id"] if index + 1 < len(nodes) else None
        _tsv_row("fallback", node_id, rc, stage, detail, call_id=call_id)
        _banner(
            f"FALLBACK {node_id} → {next_node or 'exhausted'}（{stage} 失败 rc={rc}）"
            f" call_id={call_id}"
        )

    _tsv_row("exhausted", "-", _EXIT_EXHAUSTED, "exhausted", ";".join(attempts), call_id=call_id)
    _banner(f"exhausted（{len(nodes)} 腿全失败）call_id={call_id}: {'; '.join(attempts)}")
    return _EXIT_EXHAUSTED


if __name__ == "__main__":
    sys.exit(main())
