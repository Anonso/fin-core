# read_market_snapshot 板块指数支持（BUG-024 材料层·细分板块日线 lane）

> 状态：设计稿 v2（外审后修正）。合入后本文件删除，Git 即归档。
> 规则 5 判据：数据管线取材 + 公共入口语义扩展 + 人格契约同步 → 按核心处理。
> 上游先例：snapshot-index-support（96f8fcd，主指数 lane，设计门 1P1/4P2/4P3
> 全采纳）——本稿复用其 lane 隔离与「绕过即绕过」判据，推广到细分板块。
> NOW #24（主线·准备，owner 09-05 认可）；persona-governance-v1 §5 流程
> （设计门→施工→persona_regression 回归）。
> 设计门：cmd·deepseek-v4-pro·max（无 fallback），elapsed ≈1250s，
> P1×0/P2×6/P3×7 → 采纳 12、部分采纳 1、驳回 0；裁决记录见 §7；台账
> `~/.local/state/fin-analyse/design-gate/board-index-support-20260905/`。

## 1. 问题与立项证据

BUG-024 样本#1（09-04 盘前读法）：大盘/科技主线类问题的「线」需要细分板块
日线序列（owner 词汇：电子/半导体/通信设备/液冷等），主指数 lane 只覆盖五个
宽基/科技宽基，细分板块仍无工具可拉——人格取材下限条款（BUG-024 v3 增补，
备份 r9；〔外审修正 Q1-P3〕条款编号跨文档以人格文件现行修订史编号为准）
被迫把「板块指数日线」列为断供例子诚实降级。owner 09-05 认可立项（NOW #24）。

### 1.1 数据源实弹核验（2026-09-05，本会话探测；全部命令与响应形状见本节）

- **东财 BK 板块体系在本网络不可用**：`push2his.eastmoney.com`（日线端点）
  curl 复现 TLS 中断（`unexpected eof while reading`）；与 08-02 在案注释一致
  （eastmoney_request_contract.py：push2/push2his TLS 被干扰，quote 已切
  push2delay，日线无等价替代）。东财 BK lane 无日用可行路径，排除。
- **腾讯板块体系三腿全通**（`pt` + 8 位码，含字母如 `02GN2376`）：
  1. 板块榜（码表发现）：`proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?
     board_type=hy2|gn` → 二级行业 124 块（申万式二级命名，如「半导体」
     `pt01801081`）+ 概念 802 块（如「液冷概念」`pt02GN2224`）。
  2. 行情 quote：`qt.gtimg.cn/q=pt01801081` → `v_pt…="…"` 88 字段行，与
     个股/指数行同构：f30 时间戳（`20260904150006`）、f47/f48 涨跌停 = `-1`
     哨兵（同主指数）逐位对齐。
  3. 日线：**`fqkline/get` 对板块码只吐最新 1 根（实证，四种参数变体皆然）**；
     `newfqkline/get?param=pt…,day,START,END,120,qfq` 有全量历史——板块基期
     2025-05-19 起 320 根；最年轻探测样本（稳定币概念，2025-07-03 基期）288 根。
     行形状 11 字段，idx 0–5 = `[date, open, close, high, low, volume]` 与
     个股 qfqday 行同构（idx 6 起为附加信息对象，现有 parser 只读 0–5）。
     〔外审修正 Q2-P2〕**收盘后当日 bar 的即时可得性未验证**（探测时点为
     周六休市）——列入 §5 验收：09-07（周一）收盘后核当日 bar；若腾讯延迟
     吐当日 bar，READY 判据不受影响（§2.5 同判语义，缺当日只加 limitation）。

### 1.2 板块指数的语义属性（诚实分级落点）

腾讯板块指数是**腾讯自编的成分聚合统计**，不是申万/中证官方行业指数；且
不存在跨源同物（东财 BK 与腾讯 pt 是不同指数，非双源关系）→ 板块 lane
**单源是仪器属性不是数据缺陷**：不造假双源、不报永久 gap，单源属性以
`context_limitations` 常驻标注（`BOARD_INDEX_TENCENT_SINGLE_SOURCE`），
答案引用时口径为「腾讯板块指数（单源参考）」。

