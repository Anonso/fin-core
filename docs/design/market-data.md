# market-data · 设计页（快照/资格化/按需行情/两融/官方记录/市场概览 + 交易日历）

> 依据：rebaseline-20260827.md 附录 C.4 映射——`internal-module-catalog.md` 六条目（Market Snapshot / Market Data Qualification Primitives /
> On-Demand Tactical Market Context / Margin Evidence / Official Company Record Evidence / Current Market Overview）+ 交易日历权威 +
> a-share-data-qualification 调研（原件随迁 `docs/research/`，现位于 `docs/pm/research/`）；代码只读核对（页内断言带 file:line）。
> 定位：W2' 移植施工输入；本文不改代码。

## 目标 / 非目标

**目标：**

- 记录六类行情/证据读能力的共同不变量：cache_only/stale_fallback、provenance（source/provider version/revision/qfq）、deadline 语义、双源资格门槛、交易日历权威。
- 固定各 reader 的 durable/无状态边界与 artifact root，供移植按 owner 划分。

**非目标：**

- 不定义 read-capabilities 薄 server（D1 v2 已是设计页）；不定义 G/持仓消费语义（见 g-cognition / portfolio 页）；不含 OpenCLI transport fallback 实现细节（只登记边界）。

## 数据与 schema 事实（owner 清单）

| 模块 | 状态 owner | artifact root / 状态性质 | 关键代码 |
| --- | --- | --- | --- |
| Market Snapshot（legacy composite） | `MarketDataCache`（缓存）/ `ProviderRegistry`（provider 列表与熔断） | 磁盘缓存带 `_expires_at`；`allow_stale` 只返回过期数据，不触发 provider | `market/snapshot.py:42-45,144-155`；`market/cache.py:49-74` |
| Qualification Primitives | 无 durable state | 只在当次按需请求内校验/投影 | `market/data_qualification.py`（目录条目） |
| On-Demand Tactical Context | 不可变 artifact（完成日线/30 分钟线） | `fin-analyse/on-demand-tactical-context-v1/{daily-bars,thirty-minute-bars}` 两 root 物理分离 | `market/on_demand_tactical_context.py:87-89` |
| Margin Evidence | immutable raw/manifest/latest receipt | `fin-analyse/margin-evidence-v1` | `margin/evidence.py:24` |
| Official Company Record | immutable raw/manifest/latest receipt | `fin-analyse/official-record-evidence-v1` | `official_records/evidence.py:15` |
| Current Market Overview | 无持久状态（单次调用内存） | source 独占一个 `requests.Session` | `market/current_overview.py:269-292`（目录条目） |
| Trading Calendar | 无可变状态；冻结 artifact | `config/market/a_share_calendar_2026.json`；phase policy 锁定 `a-share-order-entry-hours-2023-v1`（`market/trading_calendar.py:22`） | `session_at` :322 |

## 关键不变量

### cache_only / stale_fallback

- `data_mode=cache_only` → 只走缓存：hit 立即返回、stale 用过期盘面兜底、miss 明确返回；**不发起 live provider 调用**（`snapshot.py:148-149`）。
- `MarketDataCache.get(key, allow_stale=False)`：允许 stale 时返回过期数据作为 fallback，`_expires_at` 决定跨进程 freshness（`cache.py:49-74`）。
- 官方记录：传输失败只能回放同 source scope 的已验证 latest capture，并显式 `OFFICIAL_RECORD_EVIDENCE_STALE_CACHE`（`official_records/evidence.py:199-200`）；完整官方空响应是 `EMPTY`，绝不伪装 unavailable（目录条目）。

### provenance（来源可归因）

- 按需行情：每个 frame 带 cutoff/coverage/source/provider version/qfq adjustment/raw revision/typed gaps；provenance 逐条 `provider_id@provider_version`（`on_demand_tactical_context.py:621`）。
- 两融：结果携带 `source_revision` 与 `denominator_source_revision`（`margin/evidence.py:50,320,434`）。
- 官方记录：capture `revision`（raw `payload_sha256`）随官方更正改变，同一 document 的 canonical row hash 也随更正改变（`official_records/evidence.py:218-219`；目录条目）。
- 概览：结果固定单源 `PARTIAL` external reference，显式声明 observation age/延迟/BJ 未覆盖/持续性未评估（目录条目）。

