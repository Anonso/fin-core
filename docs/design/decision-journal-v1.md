# decision-journal-v1 · 决策日志 v1 短设计

> 依据：owner 2026-09-04 会话提出（「每次问询与决策是宝贵经验，应结构化存储供复盘」）。
> 定位：规则 5 短设计——新增 durable state + 公共工具面，核心处理，动代码前过设计门。
> 代码事实均带 file:line，施工以本文为契约。
> **v2**（2026-09-04 设计门外审 345s/10 发现〔3P1+7P2〕/10 采纳后修订；修订处以
> 〔外审修正 Q?-P?〕标注，台账 `~/.local/state/fin-analyse/design-gate/decision-journal-v1-20260904/`）。

## 背景与相邻判断（不施工项钉死在这里）

2026-09-04 会话盘点结论，三条只有一条施工：

1. **标的/指数完成日线 artifact**：已在自动积累（`$XDG_STATE_HOME/fin-analyse/
   on-demand-tactical-context-v1/daily-bars/`，immutable + provenance 齐全）。
   消费现状 = 同 scope 重放（`qualification_sources/eastmoney_daily_bars.py:187-194`，
   本职）；历史轴零读方（`EastmoneyDailyBarReplayReader` 全仓零引用，生产 as_of 恒
   null）。**历史读路径不施工**，挂 NOW.md 待办「断供 fallback 画像」同一使用触发。
2. **大盘概览留痕**：`read_market_overview` 无持久化是系统自知 gap
   （`MARKET_OVERVIEW_PERSISTENCE_NOT_EVALUATED`）。**挂随 D-031**（Daily 恢复时
   生成侧顺手落 immutable artifact），不独立开工。
3. **决策日志**：真空白，本文施工项。

## 目标

- owner 口述投资决策 → 结构化留痕（动作/标的/决策日/理由），append-only 可审计。
- 复盘类问询可检索：按标的/决策日/动作过滤装配，答案回指记录原文。
- 不改问询面哲学：agent loop 归客户端，本功能只是薄 server 上两条窄缝（一读一写）。

## 非目标（每条一句 why）

- **不做后台自动抽取问询/决策**：重建编排层（问询面哲学禁止）+ 幻觉入库风险；
  owner 显式声明才是真实意图。
- **不做复盘分析面/报表**：家规 10 自指——等真实记录攒出使用证据再议。
- **不动 G 域**：owner 决策非 G 来源；Z 不验证 G；独立 source 性质，不入 G 认知库。
- **不催记录**：anti-nag 纪律（同「不催更新持仓」）——owner 口述时才提议留痕，
  一次未确认不追问，不主动提醒「要不要记」。

## 数据模型（最小闭集）

一条决策记录：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `decision_id` | TEXT PK | `DJ-<decision_date>-<4hex>`，append 时生成，冲突重试；日期段取 **Asia/Shanghai 日历日**（下同，`recorded_at` 保持 UTC offset——北京 00:00–08:00 落库不跨日错 ID）〔外审修正 Q2-P2〕 |
| `schema_version` | TEXT | `decision-journal.v1` |
| `decision_type` | TEXT CHECK | 闭集 `buy / sell / plan / revert`；闭集外 preview 即拒，agent 改写或 owner 澄清（质量门，不加 other 兜底）。**字段名刻意不叫 `action`**：外层工具动词已占用 `action`（与 update_user_watchlist 契约词一致），避免公共写接口一名两义〔外审修正 Q1-P1〕 |
| `symbol` | TEXT NULL | 可空=组合级决策（如「总仓位降到五成」）；**有值时入库前经 FIN identity resolver 归一为 canonical 形态**（如 `601899.SH`），归一失败 preview 即拒——不存名称/简码原样，防按标的检索退化为字面匹配〔外审修正 Q1-P2〕 |
| `decision_date` | TEXT | YYYY-MM-DD 必填，owner 口述的决策时点（默认=记录日的 Asia/Shanghai 日历日）；复盘粒度到日，行情/G 均可按日对齐〔外审修正 Q2-P2〕 |
| `rationale` | TEXT NOT NULL | 为什么——本功能的价值主体；非空且 ≤2000 字符〔外审修正 Q1-P2〕 |
| `note` | TEXT NULL | 幅度/仓位/价格等补充，自由文本不结构化，≤500 字符〔外审修正 Q1-P2〕 |
| `source` | TEXT | 常量 `owner_stated`，服务端强制（学 watchlist assistant 来源强制，`user_watchlist.py` provenance 同法） |
| `revert_of` | TEXT NULL | 引用被更正的 decision_id；**IFF 约束**：`decision_type='revert'` ⇔ `revert_of` 非空（双向 CHECK），且目标必须存在、**未被 revert 过**（`revert_of` 上 partial unique index → 每记录至多一次更正，`reverted_by` 语义唯一）；preview 与 DB 约束同时钉死〔外审修正 Q2-P1〕 |
| `recorded_at` | TEXT NOT NULL | 落库时点 UTC offset |

