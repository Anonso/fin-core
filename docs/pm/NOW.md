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
> 最后核对：2026-09-06（Asia/Shanghai）。

## 生产声明

旧飞书/Hermes 咨询入口已停用（2026-08-27 拍板，允许报错不可用）；gateway 本体
（飞书 WS，Hermes venv）继续运行未动。当前生产：Daily/ZSXQ 单元与薄 server =
`~/fin-core`（consult-agent/.mcp.json + systemd 单元，2026-08-29 步5 重指向）。
Daily 四班推送 2026-09-01 起停用（D-030，8 个 systemd timer 已 disable，
单元与 durable 状态机保留，可一键恢复；ZSXQ 采集不受影响）。
**老仓 `~/fin-analyse` 已退役删除**（2026-09-05，owner 令「做干净」；家规 4
备份+manifest+恢复演练后删：`/home/ypk/fin-backups/fin-analyse-retirement-20260905/`
0700/0600，全树 33,598 文件+2,753 commits 含 14 个未 push，sha256 见 MANIFEST）。
release 保留 `current`（→`319faf62`）+ `ff7441e2`（BUG-002 回滚候选）+
`13c791ca`（Daily 脱钩回滚候选）至 P5，其余 10 个已删；**有运行时读方**：
hermes-gateway-fin/fq 两网关的 MCP 子进程（`fin_analyse.gateway.mcp_server`，
旧咨询入口 08-27 停用后设计内允许报错）仍跑在 `current/.venv` 上——该绑定
随 D-032/P5 Hermes 问询 agent 重接时切换，删 release 前置；主仓删除后
release 降级为纯文件资产（原为 git worktree，运行/回滚不受影响，git 操作失效）。
ZSXQ 采集腿源真身已迁 fin-core（NOW #32，09-05）。
2026-09-03 老仓可删审计：活跃引用清零（bashrc finlog 别名与 claude-mem
分支、`~/.local/bin/claude` 注入分支、codex-proxy 死常量、consult README
开发域指向全部改指新仓/删除），旧仓进入可退役状态；真删除仍按家规 4
先备份 + manifest。
2026-09-05 路由事实：生产问询路由 codex-open（opencode-go）禁用（429 长期
故障，owner 拍板；`enabled: false` 翻回 + 重启网关即恢复优先位），问询链暂
单腿 codex-glm；非 GLM 手动问询腿换 finqa-cmd（Command Code · Go Plan，
BUG-031 双验闭环用其闭环）。提取链 cognition 按 owner 设计（09-05）拆两独立
链节点、每节点 enabled 开关：glm5.3-flash → opencode ds flash（禁用中，恢复
翻 `enabled: true` 即回 DS 槽位首位，无其他改动）→ cmd ds flash → qwen
（llm.yaml ee0f4e1 → 25724de 两节点重构；09-05 晚 owner 取消「09-07 前不动生
产」窗口——ee0f4e1 所记「窗口 owner 拍板」归属有误，owner 原话为准。配置已随
单元按提交重渲染机制生效〔post-commit 渲染 + runtime-config 按 commit 快照〕，
22:38 部署核对成套过：HEAD 3a3564f=单元钉定 SHA、lock 净、poller PID 624551
exit 0、flash 探针 200——D-045 发版核对加项已满足）。外部审视入口
`scripts/codex_open.sh` 重构（D-045）：评审者链 cmd·deepseek-v4-pro 主 →
glm·glm-5.3 替补（此「外部审视入口 codex-open」与生产问询路由 codex-open
同名不同物）。
2026-09-05 机器问询链 finqa-chain 上线：节点表 `config/finqa_nodes.yaml`
（claude→codex 暂关→commandcode，声明序+enabled，提取链语义）+ launcher
`scripts/finqa_chain.py`（无头自动 fallback、横幅/tsv/exit78 设计门语义、
session 可丢、无熔断；设计门 cmd·ds-pro ≈250s 采纳 11 不采纳 1）；盲评
runner CC 腿已迁移（验收 0）。codex_routes.yaml 冻结服务效果评估（resume
语义不同）两链并存，合并等真实需求。
2026-09-06 owner 澄清用法：问询唯一真实入口 = `finqa` 无头单发（launcher 唯一
权威，双活腿 claude+commandcode 自动兜底），TTY 交互形态 owner 不自用——
codex_routes 交互路由链（glm-official enabled / opencode-go disabled，
读方 = codex-proxy 三件套 + guo_teacher_research 默认路径）**无真实使用者**，
改挂退役候选（考后处置；`codex-routes/codex-glm/` 认证/模型目录为 codex_open.sh
评审替补腿资产，退役时保留）。09-05「生产问询路由单腿 codex-glm」的考试风险
提示作废——考试链路走 finqa，与 codex_routes 无关。
**腿角色重排（2026-09-06 owner 二次拍板）**：owner 真实问询=有头手动（终端 CC/
Windows ZCode 连 WSL），无头链流量≈测试——生产链序仅 commandcode（ds-pro）；
测试腿名册 A=commandcode-flash、B=zcode（glm-5.3-flash 单旋钮锚定）、C=claude
（glm-5.3-flash，`--model` 逐次指定实测 served 实证）。评审链 glm 替补再换
claudecode·glm-5.3（`--model` 逐次指定；zcode 退出评审链回测试腿）。zcode 机制
边界实测钉死：无逐次旗标、--settings 0.16.5 未实现、ZCODE_HOME 仅遥测、模型
会话创建时钉定（全局证伪 rc=1）——zcode 家族 flash 弱腿不可行即因单旋钮。
**GLM 无头统一 zcode（2026-09-06 owner 拍板）**：问询链 claude 腿（CC·glm-5.3）
退出，换 zcode 腿（glm-5.3，config 单旋钮 zhipu/glm-5.3）；评审链 glm 替补从
codex-glm home 换 zcode（codex_open.sh glm profile 重写，precheck=旋钮防漂移）；
zcode-flash 测试腿随之取消（单旋钮已让位 glm-5.3，弱腿测试由 commandcode-flash
承担）。harness 本体（claude/codex 二进制）与 codex-glm 资产保留不删，有头 CLI
不动；CC 无头接线位置未定位之谜随腿退出不再阻塞问询链。影响：同模型换 harness
（答案风格可能微变，人格/MCP 不变）；zcode 腿生产使用记录从零起（冒烟+对比探针
补证）；评审者 harness 漂移（packet 缓存失效）；交互 zcode 默认模型 flash→5.3。
**codex_routes 退役已执行（2026-09-06 owner 令「现在退役」）**：闭包定论后移出
codex_routes.yaml + codex-proxy 三脚本 + codex-proxy-a/b-manual 状态目录
（含 sqlite 会话档，无运行进程/无 crontab/systemd 指向，静止态 mv）→
备份 `/home/ypk/fin-backups/codex-routes-retirement-20260906/`（0700/0600，
MANIFEST 含 sha256，目录结构镜像可恢复）；`manage-fin-codex-routes` skill
同删（Git 即归档）；`codex-glm/` 认证/模型目录保留。悬空读方后置项（不阻塞）：
`tools/effect_evaluation/fin_arm_capture.py` `_ROUTE_CONFIG`（Hermes 时代评估栈
provenance 字段）、`guo_teacher_research` 孤立子模块 codex_route_config/
codex_runtime/runtime_diagnostics（包外无活调用方，5 测试在护）、
`fin_tool_usage_audit.py:28` 指向已不存在的 codex-proxy-a（退役前即悬空）。
**影响范围复核（同日）**：网关重启安全——release 快照 `service_registry.py`
硬编码 codex-proxy/yaml 但全部 lazy 触达（registry 文档级 lazy + `_services()`
惰性构建 + 「Lazy import seam」双缝），MCP 启动不构建 consult runtime，缺失
仅在已停用 consult 工具被调用时命中 = 08-27「设计内允许报错」路径；测试
guo_teacher_research codex 系 52 passed + effect_evaluation 102 passed（hermetic
不读真实 yaml）；shell/systemd 无 FIN_CODEX_ROUTE 导出、codex-remote-control
单元无关（CODEX_HOME=~/.codex）、Hermes 侧仅 memory 叙述无代码读方、两个
codex home config 均自足。标注：旧 release 的 service_registry/production_runtime
硬编码属回滚资产已知限制（主支从未有此引用，旧架构仅存 release）；并行会话
switch-codex-open-provider 未提交稿仍引用已退役的 manage-fin-codex-routes
skill，落稿时需知悉。2026-09-06 CLI 收口（D-050）：唯一
入口 `finqa`（--node 钉腿 + -i 交互），launcher=起腿知识唯一权威；测试腿
zcode-flash/commandcode-flash（低一档·effort max）+ 探针接线 + cmd_version_pin
单源 + bashrc 六函数退役（finqa-c/-cmd 过渡别名）；设计门 3 发现全采纳
（台账 design-gate/finqa-cli-unify-20260906/）。2026-09-07 opencode-go 渠道
复测恢复（curl 与生产 OpenAI SDK 探针 /models+chat 均 200；休眠缘由即 429 限
额），owner 拍板全量启用：提取链 DS 槽位 `deepseek_flash_opencode` 翻回
enabled（回第一位），问询链 `codex-opencode` 池条目翻回 enabled——生产问询
链序 = commandcode→codex→claude，finqa-x 复活（bashrc 注释同步；-i 交互分支
同日补接——launcher 黑名单摘除即通，argv/env/cwd 本就预埋，TUI 伪终端实测起框）；
finqa_nodes
codex 节点本体常态 true 未动。

