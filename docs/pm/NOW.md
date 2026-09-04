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
> 最后核对：2026-09-04（Asia/Shanghai）。

## 生产声明

旧飞书/Hermes 咨询入口已停用（2026-08-27 拍板，允许报错不可用）；gateway 本体
（飞书 WS，Hermes venv）继续运行未动。当前生产：Daily/ZSXQ 单元与薄 server =
`~/fin-core`（consult-agent/.mcp.json + systemd 单元，2026-08-29 步5 重指向）。
Daily 四班推送 2026-09-01 起停用（D-030，8 个 systemd timer 已 disable，
单元与 durable 状态机保留，可一键恢复；ZSXQ 采集不受影响）。
老仓 `~/fin-analyse` 已归档（2026-08-29 步7）；release 退役后保留 `current`
（→`319faf62`）+ `ff7441e2`（BUG-002 回滚候选）+ `13c791ca`（Daily 脱钩回滚候选）
至 P5，其余 10 个已删；保留三个仅为回滚资产，无运行时读方。
2026-09-03 老仓可删审计：活跃引用清零（bashrc finlog 别名与 claude-mem
分支、`~/.local/bin/claude` 注入分支、codex-proxy 死常量、consult README
开发域指向全部改指新仓/删除），旧仓进入可退役状态；真删除仍按家规 4
先备份 + manifest。

## 板 A · 重构阶段（对齐 rebaseline §6）

| 阶段 | 状态 | 指针 |
| --- | --- | --- |
| P0 止血文档手术 | ✅ 完成 | f0b8b6b8 |
| P1 CLI 首链（D1 薄 server + D2 顾问人格） | ✅ 完成：六题 Q1–Q6 全过，codex/CC 双客户端接通 | [read-capability-server-design](read-capability-server-design.md)、[consult-agent-workspace-design](consult-agent-workspace-design.md)、[night-shift-report-20260827](night-shift-report-20260827.md) |
| W2 原地手术（备份/部署/Daily 脱钩/归档/L1 池） | ✅ 完成：生产 release `319faf62` | — |
| 路由重排 D-018/019/021 | ✅ 完成（文件层 + 运行态） | [../DECISIONS.md](../DECISIONS.md) |
| W2' 新仓移植（`~/fin-core`） | ✅ 完成：07 七步全清（2026-08-29，cutover 见 [../migration-manifest.md](../migration-manifest.md) 步4/5/6/7 记录） | ~~new-repo-migration~~（设计稿随老仓归档入 Git 史） |
| 外部项目吸收 | ⏳ 盘点+举证机制已闭环（09-03：五候选范围盘点、首轮盲评 118=118 零污染）；**吸收 0 项**——四候选挂观察名单等失效样本触发（finq n 即举证入库），过闸才施工；A2 typed 数据源未开 | [research/2026-09-03](research/2026-09-03-external-analysis-absorption-scope.md)、[盲评 pilot](research/2026-09-03-consult-blind-eval-pilot.md) |
| W3-4 深化调优 | ✅ 完成：二轮复盲评 7.59>7 闭环（08-31，55/56 票；GLM 缺票最坏 7.48）；01/03/05 调优已随二轮闭环收口；GLM 三节点已恢复（D-028 解除，9a0320f） | 台账 `$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/` |
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
| 顾问人格 | 全部问询的工具选择、证据纪律、输出格式 | 问询验收中 | 持仓类/老师体系类问题，验工具按规则被调；泛化体系题免提醒验个性化（如「小仓该不该更激进」，验答案自动带账户具体约束与刻度版本，owner 09-04 要求不依赖提醒；09-04 首枪过：泛化原题免提醒，自动带 4.3 万/12.6% 现仓/闲钱边界/35–50% 带+买腿先行（v5 装载即执行）） | consult-agent/CLAUDE.md；BUG-005/025/030（均已闭；030 真周五实弹复验 09-04）；BUG-031（档位口径统一+带冻结，09-04 施工：CC 腿探针 PASS，codex 腿待 finqa-x 恢复补验）|
| 问询模型/路由 | 答案质量、成本、时延 | 在用 | 任意问询 | config/llm.yaml；D-018/019/021 |
| 连续性/记忆 | 续问与跨会话上下文 | 在用（codex 客户端读不到 CC 记忆 = 已知边界） | 续问（六题 Q4） | consult-agent-workspace-design.md |
| 外部检索 | 时事与星球外信息 | 在用 | 时事类问题，验引用可溯源 | consult-agent/.mcp.json |
| 识图 | 图片理解 | 在用 | 带图问询 | llm.yaml vision 链 |

