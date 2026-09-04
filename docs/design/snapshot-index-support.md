# read_market_snapshot 指数支持（BUG-024 材料层修复）

> 状态：设计稿 v2（外审后修正）。合入后本文件删除，Git 即归档。
> 规则 5 判据：改数据管线取材 + 跨层契约（工具描述/符号语义）→ 按核心处理。
> 外审记录：codex-open · deepseek-v4-pro · max（packet 冻结评审，09-04），
> 1P1/4P2/4P3 全采纳；裁决逐条见各节〔外审修正〕标注。
> 台账：`$STATE/fin-analyse/design-gate/snapshot-index-support-20260904/`。

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

### 2.1 provider 层：固定指数别名表（挂在 read_market_snapshot 入口，不进共享 helper）

〔外审修正 Q1-P1〕`_resolve_on_demand_instruments` 是三条公共入口的共享解析器
（read_market_snapshot 直接用；read_margin_evidence / read_external_evidence 经
`_resolve_source_native_instruments` 间接用）。**别名 lane 必须在
`read_market_snapshot` 调共享 helper 之前拆分**：命中别名的输入直接产出指数
符号、从 equity 列表剔除；共享 helper 保持纯个股，margin/external 零外溢。

- 别名表（代码内常量，不进 config——会变的选择才进配置，指数集是市场结构）：

  | 键（strip 后精确匹配） | 规范符号 |
  | --- | --- |
  | 上证指数 / 上证 / 沪指 / `000001.SH` | 000001.SH |
  | 深证成指 / 深成指 / `399001.SZ` | 399001.SZ |
  | 创业板指 / `399006.SZ` | 399006.SZ |
  | 科创50 / 科学家50（G 黑话）/ `000688.SH` | 000688.SH |
  | 深证综指 / `399106.SZ` | 399106.SZ（两市量能求和用） |

  〔外审修正 Q1-P2〕键含**规范指数符号本身**：限定符输入（`000688.SH`）显式
  声明 venue、无歧义，直接命中；裸六位码仍只解个股（`000688`=国城矿业）。
- 常量落位 `fin_analyse/market/index_symbols.py`（别名 map、规范符号集、
  规范显示名 map 三件），provider 路由、§2.4 路由判据、§2.3 gap 抑制共用，
  不复制三份。
- 〔外审修正 Q1-P3〕同调用混多别名（「上证指数」+「沪指」）→ 按符号去重；
  指数 lane 不参与共享 helper 的 `len(resolved) != len(targets)` 断言
  （该断言只对 equity 子列做），不误报 UNRESOLVED。

### 2.2 腾讯日线 parser 收 `day` 键（一行）

`rows = stock.get("qfqday") or stock.get("day")`——指数无复权概念，腾讯把行放
`day` 键；两形状字段一致，parse 后续零改动。

### 2.3 腾讯行情 parser 收 `-1` 哨兵（一处）

`_optional_positive_decimal` 仅在涨跌停两个字段的调用点放宽：`"-1"` 与非正数
→ None（指数无涨跌停是语义事实，不是坏数据）；价格位（f3）校验保持严格。
〔外审修正 Q3-P3〕既有语义「上限价缺失 → `price_limits_missing` gap」对指数
必须抑制：规范符号 ∈ 指数集时该 gap 不产生（指数无涨跌停不是坏数据），
fixture 显式断言指数样本零 `price_limits_missing`。

### 2.4 指数日线直发腾讯（路由，防 deadline 必杀）

〔外审修正 Q4-P3〕`_FallbackDailyBarReader.read` 增一条路由：请求符号 ∈
固定指数符号集（§2.1 规范符号集）→ **绕过链只调腾讯**（`self._fallback.read`
直接返回，腾讯失败原样上抛走既有 `COMPLETED_DAILY_BARS_UNAVAILABLE`），
不是交换 primary/fallback——腾讯失败不得落回东财（预算必杀会复活）。
其余符号行为零变化。判据用符号集全等，不做前缀推演（防 000001 裸码歧义复活）。

## 3. 契约变更（对外可见面）

- `read_market_snapshot` 工具描述（`read_capabilities/server.py`）：加
  "major indices by exact Chinese name (上证指数/深成指/创业板指/科创50/深证综指)
  return index daily bars + technicals; bare six-digit codes resolve to equities only"。
- consult-agent 人格（`~/fin-data/consult-agent/CLAUDE.md`，〔外审修正 Q3-P2〕
  备份 r10）两处同步：①「分析顺序」线源句 `read_market_snapshot 行情日线` 补
  「指数日线用名称查（科创50/上证指数等）」；②r9 取材下限段的断供例子
  「如指数日线断供期」改为仍然为真的缺口（板块指数日线），防与新增能力矛盾。
- 输出形状零变化（指数也走 TacticalInstrumentContext，quote+bars+technicals）。

## 4. durable state / 并发 / 幂等

〔外审修正 Q2-P2，原稿机制描述失实〕腾讯日线 reader 是**无 artifact 缓存的
直连回放**（模块 docstring 明示 no artifact cache），每次 read 直接 HTTP、
零 durable 写入；`(symbol, venue, provider_version)` artifact key 隔离只存在
于东财 reader，而指数 lane 恰好绕过东财。真实机制即最终态：

- 指数 lane 零 durable 写入，幂等 = 无状态重放（只取已完成 bar + cutoff 过滤，
  同日重放内容稳定）；
- 并发 = 各请求独立 HTTP，无锁无互踩；
- 无 schema 迁移、无生产库写入；既有个股东财 artifact 完全不受影响。

## 5. 明确不做（防蔓延）

- 指数 30 分钟线：不在范围，失败走既有 named gap（THIRTY_MINUTE_BARS_*）。
- 指数成交额序列：腾讯 fqkline 指数行无金额位（实测 6 字段）；量能单点仍走
  overview/外搜。若 owner 实弹仍缺量能线，再按使用证据立项。
- 不改 `AShareConsultationInstrumentIdentityResolver`（watchlist/margin 等共用；
  指数 lane 在 read_market_snapshot 入口拆分，共享 helper 纯个股）。
- 不修东财指数 lane（直发腾讯已闭环；东财指数修复无增量收益）。
- 裸码解指数：不做（歧义真实存在；名称与限定符符号是无歧义入口）。

## 6. 验收

1. 单测：日线 parser 指数 fixture（`day` 键）；行情 parser `-1` fixture 且
   断言零 `price_limits_missing`；别名解析（名称/限定符→符号、裸码仍个股、
   多别名去重无误报）。
2. 〔外审修正 Q3-P2/Q4-P3〕路由单测：指数符号 → **只调腾讯、东财零尝试**
   （腾讯失败不落回东财）；margin/external 不受别名外溢的回归测试
   （同输入「科创50」在两工具不产指数符号）。
3. 端到端探针（provider 真路径，名称「科创50」）：daily bars ≥120、
   MA/MACD 技术因子在、quote 双源齐、无 parse-failed gap。
4. 回归：`601899.SH` 仍 READY 零 gap；个股名/码解析行为不变。
5. owner 终验：下个交易日盘前读法实弹，线层现指数日线序列。
