"""F1 answer-audit: leak-lexicon scan over finalized headless consultation answers.

审计是只读旁路：findings 只落账（audit.tsv），**禁止回调、拦截、改写、参与 rc**——
「命中≠处置」是本模块的产品不变量（设计 docs/design/consult-answer-audit-v1.md，
设计门 2026-09-06）。词表唯一权威源=config/answer_audit.yaml（会变项进配置）。
v2 扩展位（session 归档异步扫描，覆盖有头交互）前置未发生，不预建（家规 11）。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "answer_audit.yaml"

_cached_config: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    """One lexicon hit family: matched tokens come from the closed lexicon only."""

    check_id: str
    severity: str
    count: int
    tokens: tuple[str, ...]


def load_config(config_path: str | Path | None = None, *, reload: bool = False) -> dict[str, Any]:
    """Load (and cache) the lexicon config; `reload=True` for tests."""

    global _cached_config
    if _cached_config is not None and not reload and config_path is None:
        return _cached_config
    path = Path(config_path) if config_path else _DEFAULT_CONFIG
    import yaml

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "fin.answer-audit/v1":
        raise ValueError(f"answer-audit: malformed config: {path}")
    lexicons = payload.get("lexicons")
    checks = payload.get("checks")
    if not isinstance(lexicons, dict) or not isinstance(checks, list) or not checks:
        raise ValueError(f"answer-audit: config missing lexicons/checks: {path}")
    if config_path is None:
        _cached_config = payload
    return payload


def audit_answer(text: str, config: dict[str, Any]) -> tuple[Finding, ...]:
    """Pure scan: closed-lexicon families over the answer text; zero IO."""

    if not isinstance(text, str) or not text:
        return ()
    lexicons = config["lexicons"]
    findings: list[Finding] = []
    for check in config["checks"]:
        family = check.get("lexicon")
        pattern = lexicons.get(family)
        if not isinstance(pattern, str):
            raise ValueError(f"answer-audit: check {check.get('id')!r} unknown lexicon {family!r}")
        target = text
        if check.get("first_line_only"):
            lines = text.splitlines()
            target = lines[0] if lines else ""
        tokens = re.findall(pattern, target)
        if tokens:
            findings.append(
                Finding(
                    check_id=str(check.get("id", family)),
                    severity=str(check.get("severity", "leak")),
                    count=len(tokens),
                    tokens=tuple(dict.fromkeys(str(token) for token in tokens)),
                )
            )
    return tuple(findings)


def audit_and_log(
    text: str,
    *,
    call_id: str,
    node_id: str,
    config_path: str | Path | None = None,
    state_root: str | Path | None = None,
) -> int:
    """Scan + append one row per finding. Returns rows written (0 = clean).

    调用方（finqa_chain 钩子）负责 fail-open：本函数异常向上抛，由钩子吞成
    stderr 一行——审计自身绝不影响答案字节与 rc。tokens 只来自封闭词表（内部词
    非用户数据），不落答案原文与问题文本（家规 3）。
    """

    config = load_config(config_path)
    findings = audit_answer(text, config)
    if not findings:
        return 0
    base = Path(state_root) if state_root else (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        / "fin-analyse"
    )
    target = base / "finqa-chain" / "audit.tsv"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    rows = []
    for finding in findings:
        tokens = ";".join(finding.tokens)[:200]
        rows.append(
            f"{int(time.time())}\t{call_id}\t{node_id}\t{finding.check_id}"
            f"\t{finding.severity}\t{finding.count}\t{tokens}\n"
        )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        stream.write("".join(rows))
    if target.stat().st_mode & 0o777 != 0o600:
        os.chmod(target, 0o600)
    return len(rows)
