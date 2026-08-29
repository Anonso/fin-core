"""P0 已发布下注清单：纯事实投影的窗口/截断/降级契约测试（A5/A8/同值）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from fin_analyse.consultation.open_risk_ledger import (
    LEDGER_MAX_CHARS,
    MAX_PRODUCT_SCAN_ROWS,
    MAX_PUBLISHED_BET_ROWS,
    PublishedBetLedgerReader,
    PublishedBetRow,
    render_published_bet_ledger,
)

_EPOCH = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def _bet_product(*, size_r: float = 0.5, review_point: str = "第 10 个交易日复核") -> dict:
    return {
        "contract_id": "consultation_product",
        "contract_version": "v1",
        "disposition": "MANUAL_REVIEW",
        "no_action": False,
        "bet_expression": {
            "odds_low": 0.55,
            "odds_high": 0.7,
            "reward_risk": 2.0,
            "size_r": size_r,
            "entry_timing_basis": "回踩确认",
            "exit_conditions": "跌破低点离场",
            "thesis_odds_rationale": "主线强化。",
            "horizon": "SWING",
            "horizon_days": 20,
            "review_point": review_point,
            "target_position_pct_max": None,
        },
    }


def _plain_product() -> dict:
    return {"contract_id": "consultation_product", "contract_version": "v1"}


def _db(tmp_path) -> str:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE chains (chain_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,"
        " chain_kind TEXT NOT NULL DEFAULT 'consultation', business_key TEXT,"
        " status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE products (product_id TEXT PRIMARY KEY,"
        " chain_id TEXT REFERENCES chains(chain_id), job_id TEXT,"
        " product_version INTEGER, status TEXT, product_json TEXT, artifact_hash TEXT,"
        " created_at REAL)"
    )
    return str(path), connection


def _insert(
    connection: sqlite3.Connection,
    created_at: float,
    product: dict,
    *,
    principal_id: str = "principal-1",
) -> None:
    chain_id = f"c-{principal_id}-{created_at}"
    connection.execute(
        "INSERT INTO chains (chain_id, principal_id, status, created_at, updated_at)"
        " VALUES (?, ?, 'active', ?, ?)",
        (chain_id, principal_id, created_at, created_at),
    )
    connection.execute(
        "INSERT INTO products (product_id, chain_id, product_json, created_at)"
        " VALUES (?, ?, ?, ?)",
        (f"p-{principal_id}-{created_at}", chain_id, json.dumps(product, sort_keys=True), created_at),
    )


def _epoch(day_offset: int = 0, hour: int = 8) -> float:
    return (_EPOCH + timedelta(days=day_offset, hours=hour - 8)).timestamp()


def test_reader_projects_only_published_bet_rows_newest_first(tmp_path) -> None:
    """A8：只投影含 bet_expression 的行、按 created_at DESC、以注数计而非产品行数。"""
    path, connection = _db(tmp_path)
    for day in range(5):
        _insert(connection, _epoch(day), _plain_product())
        _insert(connection, _epoch(day, 9), _bet_product(size_r=0.1 * (day + 1)))
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "READY"
    assert len(read.rows) == 5
    sizes = [row.size_r for row in read.rows]
    assert sizes == sorted(sizes, reverse=True)
    assert all(row.horizon == "SWING" for row in read.rows)
    assert all(row.horizon_days == 20 for row in read.rows)
    assert all(row.review_point == "第 10 个交易日复核" for row in read.rows)
    assert read.data_gaps == ()


def test_reader_skips_bets_from_products_not_final_manual_review(tmp_path) -> None:
    """B: bet 只属于最终 MANUAL_REVIEW——NO_ACTION/OBSERVE 产品不进台账。"""
    path, connection = _db(tmp_path)
    downgraded = _bet_product()
    downgraded["disposition"] = "NO_ACTION"
    downgraded["no_action"] = True
    _insert(connection, _epoch(0), downgraded)
    observed = _bet_product()
    observed["disposition"] = "OBSERVE"
    observed["no_action"] = True
    _insert(connection, _epoch(0, 9), observed)
    _insert(connection, _epoch(1), _bet_product())
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "READY"
    assert len(read.rows) == 1
    assert read.rows[0].size_r == 0.5


def test_reader_caps_bets_at_twenty_even_when_more_exist(tmp_path) -> None:
    path, connection = _db(tmp_path)
    for day in range(30):
        _insert(connection, _epoch(day), _bet_product(size_r=0.1 * (day + 1)))
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert len(read.rows) == MAX_PUBLISHED_BET_ROWS
    assert read.rows[0].size_r == 3.0


def test_reader_scan_cap_drops_bets_beyond_two_hundred_product_rows(tmp_path) -> None:
    """A8：扫描上限 200 产品行——更老的下注不在窗口内，诚实省略。"""
    path, connection = _db(tmp_path)
    for index in range(MAX_PRODUCT_SCAN_ROWS + 10):
        if index < MAX_PRODUCT_SCAN_ROWS:
            _insert(connection, _epoch(0, 8) - index * 60, _plain_product())
        else:
            _insert(connection, _epoch(-100) - index * 60, _bet_product())
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "EMPTY"


def test_reader_skips_malformed_and_betless_rows(tmp_path) -> None:
    """A5：坏 JSON 与无 bet_expression 的行只跳过，不失败、不伪造。"""
    path, connection = _db(tmp_path)
    connection.execute(
        "INSERT INTO products (product_id, product_json, created_at) VALUES (?, ?, ?)",
        ("p-bad", "{not json", _epoch(2)),
    )
    _insert(connection, _epoch(1), _plain_product())
    _insert(connection, _epoch(0), _bet_product())
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "READY"
    assert len(read.rows) == 1


def test_reader_unavailable_when_database_missing(tmp_path) -> None:
    read = PublishedBetLedgerReader(tmp_path / "missing.db").read(principal_id="principal-1")

    assert read.status == "UNAVAILABLE"
    assert "OPEN_RISK_LEDGER_UNAVAILABLE" in read.data_gaps


def test_render_is_whole_line_truncated_and_annotated() -> None:
    """A8：整行剔除（绝不断行）+「仅显示最近 X 条」标注；超限自最旧行剔除。"""
    rows = tuple(
        PublishedBetRow(
            created_at=_EPOCH + timedelta(hours=-index),
            size_r=0.5,
            horizon="SWING",
            horizon_days=20,
            review_point="第 " + str(index + 1) + " 个交易日复核" + "。" * 180,
        )
        for index in range(20)
    )

    rendered = render_published_bet_ledger(rows)

    assert len(rendered) <= LEDGER_MAX_CHARS
    assert "已发布下注清单" in rendered
    assert "是否仍持有/在计划期内由你自行判断" in rendered
    assert "仅显示最近 " in rendered
    kept = [line for line in rendered.splitlines() if line.startswith("- ")]
    assert kept
    assert len(kept) < 20
    assert all("复核" in line for line in kept)


def test_render_empty_rows_returns_empty_string() -> None:
    assert render_published_bet_ledger(()) == ""


def test_render_is_deterministic_for_same_rows() -> None:
    rows = tuple(
        PublishedBetRow(
            created_at=_EPOCH,
            size_r=0.5,
            horizon="INTRADAY",
            horizon_days=None,
            review_point="14:20 复核",
        )
        for _ in range(3)
    )

    assert render_published_bet_ledger(rows) == render_published_bet_ledger(rows)


def test_reader_is_scoped_to_principal(tmp_path) -> None:
    """F-SEC-1：台账按 principal 隔离——A 可见自己的注，B 的注对 A 不可见。"""
    path, connection = _db(tmp_path)
    _insert(connection, _epoch(0), _bet_product(), principal_id="principal-a")
    _insert(connection, _epoch(1), _bet_product(size_r=0.9), principal_id="principal-b")
    connection.commit()
    connection.close()

    read_a = PublishedBetLedgerReader(path).read(principal_id="principal-a")
    read_b = PublishedBetLedgerReader(path).read(principal_id="principal-b")

    assert read_a.status == "READY"
    assert len(read_a.rows) == 1
    assert read_a.rows[0].size_r == 0.5
    assert read_b.status == "READY"
    assert len(read_b.rows) == 1
    assert read_b.rows[0].size_r == 0.9


def test_reader_rejects_blank_principal(tmp_path) -> None:
    path, connection = _db(tmp_path)
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="  ")

    assert read.status == "UNAVAILABLE"
    assert "OPEN_RISK_LEDGER_UNAVAILABLE" in read.data_gaps


def test_strip_removes_trailing_ledger_block_and_preserves_prefix() -> None:
    from fin_analyse.consultation.open_risk_ledger import strip_published_bet_ledger_notes

    notes = (
        "个人策略内容\n\n"
        "已发布下注清单（最近发布的下注记录，仅事实投影；是否仍持有/在计划期内由你自行判断）：\n"
        "- 一行下注"
    )
    stripped = strip_published_bet_ledger_notes(notes)

    assert stripped == "个人策略内容"
    assert "已发布下注清单" not in stripped


def test_strip_is_noop_without_ledger_and_none_safe() -> None:
    from fin_analyse.consultation.open_risk_ledger import strip_published_bet_ledger_notes

    assert strip_published_bet_ledger_notes(None) is None
    assert strip_published_bet_ledger_notes("仅策略内容") == "仅策略内容"


def test_strip_ledger_only_notes_returns_none() -> None:
    from fin_analyse.consultation.open_risk_ledger import strip_published_bet_ledger_notes

    notes = (
        "已发布下注清单（最近发布的下注记录，仅事实投影；是否仍持有/在计划期内由你自行判断）：\n"
        "- 一行下注"
    )
    assert strip_published_bet_ledger_notes(notes) is None


def test_out_of_range_timestamp_row_is_skipped_not_fatal(tmp_path) -> None:
    """审计 major 2：1e300 时间戳只跳过坏行，绝不逃逸为异常。"""
    path, connection = _db(tmp_path)
    _insert(connection, 1e300, _bet_product())
    _insert(connection, _epoch(0), _bet_product())
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "READY"
    assert len(read.rows) == 1


def test_footnote_counts_toward_max_chars() -> None:
    """审计 minor 1：脚注计入候选长度——差 1 放不下脚注时宁缺毋滥整段为空。"""
    newer = PublishedBetRow(
        created_at=_EPOCH,
        size_r=0.5,
        horizon="INTRADAY",
        horizon_days=None,
        review_point="复核点 A" * 3,
    )
    older = PublishedBetRow(
        created_at=_EPOCH - timedelta(hours=1),
        size_r=0.5,
        horizon="INTRADAY",
        horizon_days=None,
        review_point="复核点 B",
    )
    both = render_published_bet_ledger((newer, older))
    lines = both.splitlines()
    header_len = len(lines[0])
    newer_line_len = len(lines[1])
    fits = header_len + 1 + newer_line_len + len("\n仅显示最近 1 条")

    rendered_fit = render_published_bet_ledger((newer, older), max_chars=fits)
    assert len(rendered_fit) <= fits
    assert "仅显示最近 1 条" in rendered_fit
    assert "复核点 A" in rendered_fit
    assert "复核点 B" not in rendered_fit

    rendered_tight = render_published_bet_ledger((newer, older), max_chars=fits - 1)
    assert rendered_tight == ""


def test_scan_capped_marks_and_annotates_the_ledger(tmp_path) -> None:
    """扫描窗口触顶 → scan_capped=True 且渲染带「更早记录未展示」标注。"""
    path, connection = _db(tmp_path)
    for index in range(MAX_PRODUCT_SCAN_ROWS + 5):
        _insert(connection, _epoch(0, 8) - index * 60, _plain_product())
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "EMPTY"
    assert read.scan_capped is True

    rows = (
        PublishedBetRow(
            created_at=_EPOCH,
            size_r=0.5,
            horizon="INTRADAY",
            horizon_days=None,
            review_point="复核点 A",
        ),
    )
    rendered = render_published_bet_ledger(rows, scan_capped=True)
    assert "（扫描窗口有限，更早的下注记录未展示）" in rendered


def test_scan_not_capped_when_window_not_exhausted(tmp_path) -> None:
    path, connection = _db(tmp_path)
    _insert(connection, _epoch(0), _bet_product())
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.scan_capped is False


# ── 上下文预注入治理：PublishedBetRow typed subject_tickers 投影 ────────────


def test_bet_row_projects_subject_tickers_from_finalized_product_typed_targets() -> None:
    """只从 finalized product 的 typed manual_review_targets 投影 subject_tickers；
    缺失/畸形保持 UNKNOWN（None），绝不从 answer 文本或公司名推断。"""
    from fin_analyse.consultation.open_risk_ledger import _parse_bet_row

    base = _bet_product(size_r=0.5)
    with_targets = {**base, "manual_review_targets": ["600111.SH", "002409.SZ"]}
    row = _parse_bet_row(_epoch(0), json.dumps(with_targets, sort_keys=True))
    assert row is not None
    assert row.subject_tickers == ("600111.SH", "002409.SZ")

    assert _parse_bet_row(_epoch(0), json.dumps(base, sort_keys=True)).subject_tickers is None
    bad_targets = (
        {**base, "manual_review_targets": "600111.SH"},
        {**base, "manual_review_targets": [1]},
        {**base, "manual_review_targets": []},
    )
    for payload in bad_targets:
        parsed = _parse_bet_row(_epoch(0), json.dumps(payload, sort_keys=True))
        assert parsed is not None
        assert parsed.subject_tickers is None
def test_reader_keeps_unknown_target_rows_as_typed_fact(tmp_path) -> None:
    """旧行缺 typed target → row 保留（时间/R/horizon/review point 仍是事实），
    subject_tickers=None（UNKNOWN）由选择/展示层处理。"""
    path, connection = _db(tmp_path)
    _insert(connection, _epoch(0), _bet_product(size_r=0.5))
    connection.commit()
    connection.close()

    read = PublishedBetLedgerReader(path).read(principal_id="principal-1")

    assert read.status == "READY"
    assert len(read.rows) == 1
    assert read.rows[0].subject_tickers is None