## 板 A · 重构阶段（对齐 rebaseline §6）

| 阶段 | 状态 | 指针 |
| --- | --- | --- |
| P0 止血文档手术 | ✅ 完成 | f0b8b6b8 |
| P1 CLI 首链（D1 薄 server + D2 顾问人格） | ✅ 完成：六题 Q1–Q6 全过，codex/CC 双客户端接通 | [read-capability-server-design](read-capability-server-design.md)、[consult-agent-workspace-design](consult-agent-workspace-design.md)、[night-shift-report-20260827](night-shift-report-20260827.md) |
| W2 原地手术（备份/部署/Daily 脱钩/归档/L1 池） | ✅ 完成：生产 release `319faf62` | — |
| 路由重排 D-018/019/021 | ✅ 完成（文件层 + 运行态） | [../DECISIONS.md](../DECISIONS.md) |
| W2' 新仓移植（`~/fin-core`） | ✅ 完成：07 七步全清（2026-08-29，cutover 见 [../migration-manifest.md](../migration-manifest.md) 步4/5/6/7 记录） | ~~new-repo-migration~~（设计稿随老仓归档入 Git 史） |
| 外部项目吸收 | ⏳ 盘点+举证机制已闭环（09-03）；**吸收 0 项**——09-05 二轮专业标准对照（ai-berkshire 镜子+5 题动态）：候选 1/5 缺口缩窄（承重拷问/三重检已有同构）、候选 4 动态零命中、管理层维度→知识脑新候选，测试暴露是否够闸②举证待 owner 裁决；cmd·flash 腿元叙述泄漏已记录；A2 typed 数据源未开；09-06 专业人设评审（判者=ds-pro 四人设重评盲评存量）实证自评判宽松偏置，六案立案 BUG-049~054 + 人格 r19 同日施工+回归收口，评审方法定常设第二判者 | [scope 09-03](research/2026-09-03-external-analysis-absorption-scope.md)、[盲评 pilot](research/2026-09-03-consult-blind-eval-pilot.md)、[gap 探查 09-05](research/2026-09-05-professional-standards-gap-hunt.md)、[人设评审 09-06](research/2026-09-06-persona-judge-review.md) |
| W3-4 深化调优 | ✅ 完成：二轮复盲评 7.59>7 闭环（08-31，55/56 票；GLM 缺票最坏 7.48）；01/03/05 调优已随二轮闭环收口；GLM 三节点已恢复（D-028 解除，9a0320f） | 台账 `$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/` |
| D3 三天真实使用门 | ❌ **取消**（owner 09-06 拍板「取消考试门」，09-07 09:00 开考作废，D-043 门语义废止）；真实使用证据回归家规 10 口径：finq usage.jsonl 使用日志即准入队列 | ~~D-043~~；供数 = finq usage.jsonl（继续） |
| P4 纯使用 / P5 飞书家人 | ⏳ 之后（KB/188M 根收拢 = P5 前独立步）；P5 路线已定候选方案 A：Hermes 直接当问询 agent（D-032） | rebaseline §6；D-032 |

