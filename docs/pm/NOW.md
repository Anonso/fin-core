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
> 最后核对：2026-09-01（Asia/Shanghai）。

## 生产声明

旧飞书/Hermes 咨询入口已停用（2026-08-27 拍板，允许报错不可用）；gateway 本体
（飞书 WS，Hermes venv）继续运行未动。当前生产：Daily/ZSXQ 单元与薄 server =
`~/fin-core`（consult-agent/.mcp.json + systemd 单元，2026-08-29 步5 重指向）。
Daily 四班推送 2026-09-01 起停用（D-030，8 个 systemd timer 已 disable，
单元与 durable 状态机保留，可一键恢复；ZSXQ 采集不受影响）。
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
| 外部项目吸收 | ⏳ 范围盘点已入队（旁路·owner，D-016 三道闸），施工待举证 | D-020 |
| W3-4 深化调优 | ✅ 完成：二轮复盲评 7.59>7 闭环（08-31，55/56 票；GLM 缺票最坏 7.48）；01/03/05 调优已随二轮闭环收口；GLM 三节点因额度耗尽暂关（D-028） | 台账 `$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/` |
| D3 三天真实使用门 | ⏳ 建设完成后一次性执行；供数 = finq usage.jsonl | D-020 |
| P4 纯使用 / P5 飞书家人 | ⏳ 之后（KB/188M 根收拢 = P5 前独立步）；P5 路线已定候选方案 A：Hermes 直接当问询 agent（D-032） | rebaseline §6；D-032 |

## 板 B · 能力地图（影响问询结果的每个接线点）

状态六档：`待施工 / 文件层 / 运行态 / 问询验收中（已接入但有未闭环缺陷）/ 在用（无未闭环）/
观察期未接入（建成、产品无读方）`。

问询探针 = 一次真实提问，看 trace 三字段（`~/fin-data/trace/read-capability/calls.jsonl`：
工具被调、`data_gaps` 空、`status` 正常）判「起了作用没」；效果好坏归打分/盲评，不混判。

推进位标记（执行顺序，与状态六档无关；主线当前 = CLI 实弹三连验（BUG-005/012/002，手动 CLI 随用随验）；时间窗项放旁路·时间触发到点执行、不占主线位（owner 08-30 裁定）；完整顺序看待办队列）：
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
| read_market_snapshot | 标的行情 | 在用（08-31 EASTMONEY f48 浮点契约修复闭环：端到端探针两标的 READY gaps=()；容量半边 08-28 已修；BUG-022 已闭：09-01 22:33 真实 CLI 问询 002428 READY/gaps=[]/94.9） | 最新价，验 gaps 空 | [../design/market-data.md](../design/market-data.md)；BUG-011/022 已闭 |
| read_market_overview | 大盘结构 | 问询验收中（08-31 定修 + 09-01 修复尝试2〔gate 5 + 失败诊断〕已部署；**09-01 08:55 盘前实弹 gap 仍在 → 未闭环**；09:35 盘中 gaps=[]；D-030 停推后无自动窗口，待手动 CLI 盘前实弹） | 「今天大盘怎么样」，验 gaps 空 | [../design/market-data.md](../design/market-data.md)；BUG-002 |
| read_margin_evidence | 两融语义 | 在用（08-30 实弹闭环：全市场拥挤度语义生效，账户语义混淆清零） | 两融问题 | BUG-004 已闭 |
| read_ready_evidence | 当天高相关本地参考材料注入（非 G、非公告） | 问询验收中（供料链已通：08-30 实弹公告腿过 + items 非空实证；08-31 残余二定修：标题 4 字门 + 有事实帖优先 + latest-focus 误判修正，公共 RPC 端到端仅返回目标帖；**09-01 21:01/21:08 实弹仍 ready_evidence_unavailable → 未闭环**） | 当天老师相关提问，验工具被调 + 有料则注入 | BUG-012 |
| read_external_evidence | 官方记录/公告证据（OfficialRecordEvidence） | 问询验收中（08-30 公告探针过：外搜带时点、持仓联动正确；现役面=外搜 MCP 辅助面） | 公告类问题，验工具被调 + gaps 空 | BUG-012 公告腿已闭 |
| read_user_watchlist | 自选股清单（user context 注意力焦点，永非投资证据；含 provenance/tags） | 在用（08-29 接入；09-01 加标签/来源投影） | 「看下当前自选股」，验工具被调 + 空表诚实答空 | 短设计已按规则 5 归档（git 历史：read-user-watchlist-tool、watchlist-tags-and-owner-profile）；写通道=manage_user_watchlist.py |
| update_user_watchlist | 自选股受限写（add/tag/remove；不得自动删除，remove 需用户明确指示；assistant 来源服务端强制；preview→apply 两段式） | 运行态（09-01 建；待真实问询使用） | 「把 XX 加入自选 / 给自选打标签 / 删掉 XX」 | 短设计已按规则 5 归档（git 历史：watchlist-tags-and-owner-profile） |

