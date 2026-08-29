# FIN Rebaseline 方案 v1.6（含双评审裁决）

> 日期：2026-08-27 · v1.5 作者：CC 战略顾问会话；v1.6 = GPT 评审（`docs/pm/rebaseline-20260827-review.md`）+ DeepSeek 对抗评审（`$XDG_STATE_HOME/fin-analyse/rebaseline-review-20260827/codex-adversarial-review.md`）逐条裁决后的修订。
> 状态：**方向③经双评审确认成立；执行方案按 §0.5 修订；待所有者批准执行。**
> v1.5 原文保留在 §0 之后作为历史参考；与 §0.5 冲突处以 §0.5 为准。

---

## 0.5 v1.6 评审裁决与结构性修订（当前权威）

### 0.5.1 核心重构：从"切除手术"到"建新→验证→饿死旧机器"

两家评审独立收敛于同一判决：方向③成立，但 W2 的"删除手术"执行面被推翻。接受其重构：

- **未引用代码不产生运行边界。** 唯一真正贵的运行时边界是 `gateway/mcp_server` 的 110K 行闭包；建新薄 server 即解决，与预删 23 个模块无关。
- 预烘焙删除清单与保护面存在 ≥8 处真实 import 冲突（cognition→moa、deepen→vision/claims、scraper→claims、market→context、runtime_context→temporal、provider→researcher/production_runtime），按清单执行第一刀就会打断 ZSXQ→深化→G。
- **新顺序**：建设/修复（W2'）→ 自然使用观察（P4'，无强制配额）→ keep-set 闭包驱动的惰性归档（W6+）。keep-set = 薄 server + Daily + ZSXQ + 深化 的 import 闭包；闭包外且无引用者逐批归档删除；23 模块清单降级为候选。

### 0.5.2 八项结构性修订（R1-R8）

| # | 修订 | 替代的 v1.5 内容 |
|---|---|---|
| R1 | W2 改为"修复+建设"，破坏性归档推后且惰性化 | §6 W2 行、§6.1 |
| R2 | D1 spike 重定义：(i) `ProductionReadRequest/Result` 类型搬叶子模块切断 provider→production_runtime；(ii) 只读薄 server（约数百行起步，闭包允许先大后小）；(iii) reader 装配从 `production_runtime:840` 抽出新 wiring 模块 | §6 P1①、§12 假设 A |
| R3 | Daily = 生成器替换（consultation-chain 委托 → L1 直调），**保留 semantic_state 的 durable 状态机本体**（product+obligation 原子提交/CAS/claim fencing 不动）；检查点入口接受 pinned-SHA checkout 身份；脱钩前先取健康基线（评审当日 2/4 检查点 codex_timeout 失败中） | §6 W2②、§8 风险3 |
| R4 | 部署 = checkout **指定 SHA**（非 pull main）；W2' 期间 main 独占写 | §5.3 |
| R5 | 备份 = 停 writer + SQLite backup API；覆盖全部 durable stores（knowledge-base、semantic-research sqlite、Daily outbox、runtime-truth、ZSXQ ledger） | §6 W2⓪ |
| R6 | A/B 协议：**按域裁决**（账户/G/行情/连续性各自冻结门槛与 as_of pin，不用总冠军否定整条管线）+ 消融臂（同人格无工具）；仪表 = 薄 server 自带每调用 trace jsonl（替代硬编码旧 home 的 audit 脚本）；主实验 glm-5.3 + 一次 Sol 确认复跑 | §7 |
| R7 | 薄 server v1 **只读**：无 envelope（本地单 principal 信任模型）、不含 watchlist/审核写入口；账户确认链 P1 不经 CLI（沿用既有脚本），identity 设计推迟 P5 | §5.1、§7 |
| R8 | 顾问连续性 = CC 原生项目记忆（目录即身份，顺带解决家人隔离）；人格规则"数字一律重读工具，记忆可陈旧" | §6 P1② |

### 0.5.3 事实勘误（v1.5 → 以此为准）

仓库 **69 天**（首提交 2026-06-19）、≈37.8 commits/天，非 90/29 · Slice 5 为 5/5 胜中 **3/5 严格可比** · `read_shared_knowledge` 已被 `8c0718a3` 移除（"授权未消费"仅适用 teacher_cognition） · NOW 同日 ≥6 个 current SHA 段 · `mcp_server.py:2583` 本有 stdio 入口，真实成本在 `_services()` 的 110K 闭包 · gateway 经 current 符号链接加载可变代码并动态读 `fin-data`，"封存"精确含义 = **不动 current 指针/其目标目录/fin-data 配置直至 P5** · fin-data 实为 **11+** 目录（含 1.1G `codex-proxy-runtime`、93M `codex-runtime-v1`） · "8 模板"实为 2 相位 × 4 检查点 = 8 个 systemd unit 实例、单一生成代码路径 · 证据目录（`r1-direct-ab-20260824/`、`slice5-ab-20260825/`、`p1-r1-owner-closure/` 等）从任何删除清单**豁免**。

### 0.5.4 部分接受/降级（附理由）

- DS#6（CLI 无法约束 Bash/Edit）：接受结构结论（R7 只读面），拒绝升级为阻断——本地单人用户即 principal；真实交易副作用在 FIN 中本不存在。
- DS#12（2×2 实验）：接受因果措辞收敛（人格单因果未被历史实验识别），以"glm-5.3 主实验 + Sol 确认复跑"替代全 2×2。
- DS#14（MEMORY.md 是未定义 durable owner）：接受机制纠正（CC 原生项目记忆），家人身份隔离由目录即身份结构性解决，不设五项测试门。

### 0.5.5 冻结语义（已拍板 2026-08-27：**停用旧入口**）

旧飞书咨询入口允许报错不可用；fin-data 11+ 目录与路由配置获得清理自由，L1 施工不受封存束缚。gateway 本体（飞书 WS、Daily delivery）继续运行；P0 在 NOW 中明示"旧咨询入口已停用"。current 指针与其目标 release 目录在 gateway 存续期间仍不主动改动——停用 ≠ 主动破坏，只是不再保护其可用性。

