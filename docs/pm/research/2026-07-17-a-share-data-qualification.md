# A 股关键时点数据资格方案

**日期:** 2026-07-17

**历史研究问题（非当前队列）:** `当前行情与信息源够不够做 10:03/14:40 决策？`

> 本文只保留资格研究证据与历史完成口径；当前状态和优先级只以 [NOW](../NOW.md) 为准。

**当前结论:** `OBSERVER_V1_IMPLEMENTED / TWO_RAW_SOURCE_CANDIDATES_IMPLEMENTED / DAY0_AND_CAMPAIGN_ENGINEERING_CHAIN_IMPLEMENTED / LIVE_AND_FIVE_DAY_EVIDENCE_PENDING`

现有 FIN 数据链可以继续用于研究、候选生成和模拟盘开发，但还不能被认定为 14:39 下单决策的执行级数据。主要缺口不是“数据种类不够多”，而是缺少源时间、盘口/交易状态、交易日历、公司行动以及五日关键时点证据。完成下述资格观察前，现有行情不得直接授权真实新增风险。

## 1. 本轮要回答的问题

资格测试只回答三件事：

1. 10:03 盘面分析和 14:40 最终决策能否在截止时间前拿到可追溯输入；
2. 哪些数据可以降级为 `UNKNOWN` 而不中断流程，哪些数据未知时必须阻止新增风险；
3. 现有免费源、券商源或候选付费源分别适合 `RESEARCH`、`PAPER`、`EXECUTION` 中哪一级。

它不验证策略收益，不把外部行情或公告写成老师认知，也不产生真实委托。

## 2. 当前实现盘点

| 能力 | 当前实现 | 可复用性 | 执行级缺口 |
|---|---|---|---|
| 统一市场快照 | `fin_analyse/market/snapshot.py` | 可复用上层请求、缓存和 data gap 语义 | 实时路径实际可退化为日线最后收盘价；freshness 是读取时刻，不是源时刻 |
| 报价 provider | `easyquotation`、腾讯、东方财富、mootdx 等 | 可作研究级交叉观察 | `QuoteResult` 没有源时间、买一卖一、涨跌停价、停复牌/集合竞价状态和序列号 |
| 多源共识 | `fin_analyse/market/consensus.py` | 可复用纯 ticker/百分比归一化和研究交叉检查 | `observed_at` 是本地时间，且当前一致值采用首个有效值，不能成为执行价格权威 |
| K 线/财务/两融/北向 | provider registry 与 warm cache | 可用于非关键 evidence | 缓存 TTL 不能证明关键时点新鲜度；复权与公司行动缺少统一版本证据 |
| 公告 | `researcher/providers/cninfo.py` | 可作为公告候选入口 | 需验证发布时间、抓取时间、公告标识、修订/撤回和原文哈希 |
| 研报/业绩预测 | `eastmoney_reports.py` | 可作外部 evidence | 不是公司官方披露，不能替代公告或老师认知 |
| 事件与资金 | 龙虎榜、解禁、大宗、股东人数、个股两融等 | 可作风险与解释 evidence | 缺统一市场两融拥挤度、官方交叉源和 point-in-time 版本 |
| 交易日/微观结构 | paper session 与简化成交 | 仅可作迁移材料 | 当前按工作日判断，未覆盖法定休市、临停、ST/板块涨跌幅、T+1 可卖与真实盘口 |
| benchmark | `fin_analyse/market/source_benchmark.py` | 可复用 provider、dataset、延迟与 JSONL 产物结构 | 当前只检查字段存在和调用耗时；timeout 后线程可能继续运行，脚本还引用已迁移服务，不能直接承担固定时点采集 |

所以第一步不是立刻购买更多数据，而是把现有源和候选正式源放进同一套可审计资格协议。资格结果必须落原始观察，不接受“页面看起来正常”作为证据。

只读核对发现的现状 P0：