**上下文不存拷贝**：复盘装配天然可按 `decision_date` 用现有 PIT 工具重放当时材料
（日线事后可回源；G 按 as_of 检索已具备）——不设 context_refs 字段。已知边界：
当时本地行情 artifact 未必存在，日线回源兜底覆盖，read 端如实报 gaps。

## Durable store

- 位置：`$XDG_STATE_HOME/fin-analyse/decision-journal-v1/`，与 `user-watchlist-v1`
  同根同权限纪律：目录 0700 / 文件 0600 owner-only，不入 git、不出本机（硬边界 3）。
- 形态：SQLite 单表 + audit_events，镜像 `UserWatchlistStore` 的既有模式
  （`portfolio/user_watchlist.py`：owner dir/file 校验 `_require_owner_dir/_file`、
  revision + CAS、审计事件、首写并发复验）。选 SQLite 不选 JSONL：读工具的常态是
  过滤检索（标的/时间窗/动作），与 usage.jsonl（人读台账）定位不同。
- 时序/幂等：**append-only，无 update/delete 路径**（家规 4 备份条款不触发）；
  写入只经 preview→apply 单飞通道，store 层 CAS 沿用。

## 工具面（薄 server 12 读 + 1 写 → 13 读 + 2 写）

**`record_decision`（受限写，完全复刻 update_user_watchlist 三动词形态**，
`server.py:601-697` / `watchlist_write_service.py`）：

- `preview`：agent 把口述决策结构化为记录草稿 → 服务端校验（decision_type 闭集 /
  rationale 非空≤2000 / note ≤500 / symbol 归一 / revert IFF：revert 须带已存在且
  未被更正的 revert_of）→ 零写，返回 `confirmation_phrase` + `candidate_token`
  （进程内单次消费 token，TTL 15 分钟，重启失效 fail-closed——照抄
  `LocalWatchlistPreviewTokenManager`）。**confirmation_phrase 逐字含全部实质字段**：
  decision_type、symbol（或「组合级」）、decision_date、rationale 全文、note（如有）、
  revert 目标 id（如有）——owner 确认的是确切内容，不是摘要〔外审修正 Q1-P2〕。
- `apply(token)`：单次消费 token → append → 返回 `decision_id`；重复/过期/未知
  token 一律 REJECTED。**失败语义钉死**：先 consume 后 commit，commit 失败 token
  不复活、零行落库，只能重新 preview（与 watchlist 同款 fail-closed，防 token
  消费与 SQLite 提交拆段导致的重复落库）〔外审修正 Q2-P2〕。无显式确认不落库；
  **headless/one-shot 会话只 preview 不 apply**（人格纪律同款，server.py:267-268 同文）。
- `list`：零写镜像（最近 N 条 + 过滤），供 agent 会话内对账。

**`read_decision_journal`（只读）**：过滤参数 `symbol? / date_from? / date_to? /
decision_type? / limit?`（默认 50，上限 200）；排序 `decision_date DESC,
recorded_at DESC`；返回结构化记录 + typed gaps（`decision_journal_unavailable` /
空表诚实答空，沿 read_user_watchlist 措辞先例 server.py:245-254）。被更正的记录
原样返回并带 `reverted_by` 指针，不隐藏。〔外审修正 Q1-P2：排序/limit/参数闭集冻结〕

## 人格接线（`~/fin-data/consult-agent/CLAUDE.md`，git 外数据根，改前备份）

- 工具规则增补：owner 口述买卖决策时 → 提取结构化 preview，确认后 apply；
  复盘/回顾类问题（「当初为什么买 X」「复盘一下」）→ **决策动机/历史事实查
  `read_decision_journal`，分析判断仍按现人格 G-first 硬规则（CLAUDE.md:292），
  两者可双查（日志供事实、G 供框架）**——不得让日志查询取代 G-first 反证链，
  防同题弱于直接 Agent〔外审修正 Q4-P2〕；答案引用记录原文而非转述。
