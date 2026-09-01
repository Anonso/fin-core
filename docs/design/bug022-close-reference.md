# BUG-022 短设计：盘后收盘价合格化（close-reference qualification）

日期：2026-09-01 · 级别：核心（公共入口/接口契约，规则 5）

## 目标 / 非目标

**目标**：`read_market_snapshot` 在盘后（AFTER_CLOSE/CLOSED_DAY）对双源同价的
最近完成交易日收盘价，返回 `status=READY`、价格可投影、`data_gaps=()`，并显式
标注为盘后参考（`observation_mode=CLOSE_REFERENCE`、`reference_only=true`）。

**非目标**：不改盘中/盘前/BREAK/UNKNOWN 语义；不把盘后报价当 LIVE；不伪造
daily bar（未完成/不可用必须仍可见）。

## 语义设计

1. 新增 per-instrument 字段（additive，向后兼容）：
   - `price`：顶层冗余投影，模型直接消费，值 = `quote.qualified_price`；
   - `observation_mode`：`LIVE | CLOSE_REFERENCE | REFERENCE_ONLY | UNAVAILABLE`；
   - `context_limitations`：不阻塞价格结论、但必须显形的缺料列表。
2. `close_qualified` 判定（全部满足）：
   - phase ∈ {AFTER_CLOSE, CLOSED_DAY} 且 session 无 gaps；
   - 期望收盘交易日 = `calendar.previous_open_date(before=本地日+1天)`
     （AFTER_CLOSE=当日；周末/假日=上一交易日）；
   - 双源 facts 齐全（len==2）、`disagreement_ratio <= _READY_DISAGREEMENT`、
     无 DUAL_SOURCE_QUOTE_CONFLICT / INCOMPLETE / DISAGREEMENT；
   - `quote.qualified_price` 非空且未停牌（not primary_suspended）；
   - 全部 fact 的 `source_event_at` 本地日期 == 期望收盘交易日（防隔日旧价冒充）。
3. `close_qualified` 时：
   - status 覆盖为 READY；
   - `PRIMARY_TRADING_STATUS_UNKNOWN`、`NON_CONTINUOUS_REFERENCE_QUOTE` 从
     data_gaps 移除（盘后 trading_status=unknown 是预期，参考性由
     `observation_mode`/`reference_only` 承载）；
   - `MARKET_SESSION_REFERENCE_ONLY`、`CURRENT_TRADING_DAY_BAR_NOT_INCLUDED`
     （及 bars_gap 中的缺料）移入 `context_limitations`——真实缺料仍机器可见；
   - `reference_only=true`、`manual_review_eligible=false`。
4. 非 `close_qualified` 路径零改动（含 PRE_OPEN/BREAK/盘中/UNKNOWN）。

## 改动文件

- `fin_analyse/market/on_demand_tactical_context.py`：`_collect_symbol` 增加
  close-qualification 分支与字段投影；`TacticalInstrumentContext` /
  `OnDemandTacticalContext` 增加 additive 字段并聚合。
- `fin_analyse/guo_teacher_research/production_capability_provider.py`：
  `_on_demand_market_snapshot_value` 透传（to_agent_dict 已含，确认即可）。
- `tests/market/test_on_demand_close_reference.py`：盘后双源同价 → READY /
  gaps=() / CLOSE_REFERENCE / limitations 含盘后+bar lag；隔日旧价不通过；
  盘前/BREAK 行为不变回归。

## 验证

1. focused 新测试 + 默认套件 + ruff。
2. 晚间直调实弹：000657.SZ / 600879.SH → `data_gaps=()`、`price` 非空、
   `observation_mode=CLOSE_REFERENCE`、limitations 含盘后标注与 bar lag。
3. 真实 CLI 问询「最新价」一次闭环后关 BUG-022。

## 为什么不是别的做法

- 不改 `_qualify_quotes`：盘中/盘前语义零扰动，close 判定集中在 `_collect_symbol`
  一处，有完整上下文（事件日期/日历/分歧比）。
- 不把 daily bar 缺料静默：进 `context_limitations` 保持机器可查，避免「gaps 空」
  变成不诚实。
- 不把盘后当 LIVE：`reference_only=true` + `CLOSE_REFERENCE` 标注不退化。
