# read_market_snapshot 指数支持（BUG-024 材料层修复）

> 状态：设计稿（待外审）。合入后本文件删除，Git 即归档。
> 规则 5 判据：改数据管线取材 + 跨层契约（工具描述/符号语义）→ 按核心处理。

## 1. 问题与立项证据

BUG-024 样本#1（09-04 盘前读法）：大盘/科技主线类问题的「线」（指数近 N 日
走势、回撤、支撑距离）全链无工具可拉，agent 只能拿 G 旧叙事或外搜拼。
探针实证（09-04，CC）：

- 服务层喂 `000688.SH`：quote 出价（科创50 1604.47 / 上证 3963.6，实时合理），
  但 daily bars 双源皆挂；个股对照 `601899.SH` READY/120 bars——**指数特异**。
- 根因四点，逐个钉死：
  1. **身份解析无指数 lane**（`guo_teacher_research/production_capability_provider.py`
     `_resolve_on_demand_instruments` → `consultation/instrument_identity.py`）：
     名称「科创50」UNRESOLVED；带限定符 `000688.SH` 被
     `verified_a_share_equity_venue`（个股 venue 校验）MISMATCH 拒掉；
     **裸 `000688` 静默错解成 `000688.SZ` 国城矿业**（现存陷阱，非本设计引入）。
  2. **腾讯日线 parser 只读 `qfqday` 键**（`qualification_sources/tencent_daily_bars.py:263`）；
     指数报文（实测 `sh000688,day,…,qfq` → HTTP 200）行在 `day` 键下，形状与个股
     完全一致 `[date, open, close, high, low, volume]`。
  3. **腾讯行情 parser 对 `-1` 哨兵炸解析**（`qualification_sources/tencent_raw.py:281`）：
     实测指数行 88 字段与个股同形，但 f47/f48（涨跌停）= `"-1"`（指数无涨跌停），
     `_optional_positive_decimal` 正则不收负号 → ValueError →
     `TENCENT_RAW_SOURCE_PAYLOAD_PARSE_FAILED`（今晨 PARTIAL gap 的直接来源）。
  4. **东财日线 lane 对指数不可用且烧预算**：push2his 直连指数 secid（1.000688）
     返回空 data / 代理断连；opencli 兜底分钟级（本次探针两指数耗时 2–3 分钟，
     tab 泄漏报错见 BUG-026 域），远超 snapshot 32s 预算——若指数走
     东财优先→腾讯兜底链，兜底必被 deadline 杀。**指数日线必须直发腾讯。**

## 2. 方案（四点对四根因，全部小改）

### 2.1 provider 层：固定指数别名表（改 `_resolve_on_demand_instruments`）

- 别名表（代码内常量，不进 config——会变的选择才进配置，指数集是市场结构）：

  | 别名（精确匹配，strip 后） | 规范符号 |
  | --- | --- |
  | 上证指数 / 上证 / 沪指 | 000001.SH |
  | 深证成指 / 深成指 | 399001.SZ |
  | 创业板指 | 399006.SZ |
  | 科创50 / 科学家50（G 黑话， persona 已有同义映射） | 000688.SH |
  | 深证综指 / 深证综指全称 | 399106.SZ（两市量能求和用） |

- 命中别名 → 直接产出该规范符号（带 name 标签），**不进个股身份解析器**；
  未命中 → 走既有 equity 路径，行为零变化。
- **裸六位代码保持只解个股**（`000688` 仍 = 国城矿业）：代码命名空间指数/个股
  真实相撞（000001=上证指数 vs 平安银行），唯一无歧义入口是名称。工具描述
  明示「指数用名称查」。
- 别名与个股符号不进同一去重键则无碰撞：指数规范符号 `.SH`/`.SZ` 后缀与
  个股区隔（000688.SH vs 000688.SZ 是不同串）。

### 2.2 腾讯日线 parser 收 `day` 键（一行）

`rows = stock.get("qfqday") or stock.get("day")`——指数无复权概念，腾讯把行放
`day` 键；两形状字段一致，parse 后续零改动。

### 2.3 腾讯行情 parser 收 `-1` 哨兵（一处）

`_optional_positive_decimal` 仅在涨跌停两个字段的调用点放宽：`"-1"` 与非正数
→ None（指数无涨跌停是语义事实，不是坏数据）；价格位（f3）校验保持严格。

### 2.4 指数日线直发腾讯（路由，防 deadline 必杀）

`_FallbackDailyBarReader.read` 增一条路由：请求符号 ∈ 固定指数符号集
（§2.1 表的规范符号集合）→ 腾讯优先、跳过东财尝试；其余不变。判据用符号集
全等，不做前缀推演（防 000001 裸码歧义复活）。

## 3. 契约变更（对外可见面）

- `read_market_snapshot` 工具描述（`read_capabilities/server.py`）：加
  "major indices by exact Chinese name (上证指数/深成指/创业板指/科创50/深证综指)
  return index daily bars + technicals; bare six-digit codes resolve to equities only"。
- consult-agent 人格「分析顺序（点线面）」线源句：`read_market_snapshot 行情日线`
  补「指数日线用名称查（科创50/上证指数等）」。
- 输出形状零变化（指数也走 TacticalInstrumentContext，quote+bars+technicals）。

## 4. durable state / 并发 / 幂等

- 腾讯日线 reader 复用现有 immutable artifact 机制，artifact key 按
  (symbol, venue, provider_version) 隔离——`000688.SH` 与 `000688.SZ` 不同 key，
  指数与个股互不污染；同日重放幂等（既有语义）。
- 无 schema 迁移、无生产库写入；写入面 = 既有 on-demand artifact root，不动。

## 5. 明确不做（防蔓延）

- 指数 30 分钟线：不在范围，失败走既有 named gap（THIRTY_MINUTE_BARS_*）。
- 指数成交额序列：腾讯 fqkline 指数行无金额位（实测 6 字段）；量能单点仍走
  overview/外搜。若 owner 实弹仍缺量能线，再按使用证据立项。
- 不改 `AShareConsultationInstrumentIdentityResolver`（watchlist/margin 等共用，
  指数 lane 只挂 snapshot 的 provider 入口，不外溢）。
- 不修东财指数 lane（直发腾讯已闭环；东财指数修复无增量收益）。
- 裸码解指数：不做（歧义真实存在，名称是无歧义入口）。

## 6. 验收

1. 单测：日线 parser 指数 fixture（`day` 键）；行情 parser `-1` fixture；
   别名解析（名称→符号、裸码仍个股、`000688.SH` 走指数 lane）。
2. 端到端探针（provider 真路径，名称「科创50」）：daily bars ≥120、
   MA/MACD 技术因子在、quote 双源齐、无 parse-failed gap。
3. 回归：`601899.SH` 仍 READY 零 gap；个股名/码解析行为不变。
4. owner 终验：下个交易日盘前读法实弹，线层现指数日线序列。