## 板 B · 能力地图（影响问询结果的每个接线点）

状态六档：`待施工 / 文件层 / 运行态 / 问询验收中（已接入但有未闭环缺陷）/ 在用（无未闭环）/
观察期未接入（建成、产品无读方）`。

问询探针 = 一次真实提问，看 trace 三字段（`~/fin-data/trace/read-capability/calls.jsonl`：
工具被调、`data_gaps` 空、`status` 正常）判「起了作用没」；效果好坏归打分/盲评，不混判。

推进位标记（执行顺序，与状态六档无关；主线 = 准备期·基础功能深化两主项已清尾（09-06，#25 三卡 seed+#26 人格 r18 瘦身），下一主线项待 owner 指定（候选：旁路·排后 #27 先重读 point-line-plane survey）；owner 侧最近验收 = BUG-024 盘前读法实弹〔09-07 周一〕+人格 r18 实弹复核；时间窗项放旁路·时间触发到点执行、不占主线位〔owner 08-30 裁定〕；完整顺序看待办队列）：
`【先决】【主线】【旁路·时间/使用/owner/随手】【随部署】`。

使用路由（D-043/C4，owner 09-05 口述确认「按优势面路由」，见 usage-profile）：凡需本地持久上下文或工具序列的题（G 覆盖/持仓/
评分/时间线/黑话/大盘行情/当日参考/方法论卡）走 FIN；纯外部时事可直连 Agent；
运行态/问询验收中能力不接真实工作流（六档=使用价目表）。

问询面运行源：薄 server 与 Daily/ZSXQ 单元均由本仓起（单元绑 HEAD，
**本仓提交即须重渲染单元**，见 migration-manifest 运维铁律）——问询面
「运行态/在用」按 fin-core HEAD 生效计；gateway 除外。

### L1 问询大脑（决定怎么想）

