# 设计稿：read-capability 薄 server（D1 工程设计 v2 · 已过双评审裁决）

> 状态：设计已按 2026-08-27 GPT+DS 并行盲评裁决修订（裁决记录见父方案 §0.5.7）；未批准动代码。
> 父方案：`rebaseline-20260827.md` §0.5 R2/R7。
> 原则约束（家规 v2）：特性内聚——本模块的中心在自己的包内；设计先行——本文即动代码前的唯一设计。

## 1. 目标 / 非目标

**目标**：给 CLI（v1 仅 CC）一个脱离 gateway 的 stdio-MCP 只读入口，暴露 FIN 的 6 个读能力，自带调用 trace。这是 rebaseline 后的产品本体入口。

**非目标**：不做写入口（持仓确认/watchlist 不暴露）；不做 envelope/多用户身份；不做最小 import 闭包的彻底手术（v1 接受实测 0.54s/121 模块的启动闭包，断根断在两条硬边上：`production_runtime` 与 `capability_broker`(moa) 不入薄 server 的 `sys.modules`）；不替代 gateway（它继续服务飞书 WS 与 Daily delivery）。

## 2. 模块结构（特性内聚）

新包 `fin_analyse/read_capabilities/`，三个文件，中心全部在包内部：

```
fin_analyse/read_capabilities/
├── types.py      # 五类型叶子化：ProductionReadRequest / ProductionReadResult /
│                 # CapabilitySource / SourceKind / SourceTrust（后三者自
│                 # capability_broker 迁入，否则 Result.sources 断不开 broker 的
│                 # 顶层 moa import）。零 fin_analyse 内部依赖（只 stdlib）。
├── wiring.py     # reader 装配：只构造 6 工具所需；G reader 由 provider __init__
│                 # 以 kb_root 默认构造（provider.py:182），不由本文件重建
└── server.py     # stdio MCP server：工具注册、请求分发、deadline、trace、
│                 # stdout guard（复制 gateway/mcp_server.py:19-58 的守卫——
│                 # 任何 stray print 都会损坏 JSON-RPC 流）
```

**改线点（三处，缺一断不了根——评审 A1/A2/P0-2）**：
1. `capability_broker.py`：五个类型定义迁出到 `read_capabilities.types`，本文件 re-export 保持既有引用方不变（broker 自身的 moa import 保留——它真用 MoAEngine）。
2. `production_capability_provider.py`：改从 `read_capabilities.types` import 五类型（**不经 broker re-export**，否则 moa 闭包回归）；`production_runtime` 同步改。
3. `ready_evidence.py`：顶层 `from production_runtime import ...` 改为从 `read_capabilities.types` import（第 6 工具住在此文件，不改线则 production_runtime 110K 闭包经第二条 import 路径原样回归）。

引用面实测 7 文件（3 源码 + 4 测试）。**断根验收**：薄 server import 完成后断言 `sys.modules` 不含 `production_runtime` 与 `capability_broker`（import-hook 式测试）。

## 3. 工具面（v1 共 6 个）

| 工具 | 来源 reader | question | instruments | as_of 语义 | 默认 deadline |
|---|---|---|---|---|---|
| `read_g_context` | provider 默认构造的 AgentRuntimeContextProvider（**G 认知主线**：pinned/fresh G、分层投影、审计时点） | 必填 | 可选 | 可选，支持 PIT（audit_by_ref） | 30s |
| `read_actual_portfolio` | ActualAdvisoryPortfolioStore 只读投影 | 必填 | — | 无（读最新确认快照） | 10s |
| `read_market_snapshot` | on-demand tactical context（无 on-demand 时退回 `_market_snapshot`） | 必填 | 可选 | 缺省 server 时钟 | **32s**（继承旧 transport 预算 `local_capability_transport.py:78`） |
| `read_market_overview` | market overview reader | 必填（`ProductionReadRequest.__post_init__` 校验非空——"无参数"不成立，server 以 question 占位串填充） | — | 缺省 server 时钟 | **22s**（=20s 内部预算+2s 余量） |
| `read_margin_evidence` | margin evidence reader | 必填 | 可选 | 缺省 server 时钟 | 30s |
| `read_ready_evidence` | RecentReferenceReadyEvidenceReader（provider 已默认装配；**ZSXQ 分级文章第一研究入口的产品路径，非可选**） | 必填 | 可选 | **reader 强制 as_of**（`ready_evidence.py:59` 缺省即 unavailable）——server 在客户端省略时补当前时刻 | 30s |

**`read_teacher_cognition` 移出 v1**（评审 P0-1）：G 主线是 `read_g_context`（分层上下文+审计），teacher_cognition 只是次级 persona/pattern 记忆；且生产中它已死——`knowledge-base/runtime/cognition` 目录模式实测 775，`open_existing_owner_only_read` 返回 `owner_only_jsonl_root_invalid`，即当前恒 `teacher_cognition_reader_unavailable`。未来修好目录权限且有真实需求再纳入。