### L3 供给链（决定上面缝的数据质量）

| 环节 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| ZSXQ 采集 | 知识新鲜度 | 问询验收中 | 验 G 工作集 fresh pair 含新文（无直接工具，间接缝） | [../design/zsxq-capture.md](../design/zsxq-capture.md)；BUG-003/006 已闭 |
| 入库/索引 | 检索命中一致性 | 在用（BUG-007 已闭：默认路径换缝 + repo 副本绝根 08-29） | 验 G/深化命中历史文章（间接缝） | BUGS.md BUG-007 |
| 文章标签 | 星球内容检索组织（尚无产品读方） | 观察期未接入 | 「翻星球内容而不得」即接入凭证 | 【旁路·使用】D-024 |
| 深化 deep-read | 文章支撑证据 | 在用（B2 二轮复盲评 08-31 闭环：7.59>7、逐字 63/63；残余缺陷面=模板噪声/主题簇误归类/量化锚点覆盖，见打分表2；空壳 0→3 修复实证） | 需文章支撑的问题，验引用可溯源 | [../design/deepen.md](../design/deepen.md)；B2 台账 `$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/` |
| G 准入/工作集 | G 注入新鲜度 | 问询验收中（深化第一刀后的 manifest 契约失配已消——08-29 晚六题 g_context 零失配码；fresh pair 专项探针待跑） | 老师体系问题，验 fresh pair | [../design/g-cognition.md](../design/g-cognition.md)；CC 收口 b2da8d9c |
| 知识脑 knowledge_brain | 方法论知识卡 | 问询验收中（两卡已点亮进问询上下文；方法论探针未命中） | 方法论类问题，验命中卡（risk_check/高PE 关键词） | knowledge_brain/seed_methodology_qa.py（541368d8） |
| 薄 server 装配 | 八工具可用性（七读一写，单缝失败隔离降级） | 在用 | 任一问询，验 gaps 可查 | read_capabilities/ |

### 其他产品面

| 产品面 | 状态 | 验收手段 | 指针 |
| --- | --- | --- | --- |
| Daily 简报 | 问询验收中（四班 L1 实证已通；B1 盲评 7.67<9 不闭环，同条件 9=9 打平、差距全在带伤班次——带伤主因 BUG-015 冻结时钟已修，08-31 postmarket 班实弹 gaps=[]+行情+G 对表齐活；gap 记账哑已修 08-30；08-31 G 认知接为第四材料键〔设计门 8/8 采纳〕+ 两融项删除、不催更新；BUG-016/017 已部署，**09-01 09:35 morning 真实班 gaps=[]+正文带指数点位/成交额 → 首次真实正文确认通过**；**09-01 14:20/15:30 close+postmarket 推送因工作树脏被身份门拒、已放弃补发**；D-030 09-01 停推，复验并入 D-031 验证；盘前 08:55 概览 gap 未消失〔BUG-002 未闭环〕） | 四班交付记录 + B1 盲评 | 【最后】BUG-016/017；[../design/daily-delivery.md](../design/daily-delivery.md)；BUG-002/008 |

## 待办队列（只放未决项）