| 能力 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| 顾问人格 | 全部问询的工具选择、证据纪律、输出格式 | 问询验收中 | 持仓类/老师体系类问题，验工具按规则被调；泛化体系题免提醒验个性化（「小仓该不该更激进」类原题，验自动带账户约束/闲钱边界/刻度带与买腿顺序；09-04 首枪过，owner 要求常态化不依赖提醒） | consult-agent/CLAUDE.md；开放：BUG-024（施工全清，owner 09-07 盘前实弹终验）；已闭：005/025/030/031（09-05 双验闭环，非 GLM 腿换 finqa-cmd） |
| 问询模型/路由 | 答案质量、成本、时延 | 在用 | 任意问询 | config/llm.yaml（L1）；codex_routes.yaml（效果评估，冻结）；config/finqa_nodes.yaml（机器问询链 finqa-chain，09-05） |
| 连续性/记忆 | 续问与跨会话上下文 | 在用（codex 客户端读不到 CC 记忆 = 已知边界） | 续问（六题 Q4） | consult-agent-workspace-design.md |
| 外部检索 | 时事与星球外信息 | 在用 | 时事类问题，验引用可溯源 | consult-agent/.mcp.json |
| 识图 | 图片理解 | 在用 | 带图问询 | llm.yaml vision 链 |

### L2 七个上下文缝（决定装了什么）

| 工具 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| read_g_context | G 主线证据注入 + 元认知调节（09-06 起：可选 meta_calibration 块，fail-open 无画像=现状） | 在用 | 老师体系覆盖的问题，验证据链 + 三维打分；调节器验注入/退化探针 | [../design/g-cognition.md](../design/g-cognition.md)；Git 史 bd15e50（审计门 20260907：P2×3 修复——真原子写/provider 等价测试/画像 v2；台账 design-gate/audit-g-meta-calibrator-20260907/） |
| read_actual_portfolio | 持仓名称/现价/变化栏 | 在用 | 「分析我的持仓」 | [../design/portfolio.md](../design/portfolio.md)；探针 08-29 ok 无 gaps（BUG-001/008 已闭） |
| read_market_snapshot | 标的行情 + 主指数日线（大盘线） | 在用（09-05 板块 lane 上线〔NOW #24，ff372bd〕：液冷/半导体/通信设备等中文名直查腾讯板块指数日线+技术因子——单源 limitation 常驻、无 30m/60m，端到端 READY/120 bars/零 gap+消费端探针答引板块序列带单源口径；09-04 主指数 lane〔96f8fcd〕与个股语义零变化；08-31 EASTMONEY f48 修复与 BUG-022 已闭） | 名称查「科创50」日线，验 bars+gaps 空 | [../design/market-data.md](../design/market-data.md)；BUG-011/022 已闭；git 96f8fcd |
| read_market_overview | 大盘结构 | 在用（09-04 09:07 盘前实弹闭环：整链拒未再现，PARTIAL+7 命名 gap〔5 常驻良性+2 盘前源数据缺席=f3/f6 占位与广度，源属性〕；owner 会话 1c718317 答案诚实降级质量在线。08-31 定修+09-01 gate5 两次修复生效） | 「今天大盘怎么样」，验 gaps 空 | [../design/market-data.md](../design/market-data.md)；BUG-002 已闭 |
| read_margin_evidence | 两融语义 | 在用（08-30 实弹闭环：全市场拥挤度语义生效，账户语义混淆清零） | 两融问题 | BUG-004 已闭 |
| read_ready_evidence | 当天高相关本地参考材料注入（非 G、非公告） | 在用（BUG-012 全链闭环 09-03：残余三投影门外审裁决 A 定修，端到端 RPC status ok/gaps=[] 三字段全过、真实 CLI 触发实证；宏观叙事帖可注入，映射类证据归 read_external_evidence；残余一券商通道为已知限制 P2-8） | 当天老师相关提问，验工具被调 + 有料则注入 | BUG-012 已闭 |
| read_external_evidence | 官方记录/公告证据（OfficialRecordEvidence） | 在用（08-30 公告探针过：外搜带时点、持仓联动正确；现役面=外搜 MCP 辅助面） | 公告类问题，验工具被调 + gaps 空 | BUG-012 公告腿已闭 |
| read_user_watchlist | 自选股清单（user context 注意力焦点，永非投资证据；含 provenance/tags） | 在用（08-29 接入；09-01 加标签/来源投影） | 「看下当前自选股」，验工具被调 + 空表诚实答空 | 短设计已按规则 5 归档（git 历史：read-user-watchlist-tool、watchlist-tags-and-owner-profile）；写通道=manage_user_watchlist.py |
| update_user_watchlist | 自选股受限写（add/tag/remove；不得自动删除，remove 需用户明确指示；assistant 来源服务端强制；preview→apply 两段式） | 运行态（09-01 建；待真实问询使用） | 「把 XX 加入自选 / 给自选打标签 / 删掉 XX」 | 短设计已按规则 5 归档（git 历史：watchlist-tags-and-owner-profile） |
| read_decision_journal + record_decision | 决策日志（owner 口述决策结构化留痕：buy/sell/plan/revert；复盘查事实不代 G-first；不催记录·09-06 窄口=持仓操作建议尾部单条 preview 草稿；revert IFF append-only 更正） | 运行态（09-04 建〔b72ddf5，13 只读+2 写〕；待真实问询使用） | 「当初为什么买 X」验工具被调 + 记录命中 + G 未被取代 | 设计稿随合入归档（git 史 decision-journal-v1 4edfdac）；外审台账 $STATE/fin-analyse/design-gate/decision-journal-v1-20260904{,-diff}/；D-042/D-049；数据=$STATE/fin-analyse/semantic-research-v1/decision-journal-v1/ |

