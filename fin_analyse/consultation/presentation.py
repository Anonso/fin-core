"""FIN-owned presentation of one finalized consultation product."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from fin_analyse.consultation.daily_workspace_product_contracts import (
    is_explicit_daily_workspace_availability_notice,
    is_public_daily_workspace_product,
)

PRESENTATION_SCHEMA = "fin.consultation-presentation/v1"
PRESENTATION_FORMAT = "markdown"
MAX_PRESENTATION_CHARS = 7_000

_GAP_LABELS = {
    "MARKET_OVERVIEW_SINGLE_SOURCE": "单一公开行情源",
    "MARKET_OVERVIEW_PERSISTENCE_NOT_EVALUATED": "主线持续性尚未评估",
    "MARKET_OVERVIEW_BJ_NOT_COVERED": "暂未覆盖北交所",
    "MARKET_OVERVIEW_PROVIDER_CONCEPT_TAXONOMY_LIMITED": "概念分类受数据源口径限制",
    "MARKET_OVERVIEW_DELAYED_REFERENCE": "延迟、仅供参考",
    "MARKET_OVERVIEW_SECTION_ROWS_UNPROJECTABLE": "盘面排名数据尚未更新（分节行无法投影）",
    "CURRENT_MARKET_NOT_EVALUATED": "当前市场尚未评估",
    "ACTUAL_ADVISORY_PORTFOLIO_STALE": "用户确认的持仓快照已过期",
    "ACTUAL_ADVISORY_MARGIN_DEBT_UNKNOWN": "融资负债未提供",
    "CONSULTATION_CONTEXT_DEADLINE_REACHED": "咨询上下文读取超时",
    "CONSULTATION_DEADLINE_REACHED": "本轮咨询超过安全时限",
    "EASTMONEY_RAW_UNAVAILABLE": "主行情源不可用",
    "TENCENT_RAW_UNAVAILABLE": "交叉核验行情源不可用",
    "TRADING_CALENDAR_UNAVAILABLE": "交易日历不可用",
    "OPEN_RISK_LEDGER_BUDGET_OMITTED": "已发布下注清单未进入本轮 Agent 上下文（预算）",
    "daily_workspace_prior_context_not_consumed": ("上一版工作区尚未进入本轮 Agent typed context"),
    # 上下文预注入治理（M3）：决策重要码的固定中文后果（追加项）。
    "g_context_no_relevant_items": "本次没有相关的老师材料",
    "pinned_source_relevance_gate_skipped": "置顶材料未通过本题相关性门，未注入",
    "pinned_source_identity_conflict": "置顶材料来源标识不一致，未注入",
    "recent_reference_index_unavailable": "知识索引不可用，同日参考材料未检",
    "g_context_no_extractable_units": "相关老师材料暂无可提取要点",
    "g_working_set_sources_changed": "老师材料工作集在咨询期间更新，已按当前版本处理",
    "g_context_unavailable": "老师材料不可用",
    "g_context_read_failed": "老师材料读取失败",
    "g_context_attestation_invalid": "老师材料使用证明校验未通过",
    "g_context_bound_sources_mismatch": "老师材料来源绑定不一致",
    "g_context_bound_sources_missing": "老师材料来源绑定缺失",
    "g_context_candidates_truncated": "老师材料候选被截断",
    "g_context_canonical_budget_exceeded": "老师材料超出预算",
    "g_context_generation_mismatch": "老师材料代际不一致",
    "g_context_items_truncated": "老师材料条目被截断",
    "g_context_point_in_time_unavailable": "老师材料时点不可用",
    "g_context_product_binding_missing": "老师材料来源绑定不完整",
    "g_context_result_invalid": "老师材料结果校验失败",
    "g_context_source_freshness_incomplete": "老师材料新鲜度不完整",
    "teacher_cognition_product_binding_missing": "老师认知来源绑定不完整",
    "market_overview_product_binding_missing": "市场概览来源绑定不完整",
    "market_snapshot_product_binding_missing": "行情快照来源绑定不完整",
    "PAPER_ACCOUNT_TRUTH_UNAVAILABLE": "模拟账户事实不可用",
    "PAPER_ACCOUNT_TRUTH_INCOMPLETE": "模拟账户事实不完整",
    "ACTUAL_ADVISORY_CONTEXT_UNAVAILABLE": "正式持仓上下文不可用",
    "PAPER_CONTEXT_UNAVAILABLE": "模拟账户上下文不可用",
    "CONSULTATION_REQUEST_CONTEXT_UNAVAILABLE": "咨询请求上下文不可用",
    "CONSULTATION_CONTEXT_NOT_SELECTED": "Agent 未选择有效上下文",
    "CONSULTATION_SELECTED_CONTEXT_DRIFTED": "所选上下文在回答期间发生变化",
    "CONSULTATION_SELECTED_CONTEXT_PROOF_UNAVAILABLE": "所选上下文无法完成校验",
    "CONSULTATION_STORED_PRODUCT_NOT_CURRENT": "已存储结论与当前上下文不一致",
    "CONVERSATION_GENERATION_CLOSED": "该会话代已结束",
    "CONVERSATION_LANE_BUSY": "会话正在处理上一请求",
    "CONVERSATION_ROUTE_UNAVAILABLE": "会话路由不可用",
    "continuation_not_accessible": "无法续接上一轮分析",
    "OPEN_RISK_LEDGER_UNAVAILABLE": "已发布下注清单不可用",
    "USER_CONTEXT_CONTRIBUTION_INVALID": "部分用户上下文校验未通过，已省略",
    "USER_CONTEXT_CONTRIBUTION_BUDGET_EVICTED": "部分用户上下文因预算未进入本轮",
    "INVESTMENT_MEMORY_RECALL_UNAVAILABLE": "个人决策记录暂时不可用",
    "INVESTMENT_MEMORY_WRITE_UNAVAILABLE": "本轮表达的决定未能记录",
    "INVESTMENT_MEMORY_DELETE_UNAVAILABLE": "删除决策记录暂时不可用",
    "agent_runtime_unavailable": "Agent 运行时不可用",
    "agent_runtime_contract_violation": "Agent 输出未通过合同校验",
    "agent_runtime_context_invalid": "Agent 输入上下文校验失败",
    "agent_runtime_required_capability_missing": "缺少必要的能力授权",
    "consultation_product_subject_invalid": "结论的标的校验未通过",
    "consultation_product_subject_missing": "结论缺少必要标的",
    "consultation_product_manual_review_target_invalid": "复核标的校验未通过",
    "consultation_product_profile_invalid": "结论分析档位校验未通过",
    "consultation_context_option_selection_invalid": "上下文选择校验未通过",
    "consultation_bet_expression_invalid": "下注声明字段校验未通过",
    "consultation_bet_expression_missing": "下注声明字段缺失",
    "consultation_claim_derivation_invalid": "部分结论引证校验未通过",
    "consultation_claim_subject_reference_invalid": "部分结论标的引用校验未通过",
    "daily_workspace_prepared_product_missing": "定时咨询未能生成结论",
    "daily_workspace_consultation_unavailable": "定时咨询的 Agent 咨询不可用",
    "daily_workspace_generation_failed": "定时咨询生成失败",
    "daily_workspace_state_unavailable": "定时工作区状态不可用",
}
# 纯工程/审计/可重试内部码：不面向用户展示（审计仍可见原 code）。
_GAP_OMIT_CODES = frozenset(
    {
        "CONSULTATION_DIRECT_PRIMARY_MAXIMUM_SECONDS",
        "CONSULTATION_DIRECT_PRIMARY_MINIMUM_SECONDS",
        "CONSULTATION_G_RUNTIME_MINIMUM_SECONDS",
        "CONSULTATION_PROXY_CHAIN_MINIMUM_SECONDS",
        "CONSULTATION_PROXY_DISCOVERY_MINIMUM_SECONDS",
        "CONSULTATION_PROXY_RUN_MINIMUM_SECONDS",
        "agent_runtime_data_gap_invalid",
        "daily_workspace_run_ledger",
    }
)
# 未知 code → 一次固定泛化中文（绝不把 raw token 原样回显）。
_GENERIC_UNKNOWN_GAP_MESSAGE = "另有部分数据缺口未逐项展示，已由 FIN 记录"
# 决策重要家族的前缀映射（精确表优先；家族内新码也固定给中文后果）。
_GAP_LABEL_PREFIXES = (
    ("g_mainline_", "老师主线摘要受限（具体缺口已记录）"),
    ("g_methodology_", "老师方法论摘要受限（具体缺口已记录）"),
    ("g_working_set_", "老师材料工作集受限（具体缺口已记录）"),
    ("teacher_cognition_", "老师认知检索受限（具体缺口已记录）"),
)
_SAFETY_FOOTER = "边界：仅供咨询，不执行交易；任何操作均需人工确认。"


def public_gap_message(code: str) -> str | None:
    """typed gap code → 固定中文后果；纯工程码 → None（省略）；未知 → 固定泛化。

    只作用于 FIN-generated 展示块；typed payload/product/data_gaps 不改、
    不删除（审计仍可看到原 code）。answer_summary 不做任何 scrub。
    """
    if not isinstance(code, str) or not code:
        return None
    if code in _GAP_OMIT_CODES:
        return None
    label = _GAP_LABELS.get(code)
    if label is not None:
        return label
    for prefix, family_label in _GAP_LABEL_PREFIXES:
        if code.startswith(prefix):
            return family_label
    return _GENERIC_UNKNOWN_GAP_MESSAGE


def _gap_messages(gaps: Sequence[str]) -> list[str]:
    """展示用 gap 行：已知码→中文后果；未知码合并为一次泛化行；工程码省略。"""
    messages: list[str] = []
    generic_once = False
    for code in gaps:
        message = public_gap_message(code)
        if message is None:
            continue
        if message == _GENERIC_UNKNOWN_GAP_MESSAGE:
            generic_once = True
            continue
        if message not in messages:
            messages.append(message)
    if generic_once:
        messages.append(_GENERIC_UNKNOWN_GAP_MESSAGE)
    return messages


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, *, limit: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    return normalized[:limit] if normalized else fallback


def _text_list(value: object, *, limit: int = 16, item_limit: int = 1_000) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    projected: list[str] = []
    for item in value:
        normalized = _text(item, limit=item_limit)
        if normalized and normalized not in projected:
            projected.append(normalized)
        if len(projected) == limit:
            break
    return projected


def _canonical_product(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    product = _mapping(payload.get("product"))
    if (
        product.get("contract_id") != "consultation_product"
        or product.get("contract_version") != "v1"
        or not _text(product.get("answer_text"), limit=MAX_PRESENTATION_CHARS)
    ):
        return {}
    return product


def _daily_workspace_product(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    product = _mapping(payload.get("product"))
    workspace_ref = product.get("workspace_ref")
    product_version = product.get("product_version")
    parent_version = product.get("parent_product_version")
    if (
        payload.get("action")
        not in {
            "daily_workspace_open",
            "daily_workspace_ask",
            "daily_workspace_scheduled",
        }
        or product.get("schema_version") != "fin.daily_workspace_product/v1"
        or not isinstance(workspace_ref, str)
        or not workspace_ref
        or payload.get("workspace_ref") != workspace_ref
        or not isinstance(product.get("trading_day_id"), str)
        or product.get("origin") not in {"scheduled", "on_demand"}
        or not isinstance(product_version, int)
        or isinstance(product_version, bool)
        or not isinstance(parent_version, int)
        or isinstance(parent_version, bool)
        or product_version != parent_version + 1
        or not isinstance(product.get("first_screen"), Mapping)
        or not is_public_daily_workspace_product(product)
    ):
        return {}
    return product


def _render_daily_workspace(
    payload: Mapping[str, Any],
    product: Mapping[str, Any],
) -> str:
    if is_explicit_daily_workspace_availability_notice(product):
        gaps = _text_list(
            (*_text_list(product.get("data_gaps")), *_text_list(payload.get("data_gaps"))),
            limit=24,
            item_limit=128,
        )
        lines = ["定时咨询失败通知：本次未能生成咨询结论，未采用锅老师认知或 Agent 产物作为结论。"]
        if gaps:
            lines.append("原因：" + "、".join(_gap_messages(gaps)))
        return _bounded(lines, footer=None)
    consultation_product = _mapping(product.get("consultation_product"))
    answer = _text(
        consultation_product.get("answer_text"),
        limit=MAX_PRESENTATION_CHARS,
        fallback="FIN 当前无法形成咨询结论。",
    )
    return _bounded([answer], footer=None)


def _bounded(lines: list[str], *, footer: str | None = _SAFETY_FOOTER) -> str:
    rendered = "\n\n".join(lines)
    if len(rendered) <= MAX_PRESENTATION_CHARS:
        return rendered
    if footer is None:
        return rendered[:MAX_PRESENTATION_CHARS].rstrip()
    rendered_footer = f"\n\n{footer}"
    return rendered[: MAX_PRESENTATION_CHARS - len(rendered_footer)].rstrip() + rendered_footer


_DEGRADED_FRESH_NOTICE = (
    "（连续性已降级：本次未能续接上一轮分析，答案已按新一轮分析生成；这不表示上一轮已成功恢复。）"
)
_DEGRADED_FRESH_FAILURE_NOTICE = (
    "（连续性已降级：本次未能续接上一轮分析，改按新一轮处理后仍未形成可用答复。）"
)
_DEGRADED_MODEL_NOTICE = (
    "（模型已降级：本次由备用模型生成；结论仍通过同一 FIN 数据、权限与安全边界。）"
)


def _continuity_degraded(payload: Mapping[str, Any]) -> bool:
    result_meta = _mapping(payload.get("result_meta"))
    # 只从 typed 枚举判断，不猜测 action/status/data gap/正文。
    return result_meta.get("continuity") == "DEGRADED_FRESH"


def _model_degraded(payload: Mapping[str, Any]) -> bool:
    result_meta = _mapping(payload.get("result_meta"))
    return result_meta.get("model_quality") == "DEGRADED"


def _render_product(payload: Mapping[str, Any], product: Mapping[str, Any]) -> str:
    summary = _text(
        product.get("answer_text"),
        limit=MAX_PRESENTATION_CHARS,
        fallback="FIN 当前无法形成咨询结论。",
    )
    lines = [summary]
    if _continuity_degraded(payload):
        lines.append(_DEGRADED_FRESH_NOTICE)
    if _model_degraded(payload):
        lines.append(_DEGRADED_MODEL_NOTICE)
    return _bounded(lines, footer=None)


def _render_ledger_display_block(
    payload: Mapping[str, Any],
    product: Mapping[str, Any],
) -> str | None:
    """DECIDE_NOW 的台账展示：只渲染 final 非空 manual_review_targets 与
    row subject_tickers 逐行 ANY-intersection 的行；无交集/无 typed 行 →
    None（整块省略，不显示行数/数量/按需提示）。绝不回退整块字符串或
    context_binding.subjects。"""
    targets = product.get("manual_review_targets")
    if not isinstance(targets, list) or not targets:
        return None
    rows = payload.get("open_risk_ledger_rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    from fin_analyse.consultation.open_risk_ledger import (
        PublishedBetRow,
        ledger_rows_intersecting_targets,
        render_published_bet_ledger,
    )

    parsed_rows: list[PublishedBetRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        created = _aware_time(row.get("created_at"))
        if created is None:
            continue
        tickers = row.get("subject_tickers")
        subject_tickers = (
            tuple(ticker for ticker in tickers if isinstance(ticker, str))
            if isinstance(tickers, Sequence) and not isinstance(tickers, (str, bytes))
            else None
        )
        parsed_rows.append(
            PublishedBetRow(
                created_at=created,
                size_r=float(row["size_r"]),
                horizon=str(row["horizon"]),
                horizon_days=(
                    float(row["horizon_days"]) if row.get("horizon_days") is not None else None
                ),
                review_point=str(row["review_point"]),
                subject_tickers=subject_tickers,
            )
        )
    if not parsed_rows:
        return None
    gated = ledger_rows_intersecting_targets(parsed_rows, targets)
    if not gated:
        return None
    return render_published_bet_ledger(
        gated,
        scan_capped=bool(payload.get("open_risk_ledger_scan_capped")),
    ) or None


def _render_bet_expression(
    bet: Mapping[str, Any],
    *,
    account_context: Mapping[str, Any] | None = None,
    result_as_of: object = None,
) -> str:
    """结构化下注声明——纪律化赌徒的三行式。

    account_context 仅用于 ADVISORY_REAL 的 R→元注码参照；无账户上下文
    （PAPER/DW/无上下文）恒纯 R，与上线前逐字一致。
    """
    parts: list[str] = []
    odds_low = bet.get("odds_low")
    odds_high = bet.get("odds_high")
    if isinstance(odds_low, (int, float)) and isinstance(odds_high, (int, float)):
        parts.append(f"胜算：{float(odds_low):.0%}–{float(odds_high):.0%}")
    reward_risk = bet.get("reward_risk")
    if isinstance(reward_risk, (int, float)):
        parts.append(f"赔率：{float(reward_risk):g}R")
    size_r = bet.get("size_r")
    if isinstance(size_r, (int, float)):
        size_text = f"注码：{float(size_r):g}R"
        annotation = _r_to_yuan_annotation(float(size_r), account_context, result_as_of)
        if annotation:
            size_text += f"（{annotation}）"
        parts.append(size_text)
    timing = _text(bet.get("entry_timing_basis"), limit=200)
    if timing:
        parts.append(f"时机：{timing}")
    exit_conditions = _text(bet.get("exit_conditions"), limit=200)
    if exit_conditions:
        parts.append(f"离场：{exit_conditions}")
    rationale = _text(bet.get("thesis_odds_rationale"), limit=200)
    if rationale:
        parts.append(f"胜算依据：{rationale}")
    # A4 保守自洽提示：只有自报区间最乐观端 EV 仍为负才提示——用户自己的
    # 数字推翻用户自己的注，机器不显示 Kelly、不做注码偏离判定。
    if _negative_ev_upper_bound(bet):
        parts.append("提示：按你给的胜算区间推算期望值仍为负，供参考，最终由你裁决。")
    if not parts:
        return ""
    return "——\n下注声明：" + "\n  ".join(parts)


def _negative_ev_upper_bound(bet: Mapping[str, Any]) -> bool:
    """A4 触发条件（保守形态）：EV_high = odds_high×b − (1−odds_high) < 0。

    区间内存在非负可能时零输出；typed 数字缺失或越界时零输出。
    """
    odds_high = bet.get("odds_high")
    reward_risk = bet.get("reward_risk")
    if not (
        isinstance(odds_high, (int, float))
        and not isinstance(odds_high, bool)
        and isinstance(reward_risk, (int, float))
        and not isinstance(reward_risk, bool)
        and 0.0 <= float(odds_high) <= 1.0
        and float(reward_risk) > 0
    ):
        return False
    return float(odds_high) * float(reward_risk) - (1.0 - float(odds_high)) < 0


def _aware_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _r_to_yuan_annotation(
    size_r: float,
    account_context: Mapping[str, Any] | None,
    result_as_of: object,
) -> str | None:
    """R→元注码参照（纯展示、零门）：只在 ADVISORY_REAL 账户上下文上给出。

    判定次序钉死：① 先判 valid_until vs result.as_of 的读取后过期交错；
    ② 再按 status 映射。其余任何形态返回 None（纯 R，逐字一致）。
    """
    if account_context is None or account_context.get("mode") != "ADVISORY_REAL":
        return None
    result_time = _aware_time(result_as_of)
    valid_time = _aware_time(account_context.get("valid_until"))
    expired = result_time is not None and valid_time is not None and result_time > valid_time
    validity_unknown = result_time is None or valid_time is None
    status = account_context.get("status")
    if expired:
        return "净值快照已过期，未换算"
    if status == "READY":
        if validity_unknown:
            # 审计 major 1：无法证明有效期时不得换算金额，纯 R + 诚实原因。
            return "净值快照有效期未知，未换算"
        net_assets = account_context.get("net_assets")
        if (
            isinstance(net_assets, (int, float))
            and not isinstance(net_assets, bool)
            and net_assets > 0
        ):
            amount = float(net_assets) * 0.01 * size_r
            as_of_time = _aware_time(account_context.get("as_of"))
            date_text = as_of_time.strftime("%Y-%m-%d") if as_of_time is not None else ""
            return f"约 ¥{amount:,.0f}，按 {date_text} 确认净值 1% 参照，未计回撤高水位口径"
        return "净值未提供，未换算"
    if status == "PARTIAL":
        return "净值快照不完整，未换算"
    if status == "UNKNOWN":
        return "净值快照状态未知，未换算"
    return None


def _render_unavailable(payload: Mapping[str, Any]) -> str:
    answer = _mapping(payload.get("answer"))
    summary = _text(
        answer.get("summary"),
        limit=3_500,
        fallback="FIN 当前无法形成咨询结论。",
    )
    problem = _mapping(payload.get("problem"))
    problem_code = _text(problem.get("code"), limit=128)
    lines = [summary]
    # A2: 降级 fresh 失败也不能静默——提示早于 problem/error id/gaps。
    if _continuity_degraded(payload):
        lines.append(_DEGRADED_FRESH_FAILURE_NOTICE)
    if problem_code:
        lines.append(f"公开问题：{problem_code}")
    # A1: 技术故障的 sanitized error id 最多展示一次;不泄露内部诊断字段。
    error_id = _text(problem.get("error_id"), limit=64)
    if error_id:
        lines.append(f"错误编号：{error_id}")
    gaps = _text_list(payload.get("data_gaps"), limit=24, item_limit=128)
    if gaps:
        lines.append("数据缺口：" + "、".join(_gap_messages(gaps)))
    if payload.get("analysis_profile") == "DECIDE_NOW":
        lines.append(_SAFETY_FOOTER)
    return _bounded(lines)


def render_consultation_markdown(payload: Mapping[str, Any]) -> str:
    """Render the finalized product without re-reading Agent contributions."""

    daily_product = _daily_workspace_product(payload)
    if daily_product:
        return _render_daily_workspace(payload, daily_product)
    product = _canonical_product(payload)
    return _render_product(payload, product) if product else _render_unavailable(payload)


def project_consultation_presentation(payload: Mapping[str, Any]) -> dict[str, str]:
    """Attach the complete FIN-owned presentation contract for edge adapters."""

    return {
        "schema_version": PRESENTATION_SCHEMA,
        "format": PRESENTATION_FORMAT,
        "text": render_consultation_markdown(payload),
    }