## 2. 方案（lane 隔离，共享个股/主指数路径零改动）

### 2.1 规范符号与板块表（config，家规 6）

- 规范符号：`{code}.PT`，code = 腾讯 8 位板块码（`[0-9A-Z]{8}`，如
  `01801081.PT`、`02GN2224.PT`）。`.PT` 后缀与个股 venue 正则天然无碰撞。
- 板块表落 `config/market/board_symbols.json`：`{"boards": {规范符号: {"name":
  显示名, "aliases": [中文名…]}}}`。**与主指数 lane 的代码内表不同**：指数集
  是封闭市场结构（96f8fcd 判例），板块清单随 owner 使用面持续扩展（「等」），
  是会变的清单——改配置即扩面，不动代码；加载时 fail-closed 校验形状。
  〔外审修正 Q4-P3〕文件头注警示：扩面避开主指数别名与常见个股全名
  （板块别名优先级语义届时复核）。
- seed 13 块（owner 词汇 + G 高频，全部实弹核验 ≥160 bars）：
  半导体 01801081 / 通信设备 01801102 / 消费电子 01801085 / 元件 01801083 /
  光学光电子 01801084 / 电子化学品 01801086 / 军工电子 01801745（hy2）；
  液冷 02GN2224（别名 液冷概念）/ 算力 02GN2006（别名 东数西算）/
  存储 02GN2124（别名 存储器）/ CPO 02GN2211（别名 共封装光模块）/
  光通信 02GN2190 / PCB 02101284（别名 PCB概念）。
  「光模块」无同名单块（腾讯无该名概念），不硬造别名——未收录名走诚实失败。
- 新模块 `fin_analyse/market/board_symbols.py`：加载 config，暴露
  `BOARD_ALIASES` / `BOARD_NAMES` / `BOARD_SYMBOLS`（frozenset）/
  `split_board_aliases()`（strip 后精确匹配，命中项从 equity 列表剔除，
  与 index lane 同款三件套）。〔外审修正 Q1-P2〕`BOARD_ALIASES` 键集
  **含规范符号自身**（`02GN2224.PT` 直传直接命中），与 index lane
  「qualified symbol 直传」能力对称，不落 equity 解析器误报 UNRESOLVED。

### 2.2 provider 层：第三条拆分（挂在 read_market_snapshot 入口）

`read_market_snapshot` 内拆分顺序：**板块别名 → 主指数别名 → equity 解析器**
（共享 `_resolve_on_demand_instruments` 保持纯个股，margin/external 零外溢，
96f8fcd P1 判例延续）。命中板块的输入直接产出 `{code}.PT` 并从后续列表剔除；
同调用混多类别（「液冷」+「601899」+「科创50」）按符号去重，总数仍受
`_MAX_SYMBOLS=5` 约束。板块名与个股名精确撞名时板块别名优先（确定性，
设计内取舍）。〔外审修正 Q1-P2〕板块命中项产出 `board_names`（规范符号→
显示名）与 `index_names` 同路进投影（`_on_demand_market_snapshot_value`），
板块 `instrument.name` 不得为 None——agent 面见「液冷」不见裸 `02GN2224.PT`。

### 2.3 腾讯日线 reader 收板块（endpoint 分支，一处）

`TencentDailyBarReader._capture`：venue ∈ {sh,sz} 走既有 `fqkline/get`；
venue == `pt` 走 `newfqkline/get`（fqkline 对板块只吐 1 根，§1.1 实证）。
param/数据键拼接 `f"{venue}{symbol}"` = `pt01801081` 对两端点同构，行解析
`qfqday or day`、idx 0–5 读取零改动。`_parse_symbol` 放行 `.PT`（venue 不做
sh/sz 推演）。adjustment 语义：板块指数无复权概念，沿用主指数先例（day 键 +
系列 adjustment 标签不变，vacuous-true，本节即诚实注记）。

### 2.4 腾讯 quote 采集收板块（专用板类源，不放宽共享源）

