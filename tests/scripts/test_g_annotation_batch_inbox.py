"""G 标注批次 → 裁决收件箱 producer 挂点单测（D-051 v0.1，owner 2026-09-06 拍板定位）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.mainline_candidates import scan_annotation_batch
from scripts.consume_zsxq_capture_folder import _reconcile_g_annotation_batch_inbox

_ANNOTATION = "manual-annotations/g-cognition-mainline.md"


def _write_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, as_of: str, articles: list[dict]) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    kb = tmp_path / "kb"
    annotation = kb / _ANNOTATION
    annotation.parent.mkdir(parents=True, exist_ok=True)
    annotation.write_text(f"# 标注\n\nas_of={as_of}\n", encoding="utf-8")
    (kb / "index.json").write_text(json.dumps({"articles": articles}), encoding="utf-8")
    # 缝 mock：default_knowledge_base_root 硬编码真实 HOME/共享根，不 mock 则
    # 单测隐式依赖本机生产 KB（对齐 test_cognition_rebuild_consumer_log 先例）。
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.default_knowledge_base_root",
        lambda: kb,
    )
    return kb


def _article(date: str, column: str = "普通", title: str = "T") -> dict:
    return {"date": date, "column": column, "title": title}


def test_batch_scan_counts_after_as_of_excluding_daily_hotspot(tmp_path: Path) -> None:
    result = scan_annotation_batch(
        annotation_path=tmp_path / "a.md",
        index_path=tmp_path / "i.json",
        today=date(2026, 9, 6),
    )
    assert result.disposition == "SKIPPED"  # 文件不存在 → 真相未知

    kb = tmp_path / "kb"
    (kb / _ANNOTATION).parent.mkdir(parents=True, exist_ok=True)
    (kb / _ANNOTATION).write_text("as_of=2026-09-04\n", encoding="utf-8")
    (kb / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    _article("2026-09-04 22:00"),  # as_of 当天，不计
                    _article("2026-09-05 11:28"),
                    _article("2026-09-05 14:34"),
                    _article("2026-09-05 09:00", column="星大派每日热点"),  # 排除
                    _article("2026-09-03 08:00"),  # as_of 前，不计
                    _article("bad-date"),  # 不可解析，不计
                ]
            }
        ),
        encoding="utf-8",
    )
    result = scan_annotation_batch(
        annotation_path=kb / _ANNOTATION,
        index_path=kb / "index.json",
        today=date(2026, 9, 6),
    )
    assert result.disposition == "SCANNED"
    assert result.as_of == "2026-09-04"
    assert result.lag_days == 2
    assert result.unarchived == 2
    assert result.latest_unarchived_date == "2026-09-05"


def test_reconcile_opens_batch_item_when_lag_meets_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = _write_env(
        tmp_path,
        monkeypatch,
        as_of="2026-09-04",
        articles=[_article("2026-09-05 14:34"), _article("2026-09-05 11:28")],
    )
    _reconcile_g_annotation_batch_inbox(
        {"disposition": "SCANNED", "nominated": 0, "draft_path": None},
        annotation_path=kb / _ANNOTATION,
    )
    from fin_analyse.adjudication.inbox import open_default_inbox

    row = open_default_inbox().get("g.annotation_batch")
    assert row is not None
    assert row.status == "open"
    assert row.kind == "g-annotation-batch"
    assert "落后 2 天" in row.title
    assert "未入册老师文章 2 篇" in row.title
    assert "（最新 2026-09-05）" in row.title
    assert row.payload_ref == str(kb / _ANNOTATION)  # 无提名草稿 → 指标注文档
    assert "as_of 滚动后本项自动消项" in (row.resolution_hint or "")


def test_reconcile_composes_nomination_info_into_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = _write_env(
        tmp_path, monkeypatch, as_of="2026-09-04", articles=[_article("2026-09-05 14:34")]
    )
    _reconcile_g_annotation_batch_inbox(
        {"disposition": "SCANNED", "nominated": 1, "draft_path": "/tmp/draft.md"},
        annotation_path=kb / _ANNOTATION,
    )
    from fin_analyse.adjudication.inbox import open_default_inbox

    row = open_default_inbox().get("g.annotation_batch")
    assert row is not None
    assert "其中机器提名 1 条" in row.title
    assert row.payload_ref == "/tmp/draft.md"  # 有提名草稿 → 指草稿


def test_reconcile_resolves_after_as_of_rolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_analyse.adjudication.inbox import AdjudicationItem, open_default_inbox

    kb = _write_env(
        tmp_path, monkeypatch, as_of="2026-09-04", articles=[_article("2026-09-05 14:34")]
    )
    inbox = open_default_inbox()
    inbox.reconcile(
        AdjudicationItem(item_id="g.annotation_batch", kind="g-annotation-batch", title="旧"),
        pending=True,
    )
    # owner 完成批次：as_of 滚到 9/06（覆盖 9/05 文章）→ lag=0 → auto-resolve
    (kb / _ANNOTATION).write_text("as_of=2026-09-06\n", encoding="utf-8")
    _reconcile_g_annotation_batch_inbox(
        {"disposition": "SCANNED", "nominated": 0}, annotation_path=kb / _ANNOTATION
    )
    row = inbox.get("g.annotation_batch")
    assert row is not None
    assert row.status == "resolved"
    assert row.resolve_source == "producer"


def test_reconcile_below_threshold_keeps_inbox_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_analyse.adjudication.inbox import open_default_inbox

    kb = _write_env(
        tmp_path, monkeypatch, as_of="2026-09-05", articles=[_article("2026-09-06 09:00")]
    )
    _reconcile_g_annotation_batch_inbox(
        {"disposition": "SCANNED", "nominated": 0}, annotation_path=kb / _ANNOTATION
    )
    assert open_default_inbox().get("g.annotation_batch") is None  # lag=1 < 2


def test_threshold_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = tmp_path / "adjudication.yaml"
    conf.write_text(
        "digest:\n  escalate_after_days: 3\ng_annotation_batch:\n  lag_days_threshold: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.load_adjudication_config",
        lambda path=None: {"escalate_after_days": 3, "g_lag_days_threshold": 5},
    )
    kb = _write_env(
        tmp_path, monkeypatch, as_of="2026-09-04", articles=[_article("2026-09-05 14:34")]
    )
    _reconcile_g_annotation_batch_inbox(
        {"disposition": "SCANNED", "nominated": 0}, annotation_path=kb / _ANNOTATION
    )
    from fin_analyse.adjudication.inbox import open_default_inbox

    assert open_default_inbox().get("g.annotation_batch") is None  # lag=2 < 5