- provider registry 主要以“是否抛异常”判断成功，部分 provider 吞异常后可能返回空报价，导致真正 fallback 不再接管；资格层必须硬验 `symbol + venue + price + source time`；
- 东方财富报价路径找不到精确 ticker 时可能采用列表第一条，修复并通过错标的反例测试前，该路径直接标 `REJECTED`，不能靠多源平均稀释；
- 巨潮公告当前把时间压成日期，无法证明在 10:03/14:39 当时已经可见；财务摘要也缺披露时间与修订版本；
- 当前两融只覆盖单股最近明细，并把融资买入偏高作为正向分量，尚不能表达“全市场杠杆拥挤与去杠杆风险”；
- 海外原型只有美股日线自身统计，没有 A 股 lead-lag 验证，也没有韩国科技链与时区/交易日对齐；
- 现存 benchmark 历史只覆盖财务、单股两融和北向等慢数据，没有 quote、minute、calendar、limit/status 或 broker 对照，不能折算成五日证据。

这些文件在本地调查快照与 M4 活动发布 `f0b6bf0dd92b83f4a6c1bb4e69c034bd99cef26a` 中相关内容一致；这只是只读核对，不代表允许改动该生产发布。

资格主体不是含混的“某家数据商”，而是：

```text
source_policy_id + dataset + usage_scope + adapter_version
```

同一 provider 在财务数据上合格，不代表其报价合格；primary/fallback/交叉源的组合和版本也必须分别受测。

## 3. 数据分级与降级语义

### A. 资本安全关键数据

以下任一项缺失、过期、相互冲突且无法确定时，`CapitalPermissionGate` 阻止**新增风险**：

- 有效交易日、当前交易阶段与服务器校时状态；
- 证券身份、交易所/板块、ST/风险警示、停复牌、除权除息和当日涨跌停价；
- 用于价格上限和委托校验的执行级报价及其源时间、接收时间；
- 券商连接、可用资金、持仓、可卖数量、在途委托和订单回报；
- 标的归属、账户归属、已有人工仓与 FIN managed 仓的可验证边界。

这不等于“一有未知就全系统瘫痪”。已有仓位仍进入 `PROTECTION_ONLY`：在券商事实足够时允许撤单、减仓和风险保护，但不再开新仓，也不根据猜测重试未知委托。

### B. 机会质量数据

公告、财报/业绩预告、两融、海外科技映射、研究报告、官方消息和多数资金指标缺失时：

- 该字段标为 `UNKNOWN`，记录原因和最后有效版本；
- 分析卡继续生成，系统运行不被单个慢源卡死；
- 依赖该证据的具体机会可以降权、缩量或 `ABSTAIN`；
- 不得把缺失值补写为中性、利好或老师观点。

## 4. 资格观察合同

每次观察至少保存以下字段，作为以后实现的 FIN-owned contract 输入：

| 分组 | 字段 |
|---|---|
| 身份 | `qualification_run_id`、`trade_date`、`window`、`dataset`、`provider_id`、`symbol`、`request_id` |
| 时间 | `scheduled_at`、`triggered_at`、`request_started_at`、`first_byte_at`、`source_event_at`、`source_published_at`、`received_at`、`available_at`、`collector_clock_offset_ms`、`clock_sync_status` |
| 版本 | provider/version、endpoint/schema version、复权模式、交易日历版本、原始响应哈希与受控 raw reference |
| 质量 | status、latency、source age、missing fields、field coverage、parse warning、重试次数、是否命中缓存 |
| 行情 | last、bid1/ask1、prev close、volume、turnover、limit up/down、market phase、suspension/risk flag |
| 对比 | 对齐方法、对照源、时间差、价格 tick 差、累计量差、状态冲突与最终裁定 |
| 审计 | 数据许可/来源类别、observer 版本、异常说明、人工裁定人和时间（如有） |

`source_event_at` 不存在时，该源可以保留为研究 evidence，但不得单独取得 `EXECUTION` 资格。接收时间不能冒充源时间。历史重放只按 `available_at` 判断当时是否可见，不能因为源宣称的事件时间更早而产生前视偏差。

正式产物分三层：运行前冻结且不可回改的 `SampleManifestV1`、逐源逐标的的 `DataCaptureArtifactV1`、按数据集和用途分别裁决的 `DataQualificationReportV1`。逐字段状态至少区分 `PRESENT | NOT_DUE | NOT_APPLICABLE | UNKNOWN | STALE | CONFLICT | ERROR`，避免把“尚未到披露期”误报成数据缺失。

## 5. 五个有效交易日的观察设计

### 5.1 Day 0 预检

正式计数前完成一次不计入五日的预检：