### 0.5.6 容器判决（已拍板 2026-08-27：**③b 新家移植**）

**决策**：新代码住新仓；逻辑零重写。目录安排：

```
~/fin-core/    新代码仓（git init 新历史；包名 fin_analyse 不变 → 移植 import 零改；
               只含 keep-set 闭包 + 对应测试精选；AGENTS=家规 v2 day one；
               .claude 全新零 hooks；pyproject 依赖剪枝）
~/fin-data/    数据与运行时唯一家：consult-agent/ · knowledge/（从旧仓
               knowledge-base 迁出，数据出仓）· portfolio/ · trace/ · llm-config/
~/fin-analyse/ 旧仓：过渡期照常跑生产（units/current/gateway 不动）；开发冻结；
               gateway 存续期间保持"冻结运行"，完全归档等 P5
```

**隔离六规则**：新 git 历史 · 包名保留 · hooks/skills/settings 不搬 · 测试只搬 keep-set 对应 · 数据一律住 fin-data · 旧仓冻结不删除。

**移植清单** = keep-set 闭包并集（以薄 server wiring、Daily 检查点入口、ZSXQ consumer/poller 入口、deepen 入口四者的 import 闭包并集为准；评审已算出 provider 闭包 137 文件/83K、G 路径 15.6K）**+ 附录 C 全部资产**。

**顺序不变的关键**：P1 spike 仍在旧仓（使用验证不等搬家；spike 期用户只见 consult-agent 与终端，无残留暴露）；W2' 的"原地手术"替换为：闭包并集计算 → 新仓初始化 → 代码移植 → 依赖剪枝 → keep-set 测试精选 → Daily 脱钩在新仓 → 单元逐件重指向（daily/zsxq 先）→ 数据路径迁移含完整性校验 → 旧仓挂 ARCHIVED（gateway 除外）。

### 0.5.7 工程层双评审裁决（2026-08-27 · GPT + DeepSeek 并行盲评）

**评审对象与证据**：D1/D2 两份工程设计稿 + 附录 B 家规 v2。DS：`docs/pm/engineering-layer-review-20260827.md`；GPT：`docs/pm/engineering-rules-review-gpt-20260827.md`（自 `$XDG_STATE_HOME/fin-analyse/rebaseline-review-20260827/codex-engineering-rules-review.md` 存档）。**核验**：CC 对两份全部代码事实断言逐条对代码复核——DS 13/13、GPT 抽查 10/10 属实，零误报（各一处行号漂移，实质无误）。

**独立收敛（高置信，全部采纳）**：类型叶子化不彻底——`CapabilitySource/SourceKind/SourceTrust` 必须五类型同迁且 `ready_evidence.py` 同步改线，否则 production_runtime 闭包经第二条 import 路径原样回归；wiring 漏 cognition owner；无 server 级 deadline（`deadline_at=None` → `wait(timeout=None)` 无限阻塞）；trace 路径与 §0.5.6 容器判决冲突；工具入参 schema 未定义；家规丢「直接 Agent 不退化」（**两评审同认最高价值发现**，反制 12/根因 7 无落点）；规则 8×12 部署矛盾；规则 2 与数据出仓字面冲突；缺调度入口枚举/终态回收/备份机械验收；验收条款含不可判定措辞。

**GPT 独有（已核实，采纳）**：
- **P0-1 G 主线工具选错**：`read_g_context` 才是 G 认知主线（分层投影/审计时点）；`read_teacher_cognition` 是次级记忆且生产中已死（cognition 目录 775 → `open_existing_owner_only_read` 失败 → 恒 unavailable）。D1/D2 工具面据此重排，teacher_cognition 移出 v1。
- P0-5 家规丢 NOW 状态源 → v2.1 首行补状态源条款。
- P0-6 副作用边界过窄（外部消息/scheduler/生产写入/迁移/凭据不在硬边界内）→ v2.1 规则 2。
- P1-1 第六工具非可选（ZSXQ-first 的产品路径）且强制 as_of → 转正，server 缺省补当前时刻。
- P1-5 「只读」实含行情按需抓取与 artifact 发布 → 如实披露为既有产品行为。
- P1-6 stdout guard 必须复制进薄 server。
- P1-8 三条路径契约（portfolio XDG_CONFIG_HOME、日历仓内相对路径、kb root env）→ D1 §6 root 注入表。
- P1-2 实测 provider import 0.54s/121 模块 → 撤销懒加载缓解，改为 `sys.modules` 断根断言。
- P1-10/P1-21 缺口防臆造纪律与时点纪律 → D2 §3.1 规则 5/6。
- P1-19 记忆 owner/种子矛盾 → D2 §5 明确 CC auto-memory 为 owner + 一次性种子。
- P1-12 部署回滚须成套（SHA+lock+依赖+PID+入口）→ v2.1 规则 9。

**部分采纳（附理由）**：
- P1-13 定时产品观测：仅取「last-success 可查」一句（v2.1 规则 10）；完整 run ledger/alert/replay/连续运行门合同**否决**——那是重造观测机器，Daily 的 durable outbox 已提供交付事实。
- P1-7 trace 定位降为**调用率**仪表，消费归因留给 P1 验收场景；request revision/调用链 id/跨进程 append 锁不加。
- P1-18 契约冻结字段：仅涉 durable state 时加并发/时序/幂等三项；全字段清单否决。
- P2-5 DECISIONS 追加竞态：沿用既有顺延重号协议；单人顺序决策者下加锁是过度设计。
- C7/P2-5 轻量版：规则 7 加「附最短复现命令」半句。

**DS 独有**：B6 人格文件无回滚 → D2 §7 风险+黄金验例回归；A4 moa 启动预算 → 被 GPT P1-2 实测数据取代。

**§12 假设 A-E 裁决**：A 不成立为前提（121 模块实测）→ D1 改 sys.modules 断言；B 算术不支持（153,120−21,578−40~50k = 81-91k > 6-8 万）→ **行数退出门整体废除，使用是唯一门，行数是虚荣指标**；C 未满足可验证条件 → P1 改事件驱动不承诺天数；D semantic_state.py:2970-3005 单事务绑定 product/obligation/artifact hash/幂等键 → Daily 特性页必须列 owner/崩溃交错/重放验收；E 历史从未成立 → 改可观察记录（自然使用计数，排除验收/修复重试混入）。

