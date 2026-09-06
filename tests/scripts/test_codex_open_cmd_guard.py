"""codex_open.sh cmd 主评审者截断防护（BUG-055）。

cmd CLI 无头 `-p` 在工具权限被拒时以 rc=0 / subtype=success 结束
（stopReason=permission_denied），stdout 只到工具调用前为止；launcher 不得信任
rc，必须以 result 行 stopReason 白名单（end_turn）+ finalText 非空判定完整性，
不完整时走既存 glm 替补机制，不得静默透传半份评审。

glm 替补 precheck 依赖本机真实凭据文件（launcher 硬编码路径），故 fallback 用例
在本机（单 owner 自用机，即本仓唯一测试环境）断言替补被真实调用。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "codex_open.sh"

NDJSON_OK = (
    '{"type":"event","event":{"type":"run_start"}}\n'
    '{"type":"result","subtype":"success","stopReason":"end_turn",'
    '"finalText":"总评：无发现（stub 完整评审）"}\n'
)
PARTIAL_TEXT = "我来核对冻结包，先并行做几件核查："
NDJSON_DENIED_RC0 = (
    '{"type":"event","event":{"type":"text_delta","delta":"'
    + PARTIAL_TEXT
    + '"}}\n'
    '{"type":"result","subtype":"success","stopReason":"permission_denied",'
    '"finalText":"'
    + PARTIAL_TEXT
    + '"}\n'
)
# rc≠0 形态：CLI 对拒绝也可能非零退出（本机实测 9）；留痕须是提取后文本而非 NDJSON。
NDJSON_DENIED_RC9 = (
    '{"type":"event","event":{"type":"run_start"}}\n'
    '{"type":"result","subtype":"success","stopReason":"permission_denied",'
    '"finalText":"'
    + PARTIAL_TEXT
    + '"}\n'
)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _cmd_version_pin() -> str:
    """stub --version 跟随 launcher 的版本钉单源（config/finqa_nodes.yaml 顶层
    cmd_version_pin；键缺失时兜底 1.49.1，兼容钉单源入 config 前的 HEAD）。"""
    import re

    config = Path(__file__).resolve().parents[2] / "config" / "finqa_nodes.yaml"
    if config.exists():
        found = re.search(
            r'^cmd_version_pin:\s*"?([^"\s]+)"?\s*(?:#.*)?$',
            config.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if found:
            return found.group(1)
    return "1.49.1"


def _run_launcher(
    tmp_path: Path, stub_mode: str, *args: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(
        bin_dir,
        "cmd",
        f'''
case "$1" in
  --version) echo "{_cmd_version_pin()}"; exit 0 ;;
  status) echo "Authenticated"; exit 0 ;;
esac
[[ "$*" == *"-p"* ]] || {{ echo "stub: unexpected args" >&2; exit 64; }}
case "${{CMD_STUB_MODE:-{stub_mode}}}" in
  ok) printf '%s' '{NDJSON_OK}' ;;
  denied_rc0) printf '%s' '{NDJSON_DENIED_RC0}' ;;
  denied_rc9) printf '%s' '{NDJSON_DENIED_RC9}'; exit 9 ;;
  garbage) printf 'not json at all\\n' ;;
  empty) : ;;
esac
exit 0
''',
    )
    _write_stub(
        bin_dir,
        "zcode",
        '''[ "${1:-}" = "-p" ] || { echo "stub: expected -p" >&2; exit 64; }
echo "GLM REVIEW OK"
''',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path),
        "CMD_STUB_MODE": stub_mode,
    }
    return subprocess.run(
        [str(LAUNCHER), "exec", "--skip-git-repo-check", "评审 packet 正文"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


def test_cmd_end_turn_passes_finaltext(tmp_path: Path) -> None:
    proc = _run_launcher(tmp_path, "ok")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "总评：无发现（stub 完整评审）"
    assert "REVIEWER FALLBACK" not in proc.stderr


def test_cmd_permission_denied_rc0_falls_back(tmp_path: Path) -> None:
    """截断主形态：rc=0/subtype=success 但 stopReason=permission_denied。

    旧行为（bug）：rc=0 被当成功，半份评审直接透传。防护后：判不完整 →
    glm 替补重发 → 输出来自替补；半份文本不得出现在 stdout。
    """
    proc = _run_launcher(tmp_path, "denied_rc0")
    assert PARTIAL_TEXT not in proc.stdout, proc.stdout
    assert "REVIEWER FALLBACK" in proc.stderr
    # 横幅走 stdout 是 launcher 既存设计；替补输出必须在场。
    assert "GLM REVIEW OK" in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stderr
    tsv = tmp_path / "fin-analyse" / "design-gate" / "fallback.tsv"
    assert tsv.exists()
    row = tsv.read_text(encoding="utf-8").strip().split("\t")
    assert row[1] == "cmd" and row[2] == "glm" and row[4] == "run"


def test_cmd_garbage_output_falls_back(tmp_path: Path) -> None:
    proc = _run_launcher(tmp_path, "garbage")
    assert "not json" not in proc.stdout
    assert "REVIEWER FALLBACK" in proc.stderr


def test_cmd_denied_rc9_falls_back_with_readable_partial(tmp_path: Path) -> None:
    """rc≠0 拒绝形态：fallback 照走，stderr 留痕=提取后半份文本而非 NDJSON。"""
    proc = _run_launcher(tmp_path, "denied_rc9")
    assert PARTIAL_TEXT not in proc.stdout
    assert "REVIEWER FALLBACK" in proc.stderr
    assert f"[primary] {PARTIAL_TEXT}" in proc.stderr
    assert '"type":"result"' not in proc.stderr


def test_cmd_empty_output_falls_back(tmp_path: Path) -> None:
    proc = _run_launcher(tmp_path, "empty")
    assert "REVIEWER FALLBACK" in proc.stderr
    assert "GLM REVIEW OK" in proc.stdout or proc.returncode == 78