### deadline 语义

- deadline 只传运行预算，不进入 immutable artifact identity（目录条目）。
- 按需行情：读前/读中/读后复验 `deadline_at`（`on_demand_tactical_context.py:344,408,434`）；等待预算 `_deadline_wait_seconds`（:371,453,566）。
- 概览：no provider call may start after the caller's deadline（`current_overview.py:127`）；fetch 后越过 deadline → `CONSULTATION_DEADLINE_REACHED`（:793-798）。
- 两融/官方记录：`deadline_at` 必须 timezone-aware（`margin/evidence.py:603-606`；`official_records/evidence.py:342`）。

### 双源资格门槛（on-demand）

- 连续竞价 quote max-age 15 秒；双源差异 `≤0.3%` READY、`>0.3%..1%` PARTIAL、`>1%` UNKNOWN（`on_demand_tactical_context.py:82-83`）；报价值始终取合格 primary，reference 只做交叉验证。
- 单 reference 只能 PARTIAL/reference-only；非连续阶段允许有界最近参考价但强制 `reference_only`。
- 30 分钟线按上午/下午 session 成对聚合 60 分钟，绝不跨午休或缺 bar 拼接；单 frame 失败不污染 quote/日线（目录条目）。

### 概览 capture 窗口

- `as_of=None`（current read）只接受 `captured_at ∈ [read_started_at, fetch_returned_at]` 的 snapshot（`current_overview.py:725-729,785-790`）；盘中参考以全体证据最早时间计算年龄并限制 30 分钟（:58）。
- 完成交易日要求每条证据至少到达当日 15:00（目录条目）。

### 交易日历权威

- `AShareTradingCalendar.from_file` 离线验证 canonical SHA-256/证据等级/时区/覆盖区间/沪深双源一致性（目录条目）；`session_at` 返回带 snapshot/hash/version 的 OPEN/CLOSED/UNKNOWN（`trading_calendar.py:322`）；越界不猜测。
- phase policy 精确锁定 `[09:30,11:30)`、`[13:00,15:00)`，版本 `a-share-order-entry-hours-2023-v1`（:22）。

## 接口契约

- **on-demand**：`compile_market_evidence_plan` / `OnDemandTacticalContextReader.read(request)` / `refresh_quotes(...)`；调用方不得提交 provider、复权模式、URL、artifact path 或参数（目录条目）。
- **margin**：`MarginEvidenceReader.read(MarginEvidenceRequest) -> MarginEvidence`；只收 FIN identity resolver 已验证的 SH/SZ symbol。
- **official record**：`OfficialRecordEvidenceReader.read(...)`；每请求 ≤5 标的、查询窗口/deadline/15 分钟 validity 显式绑定。
- **overview**：`AshareMarketOverviewService.read(AshareMarketOverviewRequest) -> AshareMarketOverviewResult`；`as_of=None` 原样透传，不在 caller 层注入旧 now。
- **snapshot**：`MarketSnapshotService.get_snapshot(MarketSnapshotRequest) -> MarketSnapshotResult`（含 cache_status/cache_hit/data_freshness/data_gaps/error_code）。
- **calendar**：consumer 只依赖 `session_at` / `next_open_date` / `previous_open_date`；禁止重新引入 weekday fallback 或独立前后交易日 resolver（目录条目）。
- **来源边界**：全部六类 reader 的行情/证据固定 `NON_G / external_reference`，不能验证、强化、覆盖或写入 G；advisory-only、execution-disabled。

## 已知故障与设计回应

- **东财 push2his 不可达** → 腾讯 qfq 日线 fallback（`_FallbackDailyBarReader` 东财优先、腾讯兜底、OHLCV 行序、cutoff 严格过滤；目录条目）。
- **午间 D-1 generation 污染收盘后 D generation** → artifact key 绑定 provider-version/symbol/trading-day/completed-through-date（目录条目）。
- **OpenCLI transport 反复失败** → 300s TTL 进程内 cooldown + 每次请求单次尝试、无 daemon、无无限 retry；opencli 路径只是同一 source 的 transport fallback，不计第二来源（目录条目）。
- **官方记录分页有限** → `DOCUMENTS_TRUNCATED` 显式标记，不把有限页伪装为全量（目录条目）。
- **概览 top-page 畸形行** → 形状级破损（截断/计数失配/分节缺失）与指数投影
  不完整仍整体 `UNKNOWN`；排名分节行投影不完整自 2026-08-31 起按源降级
  （空榜 + 显式 gap + 诊断出门），不静默丢行、不静默改写排名（BUG-002
  结构性半边定修，见下）。