### L2 七个上下文缝（决定装了什么）

| 工具 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| read_g_context | G 主线证据注入 | 在用 | 老师体系覆盖的问题，验证据链 + 三维打分 | [../design/g-cognition.md](../design/g-cognition.md) |
| read_actual_portfolio | 持仓名称/现价/变化栏 | 在用 | 「分析我的持仓」 | [../design/portfolio.md](../design/portfolio.md)；探针 08-29 ok 无 gaps（BUG-001/008 已闭） |
| read_market_snapshot | 标的行情 + 主指数日线（大盘线） | 在用（09-04 指数 lane 上线〔BUG-024 材料层，96f8fcd〕：主指数名称/限定符查询给日线+技术因子，端到端「科创50」READY/120 bars/零 gap；个股语义零变化；此前 08-31 EASTMONEY f48 修复与 BUG-022 已闭） | 名称查「科创50」日线，验 bars+gaps 空 | [../design/market-data.md](../design/market-data.md)；BUG-011/022 已闭；git 96f8fcd |
| read_market_overview | 大盘结构 | 在用（09-04 09:07 盘前实弹闭环：整链拒未再现，PARTIAL+7 命名 gap〔5 常驻良性+2 盘前源数据缺席=f3/f6 占位与广度，源属性〕；owner 会话 1c718317 答案诚实降级质量在线。08-31 定修+09-01 gate5 两次修复生效） | 「今天大盘怎么样」，验 gaps 空 | [../design/market-data.md](../design/market-data.md)；BUG-002 已闭 |
| read_margin_evidence | 两融语义 | 在用（08-30 实弹闭环：全市场拥挤度语义生效，账户语义混淆清零） | 两融问题 | BUG-004 已闭 |
| read_ready_evidence | 当天高相关本地参考材料注入（非 G、非公告） | 在用（BUG-012 全链闭环 09-03：残余三投影门外审裁决 A 定修，端到端 RPC status ok/gaps=[] 三字段全过、真实 CLI 触发实证；宏观叙事帖可注入，映射类证据归 read_external_evidence；残余一券商通道为已知限制 P2-8） | 当天老师相关提问，验工具被调 + 有料则注入 | BUG-012 已闭 |
| read_external_evidence | 官方记录/公告证据（OfficialRecordEvidence） | 问询验收中（08-30 公告探针过：外搜带时点、持仓联动正确；现役面=外搜 MCP 辅助面） | 公告类问题，验工具被调 + gaps 空 | BUG-012 公告腿已闭 |
| read_user_watchlist | 自选股清单（user context 注意力焦点，永非投资证据；含 provenance/tags） | 在用（08-29 接入；09-01 加标签/来源投影） | 「看下当前自选股」，验工具被调 + 空表诚实答空 | 短设计已按规则 5 归档（git 历史：read-user-watchlist-tool、watchlist-tags-and-owner-profile）；写通道=manage_user_watchlist.py |
| update_user_watchlist | 自选股受限写（add/tag/remove；不得自动删除，remove 需用户明确指示；assistant 来源服务端强制；preview→apply 两段式） | 运行态（09-01 建；待真实问询使用） | 「把 XX 加入自选 / 给自选打标签 / 删掉 XX」 | 短设计已按规则 5 归档（git 历史：watchlist-tags-and-owner-profile） |

### L3 供给链（决定上面缝的数据质量）

