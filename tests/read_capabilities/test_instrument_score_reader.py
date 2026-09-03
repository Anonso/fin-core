"""read_instrument_scores reader tests (temp store + synthetic records)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.ingestion.instrument_scores import (
    InstrumentScoreQueryReader,
    instrument_scores_path,
)


def _record(
    *,
    code: str,
    name: str,
    article_date: str,
    lihao: float = 8.0,
    consensus: float = 8.0,
    sector: str = "半导体",
    core_business: str = "先进封装",
    status: str = "ok",
    published_at: str | None = None,
) -> dict:
    return {
        "schema_version": "fin.instrument-scores/v1",
        "source_id": f"zsxq-{code}",
        "topic_id": "t",
        "column": "普通",
        "title": f"{sector}研报",
        "article_date": article_date,
        "published_at": published_at,
        "article_score": 8.0,
        "code": code,
        "name": name,
        "core_business": core_business,
        "sector": sector,
        "lihao_score": lihao,
        "consensus_score": consensus,
        "launch_in": None,
        "horizon": None,
        "status": status,
        "review_reason": None,
        "raw_origin": "test",
        "provenance": None,
        "record_id": f"id-{code}-{article_date}",
        "extracted_at": "2026-09-02T00:00:00+00:00",
        "parser_version": "v1",
    }


def _write_store(path: Path) -> None:
    target = instrument_scores_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        _record(code="002156", name="通富微电", article_date="2026-08-20"),
        _record(code="601138", name="工业富联", article_date="2026-08-01"),
        _record(code="002156", name="通富微电", article_date="2026-05-01"),  # 窗口外
        _record(
            code="603993",
            name="洛阳钼业",
            article_date="2026-08-10",
            sector="有色",
            core_business="铜钴钼",
            status="needs_review",
        ),
    ]
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    target.write_text(body, encoding="utf-8")
    target.chmod(0o600)


def _reader(tmp_path: Path) -> InstrumentScoreQueryReader:
    config = tmp_path / "windows.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "fin.zsxq-reference-windows/v1",
                "default_days": 60,
                "windows": {"普通": {"days": 60, "unit": "natural"}},
            }
        ),
        encoding="utf-8",
    )
    return InstrumentScoreQueryReader(tmp_path, window_config_path=config)


def _request(question: str, instruments: tuple[str, ...] = (), as_of=None):
    from fin_analyse.read_capabilities.types import ProductionReadRequest

    return ProductionReadRequest(
        question=question,
        instruments=instruments,
        as_of=as_of or datetime(2026, 9, 2, tzinfo=UTC),
    )


def test_reader_window_default_and_history_hint(tmp_path: Path) -> None:
    _write_store(tmp_path)
    reader = _reader(tmp_path)
    default = reader.read(_request("通富微电 评分"))
    assert default.value["counts"]["ok"] == 1
    assert default.value["windowed"] is True
    history = reader.read(_request("通富微电 历史评分演变"))
    assert history.value["counts"]["ok"] == 2
    assert history.value["windowed"] is False


def test_reader_code_filter_and_needs_review_count(tmp_path: Path) -> None:
    _write_store(tmp_path)
    reader = _reader(tmp_path)
    result = reader.read(_request("看看评分", instruments=("603993",)))
    assert result.value["counts"]["needs_review"] == 1
    assert result.value["records"] == []  # needs_review 不进列表
    assert "instrument_scores_no_match" in result.data_gaps


def test_reader_missing_store_gap(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    result = reader.read(_request("通富微电 评分"))
    assert "instrument_scores_unavailable" in result.data_gaps


def test_reader_same_day_rows_sorted_by_published_at(tmp_path: Path) -> None:
    path = instrument_scores_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        _record(
            code="600584",
            name="长电科技",
            article_date="2026-08-29",
            published_at="2026-08-29 09:00",
            lihao=8.0,
        ),
        _record(
            code="600584",
            name="长电科技",
            article_date="2026-08-29",
            published_at="2026-08-29 12:19",
            lihao=8.6,
            consensus=8.8,
        ),
        _record(
            code="600584",
            name="长电科技",
            article_date="2026-07-07",
            published_at=None,
            lihao=9.2,
            consensus=9.2,
        ),
    ]
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    reader = _reader(tmp_path)
    result = reader.read(_request("长电科技 历史评分演变"))
    timeline = [
        (row["article_date"], row["published_at"], row["lihao_score"])
        for row in result.value["records"]
    ]
    assert timeline == [
        ("2026-08-29", "2026-08-29 12:19", 8.6),
        ("2026-08-29", "2026-08-29 09:00", 8.0),
        ("2026-07-07", None, 9.2),
    ]
