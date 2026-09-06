#!/usr/bin/env python3
"""finqa:唯一问询入口——链兜底 + 节点钉腿 + 交互透传(config-driven launcher 唯一权威)。

设计:docs/design/finqa-cli-unify-v1.md(设计稿随合入归档;本注释为权威摘要);
LLM 连接池分层:docs/design/llm-pool-layering.md(连接=llm.yaml models harness 型
条目,消费者经 conn_ref 引用;池禁传导+钉腿拒绝,2026-09-06 设计门过闸)。
起法知识唯一声明位=本文件 launcher 层(_PRECHECKS/_launch_argv/_launch_env);
bashrc 旧六函数已退役(finqa-c/-cmd 为过渡别名),README 平行文本已改指路。
节点表 config/finqa_nodes.yaml(v2):声明序=优先序;节点=「连接引用(conn_ref)
+ 本层旋钮」,连接(harness/模型/auth 位置/总开关)在 llm.yaml models 池——
池 enabled=false ⇒ 链序跳过该腿(fail-visible+横幅);钉腿(--node,含 -i)遇池禁
= 拒绝执行(rc=2,钉腿是显式意图,静默换腿污染对照实验)。session/effort 旋钮
不变(effort 维持 launcher 单点 max)。

调用:
  finqa "问题"...                 # 走链(enabled 腿兜底;stdin 非 TTY 时读 stdin)
  finqa --node <id> "问题"...     # 钉腿无头(生产腿/测试腿/探针同一机制;
                                  #   enabled:false 测试腿仅此路可达)
  finqa --node <id> -i [透传...]  # 交互式:同 argv 去 -p,os.execvpe 前台透传,
                                  #   rc 透传(zcode/codex 不支持,报错 exit 2)
环境:FINQA_NODE_TIMEOUT=秒(默认 900,仅无头)。

退出码:0 = 有腿答出(stdout=答案,stderr 末行 served-by 元信息);78 = 全部
可用腿失败(stderr 列各腿 rc/阶段);2 = 用法/配置错误;交互式 = 子引擎 rc。
答案只走 stdout,横幅/元信息只走 stderr。tsv($XDG_STATE_HOME/fin-analyse/
finqa-chain/fallback.tsv)七字段 ts/event/node/rc/stage/detail/call_id:event ∈
fallback|success|exhausted,success 行即使用证据与 provenance 数据源;detail 只记
阶段级 token,不落问题文本与上游输出(家规 3)。session 可丢:各腿照原样留档但
从不 resume;discard 腿不留档。无熔断:每次调用从首位真试起(人频次,现探现走)。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_NODES_FILE = _PROJECT_ROOT / "config" / "finqa_nodes.yaml"
_LLM_POOL_FILE = _PROJECT_ROOT / "config" / "llm.yaml"
_CONSULT_WORKSPACE = Path.home() / "fin-data" / "consult-agent"
_LLM_ENV_FILE = Path.home() / ".config" / "fin-analyse" / "llm.env"
_EXIT_USAGE = 2
_EXIT_EXHAUSTED = 78
_DEFAULT_TIMEOUT_SECONDS = 900
_INTERACTIVE_UNSUPPORTED = {
    "zcode": "zcode 本机无 TUI(前端未随装),仅无头钉腿;模型/effort 单旋钮在 ~/.zcode/cli/config.json",
    "codex": "codex harness 休眠(opencode-go 429),interactive 未接;复活时在 launcher 补分支",
}


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


def _precheck_claude(node: dict) -> str | None:
    binary = Path.home() / ".local" / "bin" / "claude"
    if not binary.is_file():
        return "claude binary missing"
    config_dir = _CONSULT_WORKSPACE / ".claude-home"
    if not config_dir.is_dir():
        return "CLAUDE_CODE_CONFIG_DIR missing"
    if not (_CONSULT_WORKSPACE / ".mcp.json").is_file():
        return ".mcp.json missing"
    return None


def _precheck_commandcode(node: dict) -> str | None:
    binary = Path.home() / ".local" / "bin" / "cmd"
    if not binary.is_file():
        return "cmd binary missing"
    return None


def _precheck_codex(node: dict) -> str | None:
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


def _precheck_zcode(node: dict) -> str | None:
    binary = Path.home() / ".local" / "bin" / "zcode"
    if not binary.is_file():
        return "zcode binary missing"
    config_file = Path.home() / ".zcode" / "cli" / "config.json"
    if not config_file.is_file():
        return "zcode config.json missing"
    try:
        configured_model = json.loads(config_file.read_text(encoding="utf-8"))["model"]
    except (OSError, ValueError, KeyError):
        return "zcode config.json unreadable"
    expected_model = node.get("model")
    if expected_model and configured_model != expected_model:
        return f"zcode model drift: config={configured_model} node={expected_model}"
    if _llm_env_key("GLM_API_KEY") is None:
        return "GLM_API_KEY missing in llm.env"
    return None


_PRECHECKS = {
    "claude": _precheck_claude,
    "commandcode": _precheck_commandcode,
    "codex": _precheck_codex,
    "zcode": _precheck_zcode,
}


def _launch_argv(
    harness: str,
    questions: list[str],
    model: str | None = None,
    *,
    interactive: bool = False,
    discard_session: bool = False,
) -> list[str]:
    """起法唯一声明位——launcher 权威;旧 bashrc 函数体为其历史基线(已退役)。"""

    if harness == "claude":
        argv = [str(Path.home() / ".local" / "bin" / "claude")]
        if not interactive:
            argv.append("-p")
        argv += [*questions, "--strict-mcp-config", "--mcp-config", str(_CONSULT_WORKSPACE / ".mcp.json")]
        return argv
    if harness == "commandcode":
        argv = [
            str(Path.home() / ".local" / "bin" / "cmd"),
            "--skip-onboarding",
            "--no-auto-update",
            "--effort",
            "max",
            "-m",
            model or "deepseek/deepseek-v4-pro",
        ]
        if discard_session and not interactive:
            argv.append("--no-session")
        if not interactive:
            argv.append("-p")
        argv += questions
        return argv
    if harness == "zcode":
        # 模型/effort 钉在 ~/.zcode/cli/config.json(precheck 校验与节点一致);
        # 键 = llm.env GLM_API_KEY 映射 ZHIPU_API_KEY(实测同值)。
        argv = [str(Path.home() / ".local" / "bin" / "zcode")]
        if not interactive:
            argv.append("-p")
        argv += questions
        return argv
    if harness == "codex":
        argv = ["codex"]
        if not interactive:
            argv += ["exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        argv += questions
        return argv
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
            pass  # precheck 已拦;此处兜底保持进程不崩
        environment["CODEX_HOME"] = str(_CONSULT_WORKSPACE / ".codex")
    elif harness == "zcode":
        glm_key = _llm_env_key("GLM_API_KEY")
        if glm_key:
            environment["ZHIPU_API_KEY"] = glm_key  # 2026-09-06 实测与智谱键同值
    return environment


def _run_node(node: dict, questions: list[str], timeout_seconds: float) -> tuple[int | str, str]:
    """Run one node headless. Returns (rc, stage); rc int=process code, str=failure token."""

    harness = node["harness"]
    precheck_failure = _PRECHECKS[harness](node)
    if precheck_failure is not None:
        return precheck_failure, "precheck"
    try:
        completed = subprocess.run(
            _launch_argv(
                harness,
                questions,
                node.get("model"),
                discard_session=node.get("session") == "discard",
            ),
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


def _run_interactive(node: dict, forward: list[str]) -> int:
    """交互式:同 argv 去 -p,os.execvpe 前台透传(本进程被子引擎替换,rc 透传)。"""

    harness = node["harness"]
    if harness in _INTERACTIVE_UNSUPPORTED:
        _banner(f"interactive 不支持 {harness}:{_INTERACTIVE_UNSUPPORTED[harness]}")
        return _EXIT_USAGE
    if node.get("session") == "discard":
        _banner("session: discard 仅无头;交互式天然留档——改 keep 节点或去掉 -i")
        return _EXIT_USAGE
    argv = _launch_argv(harness, [], node.get("model"), interactive=True)
    env = _launch_env(harness)
    try:
        os.chdir(_CONSULT_WORKSPACE)
    except OSError as error:
        _banner(f"consult workspace unavailable: {error}")
        return _EXIT_USAGE
    _banner(f"interactive {node['id']}(退出码=子引擎;会话留档可 -c/--resume 续)")
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as error:
        _banner(f"exec failed: {error}")
        return _EXIT_USAGE


def _load_pool() -> dict[str, dict]:
    """LLM 连接池:llm.yaml models 段的 harness 型条目(别名→连接)。

    只读 harness/model/enabled/managed 四个执行键;api 型条目不进本池
    (提取/识图走 config_loader)。读失败=连接层不可用,所有 conn_ref 悬空。
    """

    import yaml

    pool_file = Path(os.environ.get("FINQA_POOL_FILE") or _LLM_POOL_FILE)
    try:
        payload = yaml.safe_load(pool_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"finqa-chain: llm pool unreadable ({pool_file}): {error}")
    models = (payload or {}).get("models")
    if not isinstance(models, dict):
        raise SystemExit(f"finqa-chain: llm pool malformed: {pool_file}")
    pool: dict[str, dict] = {}
    for alias, entry in models.items():
        if isinstance(entry, dict) and entry.get("type") == "harness":
            pool[alias] = entry
    return pool


def _resolve_node(entry: dict, pool: dict[str, dict]) -> tuple[dict | None, str]:
    """节点→运行时腿:v2 节点只有 conn_ref+旋钮,连接(harness/model/开关)在池。

    Returns (runtime_node, "") 或 (None, 失败原因)——失败原因区分
    conn_ref 悬空(池缺别名)与池禁(enabled=false),两者对链序同义(跳过)、
    对钉腿同义(拒绝)。
    """

    conn_ref = entry.get("conn_ref")
    if not conn_ref:
        return None, "conn_ref missing(node schema v2)"
    conn = pool.get(conn_ref)
    if conn is None:
        return None, f"conn_ref unresolved: {conn_ref}"
    if conn.get("managed", "pool") == "pool" and conn.get("enabled") is not True:
        return None, f"pool disabled: {conn_ref}"
    harness = conn.get("harness")
    if harness not in _PRECHECKS:
        return None, f"unknown harness in pool: {harness!r}"
    runtime = dict(entry)
    runtime["harness"] = harness
    runtime["model"] = conn.get("model")
    return runtime, ""


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
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("conn_ref"):
            raise SystemExit(
                f"finqa-chain: node entry malformed(needs id+conn_ref, schema v2): {entry!r}"
            )
        nodes.append(entry)
    return nodes


def _read_questions(payload: list[str]) -> list[str] | None:
    questions = list(payload)
    if not questions and not sys.stdin.isatty():
        questions = [sys.stdin.read().strip()]
    if not questions or not questions[0]:
        return None
    return questions


def _answer_via_legs(nodes: list[dict], questions: list[str], timeout_seconds: float) -> int:
    """逐腿兜底循环(链与单腿钉腿共用):首腿成功即返回,失败依序 fallback。"""

    call_id = f"{int(time.time())}-{os.getpid()}-{os.urandom(2).hex()}"
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
            f"FALLBACK {node_id} → {next_node or 'exhausted'}({stage} 失败 rc={rc})"
            f" call_id={call_id}"
        )
    _tsv_row("exhausted", "-", _EXIT_EXHAUSTED, "exhausted", ";".join(attempts), call_id=call_id)
    _banner(f"exhausted({len(nodes)} 腿全失败)call_id={call_id}: {'; '.join(attempts)}")
    return _EXIT_EXHAUSTED


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="finqa",
        description="唯一问询入口:链兜底 / 节点钉腿 / 交互透传(节点表=config/finqa_nodes.yaml)",
    )
    parser.add_argument("--node", metavar="ID", help="钉腿:节点表 id(enabled:false 测试腿仅此路可达)")
    parser.add_argument(
        "-i", "--interactive", action="store_true",
        help="交互式:同 argv 去 -p 前台透传(zcode/codex 不支持)",
    )
    parser.add_argument("payload", nargs="*", help="无头=问题文本;交互=透传给子引擎的额外参数")
    args = parser.parse_args()

    try:
        timeout_seconds = float(os.environ.get("FINQA_NODE_TIMEOUT") or _DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        timeout_seconds = _DEFAULT_TIMEOUT_SECONDS

    nodes = _load_nodes()
    pool = _load_pool()

    if args.node is not None:
        entry = next(
            (item for item in nodes if args.node in (item["id"], item.get("alias"))),
            None,
        )
        if entry is None:
            print(f"finqa-chain: unknown node: {args.node}", file=sys.stderr)
            return _EXIT_USAGE
        node, failure = _resolve_node(entry, pool)
        if node is None:
            # 钉腿是显式意图:池禁/悬空一律拒绝(无头与 -i 同语义),不静默换腿——
            # 静默换腿会把「池禁」伪装成「腿失败」,污染对照实验归因。
            print(f"finqa-chain: pinned leg refused: {args.node}: {failure}", file=sys.stderr)
            return _EXIT_USAGE
        if args.interactive:
            return _run_interactive(node, args.payload)
        questions = _read_questions(args.payload)
        if questions is None:
            parser.print_usage(sys.stderr)
            return _EXIT_USAGE
        return _answer_via_legs([node], questions, timeout_seconds)

    if args.interactive:
        _banner("-i 需与 --node 同用(交互式按腿起);走链请去掉 -i")
        return _EXIT_USAGE
    questions = _read_questions(args.payload)
    if questions is None:
        parser.print_usage(sys.stderr)
        return _EXIT_USAGE
    chain: list[dict] = []
    for entry in nodes:
        node, failure = _resolve_node(entry, pool)
        if node is None:
            # 池禁/悬空:链序 fail-visible 跳过 + 横幅(不尝试、不伪装成腿失败)
            if entry.get("enabled"):
                _banner(f"POOL-DISABLED skip {entry['id']}({failure})")
            continue
        if not entry.get("enabled"):
            continue  # 节点级 enabled:false(测试腿)维持现状:静默不入链
        chain.append(node)
    return _answer_via_legs(chain, questions, timeout_seconds)


if __name__ == "__main__":
    sys.exit(main())
