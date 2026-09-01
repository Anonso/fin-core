"""Daily Decision Workspace generator over the L1 direct lane (v2).

The timer passes only the checkpoint enum; this generator owns the checkpoint
question and renders the single-turn L1 prompt directly against the llm.yaml
registry (``priorities.t0``, capped at two endpoints).  It replaces the V1
consultation-chain delegation: no codex CLI subprocess, no consultation
continuation semantics, no ``ConsultationResult`` projection — the L1 answer
is projected straight into the workspace version shape with honest
provenance (``generated_via="l1-direct-v1"``).

Durable invariants are untouched (see ``docs/design/daily-delivery.md``):
unavailable/failed generation raises ``DailyWorkspaceGenerationUnavailableError``
so no product is ever written, and the deterministic turn-key derivation is
byte-identical to V1 so idempotency keys stay continuous across the cutover.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)
from fin_analyse.guo_teacher_research.semantic_contract import (
    DailyWorkspaceContextProjection,
    load_daily_workspace_context_projection,
)

logger = logging.getLogger(__name__)

_CHECKPOINT_QUESTIONS: dict[DailyWorkspaceCheckpoint, str] = {
    DailyWorkspaceCheckpoint.PREMARKET: (
        "盘前工作区检查点：今天最值得处理什么（1-3 项）？为什么？哪里未知？"
    ),
    DailyWorkspaceCheckpoint.MORNING_1000: (
        "10:00 工作区检查点：较盘前发生了什么变化？现在最值得处理什么？哪里未知？"
        "下一正式工作区检查点是 14:20。"
    ),
    DailyWorkspaceCheckpoint.CLOSE_1420: (
        "14:20 工作区检查点：当下最值得处理什么？较 10:00 有什么变化？哪里未知？"
    ),
    DailyWorkspaceCheckpoint.POSTMARKET: (
        "盘后工作区检查点：今日复盘要点、明日观察项与组合复核是什么？"
    ),
}

_T0_FALLBACK_ORDER: tuple[str, ...] = ("glm53", "deepseek", "qwen")
_T0_MAX_ENDPOINTS = 2
_ATTEMPT_TIMEOUT_SECONDS = 300.0
_MAX_ANSWER_CHARS = 8000
_FAILURE_SENTINELS = frozenset({"[]", "null", "none", "''", '""'})
_MARKET_OVERVIEW_MAX_CHARS = 4000

_MATERIAL_KEYS: tuple[str, ...] = ("portfolio", "market_overview", "g_context", "g_reference")

# The two data faces a briefing is made of.  With both broken there is no
# briefing left to generate: the checkpoint must fall to the deterministic
# degraded notice instead of letting the model pad a normal-shaped answer
# (B1 blind-eval L3 attribution; design docs/design/daily-gap-ledger.md v2).
_CORE_DATA_KEYS: tuple[str, ...] = ("portfolio", "market_overview")


class DailyWorkspaceGenerationUnavailableError(RuntimeError):
    """The L1 direct lane returned a safe unavailable outcome."""

    def __init__(
        self,
        data_gaps: tuple[str, ...],
        *,
        agent_runtime_invoked: bool = False,
    ) -> None:
        self.data_gaps = data_gaps or ("daily_workspace_consultation_unavailable",)
        self.agent_runtime_invoked = agent_runtime_invoked
        super().__init__(self.data_gaps[0])


def _daily_workspace_turn_key(
    *,
    principal_id: str,
    trading_day_id: str,
    checkpoint: str,
    question: str,
) -> str:
    """Derive the deterministic A5L-3 machine turn key for a daily consult.

    Derivation is byte-identical to the V1 consultation-chain key so the
    idempotency namespace stays continuous across the generator cutover:
    same checkpoint retries reuse the same key, never any randomness.
    (on_demand questions come from the user, so their keys vary with the
    question — the same-checkpoint guarantee is scheduled-only.)
    """
    seed = f"fin-daily-workspace:{principal_id}:{trading_day_id}:{checkpoint}:{question}"
    return "fin.turn-idempotency/v1:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _load_daily_workspace_deadline(raw: object) -> datetime | None:
    """Decode the scheduler-owned deadline carried in an internal snapshot."""

    if raw is None:
        return None
    if not isinstance(raw, str):
        raise DailyWorkspaceGenerationUnavailableError(("daily_workspace_deadline_invalid",))
    try:
        deadline_at = datetime.fromisoformat(raw)
    except ValueError as error:
        raise DailyWorkspaceGenerationUnavailableError(
            ("daily_workspace_deadline_invalid",)
        ) from error
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise DailyWorkspaceGenerationUnavailableError(("daily_workspace_deadline_invalid",))
    return deadline_at


class _L1Backend(Protocol):
    def complete_bounded(
        self,
        prompt: str,
        *,
        total_timeout_seconds: float,
        wire_timeout_seconds: float,
        before_attempt: Callable[[], None],
    ) -> str: ...


# Read-only context materials rendered into the prompt.  Every entry is an
# already-rendered text section or None when that source was unavailable;
# missing materials become honest data_gaps, never fabricated content.  The
# provider receives the checkpoint question so knowledge retrieval matches
# the shift actually being generated (never a pinned premarket query).
_ContextMaterialProvider = Callable[[str], Mapping[str, str | None]]


def build_default_material_provider(
    *,
    knowledge_base_root: str,
    as_of_clock: Callable[[], datetime],
    quote_reader: Callable[[str], Any] | None = None,
) -> _ContextMaterialProvider:
    """Wire the read-only material sources used by the scheduled prepare path.

    Portfolio and market overview are local projections; the reference
    material is a bounded local retrieval over the knowledge base keyed by
    the checkpoint question passed in at generation time.  Any source that
    fails to construct or read yields ``None`` for its key (typed gap), so a
    broken source degrades the prompt honestly instead of failing the
    checkpoint.  ``quote_reader`` (one ticker in, one quote-like object out)
    is the latest-price source for the portfolio material; the default reads
    the provider registry fallback chain.
    """

    def _provider(question: str) -> Mapping[str, str | None]:
        materials: dict[str, str | None] = dict.fromkeys(_MATERIAL_KEYS)
        position_symbols: tuple[str, ...] = ()
        try:
            from fin_analyse.portfolio.actual_advisory import ActualAdvisoryPortfolioStore

            store = ActualAdvisoryPortfolioStore(clock=as_of_clock)
            portfolio_read = store.read()
            materials["portfolio"] = _render_portfolio(
                portfolio_read,
                name_resolver=_portfolio_name_resolver(),
                quote_reader=(
                    quote_reader if quote_reader is not None else _registry_quote_reader()
                ),
            )
            snapshot = getattr(portfolio_read, "snapshot", None)
            if snapshot is not None:
                position_symbols = tuple(
                    position.symbol for position in getattr(snapshot, "positions", ())
                )
        except Exception as exc:
            logger.warning("daily workspace portfolio material unavailable: %s", type(exc).__name__)
        try:
            from fin_analyse.market.current_overview import (
                AshareMarketOverviewRequest,
                build_default_a_share_market_overview,
            )

            # build_default_* returns the SERVICE, not data — the read must
            # actually run here or the prompt renders an object address.
            # 概览是活读取：必须用真实时钟——冻结到检查点 evidence_cutoff 会使
            # fetch 期间新于截止瞬间的 provider 行时间戳触发
            # PROVIDER_TIME_AFTER_QUERY 整链拒绝（交易日盘中班次确定性缺料，
            # 08-31 14:25 核对复现；周末/盘后数据不更新故既有实证未暴露）。
            service = build_default_a_share_market_overview()
            result = service.read(AshareMarketOverviewRequest())
            _record_market_overview_failure_diagnostic(result)
            materials["market_overview"] = _render_market_overview(result)
        except Exception as exc:
            logger.warning(
                "daily workspace market overview material unavailable: %s", type(exc).__name__
            )
        try:
            # G 认知材料（daily-g-context-material 设计稿，2026-08-31 设计门 8/8
            # 采纳）：与 read_g_context 同源 resolve；持仓标的作选材锚；懒 import
            # 于函数内；构造/resolve 异常 = material None + typed gap，不击穿班次。
            from fin_analyse.guo_teacher_research.runtime_context import (
                AgentRuntimeContextProvider,
                AgentRuntimeContextRequest,
            )

            provider_ = AgentRuntimeContextProvider(kb_root=Path(knowledge_base_root))
            resolved = provider_.resolve(
                AgentRuntimeContextRequest(
                    agent_id="guo_teacher",
                    question=question,
                    tickers=position_symbols,
                    now=as_of_clock().isoformat(),
                )
            )
            resolved_gaps = tuple(getattr(resolved, "data_gaps", ()) or ())
            if resolved_gaps:
                # resolve 层细节不进产品 data_gaps（一码一因），日志可见。
                logger.info(
                    "daily workspace g context resolve gaps: %s", ",".join(resolved_gaps)
                )
            materials["g_context"] = _render_g_context(resolved)
        except Exception as exc:
            logger.warning(
                "daily workspace g context material unavailable: %s", type(exc).__name__
            )
        try:
            from fin_analyse.knowledge.reference_evidence import (
                KnowledgeReferenceReader,
                KnowledgeReferenceRequest,
            )

            reader = KnowledgeReferenceReader.from_root(Path(knowledge_base_root))
            bundle = reader.read(
                KnowledgeReferenceRequest(
                    query=question,
                    window="180d",
                    as_of=as_of_clock(),
                )
            )
            materials["g_reference"] = _render_reference_bundle(bundle)
        except Exception as exc:
            logger.warning(
                "daily workspace reference material unavailable: %s", type(exc).__name__
            )
        return materials

    return _provider


def _record_market_overview_failure_diagnostic(result: Any) -> None:
    """Append one owner-only diagnostic line when the overview read is unusable.

    2026-09-01 盘前实弹：`l1_material_market_overview_unavailable` 仍在，但
    scheduled 入口吞掉 stderr，服务侧 data_gaps 无任何落盘，根因不可见。此
    诊断只写 UNKNOWN/异常路径（PARTIAL 正常出料不写），供下一盘前班次定根因。
    """
    if getattr(result, "status", None) == "PARTIAL":
        return
    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": getattr(result, "status", None),
        "data_gaps": list(getattr(result, "data_gaps", ()) or ()),
        "session_phase": getattr(result, "session_phase", None),
        "effective_trade_date": _market_text(
            getattr(result, "effective_trade_date", None)
        )
        or None,
        "observation_mode": getattr(result, "observation_mode", None),
        "provider_updated_at": getattr(result, "provider_updated_at", None),
        "provider_observation_age_seconds": getattr(
            result, "provider_observation_age_seconds", None
        ),
        "queried_at": getattr(result, "queried_at", None),
    }
    configured_state = os.environ.get("XDG_STATE_HOME")
    state_root = Path(configured_state) if configured_state else Path.home() / ".local" / "state"
    target = state_root / "fin-analyse" / "daily-workspace-overview-failures.jsonl"
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except Exception as exc:
        logger.warning("daily workspace overview diagnostic write failed: %s", type(exc).__name__)


def _render_reference_bundle(bundle: Any) -> str | None:
    """Render a knowledge reference bundle into a bounded prompt section."""

    items = getattr(bundle, "items", None)
    if not items:
        return None
    sections: list[str] = []
    for item in items[:5]:
        title = getattr(item, "title", "")
        content = getattr(item, "content", "")
        source_ref = getattr(item, "source_ref", "")
        if not content:
            continue
        sections.append(f"《{title}》（{source_ref}）：{str(content)[:800]}")
    if not sections:
        return None
    return "\n\n".join(sections)[:4000]


def _render_portfolio(
    read: Any,
    *,
    name_resolver: Callable[[str], str | None] | None = None,
    quote_reader: Callable[[str], Any] | None = None,
) -> str | None:
    """Render one portfolio read into a bounded prompt section.

    The payload is the snapshot's shared safe projection (the same read
    projection the MCP portfolio seam serves) — never a raw object repr.  A
    read without a snapshot (UNKNOWN/INVALID) maps to None — a typed gap,
    the same rule as the market overview material.  Positions are enriched
    in the projection only (zero-write): the instrument directory's
    authoritative display name when the stored name is missing or code-like,
    plus the latest quote when obtainable.  Enrichment failures degrade the
    single field to unknown, never the whole material (BUG-008 回炉点:
    名称/现价补全).
    """

    snapshot = getattr(read, "snapshot", None)
    to_safe = getattr(snapshot, "to_safe_dict", None)
    if snapshot is None or not callable(to_safe):
        return None
    try:
        payload = to_safe()
    except Exception:
        return None
    # owner 拍板 2026-08-31：无两融且永远不会有——两融项从持仓面删除
    # （store schema 不动，投影层剔除；margin_debt 恒 0 由快照承载）。
    payload.pop("margin_debt", None)
    payload.pop("margin_debt_status", None)
    positions = payload.get("positions")
    if isinstance(positions, list):
        for position in positions:
            if isinstance(position, dict):
                _enrich_position(
                    position, name_resolver=name_resolver, quote_reader=quote_reader
                )
    text = json.dumps(payload, ensure_ascii=False)
    if not text.strip():
        return None
    return text[:6000]


def _enrich_position(
    position: dict[str, Any],
    *,
    name_resolver: Callable[[str], str | None] | None,
    quote_reader: Callable[[str], Any] | None,
) -> None:
    """Fill one position's display name and latest price (projection-only).

    The stored name stays untouched unless it is missing or code-like (the
    MCP seam's predicate).  ``latest_price``/``latest_change_pct`` start as
    explicit nulls — unknown stays unknown; a failed quote never raises.
    """

    symbol = str(position.get("symbol") or "")
    code = symbol.split(".")[0] if "." in symbol else symbol
    name = str(position.get("name") or "")
    if name_resolver is not None and (not name or name in {code, symbol}):
        try:
            resolved = name_resolver(code)
        except Exception:
            resolved = None
        if resolved:
            position["name"] = resolved
    position["latest_price"] = None
    position["latest_change_pct"] = None
    if quote_reader is None or not code:
        return
    try:
        quote = quote_reader(code)
    except Exception:
        return
    price = getattr(quote, "price", None)
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        position["latest_price"] = float(price)
        change_pct = getattr(quote, "change_pct", None)
        if isinstance(change_pct, (int, float)) and not isinstance(change_pct, bool):
            position["latest_change_pct"] = float(change_pct)


def _portfolio_name_resolver() -> Callable[[str], str | None] | None:
    """Directory-backed authoritative-name resolver; None when unbuildable."""

    try:
        from fin_analyse.market.instrument_directory import RuntimeAshareInstrumentDirectory

        directory = RuntimeAshareInstrumentDirectory()
    except Exception:
        return None

    def resolve(code: str) -> str | None:
        entries = directory.lookup(code)
        return entries[0].name if entries else None

    return resolve


def _registry_quote_reader() -> Callable[[str], Any] | None:
    """Latest-quote reader over the provider fallback chain; None when unbuildable."""

    try:
        from fin_analyse.market import create_default_registry

        registry = create_default_registry()
    except Exception:
        return None

    def read_quote(code: str) -> Any:
        return registry.execute("get_quote", code)

    return read_quote


def _render_market_overview(result: Any) -> str | None:
    """Render a market overview READ RESULT as bounded, complete text.

    A ``UNKNOWN`` status read carries no usable evidence, so it maps to None
    (typed gap) instead of rendering an empty shell.  Do not dump and slice the
    JSON envelope here: a mid-token cut makes the prompt look non-empty while
    silently dropping the highest-value sections.  The text projection keeps
    the facts that answer a postmarket question (index levels, breadth,
    leaders, turnover) and truncates only at whole-line boundaries.
    """

    if getattr(result, "status", None) != "PARTIAL":
        return None
    to_value = getattr(result, "to_capability_value", None)
    if not callable(to_value):
        return None
    try:
        payload = to_value()
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None

    lines: list[str] = []
    trade_date = _market_text(payload.get("effective_trade_date"))
    observation_mode = _market_text(payload.get("observation_mode"))
    provider_updated_at = _market_text(payload.get("provider_updated_at"))
    observation_age = payload.get("provider_observation_age_seconds")
    if trade_date or observation_mode:
        bits = []
        if trade_date:
            bits.append(f"有效交易日={trade_date}")
        if observation_mode:
            bits.append(f"观察模式={observation_mode}")
        if provider_updated_at:
            bits.append(f"源更新时间={provider_updated_at}")
        if (
            isinstance(observation_age, (int, float))
            and not isinstance(observation_age, bool)
            and isfinite(float(observation_age))
            and observation_age >= 0
        ):
            bits.append(f"源延迟约{observation_age:.0f}秒")
        lines.append("时点：" + "；".join(bits))

    raw_indices = payload.get("major_indices")
    index_lines: list[str] = []
    if isinstance(raw_indices, (list, tuple)):
        for raw in raw_indices[:4]:
            if not isinstance(raw, Mapping):
                continue
            name = _market_text(raw.get("name")) or _market_text(raw.get("code"))
            if not name:
                continue
            level = _format_market_number(raw.get("level"))
            change = _format_market_pct(raw.get("change_pct"))
            turnover = _format_market_turnover(raw.get("turnover_yuan"))
            facts = [part for part in (f"点位 {level}" if level else "", change, turnover) if part]
            index_lines.append(f"{name}（" + "，".join(facts) + "）" if facts else name)
    if index_lines:
        lines.append("主要指数：" + "；".join(index_lines))

    breadth = payload.get("breadth")
    if isinstance(breadth, Mapping):
        breadth_bits = []
        for key, label in (
            ("advancers", "上涨"),
            ("decliners", "下跌"),
            ("unchanged", "平盘"),
            ("covered_instruments", "覆盖"),
        ):
            value = breadth.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                breadth_bits.append(f"{label}{value:g}" if isinstance(value, float) else f"{label}{value}")
        turnover = _format_market_turnover(breadth.get("total_turnover_yuan"))
        if turnover:
            breadth_bits.append(turnover)
        lines.append("市场宽度：" + ("；".join(breadth_bits) if breadth_bits else "不可用"))
    else:
        lines.append("市场宽度：不可用（当前指数源未提供涨跌家数）")

    _append_market_board_lines(
        lines,
        payload.get("industry"),
        title="行业",
    )
    _append_market_board_lines(
        lines,
        payload.get("concept"),
        title="概念",
    )

    raw_turnover = payload.get("turnover_leaders")
    turnover_lines: list[str] = []
    if isinstance(raw_turnover, (list, tuple)):
        for raw in raw_turnover[:8]:
            if not isinstance(raw, Mapping):
                continue
            name = _market_text(raw.get("name")) or _market_text(raw.get("code"))
            change = _format_market_pct(raw.get("change_pct"))
            amount = _format_market_turnover(raw.get("turnover_yuan"))
            if name and (change or amount):
                turnover_lines.append(
                    f"{name}（" + "，".join(part for part in (change, amount) if part) + "）"
                )
    if turnover_lines:
        lines.append("成交额靠前个股：" + "；".join(turnover_lines))
    if len(lines) <= 1:
        # A typed PARTIAL result should still carry at least one concrete
        # market fact.  Do not let a malformed/future-shaped envelope turn
        # into a green, non-empty material section.
        return None

    limitations = payload.get("limitations")
    limitation_labels = {
        "MARKET_OVERVIEW_BREADTH_UNAVAILABLE": "市场宽度不可用",
        "MARKET_OVERVIEW_SINGLE_SOURCE": "单一来源",
        "MARKET_OVERVIEW_PERSISTENCE_NOT_EVALUATED": "持续性未评估",
        "MARKET_OVERVIEW_BJ_NOT_COVERED": "不含北交所",
        "MARKET_OVERVIEW_PROVIDER_CONCEPT_TAXONOMY_LIMITED": "概念分类有限",
        "MARKET_OVERVIEW_DELAYED_REFERENCE": "延迟行情，仅作参考",
        "MARKET_OVERVIEW_SECTION_ROWS_UNPROJECTABLE": "部分榜单行不可投影",
    }
    if isinstance(limitations, (list, tuple)):
        labels = [
            limitation_labels.get(str(item), str(item))
            for item in limitations
            if str(item).strip()
        ]
        if labels:
            lines.append("限制：" + "；".join(dict.fromkeys(labels)))

    text = _join_bounded_lines(lines, max_chars=_MARKET_OVERVIEW_MAX_CHARS)
    return text or None


def _append_market_board_lines(
    lines: list[str],
    raw_section: object,
    *,
    title: str,
) -> None:
    if not isinstance(raw_section, Mapping):
        return
    for key, label in (
        ("leaders_by_change", "涨幅"),
        ("leaders_by_turnover", "成交额"),
    ):
        raw_items = raw_section.get(key)
        if not isinstance(raw_items, (list, tuple)):
            continue
        entries: list[str] = []
        for raw in raw_items[:5]:
            if not isinstance(raw, Mapping):
                continue
            name = _market_text(raw.get("name")) or _market_text(raw.get("code"))
            if not name:
                continue
            change = _format_market_pct(raw.get("change_pct"))
            turnover = _format_market_turnover(raw.get("turnover_yuan"))
            detail = "，".join(part for part in (change, turnover) if part)
            entries.append(f"{name}（{detail}）" if detail else name)
        if entries:
            lines.append(f"{title}{label}靠前：" + "；".join(entries))


def _join_bounded_lines(lines: list[str], *, max_chars: int) -> str:
    selected: list[str] = []
    used = 0
    for raw in lines:
        line = " ".join(str(raw).split()).strip()
        if not line:
            continue
        extra = len(line) if not selected else len(line) + 1
        if used + extra > max_chars:
            break
        selected.append(line)
        used += extra
    return "\n".join(selected)


def _market_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:160]


def _format_market_number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not isfinite(float(value)) or value <= 0:
        return ""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f}"
    return f"{int(value)}"


def _format_market_pct(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not isfinite(float(value)):
        return ""
    return f"涨跌 {value:+.2f}%"


def _format_market_turnover(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return ""
    if not isfinite(float(value)):
        return ""
    if value >= 100_000_000:
        return f"成交额 {value / 100_000_000:.2f}亿元"
    if value >= 10_000:
        return f"成交额 {value / 10_000:.2f}万元"
    return f"成交额 {value:.0f}元"


# strict-G 桶（daily-g-context-material 设计稿 P1-2 裁决）：recent_reference
# 等非 G 桶绝不混入「老师体系证据」材料——守住 G/Z 边界。
_G_CONTEXT_STRICT_BUCKETS = frozenset({"pinned_source", "fresh_g", "latest_commentary"})
_G_CONTEXT_MAX_CHARS = 4000


def _render_g_context(resolved: Any) -> str | None:
    """Render resolve's strict-G items into a bounded prompt section.

    daily-g-context-material 设计稿（2026-08-31 设计门 8/8 采纳）：flat 渲染
    resolve 的 g_context 条目（六字段冻结映射：title/guidance_brief/source_ref/
    published_at/usage_boundary/why_available）；4000 字上限按整条丢弃，不半条
    切断（弃条数记日志）。resolve 层 typed gaps 不进产品 data_gaps（与既有材料
    键同规，一码一因）。空（无条目或全被过滤）→ None → typed gap。
    """

    context = getattr(resolved, "llm_context", None)
    items = context.get("g_context") if isinstance(context, Mapping) else None
    if not isinstance(items, list):
        return None
    lines: list[str] = []
    used = 0
    dropped = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("source_bucket") or "") not in _G_CONTEXT_STRICT_BUCKETS:
            continue
        title = str(item.get("title") or "").strip()
        brief = str(item.get("guidance_brief") or "").strip()
        if not title and not brief:
            continue
        published_at = str(item.get("published_at") or "").strip()
        usage = str(item.get("usage_boundary") or "").strip()
        source_ref = str(item.get("source_ref") or "").strip()
        headline = f"{title}：{brief}" if title and brief else (title or brief)
        suffixes = [part for part in (usage, source_ref) if part]
        line = " | ".join(
            (published_at, headline, f"（{'；'.join(suffixes)}）" if suffixes else "")
        ).strip()
        if not line:
            continue
        if used + len(line) > _G_CONTEXT_MAX_CHARS:
            dropped += 1
            continue
        lines.append(f"- {line}")
        used += len(line) + 2
    if dropped:
        logger.info("daily workspace g context items dropped by budget: %d", dropped)
    if not lines:
        return None
    return "\n".join(lines)


def _render_prompt(
    *,
    question: str,
    materials: Mapping[str, str | None],
    context: DailyWorkspaceContextProjection | None,
    as_of: datetime,
    trading_day_id: str,
) -> str:
    sections: list[str] = [
        "你是所有者的投研助理，为今日工作区检查点生成一条结论先行、可执行的中文简报。",
        "纪律：advisory-only，不给交易指令；只使用下方材料，材料之外的信息一律"
        "如实标注为未知；不编造数字；结论先行。",
        "",
        "# 时间锚点\n"
        f"交易日：{trading_day_id}；材料读取与生成时刻："
        f"{as_of.astimezone().isoformat(timespec='minutes')}。",
        f"# 检查点问题{chr(10)}{question}",
    ]
    has_baseline = False
    if context is not None:
        parent_bits: list[str] = []
        if context.source is not None:
            parent_bits.append(
                f"同日父检查点：{context.source.checkpoint} v{context.source.product_version}"
            )
        if getattr(context, "relationship", None):
            parent_bits.append(f"关系：{context.relationship}")
        if parent_bits:
            sections.append(
                "# 当日工作区上下文（FIN 自有先验，非新证据）\n" + "；".join(parent_bits)
            )
        # 父检查点正文是「较此前变化」的唯一对比基线（BUG-008：无基线时该
        # 栏目空转——不是模型不会比，是 prompt 没给可比材料）。
        carry_answer = str(getattr(context.carry_over, "answer_text", "") or "")
        if carry_answer.strip():
            has_baseline = True
            sections.append(
                "# 当日先前检查点结论（对比基线，FIN 自有先验，非新证据）\n" + carry_answer
            )
    labels = {
        "portfolio": "# 持仓快照（最新确认）",
        "market_overview": "# 市场概览",
        "g_context": "# G 认知参考（老师体系证据）",
        "g_reference": "# 知识库参考（非 G 基准，仅参考）",
    }
    for key in _MATERIAL_KEYS:
        value = materials.get(key)
        if _material_usable(value):
            sections.append(f"{labels[key]}\n{value}")
    # 无基线（首班/上日盘后缺失）时整栏不要求，不逼模型无中生有。
    changes_requirement = (
        "再给「较此前变化」（对照上方先前检查点结论，给具体变化点，不要空话），"
        if has_baseline
        else ""
    )
    sections.append(
        "# 输出要求\n"
        "一条连贯简报：先给「最值得处理」的 1-3 项与理由，"
        "判定必须对照「G 认知参考」（一致或不一致都点名；该材料缺席时明说「无体系对照」），"
        + changes_requirement
        + "有可用行情事实时，至少引用两条带数字的当日事实并说明其含义；"
        "G 认知若包含数值锚点或主线假设，逐条给出「支持/未兑现/无直接证据」对照；"
        "不要把材料限制本身写成唯一结论，也不要用缺口清单替代复盘。"
        + "最后给「哪里未知」。"
        "持仓以最新已确认快照为准，不要输出任何提示更新持仓或强调快照过期的文字。"
        "直接输出正文，不要标题、不要 JSON。"
    )
    return "\n\n".join(sections)


# A rendered material containing this mark is a str()'d Python object that
# leaked through serialization (e.g. an address like
# "<AshareMarketOverviewService object at 0x...>").  That is corruption, not
# content: it must be excluded from the prompt AND surfaced as a gap, never
# silently passed as evidence (BUG-008: the gap check used to only test for
# emptiness, so the object repr sailed through with a green ledger).
_OBJECT_REPR_MARK = "object at 0x"


def _material_usable(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return _OBJECT_REPR_MARK not in text


def _material_gaps(materials: Mapping[str, str | None]) -> tuple[str, ...]:
    gaps: list[str] = []
    for key in _MATERIAL_KEYS:
        value = materials.get(key)
        if _material_usable(value):
            continue
        # A present-but-corrupt string (str()'d object address) is a
        # serialization defect; anything else is a source that supplied
        # nothing.  One cause, one code (B1 attribution L3).
        if isinstance(value, str) and _OBJECT_REPR_MARK in value:
            gaps.append(f"l1_material_{key}_unrenderable")
        else:
            gaps.append(f"l1_material_{key}_unavailable")
    return tuple(gaps)


class L1DirectWorkspaceGenerator:
    """Scheduled/on-demand workspace generator over the L1 direct lane.

    The checkpoint question is FIN-owned (never caller-provided); the answer
    is projected into the workspace version shape with honest provenance and
    the same unavailable semantics as V1: any failure raises
    ``DailyWorkspaceGenerationUnavailableError`` before any version is written.
    """

    def __init__(
        self,
        *,
        config_path: str | None = None,
        backend_factory: Callable[[str], Any | None] | None = None,
        material_provider: _ContextMaterialProvider | None = None,
        attempt_timeout_seconds: float = _ATTEMPT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_path = config_path
        self._backend_factory = backend_factory
        self._material_provider = material_provider
        self._attempt_timeout_seconds = float(attempt_timeout_seconds)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._backends: tuple[tuple[str, Any], ...] | None = None

    def _build_backend(self, name: str) -> Any | None:
        if self._backend_factory is not None:
            return self._backend_factory(name)
        from fin_analyse.claims.config_loader import load_llm_config
        from fin_analyse.claims.openai_backend import OpenAICompatibleBackend

        config = load_llm_config(self._config_path)
        entry = (config.get("models") or {}).get(name)
        if not isinstance(entry, dict) or not entry.get("enabled"):
            return None
        api_key = entry.get("api_key")
        base_url = entry.get("base_url")
        # Fail-closed credential resolution: unresolved ${ENV} literals or
        # blank values mean "not configured", never a fallback to OPENAI_*.
        if not isinstance(api_key, str) or not api_key.strip() or "${" in api_key:
            return None
        if not isinstance(base_url, str) or not base_url.strip() or "${" in base_url:
            return None
        return OpenAICompatibleBackend(
            model=str(entry.get("model")),
            api_key=api_key,
            base_url=base_url,
            reasoning_effort=entry.get("reasoning_effort"),
            # Reasoning models spend max_tokens on thinking before any answer
            # content, so a small cap yields an empty content shape; give the
            # thinking phase headroom (yaml entry may override).
            max_tokens=int(entry.get("max_tokens") or 8192),
            timeout=self._attempt_timeout_seconds,
            backend_name=f"daily-l1:{name}",
        )

    def _resolve_backends(self) -> tuple[tuple[str, Any], ...]:
        if self._backends is not None:
            return self._backends
        from fin_analyse.claims.config_loader import (
            get_backend_priority,
            load_llm_config,
        )

        order = get_backend_priority(
            load_llm_config(self._config_path), "t0", _T0_FALLBACK_ORDER
        )
        resolved: list[tuple[str, Any]] = []
        for name in order:
            try:
                backend = self._build_backend(name)
            except Exception as exc:
                logger.warning(
                    "daily workspace L1 backend '%s' failed to build: %s", name, type(exc).__name__
                )
                continue
            if backend is not None:
                resolved.append((name, backend))
                if len(resolved) >= _T0_MAX_ENDPOINTS:
                    break
        self._backends = tuple(resolved)
        return self._backends

    def generate(self, *, snapshot: object, principal: Any) -> dict[str, object]:
        if not isinstance(snapshot, dict):
            raise TypeError("daily workspace snapshot must be a dict")
        checkpoint_value = snapshot.get("checkpoint")
        trading_day_id = snapshot.get("trading_day_id")
        daily_workspace_context_raw = snapshot.get("daily_workspace_context")
        if not isinstance(checkpoint_value, str):
            raise ValueError("daily workspace snapshot missing checkpoint")
        if not isinstance(trading_day_id, str) or not trading_day_id:
            raise ValueError("daily workspace snapshot missing trading day")
        daily_workspace_context: DailyWorkspaceContextProjection | None = (
            load_daily_workspace_context_projection(daily_workspace_context_raw)
            if isinstance(daily_workspace_context_raw, dict)
            else None
        )
        deadline_at = _load_daily_workspace_deadline(snapshot.get("daily_workspace_deadline_at"))
        if checkpoint_value == "on_demand":
            user_context = snapshot.get("user_context")
            question = user_context.get("question") if isinstance(user_context, dict) else None
            if not isinstance(question, str) or not question:
                raise ValueError("daily workspace snapshot missing user question")
            origin = "on_demand"
            extra_gaps = ("daily_workspace_prior_context_not_consumed",)
        else:
            try:
                checkpoint = DailyWorkspaceCheckpoint(checkpoint_value)
            except ValueError as error:
                raise ValueError("unknown daily workspace checkpoint") from error
            question = _CHECKPOINT_QUESTIONS[checkpoint]
            origin = "scheduled"
            extra_gaps = ()

        materials: Mapping[str, str | None] = (
            self._material_provider(question) if self._material_provider is not None else {}
        )
        if self._material_provider is not None and all(
            not _material_usable(materials.get(key)) for key in _CORE_DATA_KEYS
        ):
            # Material death shares the backend-unavailable semantics: the
            # scheduled/ask handlers turn this into the deterministic degraded
            # notice carrying exactly these gap codes — never a fabricated
            # normal-looking briefing (B1 attribution L3).
            raise DailyWorkspaceGenerationUnavailableError(_material_gaps(materials))
        prompt = _render_prompt(
            question=question,
            materials=materials,
            context=daily_workspace_context,
            as_of=self._clock(),
            trading_day_id=trading_day_id,
        )
        answer_text = self._complete(prompt, deadline_at=deadline_at)
        return _project_workspace_product(
            answer_text,
            checkpoint=checkpoint_value,
            trading_day_id=trading_day_id,
            origin=origin,
            parent_artifact_hash=snapshot.get("parent_artifact_hash"),
            generated_at=self._clock(),
            extra_gaps=tuple(
                dict.fromkeys(
                    (
                        *extra_gaps,
                        *(
                            daily_workspace_context.data_gaps
                            if daily_workspace_context is not None
                            else ()
                        ),
                        *(
                            _material_gaps(materials)
                            if self._material_provider is not None
                            else ()
                        ),
                    )
                )
            ),
        )

    def _complete(self, prompt: str, *, deadline_at: datetime | None) -> str:
        backends = self._resolve_backends()
        if not backends:
            raise DailyWorkspaceGenerationUnavailableError(
                ("daily_workspace_l1_backend_unavailable",)
            )
        budget = self._attempt_timeout_seconds
        if deadline_at is not None:
            remaining = (deadline_at - self._clock()).total_seconds()
            if remaining <= 0:
                raise DailyWorkspaceGenerationUnavailableError(
                    ("daily_workspace_deadline_exhausted",)
                )
            budget = min(budget, remaining)
        chain_deadline = monotonic() + budget
        backend_budget = budget / len(backends)
        failures: list[str] = []
        for name, backend in backends:
            remaining = min(backend_budget, chain_deadline - monotonic())
            if deadline_at is not None:
                remaining = min(remaining, (deadline_at - self._clock()).total_seconds())
            if remaining <= 0:
                failures.append(f"{name}:deadline")
                break
            try:
                text = backend.complete_bounded(
                    prompt,
                    total_timeout_seconds=backend_budget,
                    wire_timeout_seconds=min(240.0, backend_budget),
                    before_attempt=lambda: None,
                )
            except Exception as exc:
                failures.append(f"{name}:{type(exc).__name__}")
                continue
            stripped = (text or "").strip()
            if not stripped or stripped.lower() in _FAILURE_SENTINELS:
                failures.append(f"{name}:empty")
                continue
            if len(stripped) > _MAX_ANSWER_CHARS:
                stripped = stripped[:_MAX_ANSWER_CHARS]
            return stripped
        raise DailyWorkspaceGenerationUnavailableError(
            ("daily_workspace_l1_all_backends_failed", *failures)
        )


def _project_workspace_product(
    answer_text: str,
    *,
    checkpoint: str,
    trading_day_id: str,
    origin: str,
    parent_artifact_hash: object,
    generated_at: datetime,
    extra_gaps: tuple[str, ...] = (),
) -> dict[str, object]:
    """Project one L1 answer into the workspace version shape.

    Honesty rules: the answer becomes the single ``top_items`` entry verbatim
    (never promoted from a summary), provenance states plainly that no agent
    runtime ran, and unavailable outcomes are rejected by the caller before
    any version is written.
    """

    gaps = tuple(dict.fromkeys(extra_gaps))
    return {
        "schema_version": "fin.daily_workspace_product/v1",
        "checkpoint": checkpoint,
        "trading_day_id": trading_day_id,
        "origin": origin,
        "parent_artifact_hash": parent_artifact_hash,
        "generated_via": "l1-direct-v1",
        "consultation_status": "completed",
        "agent_provenance": {
            "runtime_invoked_at_generation": False,
            "output_used": False,
            "generation": "l1-direct-v1",
            "product_bound_g_receipt": None,
        },
        "context_boundaries": {
            "prior_product": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
            "user_question": "NOT_EVIDENCE",
        },
        "input_snapshot_receipt": {
            "schema": "fin.daily-workspace-input-receipt/v1",
            "consultation_as_of": generated_at.astimezone().isoformat(),
        },
        "first_screen": {
            "top_items": [
                {
                    "item": answer_text,
                    "disposition": None,
                }
            ],
            "rationale": [],
            "changes_vs_previous": [],
            "unknowns": list(gaps),
            "portfolio_review": [],
        },
        "data_gaps": list(gaps),
        "consultation_product": {
            "contract_id": "consultation_product",
            "contract_version": "v1",
            "answer_text": answer_text,
        },
    }
