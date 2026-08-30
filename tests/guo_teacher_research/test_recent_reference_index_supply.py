"""BUG-012②：reference lane 供料换 canonical index.json 的端到端回归。

设计门裁决钉死的三件事：普通栏 allowlist 投影（P2-3）、classification
"observation" 过 strict 校验（P1-2）、索引缺失/损坏 typed gap（诚实空）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.ready_evidence import (
    RecentReferenceReadyEvidenceReader,
)
from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextProvider,
)
from fin_analyse.read_capabilities.types import ProductionReadRequest

_CST = timezone(timedelta(hours=8))


def _write_article(kb: Path, article_id: str, *, date: str, title: str) -> Path:
    path = kb / "articles" / f"20260830_test_{article_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    topic_id = article_id.removeprefix("zsxq-")
    path.write_text(
        "---\n"
        f"id: {article_id}\n"
        f"topic_id: {topic_id}\n"
        f"date: {date}\n"
        "score: 6.0\n"
        "column: 普通\n"
        "companies: [协鑫能科]\n"
        "is_qa: True\n"
        "type: q&a\n"
        "tags: [算电协同]\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "锅师回复：算电协同的核心是低成本绿电加算力负荷的闭环，绿证核销是门槛。\n",
        encoding="utf-8",
    )
    return path


def _write_index(kb: Path, rows: list[dict]) -> None:
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "index.json").write_text(
        json.dumps({"articles": rows, "updated": "2026-08-30T08:00:00+08:00", "total": len(rows)},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def _index_row(article_id: str, path: Path, *, date: str, column: str = "普通") -> dict:
    return {
        "id": article_id,
        "topic_id": article_id.removeprefix("zsxq-"),
        "date": date,
        "score": 6.0,
        "column": column,
        "companies": ["协鑫能科"],
        "tags": ["算电协同"],
        "title": "提问：算电协同的绿电门槛怎么看",
        "char_count": 400,
        "path": str(path),
        "type": "q&a",
    }


def _reader(kb: Path) -> RecentReferenceReadyEvidenceReader:
    return RecentReferenceReadyEvidenceReader(
        runtime_context=AgentRuntimeContextProvider(kb_root=kb)
    )


def _read(reader: RecentReferenceReadyEvidenceReader, question: str):
    return reader.read(
        ProductionReadRequest(
            question=question,
            instruments=[],
            as_of=datetime(2026, 8, 30, 23, 0, 0, tzinfo=_CST),
        )
    )


def test_same_day_general_column_row_is_injected_end_to_end(tmp_path: Path) -> None:
    article = _write_article(
        tmp_path, "zsxq-22258828218828111", date="2026-08-30 09:30", title="提问：算电协同的绿电门槛怎么看"
    )
    _write_index(tmp_path, [_index_row("zsxq-22258828218828111", article, date="2026-08-30 09:30")])

    result = _read(_reader(tmp_path), "帮我看下算电协同今天有什么新说法")

    assert result.value["items"], result.data_gaps
    item = result.value["items"][0]
    assert item["article_id"] == "zsxq-22258828218828111"
    assert item["source_scope"] == "reference"
    assert "ready_evidence_unavailable" not in result.data_gaps


def test_g_tier_column_and_stale_rows_never_enter_lane(tmp_path: Path) -> None:
    article = _write_article(
        tmp_path, "zsxq-22258828218828112", date="2026-08-30 09:30", title="星大派锐评：算电协同"
    )
    rows = [
        _index_row("zsxq-22258828218828113", article, date="2026-08-30 09:30", column="星大派锐评"),
        _index_row("zsxq-22258828218828114", article, date="2026-08-01 09:30"),
    ]
    _write_index(tmp_path, rows)

    result = _read(_reader(tmp_path), "帮我看下算电协同今天有什么新说法")

    assert result.value["items"] == []
    assert "ready_evidence_unavailable" in result.data_gaps


def test_missing_or_corrupt_index_is_honest_gap(tmp_path: Path) -> None:
    result = _read(_reader(tmp_path), "算电协同")
    assert "recent_reference_index_unavailable" in result.data_gaps
    assert result.value["items"] == []

    corrupt = tmp_path / "index.json"
    corrupt.write_text("{not json", encoding="utf-8")
    result = _read(_reader(tmp_path), "算电协同")
    assert "recent_reference_index_unavailable" in result.data_gaps
    assert result.value["items"] == []


def test_excluded_article_id_does_not_reappear(tmp_path: Path) -> None:
    article = _write_article(
        tmp_path, "zsxq-22258828218828115", date="2026-08-30 09:30", title="提问：算电协同的绿电门槛怎么看"
    )
    _write_index(tmp_path, [_index_row("zsxq-22258828218828115", article, date="2026-08-30 09:30")])
    provider = AgentRuntimeContextProvider(kb_root=tmp_path)

    resolution = provider._resolve_recent_reference(
        _FakeRequest("算电协同"),
        {"tickers": set(), "companies": {"协鑫能科"}},
        "2026-08-30T23:00:00+08:00",
        exclude_article_ids={"zsxq-22258828218828115"},
    )

    assert resolution["candidates"] == []


class _FakeRequest:
    question = "算电协同"

    def __init__(self, question: str) -> None:
        self.question = question
        self.positions: list = []
        self.as_of = None
        self.max_g_events = 8


def test_lane_does_not_read_priority_events_cache(tmp_path: Path) -> None:
    """回归：供料换源后 priority_events.jsonl 不再是 reference 巷道输入。"""
    article = _write_article(
        tmp_path, "zsxq-22258828218828116", date="2026-08-30 09:30", title="提问：算电协同的绿电门槛怎么看"
    )
    _write_index(tmp_path, [_index_row("zsxq-22258828218828116", article, date="2026-08-30 09:30")])
    cache = tmp_path / "runtime" / "cognition"
    cache.mkdir(parents=True)
    (cache / "priority_events.jsonl").write_text("", encoding="utf-8")

    result = _read(_reader(tmp_path), "帮我看下算电协同今天有什么新说法")

    assert result.value["items"], result.data_gaps