| 位置 | 序 | 事项 | 等谁 / 何时 |
| --- | --- | --- | --- |
| 主线 | 1 | CLI 实弹三连验：BUG-005 G-first 口径（分析必调 G）、BUG-012 ready 残余二、BUG-002 盘前概览——owner 常问三类（持仓复盘/老师体系/大盘板块）手动 CLI 随问随验，对照 trace 三字段 | 即日起随真实问询；BUG-012 今晚 21:01/21:08 实弹仍 unavailable |
| 主线 | 2 | finq 使用日志成习惯：每次 CLI 问询记一行（问题/满意/哪里不最优），不满意项当日落 BUGS | 前序=主线1；随用随记；D3 供数依赖 |
| 主线 | 3 | 标的评分维护列表 + ZSXQ 窗口分级：已交付（回填 407 条〔60 天+评分≥7〕、read_instrument_scores + read_article_search 两工具已接 thin server、G/reference 窗口分级落地）；**Windows 增量评分<7 跳过待 owner 通知** | owner 通知后做 Windows 侧；排期见 [../design/instrument-score-registry.md](../design/instrument-score-registry.md) |
| 主线 | 3.1 | 宏观统一接口 A（read_macro_brain）：ZSXQ 宏观 + 外置大脑书卡 + search_web 补充（guided 默认）；先出宏观候选清单 owner 校准 → 打标器/宏观索引 → 接口 → external_brain 槽复用 | 前序=主线3；设计 [../design/macro-brain-interface-a.md](../design/macro-brain-interface-a.md) |
| 主线 | 3.2 | G 工作集 manifest PARTIAL 修复：priority_events 契约不匹配（09-01 新栏目事件格式）→ sources_changed 与覆盖缺口根因 | 前序=主线3.1；顺带修 |
| 旁路·owner | 3.3 | 特刊名录一致性：公司提取已改读 units 结构（8/13 特刊通富等已可取，commit 7cfb16b）；剩余=特刊对“封测/先进封装”问法仍未被选入 fresh_g（选择层相关性） | 随 3.1 同批 |
| 旁路·owner | 4 | 外部项目吸收范围盘点（D-016 A1/A2 + 三道闸）：列候选组件/方法，逐项举证使用日志具体抱怨或已发生故障；无举证项不施工 | owner 列候选范围 |
| 旁路·时间 | 5 | BUG-019 ZSXQ deep-read retryable 观察（backlog 重试成功即关闭） | poller 重试 |
| 最后 | 8 | BUG-016/017 盘后 Daily 复验：D-030 停推后窗口失效，并入 D-031 验证 | D-031 实施时 |
| 最后 | 9 | D-031 Daily 生成器换问询环境（owner 09-01 指示先聚焦手动 CLI） | owner 指示恢复推送后 |
| 旁路·P5 前 | 10 | G 主线手工批注 durable 归位：从本仓布局路径（`.gitignore` 内盘上文件，fresh checkout 须自备份复原）移入 canonical KB 根 + consume 读点换 knowledge_root 缝；备份 `$STATE/fin-analyse/w2-step5-cutover-20260829/kb-repo-backup-20260829/` | P5 前 KB 收拢步首项 |
| 旁路·时间 | 11 | GLM 三节点恢复启用：llm.yaml glm53/glm53_flash/glm-vision enabled 改回 true（D-028 暂关） | 额度恢复 |
| 旁路·使用触发 | 12 | 标签检索缝开工凭证：首条真实抱怨「翻星球内容而不得」（finq 记账） | 使用触发 |
| 旁路·P5 前 | 13 | Hermes 问询 agent 同源化设计（D-032 方案 A）：人格/工具/记忆三缝同源 + P1 六题级验收；飞书传输复用既有 gateway，不新建 | D3 之后、P5 前 |

## 遗留观察（诊断/环境，上限 4 条）

1. release/gateway 运维判读：碰 release 树一律 `-B`（pyc 三来源污染）；gateway journal 近零日志是常态，判卡死先查 state.db 与官方历史。
2. codex CLI 0.149.0 静默忽略带引号的 `-c` 值 → 401；手动入口 `-c` 必须写 TOML 裸值。
3. fin-core 的 `fin_analyse` 是无 `__init__.py` 的 namespace 包：从旧仓 cwd 以 stdin 跑一次性诊断会整包 import 旧仓代码（旧逻辑+旧仓 `.env` 解键，结果看似正常实则错源）→ 诊断脚本一律文件模式跑 + 显式注入 `FIN_LLM_ENV_FILE`（指针在旧仓 `.env` 第 6 行，目标 `~/.config/fin-analyse/llm.env`）。