**落点**：D1/D2 重写为 v2；附录 B 升 v2.1（12→13 条，新增副作用硬边界，复评实名 `/review`）。

---

## 0. 评审指引（给评审模型）

你需要攻击这份方案，而不是复述它。最有价值的输出按优先级：

1. **事实错误**：§1 的任何一条与仓库/状态不符（可要求核对具体文件）。
2. **隐藏耦合**：§5/§10 的删除/归档清单里，有哪些模块间的 import 依赖会破坏"保"的部分？（提示方向：能力读簇对 `guo_teacher_research` 内部的依赖、`market`/`cognition`/`portfolio` 的反向依赖、`gateway/mcp_server.py` 的生命周期耦合。）
3. **顺序与过渡态漏洞**：§6 的执行顺序里有没有会弄坏"保护清单"（§10）的窗口期？（特别是：gateway 常驻进程、daily/zsxq systemd unit 在切换期的指向。）
4. **被低估的成本/风险**：§8 风险登记之外还有什么？
5. **实验设计缺陷**：§7 的 A/B 协议有什么混杂变量？
6. **该问所有者但没问的问题**：§12 之外还有哪些决策缺口？
7. **方向本身**：如果你认为"方向③"是错的（应重做/应采用外部宿主），给出可检验的理由，而不是品味之争。

请区分"我确认的事实"（标 ✅，附录 A 有出处）与"我的假设/估计"（标 ⚠️）。攻击假设比攻击事实更有用。

---

## 1. 背景事实（全部可复核，出处见附录 A）

> ⚠️ v1.5 原文保留如下；事实勘误以 §0.5.3 为准（仓库天数、Slice 5 可比性、shared_knowledge、NOW 段数、stdio 入口、gateway 符号链接等七处已修正）。

### 1.1 项目体量 vs 产品面

| 指标 | 数值 |
|---|---|
| 仓库年龄 | 约 90 天（2611 个提交全部在近 90 天内，≈29 commits/天）✅ |
| 主包代码 | 153,120 行 Python（`fin_analyse/`）✅ |
| 测试代码 | 190,411 行（**超过源码**）✅ |
| 子模块数 | 42 个包（含至少 4 个空壳）✅ |
| 真实产品面 | 约 5 个功能：飞书咨询问答、持仓查看/确认、Daily 推送（8 模板）、ZSXQ→深化→G 认知管线、LLM 路由运维 ✅ |
| 最大单体 | `guo_teacher_research` 42,198 行（占主包 27%），最大文件 `semantic_state.py` 6,905 行 ✅ |

### 1.2 两次坍塌史（本方案的核心背景）

**第一次坍塌（2026-08-04 复盘，`docs/RETROSPECTIVE.md`）**：53 个 worktree、138 个本地分支、41 份 release；定时抓取整月不可用但文档多次宣称部分完成。复盘诊断 9 条根因（完成口径错误、每次失败新增一层、并行无终态 owner、多份指令冲突等），立了 14 条"永久反制"。

**关键时间线事实**：治理（P0-P3）8/23 完成；frozen-sync receipt、CAS 激活、四级完成等级、binding/design-review/audit 轮次、Sol/xhigh 审核 hook——**这些重机器全部建成于 8/23 之后，即第一次复盘之后，作为对复盘的"响应"**。复盘说"完成口径错了"→ 药方是更精细的四级完成等级；说"每次失败新增一层"→ 药方是更多层。**药变成了第二次病。** 8/27 所有者原话："思绪很乱、做得很痛、基本不能并行开发。"

### 1.3 所有者使用真相（本次会话中所有者自述）

- "我想用但是用不起来，只要一开发，就算自测通过了，真实使用还是各种问题导致用不了"；对飞书联调"暂时失去了信心"。
- 定位拍板：1-5 人自用（本人+家人），"好用就行，无需对外信誉背书"。
- 痛点排序：价值兑现焦虑 > 流程仪式 / 发布脆弱 / 代码读不动（四项全中）。
- **Daily 推送每天都在读**，但同意精简模板、重新设计。
- ZSXQ 爬取"还算可以"；深化（deepen）产物质量**所有者本人还没细审过**。

### 1.4 增强价值的实证史（混合结果）

- 2026-08-24 严格同题对照（账户快照两臂不一致的前提下）：FIN 赢下全部 2 条账户链（证明持仓上下文有真实增益）；但在 3 条严格可比链（公司/事件影响/连续性）上 **0:3 输给裸 Agent**。
- 2026-08-25/26 Slice 5：修复 prompt 缺陷（整手数量、无来源概率、矛盾技术触发、融资状态——由独立盲评发现）后，同题盲评 **5/5 全胜**。
- 结论：胜负手从来不是"有没有上下文"，而是"上下文投递得好不好"——**杠杆在 prompt/人格层**（本方案将其变为最便宜的层）。
- 工具消费可靠性问题：8/24–8/26 共 106 个咨询会话仅 21 个真正调用了 FIN 工具；`read_teacher_cognition` 90 天被消费 2-3 次、`read_shared_knowledge` 0 次（授权但从未消费）。

### 1.5 关键代码结构事实

