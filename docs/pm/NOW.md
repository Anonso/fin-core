# FIN 唯一当前状态与执行队列

> **方向权威**：唯一方向权威是 [rebaseline-20260827.md](rebaseline-20260827.md)
> （§0.5 为当前权威版本；附录 B = 家规 v2.1 已落根 `AGENTS.md`）。决策史在
> [../DECISIONS.md](../DECISIONS.md)。本文件与方向文档冲突时，以 rebaseline §0.5 为准。
>
> **维护协议**（易维护是本文件第一要求）：
> 1. 本文件只回答两件事：现在做什么（板 A + 待办）、每个能力现在到哪（板 B）。
>    完成叙事进 Git 提交史 / 夜间报告，不留本文件；每格一行，超一行即违规。
> 2. 状态固定词六档：`待施工 / 文件层 / 运行态 / 问询验收中 / 在用 / 观察期未接入`。
>    禁止百分比、禁止自造词。
> 3. 板 B 能力行状态变更时，commit message 用 `cap:<能力名>: <旧档>→<新档>`；
>    能力演变时间线 = `git log --oneline --grep 'cap:'`，不建版本化文档。
> 4. 关系 / 数据流看 system-overview.md，设计事实看 docs/design/，bug 看 BUGS.md——
>    板 B 只引用不复制；design 页保持无状态，板 B 是能力状态的唯一投影。
> 5. 待办只放未决项（等谁 / 何时明确），完成即出队；开着的 bug 不复制进本文件，
>    由板 B 指针列指向 BUGS.md。遗留观察上限 4 条，超龄即清。
> 6. 落盘即 commit（docs-only 永远安全）。
> 7. 推进位五词：`先决 / 主线 / 旁路（时间·使用·owner·随手）/ 随部署 / 最后`；
>    主线同一时刻只推一项，完成即出队。
>
> 最后核对：2026-08-30（Asia/Shanghai）。

## 生产声明

旧飞书/Hermes 咨询入口已停用（2026-08-27 拍板，允许报错不可用）；gateway 本体
（飞书 WS，Hermes venv）继续运行未动。当前生产：Daily/ZSXQ 单元与薄 server =
`~/fin-core`（consult-agent/.mcp.json + systemd 单元，2026-08-29 步5 重指向）。
老仓 `~/fin-analyse` 已归档（2026-08-29 步7）；release 退役后保留 `current`
（→`319faf62`）+ `ff7441e2`（BUG-002 回滚候选）+ `13c791ca`（Daily 脱钩回滚候选）
至 P5，其余 10 个已删；保留三个仅为回滚资产，无运行时读方。

## 板 A · 重构阶段（对齐 rebaseline §6）

| 阶段 | 状态 | 指针 |
| --- | --- | --- |
| P0 止血文档手术 | ✅ 完成 | f0b8b6b8 |
| P1 CLI 首链（D1 薄 server + D2 顾问人格） | ✅ 完成：六题 Q1–Q6 全过，codex/CC 双客户端接通 | [read-capability-server-design](read-capability-server-design.md)、[consult-agent-workspace-design](consult-agent-workspace-design.md)、[night-shift-report-20260827](night-shift-report-20260827.md) |
| W2 原地手术（备份/部署/Daily 脱钩/归档/L1 池） | ✅ 完成：生产 release `319faf62` | — |
| 路由重排 D-018/019/021 | ✅ 完成（文件层 + 运行态） | [../DECISIONS.md](../DECISIONS.md) |
| W2' 新仓移植（`~/fin-core`） | ✅ 完成：07 七步全清（2026-08-29，cutover 见 [../migration-manifest.md](../migration-manifest.md) 步4/5/6/7 记录） | ~~new-repo-migration~~（设计稿随老仓归档入 Git 史） |
| 外部项目吸收 | ⏳ 待排期，范围开工时定 | D-020 |
| W3-4 深化调优 | 🔶 两刀完成 + B2 复盲评已跑（08-30：14 样本无上下文盲评 6.83<7；首轮两失败模式实证已修，缺口面=覆盖不足/翻译层失真/拼接标注）；**owner 已裁：BUG-012 先修（立即），01/03/05 调优 → 二轮复盲评随后；BUG-002/011 走时间窗** | 【主线·顺位后】[../design/deepen.md](../design/deepen.md)；台账 `$STATE/fin-analyse/deepen-blind-eval-20260830-b2-re/` |
| D3 三天真实使用门 | ⏳ 建设完成后一次性执行；供数 = finq usage.jsonl | D-020 |
| P4 纯使用 / P5 飞书家人 | ⏳ 之后（KB/188M 根收拢 = P5 前独立步） | rebaseline §6 |

## 板 B · 能力地图（影响问询结果的每个接线点）

状态六档：`待施工 / 文件层 / 运行态 / 问询验收中（已接入但有未闭环缺陷）/ 在用（无未闭环）/
观察期未接入（建成、产品无读方）`。

