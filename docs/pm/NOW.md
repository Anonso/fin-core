> **过渡快照**：复制自 fin-analyse @ c2d4dd06（2026-08-29）。cutover（迁移步 5）前，
> 状态权威在 `~/fin-analyse/docs/pm/NOW.md`；本仓板 B 待办到 cutover 后接管刷新。

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
> 最后核对：2026-08-29（Asia/Shanghai）。

## 生产冻结声明

旧飞书/Hermes 咨询入口已停用（2026-08-27 拍板，允许报错不可用）；gateway 本体
（飞书 WS、Daily delivery）继续运行；current 指针、其目标 release 目录、fin-data
生产配置冻结至 P5。当前生产 release：`319faf62`。

## 板 A · 重构阶段（对齐 rebaseline §6）

| 阶段 | 状态 | 指针 |
| --- | --- | --- |
| P0 止血文档手术 | ✅ 完成 | f0b8b6b8 |
| P1 CLI 首链（D1 薄 server + D2 顾问人格） | ✅ 完成：六题 Q1–Q6 全过，codex/CC 双客户端接通 | [read-capability-server-design](read-capability-server-design.md)、[consult-agent-workspace-design](consult-agent-workspace-design.md)、[night-shift-report-20260827](night-shift-report-20260827.md) |
| W2 原地手术（备份/部署/Daily 脱钩/归档/L1 池） | ✅ 完成：生产 release `319faf62` | — |
| 路由重排 D-018/019/021 | ✅ 完成（文件层 + 运行态） | [../DECISIONS.md](../DECISIONS.md) |
| W2' 新仓移植（`~/fin-core`） | ⏳ 已拍板施工（D-025）；设计门通过后开工，顺序见施工设计 | [new-repo-migration](../design/new-repo-migration.md) |
| 外部项目吸收 | ⏳ 待排期，范围开工时定 | D-020 |
| W3-4 深化调优 | 🔶 第一刀已完成（2026-08-29 owner 拉前拍板）：设计门过（dspro·max·82s，0P0/3P1/7P2 全采纳）→ 删罐头注入+风险刹车转 verbatim（2b229dfc，cognition 527 绿）→ 生产库清理（罐头 107+悬空 28，备份+manifest，`$STATE/canned-unit-cleanup-20260829/`）；**剩 06 零提取兜底**（新仓施工，动手前先查 `_LLM_EXTRACTION_PROMPT` 凤仙郡规则为何 08-28 仍空壳）+ 01/03/05 prompt 调优清单；设计稿合入后按规则5归档 | ~~[thesis-canned-unit-removal](../design/thesis-canned-unit-removal.md)~~（归档于 Git 史） |
| D3 三天真实使用门 | ⏳ 建设完成后一次性执行；供数 = finq usage.jsonl | D-020 |
| P4 纯使用 / P5 飞书家人 | ⏳ 之后 | rebaseline §6 |

旧执行队列已整体 superseded-by-rebaseline（历史看 Git 与 rebaseline 附录 C，不复述）。

## 板 B · 能力地图（影响问询结果的每个接线点）

状态六档：`待施工 / 文件层 / 运行态 / 问询验收中（已接入但有未闭环缺陷）/ 在用（无未闭环）/
观察期未接入（建成、产品无读方）`。

问询探针 = 一次真实提问，看 trace 三字段（`~/fin-data/trace/read-capability/calls.jsonl`：
工具被调、`data_gaps` 空、`status` 正常）判「起了作用没」；效果好坏归打分/盲评，不混判。

推进位标记（执行顺序，与状态六档无关；主线当前 = 深化，完整顺序看待办队列）：
`【先决】【主线】【旁路·时间/使用/owner/随手】【随部署】`。

问询面运行源：薄 server 由 main 工作树 `.venv` 起（consult-agent/.mcp.json），
Daily/ZSXQ 单元由生产 release 起——问询面「运行态/在用」按 main HEAD 生效计。

### L1 问询大脑（决定怎么想）

| 能力 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| 顾问人格 | 全部问询的工具选择、证据纪律、输出格式 | 问询验收中 | 持仓类/老师体系类问题，验工具按规则被调 | consult-agent/CLAUDE.md；BUG-005 |
| 问询模型/路由 | 答案质量、成本、时延 | 在用 | 任意问询 | config/llm.yaml；D-018/019/021 |
| 连续性/记忆 | 续问与跨会话上下文 | 在用（codex 客户端读不到 CC 记忆 = 已知边界） | 续问（六题 Q4） | consult-agent-workspace-design.md |
| 外部检索 | 时事与星球外信息 | 在用 | 时事类问题，验引用可溯源 | consult-agent/.mcp.json |
| 识图 | 图片理解 | 在用 | 带图问询 | llm.yaml vision 链 |

### L2 六个上下文缝（决定装了什么）