- ✅ `ProductionReadCapabilityProvider`（`guo_teacher_research/production_capability_provider.py`）是干净的依赖注入缝：持仓/行情快照/融资证据/G 认知/外部证据等读能力全部经构造器注入的只读 reader 提供，不构造 store、不隐式建目录。**这是本方案保留的核心资产。**
- ✅ MCP 读工具（`read_market_snapshot/overview`、`read_margin_evidence` 等）的实现住在 `guo_teacher_research/production_runtime.py`——即"要归档的大包"内部也住着"要保留的能力簇"。⚠️ 抽取边界需要一次 import 闭包审计（见 §12）。
- ✅ `DailyWorkspaceFinalization` 住在 `semantic_state.py`（6,905 行），且 Slice 1 记录"Daily Workspace 复用同一（咨询）答案"——**Daily 与咨询主链有真实耦合，直接归档会弄坏唯一每天正常工作的功能**。
- ✅ `/home/ypk/fin-data/` 下现有 9 个按路由切分的 codex home（primary/fallback/deepseek/proxy-a/b/两个 manual/apiclub/codesonline），是旧路由链的零件；问询行为主体（advisory prompt）版本化在仓库 `config/` 下。
- ✅ Hermes CLI 今天就能经 MCP 调用 `fin.read_actual_portfolio` 并返回真实持仓——"CLI 经 MCP 消费 FIN 读能力"已在生产被证明可行。
- ✅ `docs/pm/NOW.md`（唯一状态源）已出现三重生产身份并存（同一天内三个不同 current SHA 段落）——维护成本已超单文件承载。

---

## 2. 诊断（三层病根 + 一个机制）

1. **价值观错位**：为"交付的确定性"优化，而不是为"使用频率"优化。五层证据、四级完成度、receipt、canary 都是为保证"那一刻的答案可靠"而建；但 90 天里被保护的那个"使用瞬间"很少发生。为 1-5 人做的产品，**使用频率就是产品**；确定性是企业级价值观。
2. **边界数宿命**："自测通过、真实使用失败"不是运气差。旧链路每次问答穿越 5 道运行时边界（飞书→Hermes→FIN→codex→返回），每道边界有独立的状态积累（旧线程、receipt、pyc、路由冷却）。测试覆盖组件，故障住在边界之间的缝里和时间的累积里。于是加测试→测试长到 19 万行超过源码→成为第二个待维护代码库→缝隙依旧。**出路不是更多测试，是更少边界**：CLI 链只剩 2 道边界（CC↔MCP↔数据），均可本地当场调试。
3. **免疫系统自身免疫**：177 行工程宪法每条都是对真实事故的反应，但累积成本（如审核调度预算被路由超时烧尽）最终超过病原体。元规则：**能用结构消灭的故障类别，永远不要用流程去预防。**
4. **机制**：强模型 + 无删除预算 = 局部正确堆成全局错误（第一次复盘原话）；过程精化是一条永远显示"有进展"的跑步机，而真实使用当时不可达，能量只能流向过程。

---

## 3. 定位与北极星

> **1-5 人自用 A 股投研助理（本人+家人）。"所有者自己天天在用"是唯一最高验收等级。** MCP 读能力面是产品本体；飞书/Hermes 只是未来某天给家人加的薄壳（P5，条件开放）。

产品体验定义（继承自 `user-design-principles.md`，入口无关化后）：像与一个熟悉你持仓、老师认知、最新行情和历史对话的强 Agent 交流；得到更专业、更省心、可追溯的判断，看不到内部工具、等级、状态机和工程错误。

---

## 4. 三方向判决（为什么是方向③）

| | ① 整体重做 | ② 外部宿主 + 嫁接 | **③ 原地减法 + CLI-first（选定）** |
|---|---|---|---|
| 解决"用不起来"的速度 | 1-3 个月后 | 1-3 个月后（且宿主评估的 parity 门禁本身就是一台验收机器） | **约 2 天** |
| 代价 | 重付爬虫（ZSXQ 反爬实战喂出来的）、行情资格化、持仓确认链的全部学费；"重做"正是 AI 超产的引信 | LangAlpha 卖点 checkpoint/replay/writer-fence 恰是要删的机器；Vibe-Research 是研报生成器；差异化资产（星球管线、G 认知、持仓确认）无任何 OSS 对应物 | 手术风险（§8 已登记） |
| 判决 | ❌ | 🟡 重构后以组件/方法粒度部分吸收 | ✅ |

**对方向②的关键重构**：所有者反复看向 GitHub 项目的真实诉求是"运行时让给别人维护"。这个诉求的答案不是 LangAlpha——**CC/codex CLI 本身就是被全职团队维护的大型项目**：会话、记忆、工具循环、MCP 协议全由 Anthropic/OpenAI 维护。本方案本质就是方向②：以 CLI harness 为宿主，运行时负担整体外包；FIN 只保留无 OSS 对应物的部分（数据管线 + 领域人格）。

外部吸收（A 相位：A1 研究方法 → A2 typed 数据源）推至 W4+，带三道闸：①吸行为不吸机制（学 Mira 的 `stale_after` 时效契约，不学 CAS/write-fence）；②举证倒置（每个吸收项必须指认使用日志里的一条具体抱怨或一次已发生故障）；③先旁路后接线。

---

## 5. 目标架构

### 5.1 三域图

```
开发域 fin-analyse/（git 仓）        问询域 fin-data/consult-agent/        数据域（FIN 本体）
├─ 能力读簇 + 薄 stdio-MCP server ◄─ .mcp.json 指向 ────────────────────┐
│  （从 guo_teacher_research 抽出）                                     │ 只递数据：
├─ 认知管线（ZSXQ→深化→G，L1 key 池）                                  │ 持仓/行情/G认知/
├─ 部署 = 固定路径 checkout + uv sync + 重启 unit                       │ margin 证据
└─ 开发 CC（AGENTS 家规 v2）           ├─ CLAUDE.md 顾问人格+护栏        │
                                       │  （自 user-design-principles
    开发域也挂同一 MCP（自测用）        │   移植：G/Z 纪律、投资哲学）
                                       ├─ 模型可配置（默认 glm-5.3，
                                       │  换=换启动方式，人格不动）
                                       └─ MEMORY.md 跨会话连续性
```

关键性质：**目录即身份**——开发域里开的 CLI 是工程师，consult-agent 里开的同一个 CLI 是投研顾问；不存在"开发 CLI 调用问询 agent"的委托（那会重建被删除的嵌套模式）。问询域唯一额外进程是 stdio-MCP server（数据进程，非 agent，随会话生灭）。

### 5.2 LLM 泳道（三池零共享：凭证/failover/冷却/探活互不相通）