- 对所有机器做 NTP/系统时间检查，记录时区与最大偏差；
- 冻结 observer 版本、provider 配置、观察池、字段映射和阈值；
- 验证原始响应可重放、哈希一致且不会记录凭证；
- 明确主源和独立对照源；券商源未就绪时可先跑 research qualification，但不能由此获得 execution 资格；
- 阈值或 schema 在正式五日中发生实质变化时，从修复后重新计数。

每次采集统一计算 `scheduler_lag`、`fetch_duration`、`normalization_lag`、`ready_lag` 和 `source_age_at_ready`。源时间未知时，最后一项必须保持 `UNKNOWN`，不能拿网络请求耗时替代。

Day 0 校时采用分层质量门：`abs(clock offset) <= 250ms` 为 preferred，`250ms～2000ms` 保持可用并产生质量 warning，`>2000ms` 才硬阻断；观察窗 jitter p95 同样以 `<=100ms` 为 preferred、`>1000ms` 为硬阻断。券商正式 SLA 更严格时只可收紧。jitter 只从 plan 绑定且完整可计数的 5 日 × 4 checkpoint 历史计算：每个 checkpoint 的 primary/reference signed offset 必须一致，去重得到 20 个样本；以精确中位数为中心计算绝对偏差，取 nearest-rank p95 后向上取整。稳定的绝对偏差由独立 offset 门处理，不能把 `abs(offset)` 的 p95 误称为 jitter。持续时间用 monotonic clock，业务时间统一保存带时区的 UTC 并展示为 `Asia/Shanghai`。资本安全 checkpoint 发生硬偏差超限、时钟回拨或同步状态未知时，该窗口失败并进入 `NO_NEW_RISK`，不能用事后校时修正原证据。

正式 v3 双源 collector 不再接受命令行自报 `clock-sync-status` 或 `clock-offset-ms`。它在任何行情 provider 调用前，通过固定本地 systemd-timesyncd D-Bus message 读取 NTP 四时间戳并推导 offset，按绝对值向外取整后进入 qualification artifact；读取失败、未同步或超过 2000ms 都在 provider 前停止，250ms～2000ms 只记录质量 warning。`scripts/measure_a_share_clock.py` 可用同一 seam 做零网络、零写入预检。systemd-timesyncd 暴露的单次 `ntp_jitter_ms` 只作诊断，不能冒充 Day 0 要求的观察窗 jitter p95。正式生成计划固定 `observer_version=data-qualification.v3`，把内建系统校时语义绑定进 artifact identity；v2 手工 observer 不能冒充该 campaign。collector 的 transport、clock 或 clock evidence 注入都强制降为 `TEST_ONLY`。

正式 operator 路径已进一步收敛：`collect_a_share_qualification.py` 固定读取 `$XDG_STATE_HOME/fin-analyse/a-share-paper-data-v1/{qualification-plan.json,qualification-evidence/}`，不接受调用方 plan/evidence root，并在 provider 前校验 owner-only 状态；测试依赖注入必须同时提供非空、绝对且不与 ambient formal data root 重叠的测试 XDG state。旧手工 `observe_market_data.py` 明确固定为 v2，拒绝正式 PAPER 保留 policy，并同时拒绝与 ambient 或 injected 正式 PAPER data root 重叠的 output root，因此调用方自报 clock 的 artifact 不能计入或占用 v3 campaign。

### 5.2 观察池

首版冻结 8～12 个公共 sentinel，再加当日实际 shortlist（最多 5 只），覆盖沪市主板、深市主板、创业板、科创板以及高/低成交和高/普通波动。只有出现真实错配或覆盖盲区时才扩样，不为样本数本身增加调用压力。每天另记录：

- 上证、深证、创业板、科创相关宽基指数；
- 当日实际出现的涨停、跌停、停牌、除权或特殊上市阶段样本；固定池没有时追加事件样本但不替换固定池；
- 全市场两融余额及可获得的行业/个股两融分布；
- 与主线有关的美股、韩国科技映射数据，只评估可用性和时差，不把相关性当因果。

禁入证券可以作为只读数据测试样本，但不得因此进入候选交易池。

事件样本依据前一晚权威清单或预先冻结的确定性规则选择，不能看过 provider 表现后挑样本。如果五日内没有某类真实事件，该维度保持 `INCONCLUSIVE`；fixture 只能验证 FIN 逻辑，不能冒充 live source 资格。

### 5.3 每日时窗