`TencentRawQualificationSource` 的 sample 域（sh/sz + 六位码）**不动**——
它是周期资格管线的共享源。新增轻量 `TencentBoardQuoteSource`（同文件或
邻位）：仅复用其解析基元（GB18030 解码、f30 时间戳、f47/f48 `-1` 哨兵→None、
正价校验），envelope 正则收 `v_pt(?P<code>[0-9A-Z]{8})=`，产
`QualificationSourceCapture`（venue=`pt`，raw bytes 留证纪律不变）。

### 2.5 on_demand 收集：板类专用收集路径（不复用双源资格）

`OnDemandTacticalContextService.read()` 的 per-symbol 提交按
`symbol ∈ BOARD_SYMBOLS` 分流到 `_collect_board_symbol`：

- **单源 quote**：只采 tencent_board_raw 一腿，复用 `_quote_fact` 的时间窗
  判据（连续时段 15s / 非连续 4 日）。〔外审修正 Q2-P2〕**不进
  `_qualify_quotes`**（该函数对 `len(facts)==1` 硬编码
  `PARTIAL + DUAL_SOURCE_QUOTE_INCOMPLETE`，与单源语义直接冲突）——专用
  单 fact 装配：fact 在且日线合格 → READY 判定输入；fact 在日线缺 →
  PARTIAL；fact 无 → UNKNOWN（既有 gap 语义）。无 disagreement 语义、
  无 `DUAL_SOURCE_*` gap。
- **日线**：既有 `_collect_bars`（reader 已收板块，§2.3），120 根下限不变。
- **无 30m/60m**：板块路径不提交分钟线任务，timeframes 不含 30m/60m 键
  （诚实缺席，非 UNKNOWN 占位）；daily/weekly/monthly/annual 聚合照常。
- **状态语义**：〔外审修正 Q2-P2/Q2-P3〕**完全同判 close_qualified 判例，
  不加严**——收盘后 quote 事件日 == 期望收盘交易日 且 quote/bars 合格 →
  `READY` + `CLOSE_REFERENCE`；日线最新 bar 早于期望收盘日**只加
  `CURRENT_TRADING_DAY_BAR_NOT_INCLUDED` limitation 不降 READY**（与主
  指数/个股判例同语义；若 §5 的 09-07 即时性核验通过，加严与否留后续
  真实使用证据决定，本设计不预置）。盘中 quote 新鲜 → `REFERENCE_ONLY`；
  采集失败 → UNKNOWN。`manual_review_eligible` 恒 False（单源聚合统计
  不进动作资格）。
- **常驻 limitation**：`BOARD_INDEX_TENCENT_SINGLE_SOURCE` 入
  `context_limitations`（与 data_gaps 分离；盘后 READY 探针验收「零 gap」
  不被单源属性污染）。
- `refresh_quotes` 不收板块（现无板块用例）：`.PT` 符号走既有
  `_parse_symbol` 失败 → `ON_DEMAND_MARKET_SYMBOL_UNSUPPORTED` typed 失败。
- 数据资格周期管线（SampleManifest/qualification）不接板块——lane 只服务
  问询快照，资格管线域零变化。

## 3. 契约变更（对外可见面）

- `server.py` 工具描述：板块句追加。〔外审修正 Q1-P3〕措辞与别名键集一致
  （中文块名 + CPO/PCB 字母键 + 规范符号直传），并补 "no intraday bars"
  半句防 agent 追问分时。
- consult-agent 人格（备份 r17，净行数账随施工记录）两处，**内容锚定位**
  （〔外审修正 Q1-P3〕不用行号锚；NOW #26 r17 瘦身与本次同文件，家规 13
  顺序 = 本次 r17 先行、瘦身顺延为 r18，落账时同步 NOW 行）：
  ①线源句段（「分析顺序（点线面）」节）补「板块指数日线用名称查（液冷/
  半导体/通信设备等，腾讯单源参考）」；②取材下限段断供例子换仍然为真的
  缺口（板块指数日线上线后原例变假——改「分时日内序列」，NOW #27 在案
  未建）。条款编号以人格文件现行修订史为准。
- 〔外审修正 Q1-P3〕输出形状措辞：同 schema，板块 `timeframes` 键集少
  30m/60m 两键（个股在分钟源不可用时同样 4 键，既有先例）。

## 4. durable state / 并发 / 幂等

