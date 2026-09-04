# decision-journal-v1 · 决策日志 v1 短设计

> 依据：owner 2026-09-04 会话提出（「每次问询与决策是宝贵经验，应结构化存储供复盘」）。
> 定位：规则 5 短设计——新增 durable state + 公共工具面，核心处理，动代码前过设计门。
> 代码事实均带 file:line，施工以本文为契约。

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
| `decision_id` | TEXT PK | `DJ-<recorded_date>-<4hex>`，append 时生成，冲突重试 |
| `schema_version` | TEXT | `decision-journal.v1` |
| `action` | TEXT CHECK | 闭集 `buy / sell / plan / revert`；闭集外 preview 即拒，agent 改写或 owner 澄清（质量门，不加 other 兜底） |
| `symbol` | TEXT NULL | 可空=组合级决策（如「总仓位降到五成」），格式校验沿用 FIN identity 规则（有 symbol 时） |
| `decision_date` | TEXT | YYYY-MM-DD 必填，owner 口述的决策时点（默认记录日）；复盘粒度到日，行情/G 均可按日对齐 |
| `rationale` | TEXT NOT NULL | 为什么——本功能的价值主体，非空校验 |
| `note` | TEXT NULL | 幅度/仓位/价格等补充，自由文本，不结构化（不为想象中的分析预建字段） |
| `source` | TEXT | 常量 `owner_stated`，服务端强制（学 watchlist assistant 来源强制，`user_watchlist.py` provenance 同法） |
| `revert_of` | TEXT NULL | 引用被更正的 decision_id；错录不改不删，追加 revert 记录，两笔都保留（审计完整） |
| `recorded_at` | TEXT NOT NULL | 落库时点 UTC |

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

- `preview`：agent 把口述决策结构化为记录草稿 → 服务端校验（action 闭集 / rationale
  非空 / symbol 格式 / revert_of 存在性）→ 零写，返回 `confirmation_phrase` +
  `candidate_token`（进程内单次消费 token，TTL 15 分钟，重启失效 fail-closed——
  照抄 `LocalWatchlistPreviewTokenManager`）。
- `apply(token)`：单次消费 token → append → 返回 `decision_id`；重复/过期/未知
  token 一律 REJECTED。无显式确认不落库；**headless/one-shot 会话只 preview 不
  apply**（人格纪律同款，server.py:267-268 同文）。
- `list`：零写镜像（最近 N 条 + 过滤），供 agent 会话内对账。

**`read_decision_journal`（只读）**：过滤参数 `symbol? / date_from? / date_to? /
action?`，返回结构化记录 + typed gaps（`decision_journal_unavailable` /
空表诚实答空，沿 read_user_watchlist 措辞先例 server.py:245-254）。被更正的记录
原样返回并带 `reverted_by` 指针，不隐藏。

## 人格接线（`~/fin-data/consult-agent/CLAUDE.md`，git 外数据根，改前备份）

- 工具规则增补：owner 口述买卖决策时 → 提取结构化 preview，确认后 apply；
  复盘/回顾类问题（「当初为什么买 X」「复盘一下」）→ `read_decision_journal`
  先行，答案引用记录原文而非转述。
- 不催条款入人格：不主动提醒记录；owner 没说就不记。
- 工具计数行 12+1 → 13+2（该行带「冻结契约」标注，与 NOW.md 旁路 15 一并
  owner 会签）。

## 契约影响（设计门 Q1 预答）

- 薄 server 工具面 +2：新工具注册于 `wiring.py`（READ/WRITE_TOOL_NAMES）+
  `server.py`（描述/timeout/handler/工具清单）；无既有工具签名或语义变化。
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
   read_capabilities 注册 + tests，均入 keep-set 四入口。
4. **相对直接 Agent 退化？** 直接 Agent 无持久决策日志；本功能为净增，不改既有
   问询路径，单缝失败隔离降级照旧，不退化不变量不受影响。

## 施工清单

- `fin_analyse/portfolio/decision_journal.py`（store，镜像 user_watchlist.py 模式）
- `fin_analyse/portfolio/decision_journal_write_service.py`（preview/apply/list + token）
- `read_capabilities/wiring.py`、`read_capabilities/server.py`（注册 + handler + 描述）
- `tests/portfolio/`（token 生命周期/闭集校验/revert/权限/空表）
- `~/fin-data/consult-agent/CLAUDE.md`（人格增补，备份后改）
- README 工具计数行（owner 会签，与 NOW.md 旁路 15 合并处理）

## 验证方式

- 单测：token 单次消费/过期/重启失效、action 闭集拒绝、revert 引用存在性、
  owner-only 权限（0700/0600）、空表诚实答空。
- 实弹探针：CLI `preview→apply` 两段式落库 → `read_decision_journal` gaps 空 →
  复盘问询「我为什么买 X」验工具被调 + 记录命中；finq 记账照常。