| 时窗 | 目的 | 完成条件 |
|---|---|---|
| 09:15～09:30 | 交易日历、公司行动、公告、证券状态预热 | 关键身份与日内限制在连续竞价前确定 |
| 09:30～10:03 | 开盘行为与第一轮分钟完整性 | 10:03:30 前可用为 target；10:05 是硬截止，迟到标失败而非沿用无标识旧值 |
| 14:35 | 冻结慢数据与候选集，预计算不依赖最后报价的部分 | 14:38:30 前完成；非关键慢源到点即 `UNKNOWN`，不拖延最终流程 |
| 14:38:50～14:40:05 | 对 shortlist + 少量固定 sentinel 以约 2 秒间隔采集执行候选报价 | 保留每次源时间、接收时间、状态与跨源差异；不对全池做无价值高频轮询 |
| 14:39 | 取得最终执行输入 | 14:39:30 前可用；消费报价时 source age 暂定上限 3 秒、receive age 上限 2 秒，Day 0 结合券商正式 SLA 只可收紧 |
| 不晚于 14:40 | 产出完整决策或明确 abstain | 决策绑定同一个不可变 capture hash；迟到的新买决策作废，不把延迟传给审批或追价提交 |
| 15:05 后 | 对账、分钟完整性、公告/公司行动回看 | 生成当日资格报告与异常清单 |

具体 `submit_not_after` 必须在券商资格完成后，按官方回报 SLA 和收盘安全缓冲单独冻结；14:40 决策成功不自动等于还有资格提交。

### 5.4 交叉核验

- 报价：以源时间对齐到最邻近样本；价格差按最小 tick 计，不拿错位时间的累计量做伪冲突。
- 执行关键字段：价格、涨跌停价、停牌/交易阶段和证券状态必须一致；价格超过 1 tick 或状态冲突时阻止新增风险并留样。
- 分钟线：验证预期分钟数、时间连续性、`low <= open/close <= high`、累计量单调性及日线聚合一致性。
- 公司行动：同时保存原始价、复权模式和动作版本；同一回放不得因当天重新下载而改变历史输入。
- 公告/业绩：以公告 ID、正式发布时间、抓取时间、修订关系和原文哈希去重；研报预测与公司披露分栏。
- 两融/海外：关注发布时间、所属交易日和时区；两融同时构造全市场余额、变化率、历史分位、行业集中和去杠杆状态，禁止继续把“融资买入高”单向解释为利多；晚到数据进入下一次可用快照，不能回填成当时已知。

## 6. 判定规则

每个 provider × dataset 分别给出等级，不做一个含混的“整体可用”：

- `EXECUTION_QUALIFIED`：五日所有资本安全字段可追溯，关键时点无漏窗，延迟/新鲜度达标，跨源冲突有确定性 fail-closed 处理，许可允许该用途；
- `PAPER_QUALIFIED`：足以驱动真实时钟模拟和重放，但缺少正式券商事实、执行级时间或许可；
- `RESEARCH_ONLY`：可作背景 evidence，不得生成真实订单价格或交易状态；
- `REJECTED`：身份错配、历史不可重放、静默返回错误证券、时间语义不明或关键字段不稳定。
- `INCONCLUSIVE`：观察窗内缺少真实事件或权威对照，不能据此宣称合格，也不把它误记成 provider 故障。

首版只要求拟选 primary 与一个独立 reference 完成相应用途资格，不要求把所有现有 provider 做完全笛卡尔积。某个稀有事件维度为 `INCONCLUSIVE` 时，可把相关证券/事件窗口排除并使其 `NO_NEW_RISK`，其余明确范围仍可取得 scoped qualification；只有要开放该事件范围时才补 live 证据。

五日资格的最低放行条件：

1. 10:03/10:05 与 14:35/14:39/14:40 每日都有完整 observation 和明确 verdict；
2. 所有执行关键未知都触发预期的 `NO_NEW_RISK`，没有静默 fallback 到上一收盘价；
3. 交易日历、公司行动、涨跌停、停牌和证券映射必须有真实样本，或有在当时被采集、带源时间与 raw artifact 的权威主数据证据；fixture 只验证 FIN 解析/门禁逻辑，缺少 live 事件时该维度保持 `INCONCLUSIVE`，不能取得 `EXECUTION_QUALIFIED`；
4. 原始输入可从 hash/reference 重放出相同 normalized artifact；
5. 观察代码、阈值和 provider 配置在五日内冻结。