### L3 供给链（决定上面缝的数据质量）

| 环节 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| ZSXQ 采集 | 知识新鲜度 | 问询验收中 | 验 G 工作集 fresh pair 含新文（无直接工具，间接缝） | [../design/zsxq-capture.md](../design/zsxq-capture.md)；BUG-003/006/027 已闭（027 审计链 09-04 实弹 chain_ready=true）；Windows 采集腿源真身已迁本仓（09-05，NOW #32，face071：2 脚本+4 测试+skill+consumer@ 退役；12:20 班实弹 capture/consume 双侧 face071、consumer ready；opencli 1.8.7+扩展 1.0.24） |
| 入库/索引 | 检索命中一致性 | 在用（BUG-007 已闭：默认路径换缝 + repo 副本绝根 08-29） | 验 G/深化命中历史文章（间接缝） | BUGS.md BUG-007 |
| 文章标签 | 星球内容检索组织（尚无产品读方） | 观察期未接入 | 「翻星球内容而不得」即接入凭证 | 【旁路·使用】D-024 |
| 深化 deep-read | 文章支撑证据 | 在用（B2 二轮复盲评 08-31 闭环：7.59>7、逐字 63/63；残余缺陷面=模板噪声/主题簇误归类/量化锚点覆盖，见打分表2；空壳 0→3 修复实证） | 需文章支撑的问题，验引用可溯源 | [../design/deepen.md](../design/deepen.md)；B2 台账 `$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/` |
| G 准入/工作集 | G 注入新鲜度 | 在用（manifest 契约失配已消〔08-29 晚六题 g_context 零失配码〕；fresh pair 专项探针 09-02 ✅） | 老师体系问题，验 fresh pair | [../design/g-cognition.md](../design/g-cognition.md)；CC 收口 b2da8d9c |
| 知识脑 knowledge_brain | 方法论知识卡 | 问询验收中（09-04 接口B read_shared_brain 上线〔12 只读工具，c1da3ea〕+件3 已 apply〔40 卡：38 卡带激活词+三新卡，施工门 14 发现 12 采纳，幂等复验过，三新卡实弹第一顺位点亮〕+压测验收门 PASS〔双腿 12/12+四维不退化，台账 state/analysis-mindset-stress-20260904*/〕；09-06 persona-gov 三卡入册〔管理层检查/护城河验证/反向预期，43 卡：词面重叠零冲突+幂等复验+激活探针正3负2含挤占全过+双腿抽查卡命中〔CC+cmd〕，台账 state/fin-analyse/persona-governance-3cards-20260906/〕；残余=finq 真实使用记账照常） | 方法论类问题，验 read_shared_brain 被调+卡命中+边界收紧 | [analysis-mindset-v1](../design/analysis-mindset-v1.md)；D-039；seed_methodology_qa.py（541368d8） |
| 薄 server 装配 | 十三工具可用性（12 只读+1 写，单缝失败隔离降级） | 在用 | 任一问询，验 gaps 可查 | read_capabilities/ |
| 回放证据层 cognition-replay-facts | G 认知线市场验证的接续/口径（验证会话手动 CLI，不入问询链） | 文件层（设计门 glm 替补 18 发现全采纳；8 月批次对拍 11/11+幂等/护栏探针绿+单测 30 绿；09-06 首批扫批已落回放线正文〔1 supports/5 open〕+窗口已钉（区间类 9/23、方向类 9/30，owner 09-06 拍板）+扩篮子拍板不扩；09-06 起事实层每日 23:00 systemd user timer〔fin-cognition-replay-daily，owner 拍板授权，交易日门+硬错误守卫，首验=09-07 23:00 后核 daily.log〕；下次 ingest 双触发自动重建 readmodel） | 验证批次跑 snapshot/nominate CLI 对拍黄金值 | [design/cognition-replay-facts](../design/cognition-replay-facts.md)；台账 design-gate/cognition-replay-facts-20260906/ |

### 其他产品面

