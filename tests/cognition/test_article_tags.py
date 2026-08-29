"""文章标签系统测试（设计稿 v2「验证」节逐条覆盖）。

覆盖：规则打标 / 多标合并 / 墓碑 last-write-wins / 有效集 admit /
幂等 reconciler / ingest 钩子失败不阻塞（LOCK_NB 跳径）/ 路径解析
一致性（对 scraper 实现逐分支）/ 栏目归一化全词表 / compaction 后
0600 / rule_id 内容哈希版本化 / 手动标签限额 / scraper 尾部钩子。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fin_analyse.cognition import article_tags as at
from fin_analyse.cognition.article_tags import (
    CONTENT_DIM,
    MANUAL_DIM,
    RuleTable,
    TagStore,
    canonical_rules_bytes,
    load_rules,
    reconcile,
    safe_index_article_path,
    tag_saved_articles,
)

TWO_RULES = [
    {"name": "greet", "tag": "寒暄", "keywords": ["谢谢"]},
    {"name": "research", "tag": "研报总结", "keywords": ["研报"]},
]


@pytest.fixture()
def kb_root(tmp_path: Path) -> Path:
    (tmp_path / "articles").mkdir()
    (tmp_path / "runtime" / "cognition").mkdir(parents=True)
    return tmp_path


def write_config(directory: Path, rules: list[dict], name: str = "rules.json") -> Path:
    config = directory / name
    config.write_text(
        json.dumps({"name": "content_rules", "rules": rules}, ensure_ascii=False),
        encoding="utf-8",
    )
    return config


def two_rule_config(directory: Path) -> Path:
    return write_config(directory, TWO_RULES)


def make_article(
    kb_root: Path,
    article_id: str,
    *,
    title: str = "半导体产业链跟踪",
    body: str = "券商研报上调盈利预测，目标价五十元。",
    column: str = "普通",
    is_qa: bool = False,
    incomplete: bool = False,
) -> Path:
    path = kb_root / "articles" / f"{article_id}.md"
    frontmatter = "\n".join(
        [
            "---",
            f"id: {article_id}",
            f"topic_id: {article_id}",
            "date: 2026-08-28 09:00",
            f"score: {'' if is_qa else '9.1'}",
            f"column: {column}",
            "companies: []",
            "tags: []",
            f"is_qa: {is_qa}",
            f"type: {'q&a' if is_qa else 'talk'}",
            "article_url: ",
            "content_source: topic_detail",
            f"incomplete: {incomplete}",
            "incomplete_reason: ",
            "completeness_version: 1",
            "image_count: 0",
            "images: []",
            "---",
        ]
    )
    path.write_text(f"{frontmatter}\n\n# {title}\n\n{body}\n", encoding="utf-8")
    return path


def make_index(kb_root: Path, entries: list[dict]) -> None:
    (kb_root / "index.json").write_text(
        json.dumps({"articles": entries, "total": 999}, ensure_ascii=False),
        encoding="utf-8",
    )


def index_entry(article_id: str, path: Path, **overrides) -> dict:
    entry = {
        "id": article_id,
        "topic_id": article_id,
        "date": "2026-08-28 09:00",
        "score": 9.1,
        "column": "普通",
        "companies": [],
        "tags": [],
        "title": "半导体产业链跟踪",
        "char_count": 100,
        "path": str(path),
        "type": "talk",
    }
    entry.update(overrides)
    return entry


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _seed_corpus(kb_root: Path) -> None:
    p1 = make_article(kb_root, "a1")
    p2 = make_article(kb_root, "a2", body="谢谢老师。", title="寒暄")
    p3 = make_article(kb_root, "a3", body="完全无关正文。", title="无关键词")
    make_index(kb_root, [index_entry("a1", p1), index_entry("a2", p2), index_entry("a3", p3)])


# ── 有效集语义（盲评 F8）：墓碑 last-write-wins + admit ─────────────


def test_tombstone_last_write_wins(kb_root: Path) -> None:
    store = TagStore(kb_root)
    now = datetime.now(UTC)
    assert store.tag_article("a1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1", now=now).status == "added"
    assert store.remove_tag("a1", "研报总结", now=now).status == "added"
    assert store.effective_tags("a1") == {CONTENT_DIM: [], MANUAL_DIM: []}
    # 重新 add → 再度有效
    assert store.tag_article("a1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1", now=now).status == "added"
    assert store.effective_tags("a1")[CONTENT_DIM] == ["研报总结"]
    # 历史保留：3 行、含墓碑
    rows = read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")
    assert [row["action"] for row in rows] == ["add", "remove", "add"]


def test_effective_set_admit(kb_root: Path) -> None:
    store = TagStore(kb_root)
    assert store.tag_article("a1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1").status == "added"
    duplicate = store.tag_article("a1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1")
    assert duplicate.status == "skipped_present"
    rows = read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")
    assert len(rows) == 1  # admit：有效集已有该 tag，不追加行
    assert store.remove_tag("a1", "不存在").status == "skipped_absent"


def test_multi_tag_merge_across_dims(kb_root: Path) -> None:
    store = TagStore(kb_root)
    assert store.tag_article("a1", "提问", dim=CONTENT_DIM, source="auto", rule_id="r1").status == "added"
    assert store.tag_article("a1", "学习方法探讨", dim=MANUAL_DIM, source="manual").status == "added"
    effective = store.effective_tags("a1")
    assert effective[CONTENT_DIM] == ["提问"]
    assert effective[MANUAL_DIM] == ["学习方法探讨"]


def test_manual_tag_limits(kb_root: Path) -> None:
    store = TagStore(kb_root)
    assert store.tag_article("a1", "x" * 25, dim=MANUAL_DIM, source="manual").status == "invalid"
    assert store.tag_article("a1", "y" * 24, dim=MANUAL_DIM, source="manual").status == "added"
    for i in range(9):
        assert store.tag_article("a1", f"标签{i}", dim=MANUAL_DIM, source="manual").status == "added"
    assert store.tag_article("a1", "第十一个", dim=MANUAL_DIM, source="manual").status == "invalid"
    assert len(store.effective_tags("a1")[MANUAL_DIM]) == 10


# ── 锁协议：append LOCK_NB 有界重试 / compaction 阻塞 + 0600（F4/F5/F15）


class _DirLock:
    def __init__(self, directory: Path) -> None:
        self._fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)


def test_append_lock_busy_is_reported_not_raised(kb_root: Path) -> None:
    lock = _DirLock(kb_root / "runtime" / "cognition")
    try:
        result = TagStore(kb_root).tag_article(
            "a1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1"
        )
    finally:
        lock.release()
    assert result.status == "lock_busy"
    assert not (kb_root / "runtime" / "cognition" / "article_tags.jsonl").exists()


def test_compaction_keeps_rows_drops_torn_and_forces_0600(kb_root: Path) -> None:
    store = TagStore(kb_root)
    assert store.tag_article("a1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1").status == "added"
    assert store.tag_article("a1", "学习方法探讨", dim=MANUAL_DIM, source="manual").status == "added"
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("{torn json 半行\n")
    os.chmod(store.path, 0o664)  # 模拟生产 0664 污染

    result = store.compact()

    assert result == at.CompactionResult(rows_kept=2, torn_dropped=1)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    rows = read_jsonl(store.path)
    assert len(rows) == 2  # 墓碑与历史不物理删


# ── 规则引擎：rule_id 内容哈希版本化 + fallback（盲评 F13） ──────────


def test_rule_id_derives_from_config_content(tmp_path: Path) -> None:
    payload_a = {"name": "content_rules", "rules": [{"name": "k", "tag": "研报总结", "keywords": ["研报"]}]}
    config_a = tmp_path / "a.json"
    config_a.write_text(json.dumps(payload_a, ensure_ascii=False), encoding="utf-8")
    table_a = load_rules(config_a)
    assert table_a.fallback is False
    expected_sha = hashlib.sha256(canonical_rules_bytes(payload_a)).hexdigest()[:8]
    assert table_a.rule_id == f"content_rules.{expected_sha}"

    # 改规则（换关键词）即新 id
    payload_b = {"name": "content_rules", "rules": [{"name": "k", "tag": "研报总结", "keywords": ["券商"]}]}
    config_b = tmp_path / "b.json"
    config_b.write_text(json.dumps(payload_b, ensure_ascii=False), encoding="utf-8")
    assert load_rules(config_b).rule_id != table_a.rule_id

    # 纯空白/键序差异 = 同语义 → 同 id
    config_c = tmp_path / "c.json"
    config_c.write_text(
        json.dumps(dict(reversed(list(payload_a.items()))), ensure_ascii=False, indent=4) + "\n\n",
        encoding="utf-8",
    )
    assert load_rules(config_c).rule_id == table_a.rule_id


def test_rules_fallback_on_missing_or_invalid_config(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    at.reset_fallback_warning()
    missing = load_rules(tmp_path / "nope.json")
    assert missing.fallback is True
    assert missing.rule_id.startswith("content_rules.")

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fin_analyse.cognition.article_tags"):
        at.reset_fallback_warning()
        invalid = load_rules(bad)
    assert invalid.fallback is True
    assert any("falling back to built-in" in record.message for record in caplog.records)

    schema_bad = tmp_path / "schema.json"
    schema_bad.write_text(json.dumps({"name": "content_rules", "rules": [{"tag": "缺name"}]}), encoding="utf-8")
    at.reset_fallback_warning()
    assert load_rules(schema_bad).fallback is True

    # fallback 表 rule_id 与内建表一致（provenance 不撒谎）
    assert missing.rule_id == at._builtin_table().rule_id


def test_repo_rules_config_is_semantically_valid() -> None:
    table = load_rules()
    assert table.fallback is False
    assert re.fullmatch(r"content_rules\.[0-9a-f]{8}", table.rule_id)
    assert any(rule.tag == "学习方法探讨" for rule in table.rules)


# ── 路径解析一致性（F17：与 scraper 缝逐分支同语义） ─────────────────


def _consistency_cases(kb_root: Path) -> list[dict]:
    inside = kb_root / "articles" / "plain.md"
    inside.write_text("x", encoding="utf-8")
    outside = kb_root / "outside.md"
    outside.write_text("x", encoding="utf-8")
    link = kb_root / "articles" / "link.md"
    link.symlink_to(outside)
    return [
        {},
        {"file": "plain.md"},
        {"file": "plain.md", "path": str(inside)},
        {"file": "./plain.md"},
        {"file": "sub/plain.md"},
        {"file": "/absolute/plain.md"},
        {"file": "."},
        {"file": ".."},
        {"file": 123},
        {"path": str(inside)},
        {"path": "articles/plain.md"},
        {"path": "articles/plain.md", "file": ""},
        {"path": str(outside)},
        {"path": "../outside.md"},
        {"path": "  "},
        {"path": str(link)},
        {"file": "plain.md", "path": str(outside)},
    ]


def test_path_resolution_consistent_with_scraper_seam(kb_root: Path) -> None:
    from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

    scraper = CdpBridgeScraper(knowledge_base_root=kb_root)
    for entry in _consistency_cases(kb_root):
        assert safe_index_article_path(kb_root, entry) == scraper._safe_index_article_path(entry), entry


def test_path_resolution_rejects_escape_and_accepts_path_fallback(kb_root: Path) -> None:
    inside = make_article(kb_root, "20260828_a")
    # 无 file 字段条目（存量 621 条形态）依赖 path 回退
    assert safe_index_article_path(kb_root, {"path": str(inside)}) == inside
    assert safe_index_article_path(kb_root, {"path": "../../etc/passwd"}) is None
    assert safe_index_article_path(kb_root, {"file": "missing.md"}) == kb_root / "articles" / "missing.md"


# ── 派生维度（栏目 F12 / 质量 / 深化 F10） ──────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("星大派锐评", "锐评"),
        ("星大派特刊", "特刊"),
        ("星大派好问题", "好问题"),
        ("凤仙郡小故事", "小故事"),
        ("问题回答", "问答"),
        ("回答问题", "问答"),
        ("普通", "普通"),
        ("重中之重", "重中之重"),
        ("大锅饭的宏观思考", "宏观思考"),
        ("版本强势英雄", "其他(游戏栏)"),
        ("新出现的栏目", "其他:新出现的栏目"),
        ("", "其他:"),
    ],
)
def test_column_normalization_full_vocabulary(raw: str, expected: str) -> None:
    assert at.normalize_column(raw) == expected


def test_derive_quality(kb_root: Path) -> None:
    complete = make_article(kb_root, "q1")
    truncated_flag = make_article(kb_root, "q2", incomplete=True)
    ellipsis = make_article(kb_root, "q3", body="正文结尾省略…")
    assert at.derive_quality(kb_root, index_entry("q1", complete)) == "完整"
    assert at.derive_quality(kb_root, index_entry("q2", truncated_flag)) == "截断"
    assert at.derive_quality(kb_root, index_entry("q3", ellipsis)) == "截断"
    assert at.derive_quality(kb_root, index_entry("q4", kb_root / "articles" / "ghost.md")) == "文件缺失"
    assert at.derive_quality(kb_root, {}) == "文件缺失"


def test_derive_deepen_single_enumeration(kb_root: Path) -> None:
    compact_dir = kb_root / "runtime" / "cognition" / "deep_read_artifacts" / "compact"
    compact_dir.mkdir(parents=True)
    (compact_dir / "a1.json").write_text("{}", encoding="utf-8")
    artifacts = at.deepen_map(kb_root)
    assert at.derive_deepen(artifacts, "a1") == "有产物"
    assert at.derive_deepen(artifacts, "a2") == "无产物"
    # 非 safe id 与 DeepReadArtifactService 命名同语义
    weird_id = "空 格id"
    (compact_dir / f"{at._safe_artifact_key(weird_id)}.json").write_text("{}", encoding="utf-8")
    assert at.derive_deepen(at.deepen_map(kb_root), weird_id) == "有产物"


def test_query_filters(kb_root: Path) -> None:
    p1 = make_article(kb_root, "q1")
    p2 = make_article(kb_root, "q2", column="星大派锐评")
    p3 = make_article(kb_root, "q3", body="结尾省略…")
    make_index(
        kb_root,
        [index_entry("q1", p1), index_entry("q2", p2, column="星大派锐评"), index_entry("q3", p3)],
    )
    TagStore(kb_root).tag_article("q1", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1")

    assert at.query(kb_root) == ["q1", "q2", "q3"]
    assert at.query(kb_root, tag="研报总结") == ["q1"]
    assert at.query(kb_root, column="锐评") == ["q2"]
    assert at.query(kb_root, column="星大派锐评") == ["q2"]  # 原始名也归一化
    assert at.query(kb_root, quality="完整") == ["q1", "q2"]
    assert at.query(kb_root, quality="截断") == ["q3"]
    assert at.query(kb_root, tag="研报总结", quality="完整") == ["q1"]


# ── ingest 尾部钩子（失败 warning 不阻塞，LOCK_NB 跳径） ────────────


def test_ingest_hook_tags_saved_articles(kb_root: Path) -> None:
    p1 = make_article(kb_root, "a1")
    p2 = make_article(kb_root, "a2", body="谢谢老师，辛苦了。", title="日常寒暄")
    make_index(kb_root, [index_entry("a1", p1), index_entry("a2", p2)])
    table = load_rules(two_rule_config(kb_root))
    store = TagStore(kb_root)

    report = tag_saved_articles(["a1", "a2"], kb_root=kb_root, rules=table, store=store)
    assert (report.tagged, report.already_tagged, report.lock_busy, report.errors) == (2, 0, 0, 0)
    rows = read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")
    assert {row["tag"] for row in rows} == {"研报总结", "寒暄"}
    assert all(row["dim"] == CONTENT_DIM and row["source"] == "auto" for row in rows)
    assert all(row["rule_id"] == table.rule_id for row in rows)

    # 幂等：重放同 saved_ids → 全部 skipped_present
    replay = tag_saved_articles(["a1", "a2"], kb_root=kb_root, rules=table, store=store)
    assert (replay.tagged, replay.already_tagged) == (0, 2)
    assert len(read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")) == 2


def test_ingest_hook_lock_busy_counts_without_raising(kb_root: Path) -> None:
    p1 = make_article(kb_root, "a1")
    make_index(kb_root, [index_entry("a1", p1)])
    lock = _DirLock(kb_root / "runtime" / "cognition")
    try:
        report = tag_saved_articles(["a1"], kb_root=kb_root, rules=load_rules(two_rule_config(kb_root)))
    finally:
        lock.release()
    assert report.lock_busy == 1
    assert report.tagged == 0
    assert report.incomplete is True  # busy 折进计数 → 上层 warning，不 raise


def test_ingest_hook_missing_entry_and_unmatchable(kb_root: Path) -> None:
    make_article(kb_root, "a1", body="完全无关的正文。", title="无关键词")
    make_article(kb_root, "a2")
    make_index(kb_root, [index_entry("a1", kb_root / "articles" / "a1.md")])
    report = tag_saved_articles(
        ["a1", "a2", "ghost"], kb_root=kb_root, rules=load_rules(two_rule_config(kb_root))
    )
    assert report.unmatchable == 1  # a1 无关键词命中
    assert report.errors == 2  # a2 与 ghost 都不在 index
    assert any("index_entry_missing:a2" in warning for warning in report.warnings)
    assert not (kb_root / "runtime" / "cognition" / "article_tags.jsonl").exists()


def test_ingest_hook_empty_ids_is_noop(kb_root: Path) -> None:
    report = tag_saved_articles([], kb_root=kb_root)
    assert report.requested == 0
    assert not report.incomplete


# ── reconciler（backfill 即 reconciler，可重入，F6/F7/F16） ──────────


def test_reconcile_is_reentrant_and_idempotent(kb_root: Path) -> None:
    _seed_corpus(kb_root)
    config = two_rule_config(kb_root)
    first = reconcile(kb_root, config_path=config)
    assert (first.total_articles, first.tagged, first.unmatchable) == (3, 2, 1)

    second = reconcile(kb_root, config_path=config)
    assert second.tagged == 0  # 幂等：已有 content 有效标的不重打

    rows = read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")
    assert len(rows) == 2


def test_reconcile_refresh_retires_old_rule_id(kb_root: Path) -> None:
    _seed_corpus(kb_root)
    config_v1 = write_config(
        kb_root,
        [{"name": "r", "tag": "研报总结", "keywords": ["研报", "谢谢"]}],
        name="v1.json",
    )
    first = reconcile(kb_root, config_path=config_v1)
    assert first.tagged == 2

    # 规则更新：寒暄不再算研报总结 → refresh 对旧 rule_id 行所在文章重打
    config_v2 = write_config(
        kb_root,
        [{"name": "r", "tag": "研报总结", "keywords": ["研报"]}],
        name="v2.json",
    )
    refreshed = reconcile(kb_root, config_path=config_v2, refresh=True)
    # a1 重打成功（旧行被新行按行序覆盖）；a2 新规则不再命中 → unmatchable，
    # v1 局限：其旧标保留（最终修正路径 = 让规则表覆盖该类）。
    assert refreshed.tagged == 1
    assert refreshed.unmatchable == 1
    assert refreshed.rule_id != first.rule_id

    rows = read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")
    assert len([row for row in rows if row["rule_id"] == refreshed.rule_id]) == 1
    store = TagStore(kb_root)
    assert store.effective_tags("a1")[CONTENT_DIM] == ["研报总结"]
    assert store.effective_tags("a2")[CONTENT_DIM] == ["研报总结"]  # 旧标保留
    # refresh 只动旧 rule_id 行所在文章：a3 依旧无标
    assert store.effective_tags("a3")[CONTENT_DIM] == []


def test_reconcile_counts_orphans_and_missing_files(kb_root: Path) -> None:
    p1 = make_article(kb_root, "a1")
    make_index(kb_root, [index_entry("a1", p1), index_entry("ghost-file", kb_root / "articles" / "nope.md")])
    (kb_root / "articles" / "stray_orphan.md").write_text("孤儿文件", encoding="utf-8")
    TagStore(kb_root).tag_article(
        "drifted-away-id", "研报总结", dim=CONTENT_DIM, source="auto", rule_id="r1"
    )  # id 漂移孤儿标签行：article_id 不在 index

    report = reconcile(kb_root, config_path=two_rule_config(kb_root))

    assert report.orphan_tag_rows == 1
    assert report.orphan_files == 1
    assert report.skipped_file_missing == 1
    assert report.tagged == 1


def test_reconcile_dry_run_writes_nothing(kb_root: Path) -> None:
    _seed_corpus(kb_root)
    report = reconcile(kb_root, dry_run=True, config_path=two_rule_config(kb_root))
    assert report.tagged == 2
    assert not (kb_root / "runtime" / "cognition" / "article_tags.jsonl").exists()


def test_reconcile_empty_index_is_reported(kb_root: Path) -> None:
    report = reconcile(kb_root, config_path=two_rule_config(kb_root))
    assert report.total_articles == 0
    assert report.tagged == 0


# ── scraper 尾部钩子：成功写入 + 失败不阻塞（设计稿 §3） ─────────────


HERMETIC_RULES = [{"name": "research", "tag": "研报总结", "keywords": ["研报"]}]
DOM_TEXT = (
    "三线文案大锅饭\n%DATE%\n半导体产业链跟踪\n"
    "能量评分 9.1 分\n" + "券商研报上调盈利预测。" * 20
)


def _run_incremental_with_stubbed_surfaces(kb_root: Path, monkeypatch: pytest.MonkeyPatch):
    from fin_analyse.scraper import cdp_scraper as subject

    now = datetime.now(UTC)
    dom_date = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M")
    scraper = subject.CdpBridgeScraper(knowledge_base_root=kb_root)
    scraper._client = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        scraper,
        "_load_group_timeline_batch_first",
        lambda _cutoff: subject.GroupTimelineLoadResult(
            full_text=DOM_TEXT.replace("%DATE%", dom_date),
            timeline_dates=[now - timedelta(hours=6)],
            reached_page_end=True,
        ),
    )
    monkeypatch.setattr(scraper, "_images_by_date_from_page", lambda: {})
    monkeypatch.setattr(scraper, "_write_priority_events_for_new_articles", lambda *_args: 0)
    monkeypatch.setattr(scraper, "_ensure_deep_read_artifacts_for_new", lambda *_args: 0)
    monkeypatch.setattr(scraper, "_repair_active_g_support_artifacts", lambda _result: None)
    return scraper.run_incremental_with_result()


def test_scraper_tail_hook_writes_tags(kb_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fin_analyse.cognition import article_tags as tags_module

    base = at._table_from_mapping({"name": "content_rules", "rules": HERMETIC_RULES})
    table = RuleTable(name=base.name, rules=base.rules, rule_id="content_rules.deadbeef", fallback=False)
    monkeypatch.setattr(tags_module, "load_rules", lambda path=None: table)

    result = _run_incremental_with_stubbed_surfaces(kb_root, monkeypatch)

    assert result.scrape_completed is True
    assert result.new_count == 1
    assert not [w for w in result.warnings if w.startswith("article_tags")]

    index = json.loads((kb_root / "index.json").read_text())
    article_id = index["articles"][0]["id"]
    rows = read_jsonl(kb_root / "runtime" / "cognition" / "article_tags.jsonl")
    assert len(rows) == 1
    assert rows[0]["article_id"] == article_id
    assert rows[0]["tag"] == "研报总结"
    assert rows[0]["rule_id"] == "content_rules.deadbeef"


def test_scraper_tail_hook_failure_does_not_block(kb_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fin_analyse.cognition import article_tags as tags_module

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated hook crash")

    monkeypatch.setattr(tags_module, "tag_saved_articles", _explode)

    result = _run_incremental_with_stubbed_surfaces(kb_root, monkeypatch)

    assert result.scrape_completed is True  # ingest 不被阻塞
    assert result.new_count == 1
    assert any(w.startswith("article_tags_failed") for w in result.warnings)


# ── CLI 组注册与基本走向 ────────────────────────────────────────────


def test_cli_group_registered_under_fin_cognition() -> None:
    from fin_analyse.cognition.cli import main

    assert "tags" in main.commands
    tags = main.commands["tags"]
    assert {"add", "remove", "list", "query", "compact", "backfill"} <= set(tags.commands)


def test_cli_add_remove_roundtrip(kb_root: Path) -> None:
    from click.testing import CliRunner

    from fin_analyse.cognition.article_tags import tags_cli

    p1 = make_article(kb_root, "a1")
    make_index(kb_root, [index_entry("a1", p1)])
    runner = CliRunner()

    result = runner.invoke(tags_cli, ["add", "a1", "重点跟踪", "--kb-root", str(kb_root)])
    assert result.exit_code == 0, result.output
    assert "added" in result.output

    result = runner.invoke(tags_cli, ["remove", "a1", "重点跟踪", "--kb-root", str(kb_root)])
    assert result.exit_code == 0, result.output
    assert TagStore(kb_root).effective_tags("a1")[MANUAL_DIM] == []

    result = runner.invoke(tags_cli, ["add", "a1", "x" * 25, "--kb-root", str(kb_root)])
    assert result.exit_code == 1
    assert "invalid" in result.output


def test_cli_backfill_and_query_outputs(kb_root: Path) -> None:
    from click.testing import CliRunner

    from fin_analyse.cognition.article_tags import tags_cli

    _seed_corpus(kb_root)
    runner = CliRunner()
    result = runner.invoke(
        tags_cli, ["backfill", "--kb-root", str(kb_root), "--config", str(two_rule_config(kb_root))]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tagged"] == 2
    assert payload["total_articles"] == 3

    result = runner.invoke(tags_cli, ["query", "--kb-root", str(kb_root)])
    assert result.exit_code == 0
    assert "# 3 articles [no filter]" in result.output

    result = runner.invoke(tags_cli, ["query", "--kb-root", str(kb_root), "--tag", "研报总结"])
    assert result.exit_code == 0
    assert "# 1 articles" in result.output
    result = runner.invoke(tags_cli, ["query", "--kb-root", str(kb_root), "--tag", "寒暄"])
    assert result.exit_code == 0
    assert "# 1 articles" in result.output