无 durable 写入：quote 直连回放、日线无 artifact 缓存（板块走腾讯直连，
与主指数 lane 同性质）；`{code}.PT` 符号进既有 evidence payload hash，
同请求重放天然幂等；executor 额度不变（板块路径少一腿分钟线，嵌套需求
只会更小）。config 加载为进程内只读，无并发写。

## 5. 验收

1. 单测：board_symbols（别名拆分/规范符号直传/去重/config 校验 fail-closed）、
   tencent 日线 pt 分支（endpoint/键/行解析/cutoff）、board quote 源（envelope/
   哨兵/坏行）、on_demand 板块路径（同判 READY 语义/单 fact 不进
   `_qualify_quotes`/无 dual gap/limitation 常驻/无 30m 键/names 回填/
   refresh_quotes typed 失败/五符号混查）。
2. 端到端探针（真实链路）：「液冷」「半导体」名称查询 → READY/≥120 bars/
   MA+MACD/`data_gaps=[]` + limitation 单源标注 + name=中文显示名；回归：
   「科创50」「601899」行为零变化。
   〔外审修正 Q2-P2〕**09-07（周一）收盘后加验**：当日板块 bar 即时性
   （latest bar 是否当日）；结果入台账，延迟也不改 READY 判据（§2.5）。
3. persona_regression.sh 一轮（r17 修订义务，§1 元规则）
   〔外审修正 Q4-P2〕+ 固定 6 题之外加 1 题**板块消费端探针**（题外单发、
   落台账不入固定题集，防过拟合）：「液冷这条线现在到什么阶段」——检查
   答案引用板块日线序列、不把 timeframes 缺 30m/60m 读成 gap/弱信号、
   带腾讯单源口径；+ owner 实弹复核。
4. 全量 pytest 基线对照（cognition 域既有失败与本改动无关口径同 96f8fcd）。
5. GLOSSARY 补「板块指数」条目（〔外审修正 Q3-P3〕，一句：腾讯自编成分
   聚合统计，`.PT` 命名空间，单源参考）。

## 6. 不做

- 分钟线/分时（NOW #27 排后）；板块成分股清单；地域板块（腾讯 hy1/hy3 空）；
  自动名称发现/模糊匹配（exact-match 别名，未收录诚实失败）；东财 BK 源
  （网络不可用在案）；申万/中证官方指数源（等真实触发再立）；资格管线
  （SampleManifest）接板块。

## 7. 设计门裁决记录（2026-09-05）

- 实际服务评审者：cmd·deepseek-v4-pro·max（stderr 横幅 `headless launch:
  cmd`，无 fallback）；本次入口未落 retry.ts，elapsed ≈1250s 以台账
  dir 创建（20:34）→ review.md mtime（20:55）计。
- 发现 P1×0 / P2×6 / P3×7 → **采纳 12 · 部分采纳 1 · 驳回 0**。
- P2×6 全采纳：
  - Q1 names 回填缺失 → §2.2 board_names 同路进投影（采纳）。
  - Q1 规范符号直传路径未定死 → §2.1 别名键集含规范符号自身（采纳）。
  - Q2 板块 quote 装配绕开 `_qualify_quotes` 未明说 → §2.5 定死专用单
    fact 装配（采纳）。
  - Q2 newfqkline 收盘后当日 bar 即时性未验证 → §1.1/§5 加 09-07 收盘后
    核验项，READY 判据对延迟免疫（采纳）。
  - Q3 悬空设计稿路径注释先例 → 施工注释一律 commit 号模式（采纳）。
  - Q4 板块消费端误读探针 → §5.3 题外单发板块探针（采纳，不入固定题集
    防过拟合）。
- P3×7：采纳 6——输出形状措辞（§3）、工具描述措辞+no intraday（§3）、
  内容锚+条款编号统一+r17/r18 顺序（§3）、close_qualified 加严措辞
  （并入 Q2-P2 同判方案，§2.5）、NOW 收口动作（本节记：施工落账时更新
  NOW #24 行）、GLOSSARY 术语（§5.5）。部分采纳 1——撞名扩面提示
  （config 头注警示，不加机制，§2.1）。