| 环节 | 产品影响面 | 状态 | 问询探针 | 指针 |
| --- | --- | --- | --- | --- |
| ZSXQ 采集 | 知识新鲜度 | 问询验收中 | 验 G 工作集 fresh pair 含新文（无直接工具，间接缝） | [../design/zsxq-capture.md](../design/zsxq-capture.md)；BUG-003/006/027 已闭（027 审计链 09-04 实弹 chain_ready=true） |
| 入库/索引 | 检索命中一致性 | 在用（BUG-007 已闭：默认路径换缝 + repo 副本绝根 08-29） | 验 G/深化命中历史文章（间接缝） | BUGS.md BUG-007 |
| 文章标签 | 星球内容检索组织（尚无产品读方） | 观察期未接入 | 「翻星球内容而不得」即接入凭证 | 【旁路·使用】D-024 |
| 深化 deep-read | 文章支撑证据 | 在用（B2 二轮复盲评 08-31 闭环：7.59>7、逐字 63/63；残余缺陷面=模板噪声/主题簇误归类/量化锚点覆盖，见打分表2；空壳 0→3 修复实证） | 需文章支撑的问题，验引用可溯源 | [../design/deepen.md](../design/deepen.md)；B2 台账 `$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/` |
| G 准入/工作集 | G 注入新鲜度 | 问询验收中（深化第一刀后的 manifest 契约失配已消——08-29 晚六题 g_context 零失配码；fresh pair 专项探针待跑） | 老师体系问题，验 fresh pair | [../design/g-cognition.md](../design/g-cognition.md)；CC 收口 b2da8d9c |
| 知识脑 knowledge_brain | 方法论知识卡 | 问询验收中（09-04 接口B read_shared_brain 上线〔12 只读工具，c1da3ea〕+件3 已 apply〔40 卡：38 卡带激活词+三新卡，施工门 14 发现 12 采纳，幂等复验过，三新卡实弹第一顺位点亮〕+压测验收门 PASS〔双腿 12/12+四维不退化，台账 state/analysis-mindset-stress-20260904*/〕；残余=finq 真实使用记账照常；BUG-030 日历盲点 09-04 措辞级修复+真周五实弹复验过已闭） | 方法论类问题，验 read_shared_brain 被调+卡命中+边界收紧 | [analysis-mindset-v1](../design/analysis-mindset-v1.md)；D-039；seed_methodology_qa.py（541368d8） |
| 薄 server 装配 | 八工具可用性（七读一写，单缝失败隔离降级） | 在用 | 任一问询，验 gaps 可查 | read_capabilities/ |

### 其他产品面

| 产品面 | 状态 | 验收手段 | 指针 |
| --- | --- | --- | --- |
| Daily 简报 | 问询验收中（四班 L1 实证已通；B1 盲评 7.67<9 不闭环，同条件 9=9 打平、差距全在带伤班次——带伤主因 BUG-015 冻结时钟已修，08-31 postmarket 班实弹 gaps=[]+行情+G 对表齐活；gap 记账哑已修 08-30；08-31 G 认知接为第四材料键〔设计门 8/8 采纳〕+ 两融项删除、不催更新；BUG-016/017 已部署，**09-01 09:35 morning 真实班 gaps=[]+正文带指数点位/成交额 → 首次真实正文确认通过**；**09-01 14:20/15:30 close+postmarket 推送因工作树脏被身份门拒、已放弃补发**；D-030 09-01 停推，复验并入 D-031 验证；盘前概览 gap 随 BUG-002 09-04 闭环消解〔盘前形态=设计内 PARTIAL〕） | 四班交付记录 + B1 盲评 | 【最后】BUG-016/017；[../design/daily-delivery.md](../design/daily-delivery.md)；BUG-002/008 |

## 待办队列（只放未决项）