问询探针 = 一次真实提问，看 trace 三字段（`~/fin-data/trace/read-capability/calls.jsonl`：
工具被调、`data_gaps` 空、`status` 正常）判「起了作用没」；效果好坏归打分/盲评，不混判。

推进位标记（执行顺序，与状态六档无关；主线当前 = BUG-012（立即可动手）；时间窗项放旁路·时间触发到点执行、不占主线位（owner 08-30 裁定）；完整顺序看待办队列）：
`【先决】【主线】【旁路·时间/使用/owner/随手】【随部署】`。

问询面运行源：薄 server 与 Daily/ZSXQ 单元均由本仓起（单元绑 HEAD，
**本仓提交即须重渲染单元**，见 migration-manifest 运维铁律）——问询面
「运行态/在用」按 fin-core HEAD 生效计；gateway 除外。

### L1 问询大脑（决定怎么想）

| 能力 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| 顾问人格 | 全部问询的工具选择、证据纪律、输出格式 | 问询验收中 | 持仓类/老师体系类问题，验工具按规则被调 | consult-agent/CLAUDE.md；BUG-005 |
| 问询模型/路由 | 答案质量、成本、时延 | 在用 | 任意问询 | config/llm.yaml；D-018/019/021 |
| 连续性/记忆 | 续问与跨会话上下文 | 在用（codex 客户端读不到 CC 记忆 = 已知边界） | 续问（六题 Q4） | consult-agent-workspace-design.md |
| 外部检索 | 时事与星球外信息 | 在用 | 时事类问题，验引用可溯源 | consult-agent/.mcp.json |
| 识图 | 图片理解 | 在用 | 带图问询 | llm.yaml vision 链 |

### L2 七个上下文缝（决定装了什么）

| 工具 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| read_g_context | G 主线证据注入 | 在用 | 老师体系覆盖的问题，验证据链 + 三维打分 | [../design/g-cognition.md](../design/g-cognition.md) |
| read_actual_portfolio | 持仓名称/现价/变化栏 | 在用 | 「分析我的持仓」 | [../design/portfolio.md](../design/portfolio.md)；探针 08-29 ok 无 gaps（BUG-001/008 已闭） |
| read_market_snapshot | 标的行情 | 问询验收中（容量已修；EASTMONEY 源解析失败致参考价） | 最新价，验无容量耗尽 | 【旁路·时间】[../design/market-data.md](../design/market-data.md)；BUG-002/011 |
| read_market_overview | 大盘结构 | 问询验收中（结构性半边待修） | 「今天大盘怎么样」，验 gaps 空 | 【旁路·时间】[../design/market-data.md](../design/market-data.md)；BUG-002 |
| read_margin_evidence | 两融语义 | 问询验收中（描述修复已在本仓运行树，待探针复核闭环） | 两融问题 | BUG-004 |
| read_ready_evidence | 当天高相关本地参考材料注入（非 G、非公告） | 问询验收中（供料已换 canonical index.json，端到端测试过；待实弹探针闭环） | 当天老师相关提问，验工具被调 + 有料则注入 | 【主线】BUG-012 |
| read_external_evidence | 官方记录/公告证据（OfficialRecordEvidence） | 问询验收中（7 天消费 42 次；gap 率待公告探针复核） | 公告类问题，验工具被调 + gaps 空 | BUG-012 探针改指此缝 |
| read_user_watchlist | 自选股清单（user context 注意力焦点，永非投资证据） | 在用（08-29 接入；探针「看下当前自选股」ok 无 gaps，22 只如实分组） | 「看下当前自选股」，验工具被调 + 空表诚实答空 | 短设计已按规则 5 归档（git 历史：read-user-watchlist-tool）；写通道=manage_user_watchlist.py |

### L3 供给链（决定上面缝的数据质量）

| 环节 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| ZSXQ 采集 | 知识新鲜度 | 问询验收中 | 验 G 工作集 fresh pair 含新文（无直接工具，间接缝） | [../design/zsxq-capture.md](../design/zsxq-capture.md)；BUG-003/006 已闭 |
| 入库/索引 | 检索命中一致性 | 在用（BUG-007 已闭：默认路径换缝 + repo 副本绝根 08-29） | 验 G/深化命中历史文章（间接缝） | BUGS.md BUG-007 |
| 文章标签 | 星球内容检索组织（尚无产品读方） | 观察期未接入 | 「翻星球内容而不得」即接入凭证 | 【旁路·使用】D-024 |
| 深化 deep-read | 文章支撑证据 | 问询验收中（B2 复盲评 08-30：6.83<7 → 再裁；基线两失败模式已修，残余缺陷面=覆盖/翻译层/拼接标注。08-30 增：空+vision 故障产物转 retryable 随定时深化有界补做〔cd17a11〕；8-17~8-30 全窗 13 篇重生成 32 单元 verbatim 32/32、空标题清零，旧核缺陷〔截图对话当证据〕实证被拦） | 需文章支撑的问题，验引用可溯源 | 【主线】[../design/deepen.md](../design/deepen.md)；B2 台账 `$STATE/fin-analyse/deepen-blind-eval-20260830-b2-re/` |
| G 准入/工作集 | G 注入新鲜度 | 问询验收中（深化第一刀后的 manifest 契约失配已消——08-29 晚六题 g_context 零失配码；fresh pair 专项探针待跑） | 老师体系问题，验 fresh pair | [../design/g-cognition.md](../design/g-cognition.md)；CC 收口 b2da8d9c |
| 知识脑 knowledge_brain | 方法论知识卡 | 问询验收中（两卡已点亮进问询上下文；方法论探针未命中） | 方法论类问题，验命中卡（risk_check/高PE 关键词） | knowledge_brain/seed_methodology_qa.py（541368d8） |
| 薄 server 装配 | 六缝可用性（单缝失败隔离降级） | 在用 | 任一问询，验 gaps 可查 | read_capabilities/ |

