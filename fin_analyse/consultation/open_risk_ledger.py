"""P0 已发布下注清单：纯事实投影（零门、零裁决、零写入、无合计）。

数据源：products 表 + 已发布 bet_expression 字段原文。机器不做任何
「开放/已过期/持有中」判定、不输出 R 合计、不提取标的、不做主题归因。
台账片段（注入）与展示块（公开结果）由同一纯函数渲染，逐字同值。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

MAX_PUBLISHED_BET_ROWS = 20
MAX_PRODUCT_SCAN_ROWS = 200
LEDGER_MAX_CHARS = 1_500
LEDGER_GAP_CODE = "OPEN_RISK_LEDGER_UNAVAILABLE"
LEDGER_BUDGET_GAP_CODE = "OPEN_RISK_LEDGER_BUDGET_OMITTED"

_HORIZONS = frozenset({"INTRADAY", "SHORT_SWING", "SWING"})
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_LEDGER_HEADER = (
    "已发布下注清单（最近发布的下注记录，仅事实投影；是否仍持有/在计划期内由你自行判断）："
)


@dataclass(frozen=True, slots=True)
class PublishedBetRow:
    """One published bet: original field projections only.

    ``subject_tickers`` 只从同一 finalized product 的 typed
    ``manual_review_targets`` 投影；None = UNKNOWN（旧行或畸形 target 列表），
    绝不从 answer 文本、公司名或“同风险因子”推断。
    """

    created_at: datetime
    size_r: float
    horizon: str
    horizon_days: float | None
    review_point: str
    subject_tickers: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class PublishedBetLedgerRead:
    """Bounded read result; EMPTY is the normal live state, never a gap."""

    status: Literal["READY", "EMPTY", "UNAVAILABLE"]
    rows: tuple[PublishedBetRow, ...] = ()
    data_gaps: tuple[str, ...] = ()
    # 扫描窗口触顶（200 产品行）时 True——更早记录未展示，渲染加诚实标注。
    scan_capped: bool = False

    def rendered(self) -> str | None:
        if self.status != "READY" or not self.rows:
            return None
        return render_published_bet_ledger(self.rows, scan_capped=self.scan_capped)


def render_published_bet_ledger(
    rows: Sequence[PublishedBetRow],
    *,
    max_chars: int = LEDGER_MAX_CHARS,
    scan_capped: bool = False,
) -> str:
    """同一纯函数渲染注入片段与展示块；超限自最旧行整行剔除，绝不断行。

    「仅显示最近 X 条」脚注计入候选长度（审计 minor 1）：脚注本身不得把
    最终文本推过 max_chars。``scan_capped`` 时末尾追加扫描窗口标注，
    同样计入候选长度（静默截断可见化）。
    """

    kept: list[str] = [_render_bet_row(row) for row in rows]
    while kept:
        footnote = f"\n仅显示最近 {len(kept)} 条" if len(kept) < len(rows) else ""
        scan_note = "\n（扫描窗口有限，更早的下注记录未展示）" if scan_capped else ""
        candidate = _LEDGER_HEADER + "\n" + "\n".join(kept) + footnote + scan_note
        if len(candidate) <= max_chars:
            return candidate
        kept.pop()
    return ""


def strip_published_bet_ledger_notes(notes: str | None) -> str | None:
    """从 user_notes 末尾移除台账片段（执法点预算重试用；确定性、幂等）。

    台账片段由 _with_user_context 追加在 user_notes 末尾；未命中表头时原样
    返回。这是字符串级移除，不改 option_id/身份哈希。
    """

    if notes is None or _LEDGER_HEADER not in notes:
        return notes
    stripped = notes[: notes.index(_LEDGER_HEADER)].rstrip()
    return stripped or None


def _render_bet_row(row: PublishedBetRow) -> str:
    horizon = row.horizon
    if row.horizon_days is not None:
        horizon = f"{horizon}（计划 {row.horizon_days:g} 个交易日）"
    created = row.created_at.astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
    return f"- {created} 注码 {row.size_r:g}R，时间尺度 {horizon}，复核点 {row.review_point}"


def ledger_rows_intersecting_targets(
    rows: Sequence[PublishedBetRow],
    targets: Sequence[str],
) -> tuple[PublishedBetRow, ...]:
    """逐行 ANY-intersection：row.subject_tickers 与 typed target 精确匹配。

    公共展示与注入侧共用同一 typed 判定；UNKNOWN（subject_tickers=None）
    行永不命中；空 targets → 空结果（不显示行/数量/提示）。
    """
    target_set = {target.strip() for target in targets if isinstance(target, str) and target.strip()}
    if not target_set:
        return ()
    return tuple(
        row
        for row in rows
        if row.subject_tickers is not None
        and any(ticker in target_set for ticker in row.subject_tickers)
    )


def _parse_bet_row(created_at: float, product_json: str) -> PublishedBetRow | None:
    try:
        product = json.loads(product_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(product, Mapping):
        return None
    # B: bet 只属于最终 MANUAL_REVIEW——降级/观察产品不得成为"已发布下注"。
    if product.get("disposition") != "MANUAL_REVIEW" or product.get("no_action") is not False:
        return None
    bet = product.get("bet_expression")
    if not isinstance(bet, Mapping):
        return None
    size_r = bet.get("size_r")
    horizon = bet.get("horizon")
    review_point = bet.get("review_point")
    if (
        not isinstance(size_r, (int, float))
        or isinstance(size_r, bool)
        or size_r <= 0
        or not isinstance(horizon, str)
        or horizon not in _HORIZONS
        or not isinstance(review_point, str)
        or not review_point.strip()
    ):
        return None
    horizon_days = bet.get("horizon_days")
    if not (
        horizon_days is None
        or (isinstance(horizon_days, (int, float)) and not isinstance(horizon_days, bool))
    ):
        return None
    try:
        created = datetime.fromtimestamp(created_at, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # 审计 major 2：异常/遗留时间戳只跳过坏行，绝不逃逸为异常阻断 prepare。
        return None
    subject_tickers = _subject_tickers_from_product(product)
    return PublishedBetRow(
        created_at=created,
        size_r=float(size_r),
        horizon=horizon,
        horizon_days=float(horizon_days) if horizon_days is not None else None,
        review_point=review_point.strip(),
        subject_tickers=subject_tickers,
    )


def _subject_tickers_from_product(product: Mapping) -> tuple[str, ...] | None:
    """从 finalized product 的 typed manual_review_targets 投影 ticker。

    只接受非空字符串列表；缺字段/畸形/空列表 = UNKNOWN（None）。
    """
    targets = product.get("manual_review_targets")
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(target, str) or not target.strip() for target in targets)
    ):
        return None
    return tuple(target.strip() for target in targets)


class PublishedBetLedgerReader:
    """Read-only projection over the same products table (sqlite mode=ro).

    No schema writes, no new durable owner; the repository remains the sole
    writer. Read failures degrade to UNAVAILABLE with a typed gap.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"

    def read(self, *, principal_id: str) -> PublishedBetLedgerRead:
        if not isinstance(principal_id, str) or not principal_id.strip():
            return PublishedBetLedgerRead(
                status="UNAVAILABLE",
                data_gaps=(LEDGER_GAP_CODE,),
            )
        try:
            connection = sqlite3.connect(self._db_uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error, ValueError):
            return PublishedBetLedgerRead(
                status="UNAVAILABLE",
                data_gaps=(LEDGER_GAP_CODE,),
            )
        try:
            # 按 principal 隔离（安全插件 F-SEC-1）：products 无 owner 列，
            # 经 chain_id 联 chains.principal_id 过滤；参数化查询。
            raw_rows = connection.execute(
                "SELECT p.created_at, p.product_json"
                " FROM products p"
                " JOIN chains c ON c.chain_id = p.chain_id"
                " WHERE c.principal_id = ?"
                " ORDER BY p.created_at DESC, p.product_id"
                " LIMIT ?",
                (principal_id, MAX_PRODUCT_SCAN_ROWS),
            ).fetchall()
        except sqlite3.Error:
            return PublishedBetLedgerRead(
                status="UNAVAILABLE",
                data_gaps=(LEDGER_GAP_CODE,),
            )
        finally:
            connection.close()
        scan_capped = len(raw_rows) >= MAX_PRODUCT_SCAN_ROWS
        bets: list[PublishedBetRow] = []
        for created_at, product_json in raw_rows:
            row = _parse_bet_row(float(created_at), str(product_json))
            if row is not None:
                bets.append(row)
            if len(bets) >= MAX_PUBLISHED_BET_ROWS:
                break
        if not bets:
            return PublishedBetLedgerRead(status="EMPTY", scan_capped=scan_capped)
        return PublishedBetLedgerRead(
            status="READY",
            rows=tuple(bets),
            scan_capped=scan_capped,
        )