- **报价来源身份/时间冲突** → 各自 evidence/frame typed 降级，单标的/单 frame 失败不污染其余（目录条目）。
- **on-demand 嵌套预算自挤占**（BUG-002，2026-08-28 诊断）：每个 symbol worker
  最坏向 detail executor 提交 3 个任务（quote+日线+30 分钟线），5 worker × 3=15
  超过旧 detail `max_outstanding=10`，且 `acquire(blocking=False)` 无队列——
  盘前/盘中上游慢（tencent 10s 超时）时任务滞留额度，后提交被拒，标的被
  误标 `ON_DEMAND_MARKET_CAPACITY_EXHAUSTED`（trace 三次全中、延迟整齐
  12-14s=一次上游超时+等待）。回应：detail 额度配平最坏嵌套需求（20），
  `test_on_demand_executor_budget.py` 钉死「detail 额度 ≥ symbol workers×3、
  symbol 额度 ≥ `_MAX_SYMBOLS`」不变量。语义保留：容量拒绝仍是诚实降级
  （typed unknown），只是不再因自家预算错配而误触发。
- **overview 盘前整链拒绝**（BUG-002 结构性半边，2026-08-28 晚诊断）：08-28
  09:07/09:16（PRE_OPEN）两次 `read_market_overview` 全拒
  `MARKET_OVERVIEW_SECTION_COVERAGE_INVALID`，同日 14:25 与前夜 23:19 同参数
  全过（calls.jsonl 七条对照）。晨间留存工具输出（consult 会话 8b0b5487）钉死
  机制：五个 ranked section 全部 `valid_projected_rows=0/returned=100`、
  `missing_timestamp_count=0`、reasons 全 `PROJECTED_ROWS_MISMATCH`——标识字段
  f12/f14 与时间戳 f124 齐全，唯行情数值字段缺失。根因：东财 clist 盘前对行情
  衍生字段（f3 涨跌幅/f6 成交额）返回 fltt=2 缺失占位 `"-"`，`_finite_float`
  解析失败 → `_project_board`/`_project_equity`（current_overview.py:1302,1322）
  整行 None → coverage 门（:822-837）any-reason 即全拒。定性：**校验语义与
  PRE_OPEN 模式自相矛盾**——当日 `effective_trade_date=2026-08-27`、
  `observation_mode=LATEST_COMPLETED_SESSION` 证明读侧意图就是「盘前取上一
  完成交易日」，provider 的占位形态是每交易日必然出现的合法状态而非损坏，
  校验却把自己要服务的场景拒之门外（每交易日开盘前确定性复现，非偶发）。
  实证：`uv run python /tmp/bug002_coverage_repro.py`（临时只读脚本，东财公开
  接口 GET；修复落地后由单测替代）三段闭环——现场盘后基线全过；f3/f6 任一换
  `"-"` 全 section 复现 `('PROJECTED_ROWS_MISMATCH',) valid=0/100`；端到端
  gaps 与生产记录逐字一致。明晨盘前（09:00-09:20）重跑同脚本可看 provider
  原生占位形态（脚本 [1] 段打印 f3/f6 原始类型），作最终一锤。
  回应方向（未施工，排 W3-4）：盘前占位形态下投影降级不应等于全链拒绝——
  候选：PRE_OPEN 时占位行按「无更新」处理放行既有 timestamp 校验、或 coverage
  门按 section 降级为显式 gap 而非 UNKNOWN；无论哪种，保持诚实缺口显形不变
  （f3/f6 缺席的宽度/涨跌信息照常标 gap）。
  **定修落地（2026-08-31，候选 b 收窄版）**：以「行投影地面真值」裁定降级，
  不按 coverage 计数器（生产中计数与行同源同函数，计数仅作诊断呈报）。
  语义：①覆盖门仅形状级原因致命（计数/时间戳数量失配、分节缺失/重复），
  仅 `PROJECTED_ROWS_MISMATCH` 不再整链拒绝；②任一行投影失败 → 该行源
  （行业/概念/个股，注意 industry_change 与 industry_turnover 共享合并行源）
  整源退出证据流——空榜 + 显式 gap `MARKET_OVERVIEW_SECTION_ROWS_
  UNPROJECTABLE` + `coverage_diagnostics` 随 PARTIAL 结果出门（干净读取为
  空元组，capability value 输出不变），不静默丢行、不静默改写排名；③降级
  源的行与时间戳同步退出全部下游门（trade-date/session/age/provider_
  updated_at），指数必需性不变（指数投影不完整仍整链拒绝）；④空分节源仅在
  显式降级时合法，未降级而空仍 `PAYLOAD_INVALID`。盘前形态下指数（腾讯实时
  源，昨收值）+ 广度照常诚实呈现，Daily 渲染器（status==PARTIAL）恢复可用。
  实证：`tests/market/test_current_market_overview.py` 三测钉死（盘前占位
  回归 = 08-28 09:07 生产形态；行降级不静默改写排名；纯计数失配不致命），
  market 284 绿；盘中实弹 read 无回归（PARTIAL、12/12/15 榜单、诊断空）。
  盘前生产形态实弹确认 = 下一交易日 08:55 premarket 班 `l1_material_market_
  overview_unavailable` gap 消失。
