# FIN bug 台账（跨会话 · 跨 agent 共享追加目标）

> **协议**（2026-08-28 建）：CC / codex / opencode 任何会话遇到 bug 随手追加一条，
> 编号递增（BUG-NNN，新号=文件当前最大号+1，勿凭记忆取号），不复述 NOW.md / DECISIONS.md 内容；已关闭条目不删
> （Git 即归档，不在本文件做归档仪式）；同一 bug 复发在原条目下追加一行「复发」。
> 条目格式：一行症状 + 根因 + 修复 + 状态，能短则短，不建追踪设施。

## BUG-001 问询答案编错持仓中文名（000657→「中金岭南」、600879→「航发科技」）

- 发现：2026-08-28，CLI 问询「分析持仓」答案把 000657 写成中金岭南（实为**中钨高新**）、
  600879 写成航发科技（实为**航天电子**），用户误以为多了一只不存在的票。6 只持仓名错 2。
- 根因：`portfolio_review._canonical_instrument` 用带后缀代码（如 `000657.SZ`）查只按
  裸 6 位码索引的 `knowledge-base/runtime/a_share_name_map.json`，永远查空、静默回落
  → 确认链路落库 `name=裸代码`；问询模型拿不到权威名，凭参数记忆补名。
- 修复：代码分支改查裸码，抽 `_directory_display_name` helper（异常回落语义不变）；
  补裸码/后缀码→中文名两向回归测试（`tests/portfolio/test_portfolio_review.py`；
  portfolio + context_reader focused 149 绿）。该方向此前零覆盖，是漏网原因。
- 状态：代码修复完成（2026-08-28，工作树）；存量库 name 回填随同日持仓确认更新关闭
  （全量替换落库，5 只持仓均带目录权威名）。

## BUG-002 大盘概览结构性缺口 + 行情快照容量耗尽（问询「最大数据短板」根因）

- 发现：2026-08-28，CLI 持仓问询。trace（~/fin-data/trace/read-capability/calls.jsonl）
  08-28T01:16:52 read_market_overview gaps=[MARKET_OVERVIEW_SECTION_COVERAGE_INVALID,
  MARKET_OVERVIEW_UNAVAILABLE]（延迟 5.8s）；01:18:14 read_market_snapshot gaps 含
  ON_DEMAND_MARKET_CAPACITY_EXHAUSTED（延迟 14s）→ 板块宽度/部分标的无新价（002015 用 8-25 价）。
- 根因：未诊断（section coverage 校验失败是结构性缺陷，非盘前时点效应；capacity 额度策略待查）。
- 修复：待办——修复方案落 market-data 特性设计页（docs/pm 特性设计页已建），排 W3-4 深化。
- 诊断+修复（2026-08-28 晚）：**容量半边已根治**——trace 复盘（43 调用仅 3 次
  触发、全是 snapshot、延迟整齐 12-14s）+读码钉死：非负载超限，是嵌套预算
  自挤占——每 symbol worker 最坏向 detail executor 提 3 任务（quote+日线+30min），
  5×3=15 > 旧额度 10 且无队列，上游慢时后提交被拒误标 CAPACITY_EXHAUSTED
  （symbol 层 5 提交 ≤ 10 排除顶层溢出；deadline 路径标注正确排除误标）。
  修复：detail max_outstanding 10→20 配平 + 执行器暴露配额属性 +
  `test_on_demand_executor_budget.py` 钉死配平不变量；语义不变（容量拒绝
  仍是诚实降级）。详见 market-data.md「已知故障与设计回应」。
  **结构性半边诊断完成（2026-08-28 晚）**：晨间留存工具输出（consult 会话
  8b0b5487，09:07:14）钉死机制——五个 ranked section 全部
  `valid_projected_rows=0/returned=100`、`missing_timestamp_count=0`、reasons
  全 `PROJECTED_ROWS_MISMATCH`（标识/时间戳字段齐全，唯行情数值字段缺失）。
  根因：东财 clist 盘前（PRE_OPEN）对行情衍生字段 f3/f6 返回 fltt=2 缺失
  占位 `"-"`，`_project_board/_project_equity` 整行投影失败 → coverage 门
  （current_overview.py:822-837）any-reason 即全链拒。失败仅 08-28 09:07/
  09:16 两条，同日 14:25 与前夜 23:19 同参数全过（calls.jsonl 七条）——
  每交易日盘前确定性复现，非偶发；与读侧 `LATEST_COMPLETED_SESSION` 语义
  自相矛盾（意图取上一完成交易日，却因盘前形态拒之门外）。实证：复现脚本
  三段闭环（盘后基线全过/f3 或 f6 换 `"-"` 全 section 复现 valid=0/端到端
  gaps 逐字一致），命令与回应方向见 market-data.md「已知故障与设计回应」。
  施工仍排 W3-4。
- **定修落地（2026-08-31 盘中会话，窗口项补做——09:00-09:20 窗口漏执行，
  10:40 起盘中补）**：候选 b 收窄版全链落地（语义与实证见 market-data.md
  「overview 盘前整链拒绝」条目定修段）。诚实分级：单测钉死 08-28 生产
  占位形态（回归三测）+ 盘中实弹 read 无回归 = 「跑通」级；**盘前生产
  实弹确认待 09-01 08:55 premarket 班**（`l1_material_market_overview_
  unavailable` gap 消失即闭环，需部署先于该班次）。部署前提：单元重渲染
  （运维铁律）。**部署已完成（08-31 12:05，97a9c74：post-commit 钩子重渲染
  + daemon-reload + LLM 快照；uv sync 零变更；盘前班次入口复核过）；随后
  c473ad4（仅测试修复）钩子已再渲染，生产行为不变**。
  （该确认点已于 09-01 08:55 实弹执行，结果见下一条，以最新状态为准。）
- **09-01 08:55 盘前实弹（未闭环）**：premarket 班正常 PREPARED/DELIVERED，
  但 `l1_material_market_overview_unavailable` gap 仍在（同日 09:35 morning
  班 `data_gaps=[]`、正文带指数点位，盘中侧正常）。scheduled 入口
  `redirect_stderr` 吞掉 provider warning，服务侧 data_gaps 无任何落盘 →
  根因不可见。已加失败诊断 JSONL（`$XDG_STATE_HOME/fin-analyse/
  daily-workspace-overview-failures.jsonl`，仅 UNKNOWN 写），下一盘前班次
  定根因。候选根因（未证实，不按假设修）：盘前 PRE_OPEN 指数走腾讯实时
  （昨收值）但 f124 为当日盘前时刻 → LATEST_COMPLETED_SESSION 的
  trade-date/15:00 门拒链；或腾讯行盘前缺 f3/f6 → 回退东财盘前占位 → 指数
  投影失败。
- **修复尝试 2（09-01，按设计语义修，非按假设修）**：设计承诺「降级源的
  行与时间戳同步退出全部下游门（trade-date/session/age/provider_updated_
  at）」，但实现 gate 5（section trade-date）仍检查被投影丢弃分节的时间戳
  日期——盘前占位行（f3/f6="-"，全部分节被丢弃）若带盘前当日时间戳即整链
  `MARKET_OVERVIEW_SECTION_TRADE_DATE_MISMATCH` 拒绝。修复：仅存活分节
  参与 trade-date 门；回归测试钉死「盘前被丢弃分节带盘前时间戳 → PARTIAL」。
  同日另加失败诊断落盘，二者同批部署。
