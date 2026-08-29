# portfolio · 设计页（Actual Advisory Portfolio：确认快照 → 预览/发布 → 咨询读取）

> 依据：rebaseline-20260827.md 附录 C.4 映射——`internal-module-catalog.md` “Actual Advisory Portfolio Snapshot” +
> 核心/可选完整性门 + stale 降级 +
> preview→confirm→read + 交易日历权威；代码只读核对（页内断言带 file:line）。定位：W2' 移植施工输入；本文不改代码。

## 目标 / 非目标

**目标：**

- 记录真实持仓咨询快照的 durable 设计不变量：核心完整性门与可选字段的独立 reason、算术矛盾拒绝、stale/PARTIAL 降级、CAS 发布与 preview→confirm→read 链、owner-only 文件安全。
- 固定唯一正式来源（fixed-XDG 快照），禁止回退旧 user_portfolio.json。

**非目标：**

- 不把用户确认事实升级为券商 truth、实时价格、G cognition、Z evidence 或真实执行 authority；不解析截图、不联网补字段、不保存 broker credential/account number/image hash/path（目录条目）。
- 不重做 warmup（其 canonical 迁移已收口）；不定义 read-capabilities 薄 server（D1 v2 已是设计页）。

## 数据与 schema 事实（owner 清单）

| store | owner | 位置/schema | 事实 |
| --- | --- | --- | --- |
| 正式确认快照 | `ActualAdvisoryPortfolioStore`（读）/ `ActualAdvisoryPortfolioPublicationOperator`（唯一写 seam） | fixed-XDG `$XDG_CONFIG_HOME/fin-analyse/actual-advisory-portfolio.v1.json`（`actual_advisory.py:37,206`）；schema `actual-advisory-portfolio.v1`（:30） | 目录 0700/文件 0600、regular/single-link/no-follow；revision = 原始 bytes SHA-256；`_MAX_BYTES=64K`（:39）；禁止落在 checkout 内（`forbidden_root` 注入 :208-212） |
| 通用文件 seam | `OwnerOnlyJsonSnapshotFile` | 单一固定目标 | stable read + 目录 fd flock + CAS + atomic replace（`owner_only_snapshot.py:87,103,194,279,532`）；CAS 失配 → `CAS_MISMATCH`（:42） |
| pending review | `PendingReviewStore` | 预览会话的 pending（session 代际绑定是 durable seam） | confirm 在 per-principal 锁内要求 `envelope.session_id == pending.session_id`；旧磁盘格式缺字段 fail-closed（`pending_review_store.py:52,123,186,297,327`） |
| 交易日历 | `AShareTradingCalendar` | `config/market/a_share_calendar_2026.json` | 见 market-data 页；DECIDE_NOW 的 action-ready 判定复用同一日历 |

## 关键不变量

1. **输入合同**：字段集合必须精确等于 schema、`confirmation=USER_CONFIRMED`、`source_kind ∈ {USER_CONFIRMED_BROKER_SCREENSHOT, USER_CONFIRMED_MANUAL}`、`positions_complete=true`；`as_of` 必须 timezone-aware 且不得晚于 now（`actual_advisory.py:340-356`）。
2. **算术矛盾拒绝**：重复 symbol、`net_assets==0` 却有持仓、`available_cash > net_assets + margin_debt` 任一成立 → invalid（:366-374）。
3. **核心/可选完整性门**：核心完整集 = 总资产、可用资金、每只持仓的数量/成本/有效市值（`available_cash` 等 :51-64）；价格/原始市值/成本/可卖/两融可以未知，但每项未知保留独立 reason（`NET_ASSETS_UNKNOWN`/`AVAILABLE_CASH_UNKNOWN`/`MARGIN_DEBT_UNKNOWN`/`AVERAGE_COST_UNKNOWN`，:378-388），不得用派生市值吞掉缺口或猜测。
4. **stale 降级**：`now >= as_of + 24h` → `STALE`（:31,378-380）；结构有效但有未知事实 → `PARTIAL`（可解释，不支持动作型判断）；`read()` 对 missing/invalid/未来时点返回 typed UNKNOWN 且不调用基于持仓的 G 分析（:214-233）。
5. **preview→confirm→read**：`preview`（:251）零写并投影 READY/PARTIAL+缺口；`publish`（:269）绑定 `candidate_revision` 与 `expected_current_revision`（:275-276），exact replay 零写；confirm 走 `PendingReviewStore.confirm_under_lock`（:297），`expected_candidate_revision`/session_id 不匹配即失败；唯一持仓更新入口 `review_actual_portfolio` 的确认短语 `确认更新持仓`（`portfolio_review.py:50`），语法/歧义留 owner（REJECTED/NEEDS_INFORMATION，:535-548,593-595）。
6. **自然点名只能选既有持仓**：Consultation 不得从问题中的数量/成本/价格/名称构造 position；逐股当下操作咨询才受 1–5 focus 门槛；纯事实概览不要求 focus（目录条目）。
7. **action-ready 门槛**：`DECIDE_NOW` 另要求同一上海交易日且 age≤30 分钟才标记 action-ready；否则仍可用于结构分析但只能 `NO_ACTION`（目录条目）。
8. **迁移残项（已收口）**：market warmup/benchmark 已 clean-break 到 `ActualAdvisoryPortfolioStore.read()`；`UserPortfolioStore` 已删除、旧 `knowledge-base/runtime/user_portfolio.json` 无读取方并删除（NOW 2026-08-27 条目；commits `e1281255`/`d5b583bb`）；不恢复为任何静默 fallback，两套真实持仓不同时作为正式来源。
9. **隐私**：截图中的账户号、金额、持仓和图像路径不得进入代码、fixture、catalog 或 Git；公开引用只经 `actual_advisory_snapshot_ref(revision)`（:315）。
10. **分析期身份冻结**：分析期间 revision/source kind/as-of/完整 instrument scope 漂移会作废本轮 G/Z 产品并要求重试（目录条目）。