- **报价源整型假设 vs push2delay 浮点契约**（BUG-011，2026-08-31 诊断+修复）：
  08-27 起 trace 中凡真打到 push2delay 报价源的 `read_market_snapshot` 100%
  带 `EASTMONEY_RAW_SOURCE_PAYLOAD_PARSE_FAILED`（此前「ok」样本全是未触达
  源的短路：identity 未解析/无标的 0ms 返回、或容量拒绝，见 trace 复盘）。
  根因：报价端点 08-02 由 push2 切至 push2delay（git 契约注释），f48 成交额
  以带分位浮点返回（实测 8/8 样本跨 svr 一致，如 793325655.17），解析器
  `_optional_nonnegative_quantity` 按 push2 时代整型契约写，`not isinstance(
  value, int)` 必抛 → 解析失败；且失败捕获 venue=None 使 `_qualify_quotes`
  身份比对恒假阳性追加 `EASTMONEY_RAW_IDENTITY_MISMATCH`，噪声掩盖真因。
  回应：①解析器接受有限、非负的 int|float（`Decimal(repr(...))` 最短往返
  保真转字符串；NaN/Infinity 显式拒绝）；②失败捕获以其自身 typed gap 为
  完整结论，跳过身份比对，真实身份错配照常上报。实证：盘中实弹三标的
  capture `gaps=()` + replay 一致；真实装配端到端探针（两标的 READY）
  `gaps=()`；`test_eastmoney_raw.py` 浮点契约用例 + `tests/market/
  test_on_demand_qualify_quotes_gap_hygiene.py` 回归（trace 生产签名成对
  gap 不再出现）。

## 验证方式

- **回归入口**：`tests/market/test_market_snapshot_service.py`、`tests/market/test_data_qualification.py`、`tests/market/test_on_demand_tactical_context.py`、`tests/market/qualification_sources/`、`tests/margin/test_margin_evidence.py`、`tests/margin/test_eastmoney_margin_source.py`、`tests/official_records/test_official_record_evidence.py`、`tests/official_records/test_cninfo_official_records.py`、`tests/market/test_current_market_overview.py`、`tests/market/test_trading_calendar.py`。
- **cache_only 验收**：`data_mode=cache_only` 下无 provider 调用；stale fallback 显式标注、miss 明确失败。
- **provenance 验收**：每 frame/revision 的 source/provider version/qfq 与 raw 一致；官方更正确实改变 revision。
- **deadline 验收**：越过 deadline 的调用在 fetch 前/后均 typed 失败，不产生半个 artifact。
- **双源门槛验收**：差异 0.3%/1% 分界精确映射 READY/PARTIAL/UNKNOWN；单 reference 只能 PARTIAL。
- **日历验收**：hash/phase policy/双交易所任一漂移 → UNKNOWN/typed gap，不猜日期或 session。
- **调研原件随迁**：a-share-data-qualification 调研原件迁至新家 `docs/research/`（现 `docs/pm/research/`），随本页一并移交。