### 其他产品面

| 产品面 | 状态 | 验收手段 | 指针 |
| --- | --- | --- | --- |
| Daily 简报 | 问询验收中（四班 L1 实证已通；B1 盲评 7.67<9 不闭环，同条件 9=9 打平、差距全在带伤班次；gap 记账哑已修 08-30：材料级码细分 + 双核心面断走确定性降级通知） | 四班交付记录 + B1 盲评 | 【旁路·owner】[../design/daily-delivery.md](../design/daily-delivery.md)；BUG-008 |

## 待办队列（只放未决项）

| 位置 | 序 | 事项 | 等谁 / 何时 |
| --- | --- | --- | --- |
| 主线 | 2 | BUG-012 定修：描述+供料两刀已施工（08-30，全量 2901 绿；当晚复测 2902 绿，用例数随施工漂移、非固定基线）；待实弹探针闭环（当日普通栏提问 → 工具被调 items 非空；公告类 → 走 external_evidence） | 探针随下次问询（门已开：空坍塌定修合入 `5a6f12b`、全量绿、端到端实弹过；供料看当日普通栏有无新帖） |
| 主线 | 3 | 深化收尾：01/03/05 prompt 调优（定向修覆盖不足/翻译层失真/拼接标注三实锤面）→ 二轮复盲评（同协议新 seed，均分>7 闭环）；08-30 复盲评 6.83<7 的再裁 owner 已裁走此路径 | 前序=主线2 |
| 旁路·时间触发 | 5 | BUG-002 结构性半边定修 + BUG-011 EASTMONEY 源复查（同一 repro，单会话；需实弹盘面） | 周一 08-31 09:00–09:20 盘前窗口 |
| 旁路·使用触发 | 6 | 标签检索缝开工凭证：首条真实抱怨「翻星球内容而不得」（finq 记账） | 使用触发 |
| 旁路·等owner | 7 | G 置顶错位记账毕：现置顶 3 条全为特刊（07-01/08-10/08-17，最新已 13 天），08-27 以来 9/14 次 g_context 相关性门跳过（复核同盘点 8/13 现象）——门行为正确，错位=置顶集合陈旧。两选：换置顶至当下关注（编辑 `pinned_sources.jsonl` 即换）或维持（置顶形同虚设）；要「框架特刊常驻硬注入」属行为变更，需另立案 | owner（置顶=注意力配置） |
| 旁路·等owner | 8 | 基础设施审计 A5：Windows Task PS1 的 commit 常量更新（低优先，poller 不比对不阻塞） | owner（Windows 侧手动） |
| 旁路·随手修 | 9 | BUG-004 闭环探针：两融问题验 margin 描述修复生效（修复已在本仓运行树） | 随手（下次问询顺带） |
| 随部署 | 10 | 基础设施审计 A6：Hermes cron 注册表以 apply 脚本常量为源 | 随下次部署 |
| 旁路·P5 前 | 11 | G 主线手工批注 durable 归位：从本仓布局路径（`.gitignore` 内盘上文件，fresh checkout 须自备份复原）移入 canonical KB 根 + consume 读点换 knowledge_root 缝；备份 `$STATE/fin-analyse/w2-step5-cutover-20260829/kb-repo-backup-20260829/` | P5 前 KB 收拢步首项 |

## 遗留观察（诊断/环境，上限 4 条）

1. release/gateway 运维判读：碰 release 树一律 `-B`（pyc 三来源污染）；gateway journal 近零日志是常态，判卡死先查 state.db 与官方历史。
2. codex CLI 0.149.0 静默忽略带引号的 `-c` 值 → 401；手动入口 `-c` 必须写 TOML 裸值。
3. fin-core 的 `fin_analyse` 是无 `__init__.py` 的 namespace 包：从旧仓 cwd 以 stdin 跑一次性诊断会整包 import 旧仓代码（旧逻辑+旧仓 `.env` 解键，结果看似正常实则错源）→ 诊断脚本一律文件模式跑 + 显式注入 `FIN_LLM_ENV_FILE`（指针在旧仓 `.env` 第 6 行，目标 `~/.config/fin-analyse/llm.env`）。