单个非关键源失败不会重置五日；记录 `UNKNOWN` 即可。源时间错误、错误证券、错误交易状态或静默旧报价属于资本安全错误，修复并重新预检后，该 provider 的五日资格重新计数。

交易所正式休市或全市场正式中止不计有效交易日；FIN 漏跑、provider 宕机、某只 sentinel 停牌或 checkpoint 超时都是真实观察结果，不能从分母中删除。

## 7. 采购与替换决策

五日后按能力缺口采购，不按品牌采购：

| 缺口 | 优先补法 |
|---|---|
| 14:39 执行报价/交易状态 | 优先正式券商行情与回报；独立合法源只做 corroboration |
| 官方公告 | CNINFO/交易所原文入口，保留原文标识和发布时间 |
| 交易日历/证券状态/公司行动 | 交易所或持牌数据源的版本化主数据 |
| 财务/预测 | 公司公告为事实主源，预测作为隔离的外部 evidence |
| 两融与海外 | 允许研究级源先行；只有其直接进入风险门时才升级 SLA |

任何付费方案都要另列：年/月成本、许可范围、调用限额、历史深度、主备能力、退出和替换成本，再由用户单独批准。免费源若不合格，不用“多源投票”掩盖缺少权威时间与交易状态的问题。

## 8. 历史完成口径（非当前排期）

资本安全 observer 的 quote 子切片和两个 source-only 原始报价 adapter 候选已经实现：运行前冻结 manifest；按 source policy × dataset × usage scope 保存逐标的 upstream HTTP bytes/raw hash、关键时间和裁决阈值；从同一 bytes 确定性重放并校验 symbol/venue/price/source-time/limit/checkpoint。腾讯 adapter 使用固定 GB18030 解码；腾讯响应不能权威证明交易状态时保持 typed `UNKNOWN`。东方财富 adapter 只使用 `api/qt/stock/get` 的单证券 `secid` endpoint，不复用存在错误列表第一项风险的 `clist` 路径；它保存解码前 UTF-8 JSON bytes，回验响应中的代码与市场，并按响应精度字段缩放最新价和涨跌停价。2026-07-18 对东方财富官方个股页及官方前端 bundle `https://quote.eastmoney.com/newstatic/build/vendor.js` 的只读形状核对证明了 `secid`、代码/市场、源交易时间和交易状态字段的页面用途；其中 `f292` 只有官方明确命名的 `2=交易中`、`6=停牌`、`14=盘中停牌` 被映射，其余代码一律保持 `UNKNOWN`。本环境直连 quote API 遇到上游 TLS EOF，因此没有把该核对或 bundle 冒充 live capture / qualification evidence。两 adapter 遇到错证券/市场、畸形/JSONP/错误编码、字段缩放错误、陈旧源时间、缺关键字段或 replay mismatch 都保留 raw 并 fail closed。手工单源 observer CLI 与冻结双源 campaign collector 均默认 dry-run、零网络零写入；A 股 PAPER `QualificationCampaignPlan` 强制至少五个完整资格交易日和 10:03/14:35/14:39/14:40 四个时点，并另列严格晚于资格期的运行日及各自同日 Day 0 preflight，固定 manifest、primary/reference 角色及其精确 source/adapter version。这样只有已经发生的五日窗口参与资格计数，运行日只提供当日上下文，不能先生成未来证据再把时钟倒回资格期。collector 只允许显式授权 `tencent-raw | eastmoney-raw` 和 checkout 外 evidence root，不使用 legacy、聚合或跨 provider fallback。注入 transport 在两个 adapter 自身都强制只能使用 `TEST_ONLY`；collector 的注入 source factory 同样不能签发 LIVE_CAPTURE evidence，只有命令内建 factory 能构造两个 raw live adapter。现有 legacy quote adapter 仍会诚实记录缺口，但不再是 live backend；本切片没有注册 cron、接 gateway/worker、写 production cache 或改变 provider 路由。