- 不催条款入人格：不主动提醒记录；owner 没说就不记。
- 工具计数行 12+1 → 13+2（该行带「冻结契约」标注，与 NOW.md 旁路 15 一并
  owner 会签）。

## 契约影响（设计门 Q1 预答）

- 薄 server 工具面 +2：新工具注册于 `wiring.py`（READ/WRITE_TOOL_NAMES）+
  `server.py`（描述/timeout/handler/工具清单）+ **`guo_teacher_research/
  production_capability_provider.py`（`read_decision_journal` 同名方法）**——
  `wiring.py:203` 把 READ_TOOL_NAMES 逐个 `getattr(provider, tool)`，漏改
  provider 即 server 装配崩溃〔外审修正 Q3-P1，已实证〕；无既有工具签名或语义变化。
- gaps 词汇新增 `decision_journal_unavailable` 等，仅新工具产出。
- durable state 新目录，无既有数据迁移。
- 生产人格文件在 `~/fin-data/consult-agent/`（数据根），repo 内 `consult-agent/`
  不存在——NOW.md 指针「consult-agent/CLAUDE.md」即指该文件；施工时核实 .mcp.json
  挂载路径后一并核对。

## 设计门固定四问预答

1. **契约破坏？** 无既有工具/接口变更；唯一对外新面 = 两个新工具 + 新 gap 词汇；
   README 冻结契约行工具计数随施工会签更新。
2. **durable state 时序/幂等？** append-only；写仅经单次消费 token 的 preview→apply
   （TTL/重启 fail-closed）；无 update/delete；更正走 revert 追加。
3. **引用闭包漏删？** 纯新增，无删除面；新增文件闭包 = portfolio 新模块 +
   read_capabilities 注册 + provider 同名方法（wiring getattr 映射）+ tests
   （portfolio 与 read_capabilities 两处），均入 keep-set 四入口。
4. **相对直接 Agent 退化？** 直接 Agent 无持久决策日志；本功能为净增，不改既有
   问询路径，单缝失败隔离降级照旧，不退化不变量不受影响。

## 施工清单

- `fin_analyse/portfolio/decision_journal.py`（store，镜像 user_watchlist.py 模式）
- `fin_analyse/portfolio/decision_journal_write_service.py`（preview/apply/list + token）
- `fin_analyse/guo_teacher_research/production_capability_provider.py`
  （read_decision_journal 同名方法）〔外审修正 Q3-P1〕
- `read_capabilities/wiring.py`、`read_capabilities/server.py`（注册 + handler + 描述）
- `tests/portfolio/`（token 生命周期/闭集校验/revert IFF/权限/空表）+
  **`tests/read_capabilities/`（tool descriptions 精确集、wiring 精确集、stdio
  tools/list 精确清单三处既有钉死测试同步）**〔外审修正 Q3-P2〕
- `~/fin-data/consult-agent/CLAUDE.md`（人格增补，备份后改）
- README 冻结契约行**整行重写**（该行除计数外还枚举旧 7 工具并整段声明「7 只读」，
  只改数字会保留错误清单；与 NOW.md 旁路 15 一并 owner 会签）〔外审修正 Q3-P2〕

## 验证方式

- 单测：token 单次消费/过期/重启失效、decision_type 闭集拒绝、revert IFF 双向与
  单更正唯一性、apply commit 失败零行落库、owner-only 权限（0700/0600）、
  空表诚实答空。
- 实弹探针：CLI `preview→apply` 两段式落库 → `read_decision_journal` gaps 空 →
  复盘问询「我为什么买 X」验工具被调 + 记录命中 + G-first 未被取代；finq 记账照常。

## 设计门裁决记录（2026-09-04）

- 评审者：codex-glm · glm-5.3 · max（read-only）；packet 冻结于台账
  `~/.local/state/fin-analyse/design-gate/decision-journal-v1-20260904/`。
- elapsed_seconds = **345**；发现 **10**（3 P1 + 7 P2）；采纳 **10**，不采纳 0。
- P1 三条（外层 action 一名两义 → 记录字段改 decision_type；revert 无 IFF 约束 →
  双向 CHECK + partial unique index；READ_TOOL_NAMES→provider 同名方法映射漏列 →
  施工清单补 production_capability_provider.py）均经代码实证后采纳。
- 已知状态：评审者与主会话同源（glm），独立性打折，恢复异构评审者后自消。