| 工具 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| read_g_context | G 主线证据注入 | 在用 | 老师体系覆盖的问题，验证据链 + 三维打分 | [../design/g-cognition.md](../design/g-cognition.md) |
| read_actual_portfolio | 持仓名称/现价/变化栏 | 在用 | 「分析我的持仓」 | [../design/portfolio.md](../design/portfolio.md)；探针 08-29 ok 无 gaps（BUG-001/008 已闭） |
| read_market_snapshot | 标的行情 | 问询验收中（容量已修；EASTMONEY 源解析失败致参考价） | 最新价，验无容量耗尽 | 【旁路·时间】[../design/market-data.md](../design/market-data.md)；BUG-002/011 |
| read_market_overview | 大盘结构 | 问询验收中（结构性半边待修） | 「今天大盘怎么样」，验 gaps 空 | 【旁路·时间】[../design/market-data.md](../design/market-data.md)；BUG-002 |
| read_margin_evidence | 两融语义 | 问询验收中（工具描述语义待纠） | 两融问题 | BUG-004 |
| read_ready_evidence | 官方公告/记录 | 问询验收中（全调用 unavailable；公告探针未触发工具） | 公告类问题，验工具被调 + 可用 | BUG-012 |

### L3 供给链（决定上面缝的数据质量）

| 环节 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| ZSXQ 采集 | 知识新鲜度 | 问询验收中 | 验 G 工作集 fresh pair 含新文（无直接工具，间接缝） | [../design/zsxq-capture.md](../design/zsxq-capture.md)；BUG-003/006 |
| 入库/索引 | 检索命中一致性 | 问询验收中 | 验 G/深化命中历史文章（间接缝） | BUG-007（repo 旧副本/默认路径清出） |
| 文章标签 | 星球内容检索组织（尚无产品读方） | 观察期未接入 | 「翻星球内容而不得」即接入凭证 | 【旁路·使用】D-024 |
| 深化 deep-read | 文章支撑证据 | 问询验收中（B2 两口径均 <7 → 重写范围钉死：thesis_extractor 罐头单元） | 需文章支撑的问题，验引用可溯源 | 【主线】[../design/deepen.md](../design/deepen.md)；B2 盲评（裁决台账在设计稿 Git 史） |
| G 准入/工作集 | G 注入新鲜度 | 问询验收中（深化第一刀后 manifest 契约失配，待重生） | 老师体系问题，验 fresh pair | [../design/g-cognition.md](../design/g-cognition.md)；CC 收口 b2da8d9c |
| 知识脑 knowledge_brain | 方法论知识卡 | 问询验收中（两卡已点亮进问询上下文；方法论探针未命中） | 方法论类问题，验命中卡（risk_check/高PE 关键词） | knowledge_brain/seed_methodology_qa.py（541368d8） |
| 薄 server 装配 | 六缝可用性（单缝失败隔离降级） | 在用 | 任一问询，验 gaps 可查 | read_capabilities/ |

### 其他产品面

| 产品面 | 状态 | 验收手段 | 指针 |
| --- | --- | --- | --- |
| Daily 简报 | 问询验收中（四班 L1 实证已通；B1 盲评 7.67<9 不闭环，同条件 9=9 打平、差距全在带伤班次；gap 记账哑已立案） | 四班交付记录 + B1 盲评 | 【旁路·owner】[../design/daily-delivery.md](../design/daily-delivery.md)；BUG-008 |

## 待办队列（只放未决项）

| 位置 | 序 | 事项 | 等谁 / 何时 |
| --- | --- | --- | --- |
| 主线 | 1 | W2' 新仓移植施工（D-025 拍板；顺序：设计门 → 闭包并集 → 新仓移植 → 单元重指向 → KB 副本绝根 → 老仓 ARCHIVED，以施工设计修正稿为准） | 设计门过（dspro·max·709s，1P0/5P1/2P2 全采纳，F1 数据对象改写）；施工中 |
| 主线 | 2 | 深化重写（新仓就绪后施工；剩余范围：06 零提取兜底，01/03/05 走 prompt 调优。删罐头注入已于 08-29 在本仓先行完成——2b229dfc，**新仓移植勿回罐头缝**，裁决台账在设计稿 Git 史） | owner |
| 主线 | 3 | Daily gap 记账哑两项：断料降级模板 + snapshot 材料级 gap 上报（B1 归因新发现；深化之后顺位） | 随主线排期 |
| 旁路·时间触发 | 4 | BUG-002 结构性半边定修 + BUG-011 EASTMONEY 源复查（同一 repro，单会话执行） | 周一 08-31 09:00–09:20 盘前窗口 |
| 旁路·使用触发 | 5 | 标签检索缝开工凭证：首条真实抱怨「翻星球内容而不得」（finq 记账） | 使用触发 |
| 随部署 | 6 | 基础设施审计 A2：poller 超时 15→20min + SuccessExitStatus=75（D-026：并入迁移第5步统一 apply） | 随迁移第5步 |
| 旁路·等owner | 7 | 基础设施审计 A5：Windows Task PS1 的 commit 常量更新（低优先，poller 不比对不阻塞） | owner（Windows 侧手动） |
| 旁路·随手修 | 8 | BUG-004 工具描述语义 + BUG-007 双轨清出（等迁移第一刀合入后再修，避免动闭包内文件） | 迁移后 |
| 随部署 | 9 | 基础设施审计 A6：Hermes cron 注册表以 apply 脚本常量为源 | 随下次部署 |

## 遗留观察（诊断/环境，上限 4 条）

1. release pyc 污染三来源（运行 gateway 再生插件 pyc；launcher 重装变 inode；任何不带 `-B` 的调用）→ 碰 release 树一律 `-B`。
2. gateway journal 近零日志是常态，判卡死先查 state.db 与官方历史，别拿 journal 行数当依据。
3. codex CLI 0.149.0 静默忽略带引号的 `-c` 值 → 401；手动入口 `-c` 必须写 TOML 裸值。