| 产品面 | 状态 | 验收手段 | 指针 |
| --- | --- | --- | --- |
| Daily 简报 | 问询验收中（**09-01 起 D-030 停推**，复验并入 D-031；带伤班次主因 BUG-015/016/017 已修并经 09-01 morning 真实班 gaps=[] 确认；盘前概览 gap 已随 BUG-002 09-04 闭环消解〔盘前形态=设计内 PARTIAL〕；更早施工叙事入 Git/BUGS） | 四班交付记录 + B1 盲评 | 【最后】D-031（最后9）；[../design/daily-delivery.md](../design/daily-delivery.md)；BUG-002/008/015/016/017 |
| 裁决收件箱 | 运行态（09-06 建〔D-051〕：`fin-adjudication` CLI + SQLite 收件箱 + 工作日 09:00 飞书摘要 timer；首推冒烟真发+指纹去重过；v0.3 producer×3=G 标注批次 + 回放线提名 + 评分 needs_review 闭环（D-051 追记 #2/#3；owner 定位=只放裁决入库生效类）；箱内真实项 2（G 批次落后 2 天/3 篇；评分 needs_review 积压 179 条；回放批 20260904 已落账正确不开项）；裁决执行留在各功能原确认面） | `fin-adjudication list`；`journalctl --user -u fin-adjudication-digest` | 短设计按规则 5 归档（git 史 adjudication-inbox）；D-051；数据=$STATE/fin-analyse/adjudication-inbox-v1/ |

## 待办队列（只放未决项）