Day 0 preflight 与聚合消费的工程路径已补：observer 使用 monotonic fetch/normalization/总处理 duration 和最终 `ready_at`，并在 report fsync 后生成不可变 publication receipt 记录 `published_at`；完整 run envelope、manifest、report、capture log、raw 与 receipt 均不可改写并由 hash 绑定；source adapter 必须对落盘 raw 执行确定性 replay normalization；敏感字段在持久化前 fail-closed 脱敏。`OfficialExchangeDay0Source` 只请求 SSE/SZSE 官方股票清单、风险警示/退市、停牌和除权除息报告，连同已验证年度日历保存每个原始 body，绑定固定请求范围和同日 HTTP `Date` 后从同一 bundle 重放；非 200、分页不完整、错证券、越日或缺响应均保持 typed `UNKNOWN`。固定 XDG 的 `prepare_a_share_day0.py` 默认 dry-run，仅在运行日 `[09:15,09:30)`、本机 clock 门和完整 20 样本 MAD p95 均通过后才进入该 source；正式 manifest v2 绑定 clock-history hash/sample count。campaign 先验证全部 artifact 完整性，再按绑定实际 `source_id`、observer/threshold version、plan-hash run generation 的独立 primary/reference、时窗和共同有效交易日交集计数；不可变 tactical market context 绑定稳定 `source_generation_hash`、Day 0 typed facts、双源在 cutoff 时的 source/receive age、publication receipt、FIN-owned policy version 与内容哈希，再由注入的 FIN authority 签发 schema v6 HMAC seal。`FrozenPaperQualificationReader` 只按 plan-derived run ID 把 Day 0/双源文件恢复为 typed evidence；formal PAPER 的 morning/closing application 会在每次日状态推进前重新执行 campaign assessment 与 context build/seal，并丢弃 closing caller 自带的 context。消费 gate 同时校验内容完整性与 authority seal，调用方不能靠重算公开 hash 自证来源。market context 只表达数据完整性和 data-use scope，不输出任何交易许可字段；已知停牌、风险警示或公司行动是完整的受限事实，动作政策留给后续 `CapitalPermissionGate`。

正式 PAPER 双策略同时冻结 `data-qualification.v3 + a-share-data-thresholds.v2 + 2000/3000/2000ms`，完整 observation run ID 矩阵必须由 plan generation 派生；通用、未注册或未绑定 generation 的 campaign 最多保持 `COLLECTING`。schema v3 campaign report 把 `source_generation_hash` 纳入 assessment hash，context 必须使用同一 generation。腾讯 reference 对交易状态/涨跌停的角色外 `UNKNOWN` 可以在固定 policy 下计入数据可靠性，但不因此获得资本权限；Eastmoney primary 与 LIVE Day 0 仍负责资本关键事实，任何已知冲突、价格/时效/provenance/replay/clock 问题继续 fail closed。

当前已有 fake/test Day 0、正式官方 Day 0 source/固定 XDG 手工入口、腾讯/东方财富两个 raw source adapter 候选，以及只能手工显式触发的冻结双源采集入口；脱敏冻结 raw fixture 只证明 raw 保存、hash、重放和 fail-closed 工程合同，`TEST_ONLY` publication receipt 明确不能计入真实资格。尚未执行任何外部 live capture，也没有受控 scheduler、已取得 live 资格的 primary/reference 组合或连续五日证据；任何真实响应中无法明确证明的交易状态仍为 `UNKNOWN` 且不能计数，因此不能宣称候选已经取得资本安全资格或真实 `PAPER_DATA_READY`，若未来由用户明确重启本研究，原问题的关闭条件是：

1. 券商候选行情能力进入对照表；
2. 连续五个有效交易日完成冻结版本观察；
3. 产出逐日 JSONL/raw reference、每日报告和聚合 qualification verdict；
4. 若存在缺口，提交具体采购选项而不是笼统地说“换付费数据”。

M4 已完成并通过独立只读复核；observer V1 仍保持独立手工入口和独立输出根，不读取券商凭证。以后接入受控 scheduler 时，沿用相同 schema、adapter version 和 hash 语义，并作为单独切片验收，不能因 M4 完成而自动写入生产行情链。

若未来经用户明确重启，研究顺序是在独立授权下先冻结 `QualificationCampaignPlan`，再用手工 collector 对腾讯、东方财富既定 primary/reference 角色执行受控 live 观察，同时补 calendar/公司行动与 Day 0 真实证据；随后才考虑受控 scheduler、公告、财务、两融和海外 enrichment。后续子切片缺失时继续输出 `UNKNOWN` 或锁住对应 live capability，不阻塞模拟盘或 broker-neutral 战术链。