| 泳道 | 消费者 | 特性 |
|---|---|---|
| L1 生产管线 | 深化生成、guide 类解析、（脱钩后的）Daily 生成 | 批量、吞吐优先、专属 key 小池；挂了只影响知识更新速度 |
| L2 交互层 | consult-agent 客户端自带模型 | FIN 零持有；延迟敏感 |
| 复评（原 L3） | 按需动词，无常驻设施 | 第一层 `/code-review`；第二层吓人 diff 时裸 `codex exec --sandbox read-only` 对抗审查。无配置、无状态、无轮次 |

### 5.3 部署形态

- 生产 = 固定路径 checkout（独立于开发 worktree）+ `.venv` + systemd unit 指向该路径。上线 = `git pull && uv sync && restart`；回滚 = checkout 回上一 commit。
- **gateway 过渡态**：`hermes-gateway-fin` 继续跑在已封存的旧 release 上（冻结、零改动），继续服务飞书 WS 与旧咨询入口；daily/zsxq unit 在 Daily 脱钩后重指向新 checkout。gateway 的最终命运（换薄壳 or 关停）由 P5 决定。
- 发布机器（frozen-sync receipt、CAS 激活、quarantine、record-sync、canary）整体退役；pyc/receipt 事故族随之灭绝。

---

## 6. 执行计划（阶段、验收门、顺序依赖）

| 阶段 | 时间 | 动作 | 验收门 |
|---|---|---|---|
| **P0 止血** | 1 天 | ① 本决策页定稿 ② NOW 顶部改挂 rebaseline；R1 收口队列改判 **superseded-by-cheaper-instrument**（新仪器=同题裸 CC vs consult-agent 五分钟人工对比）③ 现网封存声明（gateway 零改动）④ 家规 v2 替换 AGENTS.md（177→约 20 行）+ user-design-principles 轻修订（入口无关化；"双Agent单答案"标注随旧拓扑退役；"真实完成=飞书入口"改"=所有者在用"） | 文档级改动 ≤5 个文件；当天现网零行为变化 |
| **P1 CLI 首链** | D1-D2 | ① stdio-MCP spike（双路径预案：a. 给现有 `gateway/mcp_server.py` 配 stdio 入口；**b. 正解大概率是在 provider 缝上新写约 300 行薄 server**，只暴露 5-7 个读工具）② consult-agent 骨架：CLAUDE.md 人格（移植 user-design-principles 的 G/Z 纪律+投资哲学章节 + advisory-prompt-v1 的事故教训：内部标签禁令、附录泄漏禁令）+ MEMORY.md 连续性 + `.mcp.json`（两域各一份）③ alias `finqa` | CC 在 consult-agent 里完成一次真实咨询：调到 read 工具、答案含真实持仓/G/行情 |
| **D3 起** | 第 1-3 周 | 所有者每天 ≥2 个真实提问；一行式使用日志（日期/问题/满意与否/哪里不最优）；深化打分顺手做（答案中出现 G 内容时按"理解准确/去噪/密度"打分，另记单篇成本时延） | **连续 3 天，第 3 天对"明天还用吗"答"是"**；否则带缺陷清单回炉（预期至少一轮返工） |
| **W2 手术**（严格顺序） | 第 2-3 周 | ⓪ 全量数据备份（knowledge-base + 持仓 + G 库，一次性 tar，存 state 区外）→ ① 便宜部署先行（生产 checkout 固定路径；daily/zsxq unit 一次性重指向）→ ② **Daily 脱钩**（生成改 L1 直调；同时盘点 8 模板→所有者选核心集→精简重设计；验收=脱钩前后内容不降级、投递不中断）→ ③ 归档清扫（咨询主链 codex_runtime/semantic_state/semantic_service/router 等 + 发布机器 + 9 个路由 home + 休眠模块清单 + 审核设施 codex_review_failover/hook + 约 40 个 state 仪式目录）→ ④ 泳道施工（L1 小池） | 每刀独立可停；每刀合入当天 CLI 链全绿 + daily/zsxq 单元 dry-run 复核；主包 42→约 12 包、约 -2 万行（休眠模块）+ 咨询机械（净约 -4~5 万行，⚠️ 估计） |
| **W3-4** | 第 3-5 周 | 按使用日志抱怨清单逐项调优（唯一准入队列）；深化审计判决（样本 ≥10：均分 >7 → 只调 prompt/参数；<7 → 外部契约冻结为算子、重写生成核心，抓取/导入/存储不动） | 抱怨清单条目闭环比 ≥80%；CLI 答案质量所有者盲评不降 |
| **P4 纯使用** | 2 周 | 禁止开发冲动；只修真实使用中暴露的毛病 | 使用日志连续无断档 |
| **P5 飞书/家人（条件开放）** | 之后 | 给已验证能力套薄壳；重新设计"双 Agent"规则（旧版随旧拓扑退役）；家人面大概率是飞书（家人不开终端） | 自然续问一周稳定 |

### 6.1 归档的休眠模块清单（W2 ③）

`moa / paper / backtest / dynamics / temporal / vision / graph / learning / admin / tactical_trading / engineering_validation / decision / claims / signals / researcher / analysis / briefing / opportunity / reflection / opportunity / dataflow / project_memory / context`（多数自 7 月未动；多数属明令冻结的非目标域：MoA、PAPER/自动交易、第二控制面）。逐个走 git 归档分支→删除→连带删测试（家规：测试只护活代码）。⚠️ 每个模块删除前跑一次 import 引用闭包核对。

---

## 7. 核心实验预注册（P1 的本质）

**假设**：私有上下文（持仓/老师认知/行情新鲜度）经人格层良好投递后，答案优于裸 Agent。

**协议**：同一批所有者真实问题；两臂同模型（公平性）；consult-agent 臂 vs 裸 CC 臂；所有者盲评。

**预注册的三种解释**（防止动机化推理再造机器）：
1. consult 臂胜 → 方案验证，继续。
2. 平手 → 检查工具消费率（`fin_tool_usage_audit`）；若消费低→修人格指令；若消费正常仍平手→上下文增益不显著，接受"等于裸 Agent 但无损失"作为可用基线。
3. **裸臂胜 → 判定数据管线暂无面向用户的价值，项目再收缩一档（爬虫+深化+人工阅读为主）。禁止以"更多注入基础设施"回应。**

