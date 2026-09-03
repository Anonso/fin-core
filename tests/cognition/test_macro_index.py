"""macro_index 侧车：基线校准、增量打标、版本重评、排除裁决。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fin_analyse.cognition.macro_index import (
    classify_macro_row,
    load_macro_entries,
    load_rules,
    macro_index_path,
    read_macro_index,
    update_macro_index,
)


def _write_index(kb_root: Path, rows: list[dict[str, Any]]) -> None:
    (kb_root / "index.json").write_text(
        json.dumps({"articles": rows}, ensure_ascii=False), encoding="utf-8"
    )


def _row(article_id: str, column: str, title: str, date: str, companies=None) -> dict[str, Any]:
    return {
        "id": article_id,
        "column": column,
        "title": title,
        "date": date,
        "companies": companies or [],
    }


def _rules(
    *,
    kept: list[dict[str, str]] | None = None,
    excluded: list[str] | None = None,
    macro_terms: list[str] | None = None,
    version: int = 1,
) -> dict[str, Any]:
    base = load_rules()
    return {
        **base,
        "rules_version": version,
        "macro_terms": macro_terms if macro_terms is not None else base["macro_terms"],
        "kept": kept if kept is not None else base["kept"],
        "excluded": excluded if excluded is not None else base["excluded"],
    }


def test_repo_config_shape() -> None:
    rules = load_rules()
    assert rules["schema_version"] == "fin.macro-rules/v1"
    assert rules["rules_version"] == 1
    assert len(rules["kept"]) == 12
    assert rules["excluded"] == []
    assert "黄金" in rules["macro_terms"]


def test_classify_kept_daily_rule_and_exclusions() -> None:
    rules = _rules(
        kept=[{"article_id": "manual-1", "reason": "owner 校准"}],
        macro_terms=["市场", "黄金", "复盘"],
    )
    assert classify_macro_row(
        _row("manual-1", "普通", "某公司深度报告（低分）", "2026-08-01", companies=["a", "b", "c"]),
        rules,
    )["source"] == "manual_keep"
    assert classify_macro_row(
        _row("hot-1", "星大派每日热点", "星大派每日热点（0902）", "2026-09-02"),
        rules,
    )["source"] == "daily_hot"
    tagged = classify_macro_row(
        _row("rule-1", "普通", "当前市场流动性与大类资产复盘", "2026-09-01"), rules
    )
    assert tagged is not None and tagged["source"] == "rule"
    assert "市场" in tagged["matched_terms"]
    assert (
        classify_macro_row(
            _row("report-1", "普通", "某公司中报点评", "2026-09-01"), rules
        )
        is None
    )
    assert (
        classify_macro_row(
            _row("companies-1", "普通", "当前市场复盘", "2026-09-01", companies=["a", "b", "c"]),
            rules,
        )
        is None
    )
    assert (
        classify_macro_row(_row("plain-1", "普通", "还能撑一段时间", "2026-08-02"), rules)
        is None
    )


def test_baseline_seeds_kept_and_daily_only(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    rules = _rules(
        kept=[
            {"article_id": "k1", "reason": "owner 校准"},
            {"article_id": "k2", "reason": "owner 校准"},
        ]
    )
    _write_index(
        kb_root,
        [
            _row("k1", "普通", "还能撑一段时间，宏观回答", "2026-08-02"),
            _row("k2", "普通", "长鑫存储 IPO 对 A 股资金影响", "2026-07-07"),
            _row("hot-1", "星大派每日热点", "星大派每日热点（0901）", "2026-09-01"),
            _row("hot-2", "星大派每日热点", "星大派每日热点（0831）", "2026-08-31"),
            _row("hist-rule", "普通", "当前市场复盘", "2026-08-10"),
            _row("hist-report", "普通", "某公司深度报告", "2026-08-11"),
        ],
    )
    report = update_macro_index(kb_root, rules=rules)
    assert report.baseline_seeded is True
    assert report.incomplete is False
    entries = load_macro_entries(kb_root)
    assert entries is not None
    assert {entry["article_id"] for entry in entries} == {"k1", "k2", "hot-1", "hot-2"}
    payload = read_macro_index(kb_root)
    assert payload is not None
    assert payload["rules_version"] == 1
    assert payload["baseline_at"]
    mode = macro_index_path(kb_root).stat().st_mode
    assert mode & 0o777 == 0o600


def test_incremental_tags_only_saved_ids_and_is_idempotent(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    rules = _rules(kept=[])
    _write_index(
        kb_root,
        [
            _row("hot-1", "星大派每日热点", "星大派每日热点（0901）", "2026-09-01"),
            _row("new-rule", "普通", "当前市场复盘", "2026-09-02"),
            _row("new-noise", "普通", "某公司深度报告", "2026-09-02"),
        ],
    )
    first = update_macro_index(kb_root, saved_ids=["hot-1", "new-rule", "new-noise"], rules=rules)
    assert first.tagged == 2
    entries = load_macro_entries(kb_root)
    assert {entry["article_id"] for entry in entries} == {"hot-1", "new-rule"}
    replay = update_macro_index(kb_root, saved_ids=["new-rule"], rules=rules)
    assert replay.already == 1
    assert replay.tagged == 0
    assert len(load_macro_entries(kb_root)) == 2


def test_version_bump_reevaluates_stored_rule_entries(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    rules_v1 = _rules(
        kept=[{"article_id": "k1", "reason": "owner 校准"}],
        macro_terms=["市场", "复盘"],
        version=1,
    )
    _write_index(
        kb_root,
        [
            _row("k1", "普通", "还能撑一段时间，宏观回答", "2026-08-02"),
            _row("old-rule", "普通", "当前市场复盘", "2026-09-01"),
        ],
    )
    update_macro_index(kb_root, saved_ids=["old-rule"], rules=rules_v1)
    assert {e["article_id"] for e in load_macro_entries(kb_root)} == {"k1", "old-rule"}

    rules_v2 = _rules(
        kept=[{"article_id": "k1", "reason": "owner 校准"}],
        macro_terms=["流动性"],
        version=2,
    )
    report = update_macro_index(kb_root, rules=rules_v2)
    assert report.removed == 1
    assert {e["article_id"] for e in load_macro_entries(kb_root)} == {"k1"}
    payload = read_macro_index(kb_root)
    assert payload["rules_version"] == 2


def test_excluded_drops_entry_and_blocks_saved_reentry(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    rules = _rules(
        excluded=["hot-1"],
        macro_terms=["市场", "复盘"],
    )
    _write_index(
        kb_root,
        [
            _row("hot-1", "星大派每日热点", "星大派每日热点（0901）", "2026-09-01"),
            _row("rule-1", "普通", "当前市场复盘", "2026-09-02"),
        ],
    )
    update_macro_index(kb_root, saved_ids=["hot-1", "rule-1"], rules=rules)
    assert {e["article_id"] for e in load_macro_entries(kb_root)} == {"rule-1"}
    replay = update_macro_index(kb_root, saved_ids=["hot-1"], rules=rules)
    assert {e["article_id"] for e in load_macro_entries(kb_root)} == {"rule-1"}
    assert replay.removed == 0


def test_missing_index_file_reads_none(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    assert read_macro_index(kb_root) is None
    assert load_macro_entries(kb_root) is None


def test_manual_keep_drop_when_removed_from_config(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    kept = {"article_id": "k1", "reason": "owner 校准"}
    _write_index(kb_root, [_row("k1", "普通", "还能撑一段时间，宏观回答", "2026-08-02")])
    update_macro_index(kb_root, rules=_rules(kept=[kept]))
    assert {e["article_id"] for e in load_macro_entries(kb_root)} == {"k1"}
    report = update_macro_index(kb_root, rules=_rules(kept=[]))
    assert report.removed == 1
    assert load_macro_entries(kb_root) == []


def test_stamp_uses_provided_now(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    stamp = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
    _write_index(kb_root, [_row("hot-1", "星大派每日热点", "星大派每日热点（0901）", "2026-09-01")])
    update_macro_index(kb_root, rules=_rules(kept=[]), now=stamp)
    payload = read_macro_index(kb_root)
    assert payload["baseline_at"] == "2026-09-03T01:02:03+00:00"