- 状态：修复尝试 2 已部署（诊断 + gate 5）；D-030 09-01 停推后无自动盘前窗口，
  复验改手动 CLI 盘前实弹（NOW 主线1）；若仍失败，诊断 JSONL 直接给出剩余门名，
  按证据再修。

## BUG-003 ZSXQ 问询空返回（2026-08-28 二次诊断改写，原「停更」结论有误）

- 发现：2026-08-28，CLI 持仓问询 read_ready_evidence 恒空。**首诊误判为「认知仓停更 22 天」
  ——查的是仓库侧旧副本**（repo knowledge-base/runtime/cognition/zsxq_sources.jsonl，
  103 篇停在 08-06）。生产真仓在 shared 根（~/.local/share/fin-analyse/shared/knowledge-base/
  runtime/cognition/zsxq_sources.jsonl）：127 篇、最新 08-27、当天仍在写入——爬取/入库全活着。
- 根因（真）：新鲜度窗口分级判据错——按栏目分（锐评 2 天、特刊 20 天），不按内容性质分。
  锗 6 篇（最新 08-13）、钨 13 篇（最新 08-19）多在锐评/普通栏，2 天窗口全杀 →
  g_context_no_relevant_items。见 BUG-006。
- 附带发现：仓库侧 knowledge-base/ 存在生产 shared 根的陈旧副本，误导诊断；是否仍被
  读取需核（a_share_name_map 等默认路径都指向 repo 侧）。
- 状态：开放（并入 BUG-006 一并修；repo 侧陈旧副本单列核实项）。
- 状态收尾（2026-08-30 补账）：已关闭——根因「新鲜度窗口分级错」修复随 BUG-006③
  全链落地并经 08-29 08:51 过渡期结束确认（manifest 重生、锐评 2 交易日/特刊 30 天
  窗口投影实证）；「repo 侧陈旧副本」核实项随 BUG-007 关闭（08-28 清出 + 08-29
  迁移步6 绝根，读方经 knowledge_root 单缝指 shared 根）。ZSXQ 新鲜度当期验收
  （fresh pair 专项探针）由 NOW 板 B「G 准入/工作集」行跟踪，非本条遗留。

## BUG-006 ZSXQ/G 相关性选段三层缺陷（owner 2026-08-28 定调，部分已修）

- 发现：持仓问询 read_ready_evidence 空。复现+读码定位三层：①read_ready_evidence 只传
  tickers，而 position→topic 推断只在 positions 字段跑（runtime_context.py:1553）——ticker
  的主题推断从未执行；②_POSITION_TOPIC_RULES 仅 6 条旧持仓词（钨/稀土/紫金/钼/锡/电池），
  当前 5 只持仓零覆盖，「锗」「钽」不在 _TOPIC_KEYWORDS；③新鲜度按栏目一刀切。
- 已修（2026-08-28）：ticker 同跑 _infer_position_topics；规则表补 002428/000962/601138/
  002709/600879/605376 六条；关键词补锗/钽/稀有金属等。复现实证：同题 0 条 → 5 条
  （含 08-13 锗特刊、08-27 锐评）。provider 测试 135 绿（semantic_state 37 败为并行 W2③
  归档余波，先于此改动存在）。
- 遗留（owner 已定方向，W3-4 实施）：窗口分级改按内容性质——锐评=2 个**交易日**（跳过
  节假日，现实现疑似自然日需核）；星大派特刊加长；普通研报再加长但设上限（CC 建议：
  特刊 30 天/普通研报 45 天，owner 拍板）；_EXCLUDED_COLUMNS 整体排除「普通」栏需按
  研报分类放行；_POSITION_TOPIC_RULES 属「会变的选择」应配置化（家规 6）。
- 状态：部分关闭（①②已修未提交；③开放排 W3-4）。
- 修复进展（2026-08-28 晚，③实施，设计门已过）：①②已随 375e8ce1 入库
  （原「未提交」状态作废）。③全链实施——单源窗口配置
  `config/g_context_windows.json`（新缝 `guo_teacher_research/window_config.py`：
  锐评=2 交易日、特刊 30 天、普通 45 天、历史 60 天；owner 未拍板数字落配置可改，
  缺文件=内建默认、文件非法=响一声回默认）；锐评窗口交易日语义两层共用
  （g_working_set 准入层 + runtime_context 选择层，日历无管辖权时段静默回落
  自然日旧政策、工件缺失才响）；普通栏经 `classify_g_source` 全链放行
  （非 QA teacher_original=general G 第三桶按相关性入预算；QA 仍排除
  `g_source_ordinary_qa_excluded`；reference 层改按 source_classification 分流）；
  `_POSITION_TOPIC_RULES` 配置化 `config/position_topic_rules.json`（进程级
  快照、匹配零 IO）。codex-glm 盲评 12 发现全采纳（裁决落设计稿），测试
  guo 152/scraper 661 绿。manifest 由 capture 链下次 publish 重生成，过渡期
  sources_changed gap fail-closed；运行态随下次部署。设计稿合入即归档。
- **过渡期结束确认（2026-08-29 08:51）**：ff7441e2 部署（08-28 晚）后历经
  20:20 长跑 timeout→20:53 coalesced 退出、manifest 未重生的一夜；08-29
  08:30 manifest 由 capture publish 重生（status=READY、data_gaps=[]，
  `active_window={commentary_days:2, general_days:30}`＝新配置
  `commentary_trading_days`/`special_report_days` 的投影，锐评 2 交易日 ✓
  特刊 30 ✓）；canonical sha 与现源文件（index.json/priority_events.jsonl）
  双双 match → sources_changed 不再触发；`_validated_manifest` 通过。
  附诚实更正：08-28 晚两轮复查中 CC 以 raw bytes 误调 `_validated_manifest`
  （其参数为 Mapping）得到恒 None，据此说「旧 manifest 被读侧拒绝、
  fail-closed」不成立——真正的过渡判定是 canonical sha 比对，昨晚旧
  manifest 是否显 gap 未被正确证实；本条以 08-29 上午正确姿势实测为准。
  另：夜间 current 已前进至 319faf62（文章标签系统部署，非本条目操作），
  poller 今晨轮次全绿。
- 状态收尾（2026-08-30 补账）：已关闭——①②（375e8ce1）+ ③（窗口单源配置/交易日语义/
  topic 配置化）08-28 晚全链施工，08-29 08:51 过渡期结束确认（manifest 重生、窗口
  投影与 canonical sha 双 match）；后续 owner 撤项（08-28 深夜）把普通栏 G 准入整体
  撤销回 reference lane，成果保留、general_admission_days 移除；普通栏供料后由
  BUG-012 第二刀 canonical index.json 接管（08-30）。G 工作集新鲜度当期验收（fresh
  pair 专项探针）由 NOW 板 B「G 准入/工作集」行跟踪，非本条遗留。

## BUG-004 read_margin_evidence 工具描述语义写反（账户两融 ≠ 全市场两融）

- 发现：2026-08-28。server.py _TOOL_DESCRIPTIONS 写 "margin (两融) evidence **for the
  account** and given instruments"，实际实现是全市场两融数据（fin_analyse/margin/eastmoney.py，
  用户引入意图=大盘杠杆资金拥挤度预警，不用账户融资）。模型据此答「你融资余额为零，跳过」，
  该能力从未被用于杠杆拥挤度分析。