**已知技术风险（头号）**：工具消费可靠性——旧链 106 会话仅 21 有工具调用的模式可能在 CC 复发。对策：人格硬性指令（"任何个股/持仓问题必须先调 read_actual_portfolio + read_market_snapshot；何时调 read_teacher_cognition 写明触发条件"）+ 每两天跑 `scripts/fin_tool_usage_audit.py` 验消费率。

---

## 8. 风险登记册

| # | 风险 | 预案 |
|---|---|---|
| 1 | P1 证明无增益 | §7 预注册解释；这本身是最有价值的实验结果 |
| 2 | 手术弄坏保护面（ZSXQ/Daily/G/持仓） | 白名单 + ⓪全量备份 + 归档分支可恢复 + 每刀后 daily/zsxq dry-run |
| 3 | Daily 脱钩比预期深（semantic_state 纠缠） | 先做 import 闭包审计再动刀；必要时保留只读薄切片过渡 |
| 4 | 能力簇抽取边界失准（provider 依赖 runtime_context 等 5257 行文件的部分功能） | §12 假设 A；W2 前用 codegraph 做闭包审计，抽取而非搬整包 |
| 5 | 复胖（第三次坍塌） | §9 三防御 |
| 6 | MCP server 与 gateway 生命周期缠结 | 双路径 spike（§6 P1①） |
| 7 | 并行会话/孤儿进程干扰手术 | 动刀前过程卫生核对（当前实测：另有一个活跃 CC 会话 + 一个 6 小时手动 codex 会话 + release 目录已涨至 9 个） |
| 8 | 所有者使用习惯不成立（CLI 摩擦 > 飞书 ambient chat） | alias + 常开终端 tab；P4 观察期验证；若不成立→P5 提前，CLI 期作为能力验证期仍有价值 |
| 9 | gateway 过渡期飞书旧咨询入口在归档后行为不一致 | gateway 继续跑封存旧 release（§5.3），代码归档不影响其运行；在 NOW 明示"P1-P4 飞书咨询入口冻结" |

---

## 9. 防第三次坍塌的三道防御

1. **减法原则**：手术期净行数负增长——新写的每一行都要能在归档分支找到超过一行的尸体对应。减法的失败模式是"删坏"（有备份/分支/dry-run 兜底），不像加法会无声增殖。
2. **数字绊线**：手术后主包落定预期 6-8 万行（⚠️ 估计）；此后任何单月净增 >10% → 强制停工审计。
3. **使用即准入**：所有新功能以使用日志的具体抱怨为唯一准入口；"这个项目看起来很优雅"不构成入队理由。

补：v1 硬完成定义——**咨询每周自然使用 ≥3 天、Daily 照常到、ZSXQ 照常流，即宣布 v1 完成、停止开发**；此后"用十天，改一天"。一个需要持续开发才能维持的自用工具是坏工具。

---

## 10. 保护清单（不进任何刀口）

`scraper`（ZSXQ 抓取/导入，含 Windows 七时点 capture）· `cognition`（G 认知库）· `market` 只读 provider · `portfolio` 账户确认链（typed preview → 人工确认 → read）· zsxq timers / daily units 现有运转 · `knowledge-base/**` 全部数据（数据第一原则：**数据是产品，代码是工厂；任何手术先备份产品**）· 方法论资产（advisory-prompt 事故教训、盲评方法、user-design-principles 哲学）。

---

## 11. 已决事项记录（所有者拍板，2026-08-27）

1. 定位 1-5 人自用；"我在用"=最高验收 ✅
2. 方向③：原地减法 + CLI-first ✅
3. FIN 不再自持问询 LLM（咨询主链归档；问询大脑=consult-agent 客户端自带）✅
4. 泳道隔离；L1/L2 分池；复评=按需动词 ✅
5. codex 审核取消（设施归档；保留两层按需调用写法）✅
6. AGENTS.md 177 行 → 家规 v2 约 20 行（大幅裁剪）✅
7. Daily 每天在读；脱钩时精简模板、重新设计 ✅
8. user-design-principles.md 随 P0 轻修订 ✅
9. 顾问默认大脑 glm-5.3，必须可配置；所有者假设"A/B 结论不因换模型根本改变"（可日后五分钟实验检验）✅
10. 深化走审计判决（所有者打分为判据）✅
11. 手术原地（同仓）进行 ✅
12. 外部吸收 W4+ 带三道闸 ✅

## 12. 诚实的未知与假设（评审重点攻击面）

- **假设 A**：能力读簇（provider + 其 reader 依赖）从 `guo_teacher_research` 42K 行中抽取后约为数千行量级。未做完整 import 闭包审计——`read_g_context` 依赖 `runtime_context.py`（5,257 行）的 `AgentRuntimeContextProvider`，抽取边界可能比预期大。→ **评审裁决：不成立为施工前提**（GPT 实测 provider import 即 121 模块/0.54s）；D1 v1 不做闭包手术，改为 `sys.modules` 断根断言（production_runtime 与 capability_broker 不得入薄 server）。
- **假设 B**：主包减到 6-8 万行的估计未逐模块核对；休眠清单 -2 万行 + 咨询机械 -4~5 万行均为粗估。→ **评审裁决：算术不支持（153,120−21,578−40~50k = 81-91k）；行数退出门整体废除**——使用是唯一门，行数是虚荣指标，不再设行数目标。
- **假设 C**：P1 两天 spike 可行（有 Hermes CLI 已走通 MCP 的减险证据，但 CC 侧 `.mcp.json` + 长会话工具消费稳定性未实测）。→ **评审裁决：未满足可验证条件**（finqa 不存在、客户端配置未定）；P1 改事件驱动推进，不承诺天数。
- **假设 D**：Daily 脱钩的复杂度——8 模板各自内容与对咨询链的依赖深度未逐个盘点。→ **评审裁决：未证伪但不能按模板数施工**；`semantic_state.py:2970-3005` 在单个 `BEGIN IMMEDIATE` 事务绑定 product/obligation/artifact hash/幂等键，Daily 特性页必须先列 owner/崩溃交错/重放验收。
- **假设 E**：所有者"每天 ≥2 问"的使用承诺在摩擦降低后能否成立（历史上从未连续成立过）。→ **评审裁决：不作硬前提**；改为可观察记录——owner-initiated 自然使用计数（排除验收/修复重试混入）+ 未使用原因。
- **未定项**：家人使用形态（共享建议 vs 各自持仓/各自快照）——数据模型影响大，推迟到 P5 前再拍板；若评审者认为必须现在定，请指出理由。
- **可能被忽略的第三类风险**：本方案自身成为第三次坍塌种子的路径（我们认为是"为评优而评"式的过度评审文化，或 P4 后以'优化'名义重启大改）——防御依赖所有者自律与绊线，无机械保证。

