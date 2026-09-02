"""BUG-012②：reference lane 供料换 canonical index.json 的端到端回归。

设计门裁决钉死的三件事：普通栏 allowlist 投影（P2-3）、classification
"observation" 过 strict 校验（P1-2）、索引缺失/损坏 typed gap（诚实空）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fin_analyse.guo_teacher_research.ready_evidence import (
    RecentReferenceReadyEvidenceReader,
)
from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextProvider,
)
from fin_analyse.read_capabilities.types import ProductionReadRequest

_CST = timezone(timedelta(hours=8))
# available_at 取文章文件 mtime（st_mtime，诚实溯源：字节最后写入之时即可用
# 时点）。fixture 日期锚定 2026-08-30，运行日更晚时 mtime 会晚于 as_of 被判
# 「未来材料」——时间炸弹（08-31 起两测必挂的存量根因），故钉死 mtime。
_FIXTURE_MTIME = datetime(2026, 8, 30, 9, 35, tzinfo=_CST).timestamp()


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
    os.utime(path, (_FIXTURE_MTIME, _FIXTURE_MTIME))
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


def test_g_tier_excluded_and_ordinary_row_within_window_enters_lane(tmp_path: Path) -> None:
    g_article = _write_article(
        tmp_path, "zsxq-22258828218828112", date="2026-08-30 09:30", title="星大派锐评：算电协同"
    )
    ordinary_article = _write_article(
        tmp_path,
        "zsxq-22258828218828114",
        date="2026-08-01 09:30",
        title="提问：算电协同的绿电门槛怎么看",
    )
    rows = [
        _index_row("zsxq-22258828218828113", g_article, date="2026-08-30 09:30", column="星大派锐评"),
        # 2026-08-01 距 as_of(08-30) 29 天：owner 2026-09-02 普通栏窗口 60 天，
        # 旧“当天才注入”语义废止，窗口内普通栏应注入。
        _index_row("zsxq-22258828218828114", ordinary_article, date="2026-08-01 09:30"),
    ]
    _write_index(tmp_path, rows)

    result = _read(_reader(tmp_path), "帮我看下算电协同今天有什么新说法")

    assert [item["article_id"] for item in result.value["items"]] == [
        "zsxq-22258828218828114"
    ]
    assert "ready_evidence_unavailable" not in result.data_gaps


def test_ordinary_row_outside_60d_window_never_enters_lane(tmp_path: Path) -> None:
    article = _write_article(
        tmp_path, "zsxq-22258828218828117", date="2026-06-20 09:30", title="提问：算电协同的绿电门槛怎么看"
    )
    _write_index(
        tmp_path, [_index_row("zsxq-22258828218828117", article, date="2026-06-20 09:30")]
    )

    result = _read(_reader(tmp_path), "帮我看下算电协同有什么说法")

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


def _write_article_custom(
    kb: Path,
    article_id: str,
    *,
    date: str,
    title: str,
    companies: list[str],
    tags: list[str],
) -> Path:
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
        f"companies: {companies}\n"
        "is_qa: True\n"
        "type: q&a\n"
        f"tags: {tags}\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "锅师回复：内容正文。\n",
        encoding="utf-8",
    )
    os.utime(path, (_FIXTURE_MTIME, _FIXTURE_MTIME))
    return path


def test_reference_relevance_rejects_generic_title_keeps_domain_title(
    tmp_path: Path,
) -> None:
    """BUG-012 残余二：2 字泛词标题不再误放行，4 字领域词标题仍进入。"""
    generic_id = "zsxq-22258828218828121"
    domain_id = "zsxq-22258828218828122"
    generic_title = "三种算力金属的价格与政策主线已经交叉核验完毕。能量评分7.2分。"
    domain_title = "保偏光纤正从军用陀螺敏感材料，升级成CPO/硅光/相干光互联的偏振卡口"
    generic = _write_article_custom(
        tmp_path,
        generic_id,
        date="2026-08-30 09:30",
        title=generic_title,
        companies=[],
        tags=[],
    )
    domain = _write_article_custom(
        tmp_path,
        domain_id,
        date="2026-08-30 09:30",
        title=domain_title,
        companies=["英伟达", "康宁", "藤仓"],
        tags=[],
    )
    _write_index(
        tmp_path,
        [
            {
                "id": generic_id,
                "topic_id": generic_id.removeprefix("zsxq-"),
                "date": "2026-08-30 09:30",
                "score": 6.0,
                "column": "普通",
                "companies": [],
                "tags": [],
                "title": generic_title,
                "char_count": 400,
                "path": str(generic),
                "type": "q&a",
            },
            {
                "id": domain_id,
                "topic_id": domain_id.removeprefix("zsxq-"),
                "date": "2026-08-30 09:30",
                "score": 6.0,
                "column": "普通",
                "companies": ["英伟达", "康宁", "藤仓"],
                "tags": [],
                "title": domain_title,
                "char_count": 400,
                "path": str(domain),
                "type": "q&a",
            },
        ],
    )

    result = _read(
        _reader(tmp_path),
        "保偏光纤和CPO今天有什么新说法？请对照相关公司和主线。",
    )

    ids = [str(item["article_id"]) for item in result.value["items"]]
    assert domain_id in ids
    assert generic_id not in ids


def test_reference_fact_rows_rank_before_empty_rows(tmp_path: Path) -> None:
    """BUG-012 残余二：带 companies 的候选先于空事实候选进入 lane。"""
    empty_id = "zsxq-22258828218828123"
    fact_id = "zsxq-22258828218828124"
    empty = _write_article_custom(
        tmp_path,
        empty_id,
        date="2026-08-30 09:30",
        title="提问：算电协同的绿电门槛怎么看",
        companies=[],
        tags=["算电协同"],
    )
    fact = _write_article_custom(
        tmp_path,
        fact_id,
        date="2026-08-30 09:30",
        title="提问：算电协同的绿电门槛怎么看",
        companies=["协鑫能科"],
        tags=["算电协同"],
    )
    _write_index(
        tmp_path,
        [
            {
                "id": empty_id,
                "topic_id": empty_id.removeprefix("zsxq-"),
                "date": "2026-08-30 09:30",
                "score": 6.0,
                "column": "普通",
                "companies": [],
                "tags": ["算电协同"],
                "title": "提问：算电协同的绿电门槛怎么看",
                "char_count": 400,
                "path": str(empty),
                "type": "q&a",
            },
            {
                "id": fact_id,
                "topic_id": fact_id.removeprefix("zsxq-"),
                "date": "2026-08-30 09:30",
                "score": 6.0,
                "column": "普通",
                "companies": ["协鑫能科"],
                "tags": ["算电协同"],
                "title": "提问：算电协同的绿电门槛怎么看",
                "char_count": 400,
                "path": str(fact),
                "type": "q&a",
            },
        ],
    )

    provider = AgentRuntimeContextProvider(kb_root=tmp_path)
    resolution = provider._resolve_recent_reference(
        _FakeRequest("算电协同今天有什么新说法"),
        {"tickers": set(), "companies": {"协鑫能科"}, "topics": {"算电协同"}},
        "2026-08-30T23:00:00+08:00",
    )

    ids = [str(c["article_id"]) for c in resolution["candidates"]]
    assert fact_id in ids
    assert empty_id in ids
    assert ids.index(fact_id) < ids.index(empty_id)


def test_domain_specific_question_is_not_latest_focus() -> None:
    """BUG-012 残余二延伸：含具体领域词的问句不走 latest-focus 宽松分支。"""

    from fin_analyse.guo_teacher_research.runtime_context import (
        AgentRuntimeContextRequest,
        _build_intent_tokens,
        _is_latest_focus_query,
    )

    specific = AgentRuntimeContextRequest(
        question="保偏光纤和CPO今天有什么新说法？请对照相关公司和主线。"
    )
    assert not _is_latest_focus_query(specific, _build_intent_tokens(specific))

    broad = AgentRuntimeContextRequest(question="最近关注什么变化？")
    assert _is_latest_focus_query(broad, _build_intent_tokens(broad))