- 根因：描述文本在能力登记时写歪。
- 修复：待办——描述改写为全市场两融语义（一句话改动，随下次 release 生效）。
- 状态：开放。
- 状态勘误（2026-08-28 晚）：已关闭——修复随 commit 9a70c149 入库
  （描述改写 + test_tool_descriptions.py 钉死语义），生效随下次 release；
  关闭详情记录在 BUG-007 条目末段（当日误记于彼处）。
- 实弹闭环（2026-08-30 晚）：两融探针过——read_margin_evidence 被调，
  答案全程全市场杠杆拥挤度语义（2.65 万亿余额/一年分位/加速度判拥挤），
  并正确点出「账户无融资负债，两融只是系统性风险温度计」；残余 gaps
  仅两条已知类型化边界（ACTUAL_ADVISORY_PORTFOLIO_STALE 组合数据时点、
  MARGIN_EVIDENCE_BJ_NOT_COVERED 北交所不覆盖），非语义缺陷。

## BUG-005 问询模型编造「不调 G 的既定口径」（违反人格 G-first 规则）

- 发现：2026-08-28。持仓问询（6 只具体标的）未调 read_g_context，答案自称「按既定口径，
  组合整体回顾题用持仓+行情作答，不调 G」。人格文件（~/fin-data/consult-agent/AGENTS.md
  §110/112/124）明确相反：具体标的/行业题相关**或不确定**都先调 G。同日另发现人格文件
  是有的、规则是对的 → 模型在无强制约束处自行发明口径。
- 根因：待核——该问询会话是否真正加载了 consult-agent 人格（若 cwd 不在 consult-agent
  home，人格可能根本没进上下文）；codex/CC 两入口都要核。
- 修复：待办——核对问询入口的人格注入路径；确认注入后若仍违规，人格加一行「数据缺口
  必须报告为数据不可用，不得当成『不存在』陈述」。
- 状态：开放（与 BUG-001 同模式：无数据/无约束处模型自行补——名字、口径都会编）。
- 修复进展（2026-09-01）：owner 拍板口径改为「分析必调 G、纯查询豁免」——CLAUDE.md
  判定口径与 server.py 工具描述（read_g_context/read_actual_portfolio）同步更新，
  原「组合整体回顾/纯大盘不调 G」豁免废除，模型不再有可借用的"既定口径"；待实弹
  复验后关闭。
- 补充（2026-09-01）：唯一追加豁免 = 用户明确说不用 G 认知时（"不用老师视角/不套老师
  框架"），默认先调语义不变。

## BUG-007 知识根双轨：repo knowledge-base/ 旧副本 + 38 处模块级默认仍指仓库侧

- 发现：2026-08-28（当日两次误诊同源）。生产真仓 = ~/.local/share/fin-analyse/shared/
  knowledge-base/（runtime/knowledge_root.py 单一缝，FIN_KNOWLEDGE_BASE_ROOT 注入）；
  但 repo knowledge-base/ 留有陈旧副本（articles 停 07-21、cognition 停 08-06），且
  38 个文件含模块级默认指仓库侧（instrument_directory._DEFAULT_PATH、cognition/cli.py、
  research_package、cross_article、admin/cli.py --kb-root 默认值等）。
  后果：诊断/离线工具/未注 env 的路径读到旧副本——BUG-003 首诊「停更 22 天」即此因。
- 修复方向：①repo 侧副本清出（数据不入 git 本就该如此，规则 3/8）；②模块默认全部
  改走 knowledge_root 单缝（未注 env 时 fail-closed 而非回落 repo 副本）；③文档层
  （AGENTS/诊断 runbook）标明唯一真根。
- 状态：开放（①属 W2③ 归档清扫同类，②小改，③随手）。
- 修复进展（2026-08-28 同日）：knowledge_root 增 default_knowledge_base_root()
  （env → XDG shared 根 → 报错，永不回落 repo）；instrument_directory._DEFAULT_PATH
  改走该缝（持仓名字解析/watchlist 共用）；555 测试绿，000657 解析实证走共享根。
  遗留：其余 ~36 处旧链默认值机械替换（清单=「knowledge-base」模块级默认 grep）
  随 W2③/W3；repo 副本清出按规则 4 备份协议另做。
- 修复进展（2026-08-28 续）：005 根治——硬规则入 read_capabilities/server.py 工具描述
  （G-first/相关性不确定默认调 G/空返回=数据不可用不得当「不存在」），规则不再依赖人格
  文件加载，任何客户端任何 cwd 必达；46 测试绿。007 余量清扫——子代理 20 文件机械换缝
  （cognition/cross_article/admin/market/knowledge_brain/gateway/runtime/decision 等），
  2540 测试绿，env 覆盖实测改道。未换 3 处（priority_articles：repo reader 与生产 writer
  schema 漂移被暴露，另立案；cdp_scraper KB_ROOT：测试 monkeypatch 依赖常量；
  scraper/config：import 期 mkdir 副作用需重构）。遗留：shared index.json 的 path 仍是
  repo 绝对路径 + repo knowledge-base/ 2143 个 git-tracked 文件清出（规则 4 协议）。
- 状态：005 关闭（待 W2① 部署激活）；007 大部关闭，剩 scraper 三件套+repo 副本清出。
- 修复（2026-08-28 关闭）：margin 描述改写为全市场两融语义（market-wide leverage
  crowding，明示 NOT account-level）；其余五条对照审计无同类反向漂移（overview 的
  breadth 承诺与 BUG-002 数据缺陷相关，非语义反向，留 BUG-002 处理）；新增
  test_tool_descriptions.py 钉死关键语义（margin=market-wide、portfolio=user-confirmed
  +G-first 硬规则、snapshot 缺口诚实规则、六工具闭集），今后描述漂移触发测试。
  生效随下次 release。
- 修复进展（2026-08-28 晚，余量再清）：scraper 缝收口——cdp_scraper 裸构造
  fallback 改走 `default_knowledge_base_root()`（repo KB_ROOT 常量删除，死
  monkeypatch 测试改显式 tmp 根）；scraper/config 五个路径常量改 PEP 562
  惰性单缝（env→XDG shared→报错，永不回落 repo；import 期 mkdir 副作用
  同时删除，写点各自 mkdir）；shared 根数据修复——index.json 887 条
  + zsxq_sources 61 + priority_events/jobs 14 条 repo 绝对路径改指 shared 根
  （owner-only 备份+manifest 于 `~/.local/share/fin-analyse/
  path-repair-backup-20260828T134405`，改写后全部路径可解析，deep_read_artifacts
  疑 hash 耦合未动、其 path 仅溯源无读路径——运行时按 kb_root 相对解析）。
  污染对照实验：全套件跑前后 shared 树 7025 文件 sha256 diff=0，测试零生产写入。
- 剩余：repo knowledge-base/ 2143 git-tracked 文件清出（规则 4 协议，待 owner
  明确授权后执行）；priority_articles schema 漂移另立案不变。