## 附录 A：证据索引

- 体量/时间线：`git log` 统计 + `find …| wc -l`（2026-08-27 实测）
- 第一次坍塌：`docs/RETROSPECTIVE.md`（2026-08-04）
- 产品哲学（D2 人格素材）：`docs/architecture/user-design-principles.md`
- 增强实证：`docs/pm/NOW.md` 2026-08-24 对照条 / Slice 5 条；`$XDG_STATE_HOME/fin-analyse/r1-direct-ab-20260824/`、`slice5-ab-20260825/`
- 工具消费：`scripts/fin_tool_usage_audit.py` 输出（NOW 2026-08-27 条）
- provider DI 缝：`fin_analyse/guo_teacher_research/production_capability_provider.py`（L151-195 构造器注入只读 reader）
- Daily 耦合：`semantic_state.py` 的 `DailyWorkspaceFinalization`；NOW Slice 1 条"Daily Workspace 复用同一答案"
- 路由 home：`/home/ypk/fin-data/` 目录清单（codex-routes/ 下 6 个 + 顶层 3 个）
- 开放问题处置依据：`/tmp/fin-analyse-handoff-20260827-open-issues.md` + NOW 对应结案条

## 附录 B：家规 v2.1（替换 AGENTS.md · 已过双评审裁决）

```markdown
# fin-analyse 工程家规（v2.1 · 自用期）
定位：1–5 人自用投研助理，好用是唯一验收。
状态源：docs/pm/NOW.md 是唯一当前状态与执行队列（新家同名同位迁移）；
决策史在 docs/DECISIONS.md；两文件都是共享追加目标，按其文件头协议维护。

硬边界（永不自动化）：
1. 真实交易/资金副作用必须人工确认；研究咨询默认 advisory_only。
2. 外部消息发送、scheduler 启停、生产库写入、schema 迁移、凭据改动、
   生产数据删除：需当时匹配的明确授权，不得默认副作用。
3. secrets 与用户数据（新旧全部数据根：knowledge、portfolio、trace、
   记忆、原始 prompt/持仓/文章正文）不入 git、不出本机、不入日志。
4. 删除前 owner-only 备份 + manifest（目录 0700/文件 0600、含全部
   durable store、SQLite 用 backup API 且先停 writer），保证可恢复；
   数据是产品，代码是工厂。

工作方式：
5. 设计先行、按核心度配重：核心链路（产品入口、数据管线、durable
   state、跨功能接口契约、安全/并发/auth 边界）动代码前有一份短设计，
   一项一份，合入后删除（Git 即归档，不建 docs/archive/）；拿不准是否
   核心时按核心处理；非核心改动只需开工四句话（规则8），不写设计稿。
6. 特性内聚：功能的中心在自己的包内实现与优化；跨功能只经窄接口；
   改功能优先深化其 owner 模块，不加 pass-through 包装。会变的选择
   （模型、路由、阈值、开关、入口）进配置文件，不写死代码——改配置
   即改行为，不动代码；不为想象中的变化预建配置项（所有者拍板 2026-08-27）。
7. 诚实分级：「测试绿」≠「跑通」≠「我在天天用」，不得互相冒充；
   声称「跑通/在用」时附最短复现命令。
8. 开工前四句话：改哪些文件 / 影响哪个入口 / 怎么验证 /
   为什么不是别的做法（想过替代方案即可，一句说清）。
9. 部署 = checkout 指定 SHA + uv sync + 重启单元，并核对
   {SHA, lock digest, 已装依赖, 运行 PID, 公共入口结果} 成套一致；
   回滚同样成套（不只 checkout 代码）。部署/退役时枚举 crontab、
   systemd timer、Hermes cron 的指向，任一指向 dirty checkout 即停。
   main 永远保持可部署；半成品留分支/worktree，不合入。
10. 功能两周无人真用 → 休眠候选清单；使用日志是唯一功能准入队列；
    定时类功能的「在用」= 有近期成功交付记录可查。用户本人要求
    视为使用证据的一种（用户即使用者）。
11. 新增抽象/状态/fallback 必须有已发生故障或用户要求作证，且写清
    增强了什么、必要限制是什么、删了什么（净复杂度不增）；写不清
    就不做。人格/工具/上下文改动同此举证——公共入口同题不得弱于
    直接 Agent（不退化是最高产品不变量）。
12. 删功能同步删其测试；删除前先核引用闭包（含动态 import）与
    公共入口；测试只护活代码。
13. 并行与对接：独立功能并行开发（文件集不相交 + 运行态/部署不
    重叠；半成品不进 main）；跨功能对接先文档冻结一版接口契约
    （名称/参数/返回/失败语义；涉 durable state 时加并发/时序/幂等），
    按契约各自实现、互不等待，真实对接修订契约需双方会话确认、
    不得单方追认；分支/worktree/临时文档随任务终态回收；
    共享追加目标（DECISIONS.md/NOW.md）按其文件头协议。

废止：binding / design review 轮次 / 审核 failover / 四级完成等级 /
五层 E2E / 唯一 writer 官僚 / opencode 双 writer 变体。
复评（按需动词，无常驻）：第一层 /review（实名 skill，自固定比较点）；
第二层吓人 diff 时裸 codex exec --sandbox read-only 对抗审查。
设计门（默认开，2026-08-27 拍板）：核心链路设计稿（规则5 要求写设计稿
的那类）动代码前过**一次**外部对抗盲评——双模型并行最优、单模型可；
评审只产发现，裁决归 writer 会话逐条对代码核，采纳与否落设计稿；
不设轮次、binding、failover 或追踪设施；非核心改动豁免。
外援触发（2026-08-27 拍板）：重要设计、或同一问题 ≥2 次修复未果时，
CC 直接调用 codex/codex-open 只读对抗审计，拿反馈再推进——无需用户
中继；外援只产发现与建议，施工与裁决仍归 CC。
升级防线：两层复评每周真在手动用第二层才许包脚本（规则11）；
想接回自动触发/强制轮次必须先指认一次真实漏网事故（一次性设计门
不在此列——它是门不是轮次）。
```