**失败语义（逐类固定，不许"沿用 fail-soft"含糊）**：
- 入参校验失败（空 question、未知字段、超长）→ JSON-RPC error，typed code `invalid_params`；
- reader 抛异常 / 依赖缺失 → 工具级 `*_unavailable` + gap codes，server 不崩；
- deadline 到期 → `*_deadline_exceeded` typed gap（不抛 `TimeoutError` 裸异常，registry 的抛路径在 server 层接住转 gap）；
- **server 级 deadline 必设**：`deadline_at=None` 时 on-demand `wait(timeout=None)` 无限阻塞（`on_demand_tactical_context.py:371/1791`），一个挂死行情源会占死 stdio 事件循环、trace 与后续调用全部排队。每工具默认预算见上表，客户端可显式收紧不可放宽。

**副作用披露（评审 P1-5）**："只读"= 无用户数据写工具。但行情 on-demand 在 artifact 缺失时会**网络抓取并发布 owner-only artifact**（`eastmoney_daily_bars.py:179-211`，含 artifact lock 与 deadline 检查）——这是既有产品行为，不是薄 server 新增；margin source 同理。trace 追加是薄 server 唯一新写。并发说明：两个 CLI 进程同时抓同一 ticker 由既有 `_artifact_lock` 串行化；deadline 后被取消的 pending future 不强杀网络任务，接受其自然完成并落 artifact（缓存增益）。

## 4. 身份模型

本地单 principal：信任本地调用方（stdio 由用户自己的 CLI 拉起），无 machine envelope、无鉴权。风险面 = 本机用户自己 = 所有者。P5 家人面引入多身份时再重新设计；**家人隔离的免费性来自每个 consult-agent 目录的 `.mcp.json` 可携带独立 env（数据 root 按目录绑定）**，不来自目录本身。

## 5. 可观测（自带仪表）

每次调用追加一行 JSON 到 **`~/fin-data/trace/read-capability/calls.jsonl`**（0600，目录 0700——与 §0.5.6 容器判决"数据与运行时唯一家"对齐，不用 XDG_STATE_HOME）：
`{schema_version, ts, tool, question_digest, args_digest, status, data_gaps, latency_ms, session_hint, as_of}`。
**定位是调用率仪表，不是消费证明**（评审 P1-7）：trace 只证"调用过"，不证"结果进了答案"；消费证据由 P1 验收场景承担。并发追加为单进程 O_APPEND 单行写，日量 <百条，轮转 P4 后按实测再定（无故障不加）。

## 6. 启动、环境与路径契约（评审 B2/P1-8）

| root | 来源 | v1 处置 |
|---|---|---|
| knowledge base root | env（沿用 gateway 同名 env 常量 `KNOWLEDGE_BASE_ROOT_ENV` 的原值） | **fail-closed**：缺失/非法即启动失败退出非零（对齐 `mcp_server.py:340` preflight），不做"空工具启动" |
| portfolio 快照 | `XDG_CONFIG_HOME`（或 `~/.config`）固定相对路径（`actual_advisory.py:190-212`） | v1 现状不改；新家迁移（W2'）时改为显式注入 `~/fin-data/portfolio/` |
| 交易日历 | 仓内相对路径 `config/market/a_share_calendar_2026.json`（`current_overview.py:1121`，`Path(__file__).parents[2]`） | 随代码仓走；新家移植时 `config/market/` 随迁，迁移清单已含 |
| trace | 本文 §5 | 直落 fin-data |

reader 构造失败分两级：kb root 级失败 = fail-closed（如上）；单个 reader 构造抛异常 = 工具注册但恒返回 `*_unavailable`（fail-soft），构造不对称性如实保留并在启动日志（stderr）记一行。启动完成前 stdout guard 先就位（import 阶段即替换，对齐 gateway 做法）。

## 7. 测试策略

1. **断根断言**：import-hook 阻断 `production_runtime`/`capability_broker`，薄 server import 与六工具 happy 路径不触发（P0-2 反例的直接测试化）。
2. types 搬迁：7 引用文件 focused 全绿（broker re-export 保旧路径）。
3. wiring：真实 knowledge root 只读烟测（构造成功 + 每工具 happy 一条 + unavailable 一条 + deadline 到期一条）。
4. server：stdio 帧收发（mock reader）+ stdout guard（stray print 不进 stdout）+ trace 写入断言 + 入参错误分类断言。
5. 端到端：CC 以 `.mcp.json` 拉起，真实调用链见 §8。

## 8. 验收（P1 门的组成部分）

`consult-agent` 目录下 CC 打开，问"我的持仓怎么样" → trace 可证真实调用了 `read_actual_portfolio`（非猜中：断言 trace 记录 + 答案数字与快照 digest 一致）→ 答案含真实持仓数字。工具闭集核查：CC 会话可见的工具清单恰为 6 个读工具。假绿防御（评审 P2-1）：验收脚本固定快照 digest，人不得预写 trace。

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 三处改线漏一处 → 闭包回归 | §7.1 sys.modules 断言是硬门 |
| 0.54s/121 模块启动闭包偏大 | 实测可接受（评审 P1-2 数据）；v1 不做懒加载手术，>1.5s 再议 |
| 行情源挂死占住请求 | 每工具默认 deadline（§3 表）；pending future 接受自然完成 |
| 旧引用方破裂 | broker re-export 过渡；7 文件引用面已实测 |
| trace 无轮转膨胀 | 单行 <300B、日量 <百条；P4 后按实测定 |