## 接口契约

- **读 seam**：`ActualAdvisoryPortfolioStore.read() -> ActualAdvisoryPortfolioRead`（`actual_advisory.py:214`）；普通咨询只经 `fin.read_actual_portfolio` 按需读取，`ProductionReadCapabilityProvider` 投影核心事实、可靠派生的总市值/仓位与 revision receipt，不预取、不复制第二份账户状态。
- **写 seam**：`ActualAdvisoryPortfolioPublicationOperator.preview(source)` / `.publish(request)`（:251,269）；`scripts/manage_actual_advisory_portfolio.py validate|show|preview|publish` 默认只读、`--apply` 显式结构化。
- **review seam**：`review_actual_portfolio`（模型提交观测事实 → FIN 规范化代码/零股/追问 → 预览 → 用户确认 → CAS 发布）；Hermes bridge 对一次精确关联的 review 结果做 deterministic finalization，首次 PREVIEW_READY/REJECTED/PUBLISHED/UNCHANGED/NEEDS_INFORMATION 后不再让前台模型重复调用（目录条目）。
- **日历 seam**：消费者只依赖 `AShareTradingCalendar.session_at/next_open_date/previous_open_date`（见 market-data 页）。
- **共享持久化 seam**：`OwnerOnlyJsonSnapshotFile` 只拥有 owner-only stable read/directory-fd lock/CAS/atomic replace；领域模块仍独占 JSON/schema/时效/一致性语义（目录条目）。

## 已知故障与设计回应

- **“自测过但真实使用失败”的输入缺口** → 引入确认快照解决“先让 FIN 理解实际账户”（目录条目定位）。
- **派生市值吞掉缺口/猜测** → 每项未知独立 reason；派生市值显式 `market_value_derived`（:104-117）。
- **并发/重复发布** → CAS（candidate_revision + expected_current_revision）+ exact replay 零写（:269-276；`CAS_MISMATCH`）。
- **旧磁盘格式缺 session_id** → fail-closed，不静默放行（`pending_review_store.py`）。
- **warmup 读到 checkout 内旧 user_portfolio.json** → clean-break 到 fixed-XDG 快照并删除旧文件；不回退（NOW 收口条目）。
- **截图现价误当行情** → 截图价格只作该 `as_of` 的 reference，不是当前行情 evidence（目录条目）。

## 验证方式

- **回归入口**：`tests/portfolio/test_actual_advisory_portfolio.py`、`tests/portfolio/test_portfolio_review.py`、`tests/portfolio/test_pending_review_store.py`、`tests/scripts/test_manage_actual_advisory_portfolio.py`、`tests/consultation/`、`tests/gateway/test_consultation_mcp.py`、`tests/gateway/test_portfolio_review_mcp.py`、`tests/gateway/test_portfolio_review_e2e.py`。
- **完整性门验收**：核心集齐全但两融/可卖/截图现价未知 → 不遮住快照、可派生态市值/仓位；核心缺失 → “不完整”投影、不编造。
- **矛盾拒绝验收**：`available_cash > net_assets + margin_debt`、重复 symbol、零净资产带持仓 → invalid。
- **stale 验收**：`now >= as_of+24h` → PARTIAL+STALE；DECIDE_NOW 需同一上海交易日且 age≤30min。
- **CAS 重放验收**：exact replay 零写；expected_current_revision 不匹配 → CAS_MISMATCH；pending session_id 不匹配 → 拒绝确认。
- **身份冻结验收**：分析期 revision/as-of/scope 漂移 → 作废本轮 G/Z 并要求重试。
