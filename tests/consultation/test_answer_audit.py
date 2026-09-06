"""Tests for the F1 answer-audit旁路 (consult-answer-audit-v1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from fin_analyse.consultation.answer_audit import (
    audit_answer,
    audit_and_log,
    load_config,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def config() -> dict:
    return load_config(reload=True)


def test_clean_answer_has_no_findings(config: dict) -> None:
    text = "核心判断：倾向持有。老师 9/3 锐评给出科技调整到位的定位，你当前仓位约 12%，失效线在 37 元下方。"
    assert audit_answer(text, config) == ()


def test_tool_name_and_jargon_detected(config: dict) -> None:
    text = "我先调 read_g_context 组织框架，再按主线环境刻度 v5 定仓位，这是 G 判定。"
    findings = audit_answer(text, config)
    ids = {finding.check_id for finding in findings}
    assert {"internal-tool-names", "internal-jargon"} <= ids


def test_schema_tokens_and_launcher_marks_detected(config: dict) -> None:
    text = "结论落在 consultation_product 合同里；细节见 finqa_chain 的 fallback.tsv。"
    ids = {finding.check_id for finding in audit_answer(text, config)}
    assert "schema-gap-tokens" in ids
    assert "launcher-marks" in ids


def test_first_line_only_semantics(config: dict) -> None:
    clean = "结论：倾向持有。\n材料齐了之后再说（非首行，不命中）。"
    flagged = "材料齐了，开始分析。\n正文忽略。"
    assert audit_answer(clean, config) == ()
    ids = {finding.check_id for finding in audit_answer(flagged, config)}
    assert "firstline-metanarration" in ids


def test_empty_and_non_string_input(config: dict) -> None:
    assert audit_answer("", config) == ()


def test_findings_carry_closed_lexicon_tokens_only(config: dict) -> None:
    text = "走 finqa_chain，另有 CU-7 缺口。"
    tokens: set[str] = set()
    for finding in audit_answer(text, config):
        tokens.update(finding.tokens)
    assert any("CU-7" in token for token in tokens)
    assert all(len(token) < 60 for token in tokens)


def test_audit_and_log_appends_rows_and_respects_mode(tmp_path: Path, config: dict) -> None:
    state = tmp_path / "state"
    rows = audit_and_log(
        "答案引用 read_market_snapshot 与 CU-3。",
        call_id="c1",
        node_id="commandcode-flash",
        state_root=state,
    )
    assert rows >= 2
    target = state / "finqa-chain" / "audit.tsv"
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == rows
    for line in lines:
        fields = line.split("\t")
        assert len(fields) == 7
        assert fields[1] == "c1" and fields[2] == "commandcode-flash"
    assert target.stat().st_mode & 0o777 == 0o600


def test_audit_and_log_clean_answer_writes_nothing(tmp_path: Path, config: dict) -> None:
    state = tmp_path / "state"
    rows = audit_and_log("干净答案：倾向持有，失效线 37 元。", call_id="c2", node_id="n", state_root=state)
    assert rows == 0
    assert not (state / "finqa-chain" / "audit.tsv").exists()


def test_audit_and_log_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises((OSError, ValueError)):
        audit_and_log(
            "任意文本",
            call_id="c3",
            node_id="n",
            state_root=tmp_path,
            config_path=tmp_path / "missing.yaml",
        )


def test_probes_lexicon_refs_resolve_single_source() -> None:
    """单源守护（设计门 P1-3）：probes yaml 的 lexicon 族必须可解析，且不得内联泄漏词表。"""

    probes = yaml.safe_load((_PROJECT_ROOT / "config" / "behavior_probes.yaml").read_text(encoding="utf-8"))
    lexicons = load_config(reload=True)["lexicons"]
    for probe in probes["probes"]:
        for check in probe.get("checks", []):
            family = check.get("lexicon")
            if family:
                assert family in lexicons, f"{probe['id']}/{check['id']}: unknown family {family}"
            else:
                assert check.get("kind") == "manual" or "regex" in check, (
                    f"{probe['id']}/{check['id']}: needs lexicon, regex, or manual"
                )