- 清出完成（2026-08-28 晚，owner 确认清单后执行）：git rm 2142 tracked
  （articles/wiki/images/phase3/debug/index.json，~42M）+ 4392 ignored 本地
  运行残留（runtime/** 25.6M；guidance_chains 等 444 只读锁位文件解锁后清），
  保留 manual-annotations/（生产 poller 从 release 布局读取，
  consume_zsxq_capture_folder.py:506——release 由 worktree checkout 生成，
  必须 tracked）。引用闭包：生产链零引用（knowledge_root 单缝 + 全部单元 env
  指 shared 根）；唯一直读者 test_d3_g_quality_benchmark `_REAL_KB` 有 skipif
  门，删除后自动 skip（成为常跳测试，处置待拍板：改 tmp fixture 或按规则 12
  删）。删除前全树备份
  `~/.local/state/fin-analyse/kb-repo-removal-backup-20260828T200259/`
  （0700/0600，tar sha256 `2bc31f13…`，manifest 记 tracked 2143+ignored
  4392）。受影响面 202 绿 + 1 预期 skip。commit 228e4017。
  附带待办（W3）：5 个 owner 手动脚本仍硬编码 repo KB 路径
  （rebuild_name_map 写 repo KB、seed_shared_brain×2、report/sectors/
  clean_comments cwd 相对默认），随 W3 换缝或按规则 12 退役。
- 状态收尾（2026-08-30 补账）：已关闭，对齐 NOW 板 B 入库/索引行「BUG-007 已闭」——
  两证：①默认路径换缝（08-28 两轮：子代理 20 文件机械换缝 + scraper 缝收口；
  priority_articles 读侧漂移另案 BUG-009）；②repo 副本绝根（08-28 清出 228e4017 后，
  08-29 迁移步6 老仓最后 1 个 tracked 批注 git rm b5d65cba，owner-only 备份+manifest，
  见 migration-manifest.md）。遗留各有去处：G 手工批注 durable 归位=NOW 待办11（P5 前
  KB 收拢首项）；老仓 5 个 owner 手动脚本硬编码 repo KB 路径=未随迁（不在 keep-set，
  老仓已归档）。本仓无 repo KB 副本。

## BUG-008 L1 简报内容降级（W2④ 盲评判定 2026-08-28；主因代码缺陷非模型）

- 证据：旧链 premarket 同日对照（实名持仓+最新收盘+两份中报数字，28 个数字）vs L1 三班
  （12/0/4 个数字，全部 8-25 旧快照复算，零行情零现价零公司名，仅代码指代）。
- 根因①：daily_workspace_generator.py:159 对 build_default_a_share_market_overview()
  返回的**服务对象**直接 str() 渲染——prompt 里市场概览段是 90 字符的对象地址
  （<AshareMarketOverviewService object at 0x...>），正确用法 service.read(Request)。
- 根因②：_material_gaps 只判空字符串，对象 repr 非空→通过，data_gaps:[]——诚实缺口
  机制被击穿，降级不显形（三班简报自己吐槽了对象引用但账本全绿）。
- 其余：prompt 无交易日/as-of 锚点（简报自认「今日日期未知」）；知识检索三班全用
  premarket 问题（run_daily_workspace_checkpoint.py 不传 query）；「较此前变化」栏目
  三班空转；11:42/12:37 两次持仓快照读 INVALID（快照 10:54 即有效、现重放 READY，
  根因未定位——时点恰逢当日持仓确认更新窗口，疑似替换竞态，待查）。
- L1 真实增益（非降级项）：快（2 分 vs 10 分）、postmarket 旧链昨日失败而 L1 正常、
  全程未编造数字。
- 回炉点（优先级序）：修 overview 渲染（read() 调用）；_material_gaps 内容感知
  （拒对象 repr，损坏必落 gap）；prompt 注入 as-of；快照 INVALID 竞态排查；
  知识检索按班次传 query；持仓材料名称/现价补全；「较此前变化」栏目处置。
- 状态：开放（W2 带缺陷闭环，回炉点排 W3-4 首位；①②半小时级可先行）。
- 修复进展（2026-08-28 晚，文件层已改未部署）：①overview 渲染根治——provider
  改调 `service.read(AshareMarketOverviewRequest)`，UNKNOWN 读数回落 None→gap；
  ②`_material_gaps` 内容感知——含 "object at 0x" 的材料视为损坏：prompt 排除
  +data_gaps 显形（`_material_usable` 单点判定，渲染与账本共用）；③prompt 注入
  交易日+生成时刻时间锚点（简报不再自认日期未知）；④知识检索 query 改由
  generator 按班次传入（`_ContextMaterialProvider` 变参闭包，premarket 钉死废除，
  on_demand 用户问题同缝）。operations+scripts 787 测试绿。
- 修复进展（2026-08-28，文件层已改未部署）：⑤持仓材料名称/现价补全——
  `_render_portfolio` 弃裸 JSON/repr dump，改共享 `to_safe_dict()` 投影（同
  MCP 读投影）；目录权威名补全（stored name 缺失或 code-like 才补，
  `RuntimeAshareInstrumentDirectory` 缝，同 BUG-001 mcp_server 读投影）；
  现价注入 `latest_price`/`latest_change_pct`（provider registry `get_quote`
  fallback 链，失败降级显式 null 不炸材料；`quote_reader` 注入缝供测试）；
  UNKNOWN 持仓读数 → None→typed gap（与 overview 同规则）；⑥「较此前变化」
  栏目处置（owner 2026-08-28 拍板口径 A=留栏目+真基线）——carry_over 父
  检查点正文（context 链已有、原被 `_render_prompt` 渲染时丢弃，即空转根因）
  进 prompt 作对比基线；有基线要求给具体变化点，无基线（首班/上日盘后缺失）
  整栏不要求。generator 测试密闭化（fake store/overview，22 测 0.5s，根除
  两个 10s+ live 网络单测）；operations+scripts 791 绿。不动 L1 直调链路与
  合同。
- 剩余回炉点（W3-4）：盲评复验等部署后跑（快照 INVALID 竞态已由下方诊断
  关闭；⑤⑥运行态随下次 release 生效）。
- 诊断（2026-08-28 晚，竞态假说证伪，快照 INVALID 关闭诊断）：**非替换
  竞态**——生产文件（~/.config/fin-analyse/actual-advisory-portfolio.v1.json）
  10:54:25 发布后字节/权限/父目录 mtime 全程未变（mtime==ctime==10:54:25，
  目录条目同刻定格）。真根因是 **schema 前向不兼容**：当日 thesis 持仓理由
  落库改造（375e8ce1，11:05 提交）把位置 schema 7 键扩 8 键（+thesis），
  10:54:25 确认更新即发布新形态（5 仓全带 thesis）；而白天全部读方运行态
  停在 pre-thesis 代码（current → releases/13c791ca，09:26 切换，至今未升），
  `_raw_position` 的 `set(value) != _POSITION_FIELDS` 严格键集校验把 thesis
  判非法 → ValueError → `ACTUAL_ADVISORY_PORTFOLIO_INVALID`/snapshot=None。
  四次失败同因：close 班 11:42:29 生成、postmarket 班 12:37:07 生成
  （generated_via=l1-direct-v1，跑部署链代码，简报原文引用 UNKNOWN/INVALID）、
  14:32/14:37 网关咨询读（actual_portfolio_unavailable）。「现重放 READY」=
  含 375e8ce1 的代码新旧两形态都收。双向实证：同一生产文件，release
  13c791ca 代码读 = UNKNOWN/INVALID（隔离 cwd 加载 release 源），当前仓库
  代码读 = READY。设计教训：schema 演进没抬 schema_version（顶部仍
  actual-advisory-portfolio.v1）+ 严格键集校验 + 运行态滞后 = 部署窗口内
  全链确定性拒绝；向后兼容做了（旧文件新码可读），前向兼容没做（新文件
  旧码必拒）。恢复条件：current 切到含 375e8ce1 的 release（现网咨询读
  持仓仍会 INVALID，随下次部署自愈，无需改代码）。

## BUG-009 priority_analysis_job_status schema 漂移：repo 读侧 0/39 全拒（BUG-007 另立案）

- 发现（2026-08-28 晚）：BUG-007 换缝时暴露的「repo reader 与生产 writer 漂移」定量实判——
  用 repo 解析器只读判生产三文件：events 348/348 过、jobs 348/348 过、
  **status 0/39 全拒**（`set(data)!=REQUIRED_STATUS_FIELDS` + consumer 白名单双杀）。
- 谁在写新字段：Hermes 侧深度阅读消费者，自称 `consumer=priority_analysis_consumer_v2`，
  `delivery_target` 用 `feishu:oc_…`（带 chat id）格式；在 repo 10 字段契约外追加 6 个
  结构化字段（result_status/article_analysis_status/data_gaps/operation_advice_blocked/
  operation_advice_block_reason/portfolio_advice_status/result_classification），且分两代
  （后 19 条多 result_classification）。该消费者在本机 repo/hermes profiles/consult-agent/
  Windows 侧均无指令面残留——**已于 07-13 停写**（早于 08-27 旧 Hermes 入口停用，链路
  大概率已随 rebaseline 退役；契约只在 repo 侧单方面冻结，写侧从未受约束）。
- 谁读不懂：`PriorityJobStatus.from_dict`（严格键集）→ `PriorityJobStatusSink.list_statuses`
  （**整文件原子拒绝**，一条坏记录毒化全部）→ `priority_health.check_priority_outbox`
  → report.py 健康段恒显 `priority_status_outbox_unavailable` error。影响面=owner 健康报告
  失真；咨询链不受影响（events/jobs 两读侧均为宽容 raw dict，348/348 实判全过）。
- 对齐方案（只出方案不施工，owner 拍板后排期）：
  - 方向1（推荐）读侧升级宽容：from_dict 改「必需 10 字段 + 已知扩展白名单（v2 的 6 字段
    常量化入 repo + 测试钉死）」；consumer 白名单加 priority_analysis_consumer_v2、
    delivery_target 允许 `feishu[:chat-id]`；模块 docstring 补写侧契约文档。若 D3 后
    咨询链回归、v2 复活，无需再改。
  - 方向2 历史数据降维：39 条 v2 记录按规则4 备份后改写/移档为 legacy 文件，status 文件
    重接 repo 契约。读侧零改动；代价=改生产 durable 文件 + v2 复活即再漂移。
  - 方向3 契约退役：若确认 v2 永不回来，status 段降级为「只读审计、读不懂=显式 gap」，
    不再作 health 信号。最小改动，代价=推送健康可见性归零。
  - 配套防御（任何方向都建议）：list_statuses 整文件原子拒绝改逐条 typed 隔离+坏条计数
    显形（单条坏记录不应毒化整个文件——本次 0/39 的放大器）。
- 待拍板点：v2 消费者是否还会回来（决定方向 1 vs 3）；status 文件 39 条 7 月陈记录
  本身是否还有保留价值（已超 24h 新鲜度窗口数十倍）。
- 状态（已被下方 08-30 状态收尾取代）：方案已出，不施工（owner 拍板后随 W3-4）。
- 状态收尾（2026-08-30，方向1 已实施，设计门过）：from_dict 改「必需键齐全 +
  已登记 v2 七扩展字段白名单，未知键仍拒」；consumer/delivery 白名单增 v2 +
  `feishu:oc_[0-9a-f]+`（39/39 探针验证；`is_hermes_feishu` 不变量零改动，v2
  push 声明不计 Hermes 证据）；list_statuses 改逐条隔离 + 新
  `list_statuses_with_health` 计数，经 `PriorityDispatchHealth.bad_status_entries`
  进 provider_health MCP payload。实证：生产 39/39 解析 bad=0、单测 18/18、
  门评 12 发现全采纳（elapsed 469s，裁决录随设计稿入 Git 史）。
  **范围注记**：恒显 `priority_status_outbox_unavailable` 的 report.py 在**老仓**
  （独立解析器，两套代码）——本修只治 fin-core 读缝与 provider_health 段，
  老仓报告不受影响，其迁移/退役另案。

## BUG-010 测试基线三簇非绿（semantic_state 36 败 + route_config 1 败 + scraper 死补丁 12）

- 诊断与处置（2026-08-28 晚，逐簇判定）：
  ① **semantic_state 36 败＝环境问题，修环境**：全部 `semantic_state_sandbox_unsafe`
  ← `_semantic_snapshot_child.py` 为 0664（本机 umask 002 下 git checkout 的产物），
  被 `_require_snapshot_child` 的 022 位守卫拒绝（防组写入注入，守卫本身正确）。
  `chmod g-w` 单文件即愈：125/125 绿。生产 release 无此问题（builder 有
  permission convergence）；umask 002 的机器新 clone 需同样 g-w。
  ② **route_config review workload 1 败＝测试 fixture 腐烂，修测试**（原猜
  codex CLI 漂移不成立）：测试用字符串 replace 精确匹配 `codex_routes.yaml.example`
  旧条目（opencode 未引号 URL），D-018/D-019 路由手术后 replace 静默空转、
  断言 DID NOT RAISE。校验逻辑本身完好（codex_route_config.py:284-285）。
  修法：改自含 inline 配置，与 example 演化解耦。16/16 绿。
  ③ **scraper 死补丁 5 败 + 7 error＝W2③/BUG-007 缝改造余波，修测试**：
  `browser.DEBUG_DIR`/`scraper.{ARTICLES_DIR,IMAGES_DIR,INDEX_FILE}` 五常量已改
  `scraper.config` PEP 562 惰性单缝，测试仍 patch 旧模块属性 → AttributeError。
  补丁目标批量改指 `fin_analyse.scraper.config.*`（惰性 __getattr__ 语义下
  setattr/delattr 兼容）。scraper_incremental_v2 24/24、scraper_column 7/7 绿。
  ④ **氦金样 3 败＝A 任务 KB 清出的连带（本会话引入，当场修）**：
  test_helium_fin_codex_runtime 读 repo `knowledge-base/articles/` 氦文章
  （无 skipif 门）。文章从 git 史（228e4017^）转测试 fixture
  `tests/fixtures/guo_teacher_research/articles/20260708_01a99e429a3d.md`，
  路径改指 fixture。3 绿 1 skip（CODEX_BIN 显式 opt-in 门，设计性跳过）。
- 基线：guo_teacher_research 全目录 1208 passed / 0 failed（68 skipped 为
  显式 opt-in 门）；全量套件终验见下条补记。
- 状态：关闭。全量终验（2026-08-28 晚）：**5973 passed / 70 skipped（均显式
  opt-in 门）/ 0 failed / 0 error**（9 分 16 秒）。

## BUG-013 方法论规则注入被"锐评只取最新一条"结构性遮蔽（owner 2026-08-28 抱怨）

- 症状：问询答案未出现「大涨大卖，小涨小卖，大跌大买，小跌小买」16字方针。
- 取证：4 篇含原话的锐评（08-21/24/27/28）full+compact 全部抽出且落
  methodology_rules「操作纪律」条目（25fbef60 方法论层）——产出层零丢失。
- 根因：注入层——锐评窗口 2 交易日且只取最新一条（M1），methodology_rules
  寄生在"该篇被选中"之上；跨文章方法论资产被单篇选择遮蔽，另需意图主题匹配。
- 方向（W3-4 深化审计准入）：方法论投影改直接吃规则库/working-set 而非仅
  已选候选；或多锐评的 methodology_rules 无关窗口聚合。
- 状态：开放（排 W3-4 抱怨清单首位）。
- **owner 撤项（2026-08-28 深夜）**：普通栏 G 准入与深化资格整体撤销——全库
  质量审计（1202 篇）证实普通非QA 86% 为券商研报转载/总结（非老师原创），
  结构上 337/339 完整。撤项后普通栏回 reference lane；58 篇已生成的普通栏
  深化产物留库（惰性，不进 G 链）。BUG-006③ 其余成果（单源窗口配置/交易日
  语义/topic 配置化）全部保留。general_admission_days 配置项随之移除。
- 改号注记（2026-08-30）：本条原误撞 BUG-009（与 priority_analysis schema 漂移条
  重号），改号 BUG-013；改号前查引用面=仅本文件，无外部引用。

## BUG-014 cognition jsonl 权限漂移 0664（盲评顺带发现，2026-08-28）

- 现象：生产 shared 根 `priority_events.jsonl`/`priority_analysis_jobs.jsonl`
  实测 0664（组可读），违背 owner-only 数据约定（家规 3/规则4 口径）。
- 根因：`_append_jsonl` 仅创建时 0600，已存在文件不修正；推测为早期
  umask/工具写入遗留。
- 修复方向：批量 fchmod 0600 + `_append_jsonl` 追加路径补 chmod 防复发；
  顺带全库扫描同类漂移（P3，随手修，不阻塞任何链路）。
- 状态：开放（P3）。
- 改号注记（2026-08-30）：本条原误撞 BUG-010（与测试基线三簇条重号），改号
  BUG-014；改号前查引用面=仅本文件，无外部引用。

## BUG-011 read_market_snapshot EASTMONEY 源解析失败/身份不匹配（2026-08-29 立案；2026-08-31 诊断+修复闭环）

- 现象：08-28（周四交易日）8 次 + 08-29 板 B 探针 12 次调用全带 gaps——
  `EASTMONEY_RAW_SOURCE_PAYLOAD_PARSE_FAILED` + `EASTMONEY_RAW_IDENTITY_MISMATCH` +
  `DUAL_SOURCE_QUOTE_INCOMPLETE` + `NON_CONTINUOUS_REFERENCE_QUOTE` +
  `MARKET_SESSION_REFERENCE_ONLY`；交易日复现，非周末特有。
- 根因（2026-08-31 盘中实弹钉死）：报价端点 08-02 由 push2 切至 push2delay，
  f48 成交额以带分位浮点返回（实测 8/8 样本跨 svr 一致，793325655.17 形态），
  解析器 `_optional_nonnegative_quantity` 按 push2 时代整型契约写
  （fixture f48=185184000 整型为证）→ `not isinstance(value, int)` 必抛 →
  解析失败。`EASTMONEY_RAW_IDENTITY_MISMATCH` 是级联噪声：失败捕获
  venue=None（eastmoney_raw `_failed_capture`）使 `_qualify_quotes` 身份比对
  恒假阳性——trace 中两 gap 恒成对即此故，非两个独立缺陷。onset 无法从
  trace 追认（trace 覆盖窗 08-27 起凡真触达源者 100% 失败；此前「ok」样本
  全是未触达源的短路——identity 未解析/无标的 0ms、容量拒绝），推测自
  08-02 切端点起从未通过。
- 修复（2026-08-31）：①解析器接受有限非负 int|float，`Decimal(repr(...))`
  最短往返保真（NaN/Infinity 显式拒绝——`json.loads` 默认放行）；②失败
  捕获跳过身份比对，以自身 typed gap 为完整结论。实证：盘中实弹三标的
  capture `gaps=()` + replay 一致；真实装配端到端探针两标的 READY
  `gaps=()`；单测浮点契约用例 + gap 卫生回归。详见 market-data.md
  「报价源整型假设 vs push2delay 浮点契约」。
- 状态：已关闭（2026-08-31，实弹+端到端双实证）。

## BUG-012 read_ready_evidence 恒 unavailable + 公告探针不触发工具（2026-08-29 立案；08-30 根因诊断）

- 现象：08-28 trace 6/6 调用 `ready_evidence_unavailable`；08-29 探针「持仓公司
  官方公告」未触发该工具（5 探针仅 G/方法论探针顺带调用 2 次，均 unavailable）；
  08-30 使用盘点 7 天 12 调 0 ok，全部单码 unavailable。
- 根因（08-30 诊断，判别实验 `$STATE/fin-analyse/bug012-ready-evidence-20260830/`）：
  三重契约错位，非崩溃——① 供料错位：reference lane 只从 priority_events.jsonl
  取候选，缓存近期行全为 T0/teacher_original（G 级），`_is_reference_eligible`
  恒滤空 → 当天 reference 候选结构性为零；② `fin.read_ready_evidence` 工具描述
  落通用默认，agent 无从判断调用时机（探针不触发根因）；③「官方公告/记录」真实
  供体是 read_external_evidence（OfficialRecordEvidence，7 天消费 42 次），板 B
  契约描述写错行。
- 修复：两刀施工完毕（08-30）。①工具描述专项化（当天参考材料语义 + 公告类
  转 read_external_evidence）+ 板 B 两行契约纠偏 + 描述回归测试；②供料换
  canonical index.json（owner 裁方向 B；普通栏 allowlist 投影 + classification
  "observation" + 朴素 date→CST 归一 + typed gap + 去重），端到端测试
  test_recent_reference_index_supply.py 全过，设计门 0P0/2P1/6P2 全裁决落稿
  （设计稿按规则 5 入 Git 史：docs/design/ready-evidence-supply.md）。
- 残余：券商名不在 index companies 字段，相关性门 company 重叠通道对券商
  类问题失效（tags/标题通道可用；既有缺口，评审 P2-8 记档）。
- 残余二（08-30 晚实弹发现，四发探针+门级仪表化定位）：**选材门空转 +
  双重损耗**——① `_reference_is_relevant` 对同日 eligible 帖实际全放行
  （INTP 心理帖/地产宏观帖在保偏光纤·CPO 题下 r=1），题问相关性空转；
  ② 同日公司空帖（tickers/companies/chain_facts 三空）先占 reference
  lane 位、再到 ready 投影被 mapping 门丢弃，双重损耗吞掉槽位。结果：
  目标帖（16:38 保偏光纤专题，q&a、companies=[英伟达,康宁,藤仓]、四道门
  全过）两发实弹均未浮出，模型诚实答「今天没有专题帖」= 产品级漏答。
  链路本身已通（21:18 实弹 items 非空、干跑同题非空）；预算层排除
  （8−2=6 ≥ 4 全装得下）。待裁方向（走设计门）：relevance 门做真题问
  匹配（公司/标签/标题通道）+ 公司空帖是否免占位，或观点帖明确只走
  G/锐评面。
- 残余二定修（2026-08-31，codex-open 自审自裁）：①标题子串匹配
  `_has_common_substring` 最小长度 2→4，2 字泛词（主线/公司/什么/信息等）
  不再误放行；②选材后按 `_reference_rank_key` 排序，带 companies/tickers/
  chain_facts 的候选排在空事实帖前，消除“空帖先占槽位、投影再丢弃”的第二重
  损耗；③具体领域词（intent topics 非空）不再被 `_is_latest_focus_query`
  判成“最近关注变化”宽松分支，避免绕过相关性门。生产 index 重放：同日 9 个
  eligible 候选从 6 个误放行收敛为仅目标保偏光纤帖通过；薄 server 公共
  `read_ready_evidence` RPC 端到端仅返回目标帖。新增回归测试，默认套件
  2941 项通过。
- 状态：修复已施工（全量 2901 绿）；实弹探针 08-30 晚四发——公告类腿过
  （外搜带时点、ready 对公告题正确返空、持仓联动正确）；普通栏腿两发
  items 空，根因=残余二（选材层，非供料层），残余二已定修待实弹问询确认；
  09-01 21:01/21:08 真实问询 read_ready_evidence 仍 ready_evidence_unavailable
  （未闭环，待 NOW 主线1 判定是否相关题必中）。

## BUG-013 cognition 提取空坍塌：推理预算耗尽 + 失败哨兵误判（2026-08-30 立案并定修）

- 现象：08-30 下午/晚间 glm53/deepseek/qwen 对同一提取任务返回字面 `[]`
  （85/88 空），同 prompt/backend/文章可翻转；opencode 网关 deepseek_flash 正常。
- 根因（raw HTTP 探针定案）：三后端均 `finish_reason=length`、content 空、
  reasoning≈1 万字符、completion_tokens=4096 顶满——隐藏推理吃光 max_tokens，
  可见答案截成空；`_response_text` 空 content 抛 ValueError 在倍增恢复前短路；
  重试耗尽返回 `"[]"` 哨兵被提取层当「合法空」落不可重试语义（排空不补做）。
  glm53 另有偶发 400 内容过滤路径。原判「模型自主选最短合法 JSON」证伪，非限流。
- 修复：length 容忍空 content 走倍增恢复（答案非空截断全档 4096→8192→16384；
  空推理签名只倍增一次即终态截断）；提取层按 last_failure 区分哨兵→retryable
  硬失败并跳过同 backend nudge；deepseek_flash 双端点（llm.env 主键 +
  auth.json opencode-go 降级键，owner 指令）。72 focused + 2910 全量绿；
  端到端实弹 290.8s 出 5 单元 0 警告。
- 状态：修复已合入（5a6f12b）；低峰重生成（regen_driver_v4，regen-if-better）
  待 08-31 凌晨照跑复核。

## BUG-015 Daily 概览材料冻结时钟——交易日盘中班次确定性缺料（2026-08-31 14:25 班后核对立案并修复闭环）

- 现象：08-31 premarket/morning/close 三班 `l1_material_market_overview_unavailable`
  （close 班已在 BUG-002/011 修复后运行，排除旧拒链）；interactive 探针同时段
  全 PARTIAL。14:25 核对复现：material provider 以检查点冻结时钟
  （13:55:00.348）构造概览服务 → overview None；活时钟 → PARTIAL。
- 根因：generator 材料装配把概览服务绑到检查点 evidence_cutoff 冻结时钟，而
  fetch 返回的行情行时间戳是真实墙钟——fetch 期间任何新于冻结瞬间的时间戳
  触发 `MARKET_OVERVIEW_PROVIDER_TIME_AFTER_QUERY` 整链拒绝。交易日盘中
  数据秒级更新 → 必然触发；周末/盘后数据不更新 → 通过。既有「四班 L1 实证
  已通」与 B1 盲评「差距全在带伤班次」由此解释：带伤班次即盘中班的该缺料。
  上午两班 gap 主因是本条，非仅 BUG-002（盘前占位为叠加因素）。
- 修复（c174dfd 之后的 fix 提交）：概览是活读取——材料装配改用真实时钟构造
  服务（`build_default_a_share_market_overview()`），检查点 evidence_cutoff
  记账不变；回归测试钉「冻结时钟下服务必须以非冻结时钟构造 + 材料非 None」。
  实证：冻结时钟探针 None → 4000 字。
- 状态：已关闭（2026-08-31，15:05 postmarket 班为首次生产实弹）。
- 实弹兑现（2026-08-31 15:30）：postmarket 班 `data_gaps=[]` 全清零，正文带四指数
  收盘涨跌幅 + G 认知对表（一致/不一致/无对照均点名）+ 空仓快照，无催更新文字。

## BUG-016 Daily 盘后市场材料截断导致“有事实、无判断”（2026-08-31）

- 发现：owner 反馈 15:30 postmarket 推送基本无收获；该 run 的 durable product
  `data_gaps=[]` 且投递成功，但正文只稳定消费了指数涨跌幅/少量行业信息，未能
  使用可得的指数点位、概念榜和成交额靠前个股。
- 根因：`_render_market_overview` 将约 13K 的概览 JSON 直接 `[:4000]`，中间截断
  后以“非空材料”进入 L1 prompt；同时指数投影未透传 Tencent 的点位字段。缺确认池
  行情则是当前没有该材料 owner 的真实边界，不是模型漏读。
- 修复：改为有界整行文本投影（指数点位/涨跌/成交额、宽度、行业/概念榜、成交额
  个股榜、限制）；`MarketIndexObservation.level` 透传 f2；闭市优先既有 Eastmoney
  指数端点以补齐涨跌家数；prompt 要求有料时至少引用两条带数字事实并说明含义。
  focused 39 例、默认套件 2936 例 + ruff 已通过。
- 部署（2026-08-31 17:27）：目标 SHA `5e64c43`，`uv sync` 无依赖变更，
  systemd daemon-reload 后 gateway PID `497795` 正常运行；薄 server 公共
  `read_market_overview` RPC 返回 `PARTIAL`、四指数点位与涨跌家数。
- 身份收口（2026-08-31 17:32）：状态文档提交后 HEAD 为
  `263ecd8b`（代码内容仍包含 `3b2fd30`），unit 期望 SHA 已同步；
  `uv.lock` SHA-256=`c32179b2…`，公共 RPC 再验通过。
- 状态：已部署；09-01 09:35 morning 真实班正文确认通过（gaps=[]、带指数点位/
  成交额）；09-01 close+postmarket 班因工作树脏被身份门拒（14:10/14:20/15:25/
  15:30 全 exit 3，owner 放弃补发）；postmarket 复验顺延 09-02 15:25 班
  （D-030 09-01 停推，复验并入 D-031 验证）。

## BUG-017 Daily L1 多 backend 预算未共享（2026-08-31）

- 发现：隔离真实预演中，单个 GLM backend 连续三次约 60 秒超时后才返回空；
  generator 随后仍给第二个 backend 重开完整预算，失败链最坏耗时约翻倍。
- 根因：`L1DirectWorkspaceGenerator._complete` 只在进入链路前计算一次预算，
  没有把多个 backend 绑定到同一个 monotonic 总截止点。
- 修复：多个 backend 预先等分同一总预算，单 backend 保持完整预算；既有路由、
  backend 内重试和失败语义不变。focused 41 项、默认套件 2938 项均通过。
- 部署后隔离真实预演（2026-08-31 18:01）：完整 postmarket prompt 在
  216.2 秒返回 994 字正文，实际引用指数点位、涨跌家数、成交额和半导体/电子
  结构，并对 4300 点假设给出“未兑现”；未写生产库、未投递。
- 状态：已部署；09-01 09:35 morning 真实班确认通过；postmarket 复验顺延
  09-02 15:25 班（09-01 班次因 tree_dirty 缺失，放弃补发；D-030 09-01 停推，
  复验并入 D-031 验证）。

## BUG-018 Daily L1 禁用节点占预算名额，砍掉后续可用后端（2026-08-31）

- 发现：GLM 三节点禁用后（D-028），Daily L1 `_resolve_backends` 只解析出
  deepseek 一个后端；`t0=[glm53, deepseek, qwen]` 先截前 2 名（禁用 glm53 +
  deepseek），qwen 永远轮不到，BUG-017 的多后端总预算共享实际失效。
- 根因：`_resolve_backends` 在 `get_backend_priority` 结果上先 `[:2]` 再过滤
  enabled，禁用条目也占名额。
- 修复：先按优先级过滤不可用/禁用条目，再取前 2 个成功构建的后端；全启用时
  行为不变（同序同 2 个）。focused 8 项通过；真实配置解析 L1 chain =
  `[deepseek, qwen]`。
- 状态：已修复，随 D-028 同批部署（HEAD `0bd3a81` 后置提交）。

## BUG-019 ZSXQ deep-read retryable 使整 run 退出 1（2026-08-30）

- 发现：2026-08-30 11:02 poller run failed（systemd exit 1，failure_reason=unknown、
  cdp_adapter_failure_kind=unknown）；[DONE] retryable=2：095bcfba4a03、557595c49f26
  （7 月旧锐评 backlog）。compact/full 产物文件已写入且 JSON 结构完整，但仍被判
  retryable 未生效。changed_count=0，无新增丢失；13:03 起恢复 succeeded，G 工作集
  全程 READY/FRESH。
- 根因：待查（retryable 判据为何在产物已落盘时仍触发；是否应使整 run 失败；两篇旧文
  是否已成功重生成未确认——不在 20 天活跃窗口，不影响当前 G 工作集）。
- 修复：无（自愈）。
- 状态：开放（观察；backlog 若重试成功即关闭）。

## BUG-020 星大派新栏目落「普通」→ 系统性漏 G（2026-09-01）

- 发现：owner 反馈最近多了星大派每日热点/星大派人脉；核对 index 显示 6 篇每日热点
  （08-25 起每日一篇）+1 篇人脉全部 column=普通、teacher_original=true；08-28 撤普通
  栏后全被挡在 G 库外；0825/0826/0828 历史 deep-read 实为空壳（rank=external_context、
  units=0）。
- 根因：四层缺列——`scraper/config.py` COLUMN_PATTERNS、`classify_g_source` 精确标签、
  `g_working_set` 列闭集、`zsxq_apprentice._XINGDAPAI_COLUMNS`（→ rank=external_context
  → 深化跳过）。
- 修复：四层同步补列（每日热点=recent_change_risk/commentary 档；人脉=systematic_
  framework/special 档）；存量 7 篇 column 纠正（备份 `~/fin-data/backups/
  g-new-columns-20260901/`）；7 篇全部重做 deep-read（4-8 units/篇）。
- 状态：已修复（代码+数据），待下一次 poller 发布验证 manifest 含新文章。

## BUG-021 跳转链接文章只抓到开头（Trump Zone 特刊「目 ...」截断，2026-09-01）

- 发现：2026-09-01《Trump Zone 现象研究报告》入库正文只有标题+「目 ...」，
  长认知/deep-read 只抽出标题级单元；G 工作集却标 READY，截断文冒充完整文。
- 根因：Windows capture F-07 内联文章补抓把全文写进内存 `topic.content_text`，
  但 `collectCursorCoverage` push 的仍是补抓前的原始 cursor output——回填全文
  被静默丢弃（生产 artifact `bec2857f` 实证）；且群页 DOM 锚点取不到时无第二
  退路；WSL 保存循环对已索引 topic_id 无条件 continue，截断文无法被更完整
  capture 升级。
- 修复（老仓 `3fd7f1d8` + 已部署 Windows `capture-zsxq.cjs`）：回填后
  `output=JSON.stringify(parsed)` 再 push；链接提取三退路（群页 card 锚点→
  topic 详情页展开+任意锚点→详情页正文兜底）；每页补抓上限 5；免责声明/
  风险提示固定页脚裁掉。WSL（fin-core 工作树）：cursor 截断尾标 incomplete +
  存稿文件尾截断判据 + `_should_recapture` 接线（严格更长才原位升级）；
  deep-read 按 content hash 自然重生，G 工作集按 manifest 重算。
- 状态：代码修复完成并已真实验证（2026-09-01 13:xx）：capture 侧补抓到
  Trump Zone 全文（cursor 297→4171 字）；poller 消费后 KB 存稿 796→11362
  字节、`incomplete: False`、免责声明已裁；deep-read 重生成 5 units；
  G 工作集 manifest 重绑定在当次 run 收口阶段落盘（READY 无新 gap 待复核）。

## BUG-022 夜间 read_market_snapshot 全 gaps，盘后给不出干净收盘价（2026-09-01）

- 现象：09-01 21:01/21:08 手动 CLI 实弹，read_market_snapshot 4 调全 gaps：
  PRIMARY_TRADING_STATUS_UNKNOWN + NON_CONTINUOUS_REFERENCE_QUOTE +
  CURRENT_TRADING_DAY_BAR_NOT_INCLUDED + MARKET_SESSION_REFERENCE_ONLY
  （一次含 COMPLETED_DAILY_BARS_UNAVAILABLE）——晚间问「最新价」只能拿到
  参考/缺口语义，给不出「今日收盘价」确定结论。
- 诊断（2026-09-01 晚，直调复现）：双源报价实际在位且同价（000657=62.33、
  600879=14.67，disagreement_ratio=0，事件时间戳=今日 15:34/16:11–16:14 盘后），
  但 providers 闭市后 trading_status=unknown → `_qualify_quotes` 恒报
  PRIMARY_TRADING_STATUS_UNKNOWN + PARTIAL，合格报价不投影为 price（price=null）；
  `continuous` 按 session=OPEN 判定，闭市恒 false → NON_CONTINUOUS_REFERENCE_QUOTE；
  daily bars 最新 completed=08-31（今日 bar 尚未落），CURRENT_TRADING_DAY_BAR_
  NOT_INCLUDED 部分为真实缺料。
- 根因：盘后行情合格化语义缺失——闭市后 trading_status=unknown 被当缺陷，
  拦住本可确认的收盘价；「参考价+盘后标注」应作为合法合格态而非 gaps。
- 修复：待办——盘后应干净返回今日收盘价 + 盘后标注、gaps=()；owner 已拍板
  必补（2026-09-01）。方向：盘后双源同价按「收盘参考」合格化（价格照常投影 +
  显式盘后标注），今日 daily bar 未落作为独立 typed gap 按问题面取舍；核心语义
  变更，动代码前先短设计（规则5）。
- 修复进展（2026-09-01 晚，已实现）：close-reference 合格化合入——盘后
  （AFTER_CLOSE/CLOSED_DAY）双源同价且事件日期=最近完成交易日时，status=READY、
  price 投影、data_gaps=()、observation_mode=CLOSE_REFERENCE；真实缺料
  （MARKET_SESSION_REFERENCE_ONLY / CURRENT_TRADING_DAY_BAR_NOT_INCLUDED /
  bars_gap）进 context_limitations，不伪造不静默。新增 3 回归测试；
  晚间直调实弹 000657/600879 均 READY、gaps=()、price=62.33/14.67；
  全量 2989 绿。
- 状态：修复已实现 + 直调实弹过；待真实 CLI 晚间问询「最新价」一次闭环后关闭
  （NOW 主线2）。