| 位置 | 序 | 事项 | 等谁 / 何时 |
| --- | --- | --- | --- |
| 主线 | 0 | BUG-024 修复闭环 ✅ 施工全清（09-04）：v3 人格增补（线的取材下限+动作-持仓前置，备份 r9/r10）；材料层指数日线 lane 上线（96f8fcd，设计门外审 480s/1P1/4P2/4P3 全采纳；端到端探针+个股回归过）；owner 终验 = 下个交易日盘前读法实弹（线层现工具序列或诚实标注） | owner 实弹终验 |
| 主线 | 1 | CLI 实弹三连验 ✅ 全清（09-04 收口）：BUG-005 G-first ✅（09-02 21:22）；3.2 fresh pair ✅（09-02 21:24）；BUG-012 ✅（09-03 19:36）；BUG-002 ✅（09-04 09:07 盘前实弹，整链拒未再现，PARTIAL+命名 gap=设计内形态，BUGS 已闭） | 完成，出队 |
| 主线 | 2 | finq 使用日志：09-02/09-03 六条已回填（y=持仓分析、锐评参考探针；n=长电封测〔BUG-024：v2 点线面泛化已施工，线/面双探针验收达标〕、星球研报探针〔BUG-012 已闭〕、大盘〔BUG-002 已闭〕、宏观〔原因待补〕；--help 噪音行已清（09-04 存量条与 CC 误触条一并移除））；09-04 盘前读法一条已回填（BUG-024 样本#1：线层取材双重缺口，见 BUGS.md）；09-04 BUG-030 复验探针两条已回填（y；真周五同题复验，台账 bug030-calendar-fixprobe-20260904/）；剩余 3 条 n 原因各一句待 owner | 随用随记；原因补注待 owner |
| 主线 | 3 | 标的评分维护列表 + ZSXQ 窗口分级：已交付（回填 1629 条〔index 内全部 ≥6 可解析：5/13 起 178 篇老图/旧 OCR 已定向识图收口、存量代码↔名称错位已按名册清零〕、read_instrument_scores 时间线 + read_article_search 双查已接 thin server、G/reference 窗口分级落地；增量门槛 6.0 走 config/zsxq_capture.json〔D-036/037〕）；09-03 实弹 sync 过（8/29 长电 8.6/8.8 锚 + 7/7 参照）；首篇 [6,7) 或 <6 新帖边界样本待自然窗口 | 排期见 [../design/instrument-score-registry.md](../design/instrument-score-registry.md) |
| 主线 | 3.1 | 宏观统一接口 A：read_macro_brain 已注册；owner 校准 v0（12 普通保留 + 每日热点）已落 config/macro_brain_rules.json；macro_index 09-03 12:58 已生成（22 条：12 kept + 8 每日热点 + 2 新规则命中），reader 索引优先已生效；宏观问询实弹 ✅（09-03 19:2x read_macro_brain 被调 status ok/gaps=[]，答案融合星球宏观材料 + 外搜核验带时点） | 已交付；设计 [../design/macro-brain-interface-a.md](../design/macro-brain-interface-a.md) |
| 主线 | 3.2 | G 工作集 manifest：已修 READY + sources_changed 清（0186462）；剩余 fresh pair 专项探针 | 随主线1 真实问询 |
| 主线 | 3.3 | ZSXQ 评分时间线 v2（D-037）：门槛 ≥6（config/zsxq_capture.json）+ parser v2 + 回填 445 行 + published_at 排序 + CLAUDE 纪律已交付（6273ab8，09-03 12:58 实弹 sync 过，Windows 单侧无同步项）；首篇 [6,7) 边界样本待自然窗口 | 随自然窗口（预计数日内）；交接稿 [../design/instrument-score-timeline.md](../design/instrument-score-timeline.md) |
| 最后 | 8 | BUG-016/017 盘后 Daily 复验：D-030 停推后窗口失效，并入 D-031 验证 | D-031 实施时 |
| 最后 | 9 | D-031 Daily 生成器换问询环境（owner 09-01 指示先聚焦手动 CLI） | owner 指示恢复推送后 |
| 旁路·使用触发 | 12 | 标签检索缝开工凭证：首条真实抱怨「翻星球内容而不得」（finq 记账） | 使用触发 |
| 旁路·P5 前 | 13 | Hermes 问询 agent 同源化设计（D-032 方案 A）：人格/工具/记忆三缝同源 + P1 六题级验收；飞书传输复用既有 gateway，不新建 | D3 之后、P5 前 |
| 旁路·owner | 14 | 黑话译注下批消费方 ✅（09-03 晚：Daily `_render_g_context` 显式加译注段〔96 绿〕+ mainline 投影侧确定性附加〔不动 PIT 工件 schema，57 绿〕；推送侧实际生效仍待 D-031 恢复）；一期三落点已在用（a06db30） | 已交付；D-031 骨架稿 docs/design/d031-daily-consult-env.md 备好待 owner 恢复指示 |
| 旁路·owner | 16 | 直播总结首例入档 ✅（09-04 晚 owner 按建议终审「全保留」：CU-0904-01/02/03 archive-only 入档，机验 PASS，readmodel gen47，S-0904B=SPOKEN_FAN_TRANSCRIBED，投影排除实证）。已知副作用：as_of 滚 09-05 → mainline 投影 PIT 空窗至今晚 24:00 自愈（g_context 其余面正常）。剩：明日批次勾 9/4 锐评（as_of 已滚，它从提名单隐去，从 index 直接勾） | 明日标注批次 |
| 旁路·随手 | 15 | consult-agent README/人格工具计数过时：写「7 只读+1 写」，thin server 实为 12 工具；README 该行带「冻结契约」标注，改数需 owner 会签 | owner 会签后改 |
| 旁路·时间（周末） | 17 | opencli v1.8.6→v1.8.7 升级（Windows 侧全局 npm，扩展 v1.0.23→v1.0.24 一并）；升级后 `opencli doctor` 核两 profile 连接 | owner 09-04 指示放周末 |
| 旁路·随手 | 19 | Windows 侧 `cleanInlineArticleText` 与 WSL `_strip_disclaimer_line` 声明语义对齐（帖首/帖尾双形态；BUG-027 设计门发现6，detail 腿 browser.py 已确认方向一致不受影响） | 随手 |
| 旁路·owner | 16 | G 主线生长管线 v1（D-038）：候选扫描（来源门+article_ref 去重，纯读不写 KB）→ CC 起草（摘录 span 机验+混合材料逐段归属）→ owner 扫批入档；含缺口 B 修复（标注 hash 独立触发 rebuild，不再等下次 ingest）+ 文件名去日期化 + 消费探针（投影附件带 unit_id 审计行）；动 durable state，开工前短设计稿（规则5） | 已交付（09-04：五部件施工+实弹全过，含 reader 装配缺口修复；设计稿及裁决录/施工记录见 git 史 3332912 一带）首链已走通（09-04：13 篇 18 单元入档 generation 44，探针投影+时间线可见）） |
| 旁路·owner | 18 | 主线效果盲评（D-038）：首轮已出结果（09-04，双判者盲评解盲）：主线腿两判者皆胜（CC +7.5/200、J2 独立 +5.0/200，leg 级 13/16），增益集中在历史纵深（q5）与时点语义（q6）；预算淘汰未获伤害证据→放大预算/基线常驻按家规 11 不施工；台账 \$STATE/fin-analyse/mainline-blind-eval-20260904/（含 run1 作废留证） | 首轮收口；下轮增量=finq 并排+失败样本常驻（随用随记） |
| 旁路·使用触发 | 19 | 「断供 fallback 画像」开工凭证：真实断供发生或 owner 主动想用（届时从全库语料重编，不复用 guo:v0 快照——D-038 否决项） | 使用触发 |
| 旁路·时间（10-04） | 20 | BUG-031 档位口径复核：≥20 档位样本或满月先到先复核（凭 finq 记账与会话记录，只随证据改）；finqa-x 恢复后补发 codex 腿探针（探针题与判据在 BUGS 条目；台账 $STATE/fin-analyse/bug031-band-freeze-probe-20260904/） | 10-04 满月或样本先到；finqa-x 恢复即补发 |

## 遗留观察（诊断/环境，上限 4 条）

1. release/gateway 运维判读：碰 release 树一律 `-B`（pyc 三来源污染）；gateway journal 近零日志是常态，判卡死先查 state.db 与官方历史。
2. codex CLI 0.149.0 静默忽略带引号的 `-c` 值 → 401；手动入口 `-c` 必须写 TOML 裸值。
3. fin-core 的 `fin_analyse` 是无 `__init__.py` 的 namespace 包：从**任何别的含同名包的 cwd**（旧例=旧仓）以 stdin 跑一次性诊断会整包 import 异源代码（旧逻辑+异源 `.env` 解键，结果看似正常实则错源）→ 诊断脚本一律文件模式跑 + 显式注入 `FIN_LLM_ENV_FILE=~/.config/fin-analyse/llm.env`（直指目标，**不经旧仓 `.env` 转引**；2026-09-03 老仓可删审计后旧仓随时可退役）。