| 位置 | 序 | 事项 | 等谁 / 何时 |
| --- | --- | --- | --- |
| 旁路·排后 | 27 | 分时/盘中数据（规模大源不稳）；消息时间线视图（先重读 2026-09-03-point-line-plane-survey 评估 B1 剩余价值） | 排后，准备期主两项完成后 |
| 旁路·owner | 4 | 决策日志 v1 收尾：施工全清（09-04 合入 b72ddf5：设计门 345s/10/10 + 施工外审 474s/7 发现/6 采纳、1 P2 同根裁决；231+全仓 3141 绿 + 实弹 18/18；人格规则 8 已增补）。会签两项 ✅（09-05 owner 签：人格计数行 13+2 追认、README 冻结行整行重写并注记会签）。剩 owner：复盘问询探针（「当初为什么买 X」，随真实使用，finq 记账） | owner 随用 |
| 旁路·owner | 26 | 人格 r18 瘦身实弹复核：419→407 净 -12（删 v2–v4 历史 bullet+v5 出处叙事，修订史唯一存活载体=backups/CLAUDE.md.20260905-r15；判定口径并入规则 2/5 独有条款保留）；回归探针通过（09-06 q1–q6 全 PASS，预期跟踪位 CU 泄漏裁定在案 flash 缺陷非回归）；八股化复发则整包回滚 backups/CLAUDE.md.20260906-r18-pre-slim | owner 下次实弹使用 |
| 旁路·owner | 0 | BUG-024 盘前读法实弹终验：v3 人格增补+主指数日线 lane（09-04，96f8fcd）+细分板块 lane（09-05，ff372bd）均施工全清，剩 owner 实弹验「线层现工具序列或诚实标注」；09-07 收盘后另核板块 lane 当日 bar 即时性（评审 Q2-P2，见 BUGS BUG-024） | owner 下个交易日（09-07 周一）盘前+盘后 |
| 旁路·owner | 2 | finq 记账（D-043/C3）：y 记一字、n 必须一句原因（owner 纪律，不加校验）；存量 3 条历史缺口接受（不回填 append-only 台账），自 D3 起新账强制 | owner 随用 |
| 旁路·时间 | 3 | 评分边界样本：首篇 [6,7)（或 <6）新帖进自然窗口时，核 read_instrument_scores 时间线与 G/reference 窗口分级行为（D-033/036/037 已交付：registry 1629 条、增量门槛 6.0 走 config/zsxq_capture.json；09-05 夜核对：registry 1695 条、parser v3+增量接线落地（#26 出队）、首篇 [6,7) 边界样本已入册（603629@09-05 能量 6.8）——分级行为核对仍待做；设计 [../design/instrument-score-registry.md](../design/instrument-score-registry.md)、[../design/instrument-score-timeline.md](../design/instrument-score-timeline.md)） | 自然窗口到点核对 |
| 旁路·使用 | 34 | 元认知调节器已生效：stdio 按会话拉起（无常驻单元），新问询即带块；画像 v2 在 state、墙钟修复 448f862、A/B 单腿对比进行中（台账 meta-calibrator-ab-20260907，6/20 对） | A/B 全量报告已出（09-08）：20 对全成、零泄漏零反向、基线强+调节器边缘加固（背书滑移 1 处命中、q05 来源层级分歧待复跑归因）；台账 meta-calibrator-ab-20260907/report.md |
| 旁路·时间（09-07 23:00 后） | 33 | 回放证据层定时首验：核 daily.log 9/07 批次实跑非 skip（batch=20260907，nominations-20260907 落盘）；节假日空转 9/06 已实测 rc=0；取数全败=rc1 硬错误（守卫单测实证；代理×新浪兼容实测无碍）；首验过=删设计稿 cognition-replay-facts.md（家规5，Git 史 ab14afb 可考）+NOW L3 指针改指 Git 史 | 09-07 23:00 后 |
| 旁路·时间（09-23/09-30） | 31 | 回放证据层窗口到期：区间类 CHK-0811-02/0825-01 钉 9/23（G 口径碰头前终点）、方向类 CHK-0827-01/02-spread、CHK-0827-02-blowup 钉 9/30，到期后跑 nominate 出 relation（改判随时可做=retire+新 spec）；扩篮子已拍板不扩（09-06，中证银行单代码，mapping 留痕）；blowup 检查机器面 not_machine_v1，到期 relation=unknown 需 owner 供证据或接受 unknown | 09-30 后首个验证批次 |
| 旁路·owner | 32 | cmd 评审者 run 阶段 rc=3 排查（31h 两次失败：09-05 pre + 09-06 run）；交接稿 fin-data/handoffs/20260906-cmd-reviewer-rc3-run-failure-handoff.md；设计门结果不受影响 | owner 新会话 |
| 旁路·时间（09-06 起） | 30 | 评分增量接线实弹首验：首个采集 tick 后核日志 `[INSTRUMENT-SCORES]` 行与 registry 增长（2c8b3cf 接线+4e93495 v4 守卫；探针已幂等 added=0/updated=0；静默失败=注册表回淤，正是本次修的病） | 09-06 首个采集 tick 后 |
| 旁路·使用触发 | 28 | 语义查询（混合召回）评估已核（09-06）：trace 全量（08-27 起 1415 调用）read_article_search 184 次零 `article_search_no_match`、read_shared_brain 1/85、read_instrument_scores 3/66（已确诊=BUG-048 格式病非语义缺口）——现状无立项凭证；开工凭证=同义/换说法型零召回在 trace 或 finq 真实复发，届时首选 read_article_search embedding 召回+TF-IDF 精排（语料 1425 篇建库成本可忽略），g_context/ready_evidence 确定性分层不动 | 使用触发 |
| 旁路·使用触发 | 12 | 标签检索缝开工凭证：首条真实抱怨「翻星球内容而不得」（finq 记账） | 使用触发 |
| 旁路·P5 前 | 13 | Hermes 问询 agent 同源化设计（D-032 方案 A）：人格/工具/记忆三缝同源 + P1 六题级验收；飞书传输复用既有 gateway，不新建 | D3 之后、P5 前 |
| 旁路·owner | 16 | 直播总结入档后继：明日标注批次勾 9/4 锐评（as_of 已滚、从提名单隐去，从 index 直接勾；BUG-028 边界修复后后续批次自动可见；首例 09-04 已入档收口，git 55722a8） | owner 明日标注批次 |
| 旁路·owner | 18 | 主线效果盲评下轮增量：finq 并排记分 + 失败样本常驻（首轮 09-04 收口：主线腿双判者皆胜 CC +7.5 / J2 +5.0 per 200；预算决策按家规 11 不施工；台账 $STATE/fin-analyse/mainline-blind-eval-20260904/） | 随用随记积累后 |
| 旁路·使用触发 | 21 | 「断供 fallback 画像」开工凭证：真实断供发生或 owner 主动想用（届时从全库语料重编，不复用 guo:v0 快照——D-038 否决项） | 使用触发 |
| 旁路·时间（10-04） | 20 | BUG-031 档位口径样本复核：≥20 档位样本或满月先到先复核（凭 finq 记账与会话记录，只随证据改）。双验已闭环（09-05：CC 腿 + cmd 腿接力探针均 PASS，台账 $STATE/fin-analyse/bug031-band-freeze-probe-20260904/，撤行当日执行毕）；遗留跟踪：两腿档位数值分歧（CC 36.0~36.8 vs cmd ≈35.3，同结构不同 print）入样本复核 | 10-04 满月或样本先到 |
| 旁路·owner | 22 | meta 设施家规10 自审（D-043/C5）：决策日志 v1、run-design-gate、switch-codex-open-provider、manage-zsxq-capture、manage-zsxq-article-retirement、book-shared-brain-learning、外部审视链（codex_open.sh+双 profile）——无真实使用记录者入休眠候选清单 | 2026-09-18 |
| 旁路·随手 | 24 | 版本强势英雄补列进 G（D-044①）：source_contract 白名单补列走 45 天 G 窗口；09-05 审查实证 331 篇零车道，验收=该栏新帖进 G manifest | D3 建造静默结束后 |
| 旁路·随手 | 25 | 老师 Q&A 放行 reference 车道（D-044②）：reference 候选源照 ready_evidence 的 _QA_COLUMNS 闭集放行，zsxq_reference_windows.json 三窗口键复活；09-05 审查实证 18 篇零车道 | D3 建造静默结束后 |
| 旁路·随手 | 27 | run_ledger/alert 面量裁（D-044④）：补写入或按规则 12 废除，随 D-031 设计一并定；落定前 alert 链不可作为故障信号依赖 | 随 D-031 |
| 旁路·时间（09-07） | 28 | BUG-041 竞价窗口观测：盘前班次看 overview 诊断 JSONL，INDEX_TRADE_DATE_MISMATCH 整链拒坐实即按 BUGS 修 | 09-07 盘前班次 |
| 旁路·时间（12-01 前） | 29 | BUG-035 交易日历 renewal：生成 2027 artifact + runner/reconcile 到期前告警（CALENDAR_EXPIRING） | 2026-12-01 前 |
| 旁路·随手 | 31 | 日线第三源施工：短设计已备（docs/design/daily-bar-third-source.md，候选源待实弹核验，owner 09-05 拍板立项）；施工前跑设计门 | D3 建造静默结束后 |
| 旁路·使用触发 | 32 | 裁决收件箱 portfolio 接线（D-051 v0 砍出项）：持仓写路径回生产时挂 `save`/`confirm` 挂点，终态映射按 ConfirmStatus 8 值闭集（PUBLISHED/UNCHANGED→resolve，NO_PENDING_REVIEW/BUSY→no-op，其余不动） | 持仓写路径回生产时 |
| 旁路·会话 | 34 | G 批次 9/05-09/07 起草：勾选已锁定（keep 22/drop 3，台账 $STATE/fin-analyse/adjudication-inbox-v1/g-batch-selections.v1.jsonl 含逐条理由；「星大派锐评」一篇错标普通栏）→ 新会话按台账起草认知单元（摘录 span 逐字⊆原文+节点行）→ verify_mainline_annotation.py 机验 → owner 终审入档 → as_of 滚动自动消项；注意「完成 g.annotation_batch」不能替代入档（producer 会重开） | 新会话按台账直接开工，唯一人工=终审 |
| 旁路·owner | 33 | run-design-gate skill §0 增一行注册协议判据（D-051：新增 preview→确认/提名→扫批/需人工确认警告面必须接裁决收件箱 seam）——skill 文件本机未定位到（workspace/用户 skills 均无），owner 指认实际落点后补 | owner 指认 skill 位置 |
| 旁路·owner | 35 | 行为纪律护栏常态（施工+三腿基线+owner 终裁 09-07/08）：audit.tsv 按期人工裁决（三腿累计 20+ 行命中，flash/zcode 泄漏族同弱、生产腿干净）；人格修订走 r20 闸（persona_regression+behavior smoke 并列，改判断循环/动作合同/输出纪律节必跑 smoke、换模型/版本钉跑 full）；出闸判定生产腿复跑。三腿基线台账=\$STATE/…/behavior-regression/{20260907-002342(flash),20260907-052216(生产),20260907-235133(zcode)}/adjudication.md——zcode：判断类轴全强（G-first/带冻结最强措辞/追问稳定揪出挂错记录）、泄漏族与 cmd-flash 同弱、mainline 豁免口径待终裁；批跑承载定版=systemd 用户单元（会话后台与 setsid 三次被环境回收的教训，unit=behavior-zcode-batch）；设计稿已按家规5删转 Git 史（7f8de2a 起、344e944 终版、43472b3 收官） | owner 按期：三腿台账终裁（重点=mainline 豁免口径） |
| 最后 | 9 | D-031 Daily 生成器换问询环境（owner 09-01 指示先聚焦手动 CLI；骨架稿 docs/design/d031-daily-consult-env.md 备好）；BUG-016/017 盘后复验、黑话译注推送侧生效（一期 a06db30 / 下批 ea220af 已施工）均并入本项验证；BUG-042/043（窗口外补投出口、CLAIMED 专码）量裁随本项 | owner 指示恢复推送后 |