## 附录 C：旧项目资产吸收清单（移植/归档准入核对单）

> 旧项目不是一无是处：keep-set 闭包（约主包一半以上）全部继承，生产在转型期照常运行。本清单覆盖闭包保护不了的"无引用但有价值"资产；惰性归档或移植任何批次前，先核对此清单。

### C.1 代码层（keep-set 闭包自动保护）

`scraper` 全部 · `cognition`/G · `market` · `portfolio`（含确认链与 manage 脚本）· `margin` · provider+`runtime_context`（G 读路径）· `semantic_state` 的 Daily durable 部分（outbox/obligation/claim fencing）

### C.2 教训与契约层（显式移植）

| 资产 | 去处 |
|---|---|
| advisory-prompt-v1.json 事故教训（JSON-only、标签/附录禁令、整手/融资/触发器约束） | consult-agent CLAUDE.md 输出纪律 |
| G/Z 纪律、投资哲学（user-design-principles） | 人格哲学/纪律章节 |
| capability when-to-use 触发句式（方案B） | 人格工具使用规则 |
| 连续性诚实精神（exact resume / DEGRADED_FRESH） | 人格记忆规则（架构上由 CC 原生 resume 满足） |
| 深化目的标尺（准确理解/去噪/提密度，所有者拍板） | 深化审计打分口径 |
| r1-direct-ab-20260824/questions.json 同字节题库 | P1 A/B 复用（保证可比性） |
| 盲评 packet 构造与双 reviewer 方法 | evaluate-fin-agent-effectiveness skill |
| **fin-private-advisory-decision-framework.md**（2026-08-27 讨论冻结版：判断循环/动作合同/查询经济性/禁止模式/云南锗业黄金验例/§11 验收场景） | **consult-agent 人格操作层主源 + P1 验收场景 + 顾问记忆种子**；移植时随新家走，其引用的旧咨询链设计主页角色由 consult-agent 设计接替 |

### C.3 Skill 处置（FIN 专属）

| Skill | 处置 |
|---|---|
| design-fin-agent | **留+改写**为顾问人格维护指南 |
| evaluate-fin-agent-effectiveness | 留（盲评方法论） |
| maintain-fin-portfolio | 留 |
| manage-fin-codex-routes | 缩为 L1 泳道配置说明 |
| fin-release-launcher-chain | 随发布机器归档 |
| automate-feishu-desktop-e2e | 冻结至 P5 |
| book-shared-brain-learning（及 shared brain 35 卡数据） | 冻结评估；数据按"数据第一"原则保留，深化审计时一并判 |
| dev-orchestrator / opsx-workflow / fix-bug | 删除（流程机器遗物） |
| 通用技能包（tdd / diagnosing-bugs / codebase-design 等） | 不动（外部通用，与 FIN 无关） |

### C.4 核心业务功能设计吸收（特性页映射，2026-08-27 增补）

**机制：移植即设计提取**——W2' 拷贝 keep-set 闭包的同时，为每个核心业务特性提取设计不变量，落成新家 `docs/design/<feature>.md`（固定骨架：目标/非目标 · 数据与 schema 事实 · 关键不变量 · 接口契约 · 已知故障与设计回应 · 验证方式）。旧 `internal-module-catalog.md`（1724 行巨石）按特性拆解归位，全文留旧仓博物馆。

| 新家特性页 | 旧设计来源 |
|---|---|
| zsxq-capture | 目录 ZSXQ Scheduler/Scraper 条目 + zsxq spec + 代码不变量（content_sha256 完整性、七时点窗口单一事实源、skip-audit、3 天补拉、EIO 容错） |
| deepen | 目录 Deep Read Artifact Service + 统计口径（generated/cache_hit、hash 失效重做）+ strict-G 门 + backlog 有界排空 |
| g-cognition | 目录 Cognition Memory/Freshness Manifest + g-methodology skill + PIT 语义（as_of/audit_by_ref）+ guo:v0≠长认知边界 |
| market-data | 目录 Snapshot/Qualification/OnDemand/Margin/OfficialRecord/Overview 六条目 + a-share-data-qualification 调研（原件随迁 docs/research/）+ cache_only/stale_fallback、provenance、deadline 语义 |
| portfolio | 目录 Actual Advisory Portfolio + warmup spec（历史）+ 核心/可选完整性门 + stale 降级 + preview→confirm→read + 交易日历权威 |
| daily-delivery | 目录 Semantic Research State 的 daily 子集 + durable 不变量（outbox/obligation 原子提交、claim fencing、重放幂等）+ 新生成器设计（脱钩时同步写） |
| read-capabilities | 现有 read-capability-server-design.md 迁入 + provider DI 设计 |

随迁：两份 2026-07-17 资格化调研原件、g-methodology skill、framework/principles/DECISIONS。留馆：模块目录全文、发布 runbook、咨询链条目、fin-domain-kernel（要点并入后）。

### C.5 运维知识与文档

WSL/Windows interop、GitHub IPv4 重试等全局 skill 保留 · RETROSPECTIVE / DECISIONS / user-design-principles 原文随新家带走。