## 遗留观察（诊断/环境，上限 4 条）

1. release/gateway 运维判读：碰 release 树一律 `-B`（pyc 三来源污染）；gateway journal 近零日志是常态，判卡死先查 state.db 与官方历史。
2. codex CLI 0.149.0 静默忽略带引号的 `-c` 值 → 401；手动入口 `-c` 必须写 TOML 裸值。
3. fin-core 的 `fin_analyse` 是无 `__init__.py` 的 namespace 包：从**任何别的含同名包的 cwd**（旧例=旧仓）以 stdin 跑一次性诊断会整包 import 异源代码（旧逻辑+异源 `.env` 解键，结果看似正常实则错源）→ 诊断脚本一律文件模式跑 + 显式注入 `FIN_LLM_ENV_FILE=~/.config/fin-analyse/llm.env`（直指目标，**不经旧仓 `.env` 转引**；2026-09-03 老仓可删审计后旧仓随时可退役）。
4. 共享 checkout 并行会话的 `git add -A`/`git add .` 会把**他人已暂存未提交**的文件扫进自己的提交（2026-09-04 实证：并行 docs 提交 673ad12 扫入决策日志施工主体 10 文件，提交叙事劈裂，裁决录后记留证）→ 本仓一律 `git add <显式路径>`；暂存后尽快提交不留过夜；发现 HEAD 漂移先 `git log` 对账再续写。
