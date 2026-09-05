CC/ZCode/codex/opencode 共享合同，详见 docs/DECISIONS.md。

# fin-analyse 工程家规（v2.1 · 自用期）
定位：1–5 人自用投研助理，好用是唯一验收。
状态源：docs/pm/NOW.md 是唯一当前状态与执行队列（新家同名同位迁移）；
决策史在 docs/DECISIONS.md；两文件都是共享追加目标，按其文件头协议维护。
理解系统：先读 docs/architecture/system-overview.md（主链一页纸）与
docs/GLOSSARY.md（术语指路）；认知域深度词汇以 UBIQUITOUS_LANGUAGE.md 为权威。

协作准则（先于工程流程）：
- 准确性、独立判断与代码质量优先于取悦用户。
- 动工前先审查需求与前提：事实错误、逻辑漏洞、信息缺失、过时判断、
  不合理假设；必要时直接指出，说明原因、风险与更合理方案。不默认
  认同用户技术判断，也不为反对而反对。
- 结论分四级：代码/文档可证的事实；基于现有信息的合理推测；未验证
  的假设；无法确认的信息。无法确认不编造，未验证不冒充。
- 先读相关代码、配置、测试与项目规则再修改；沿用现有框架、风格与
  设计，避免无关重构。
- 修改后运行相关测试、检查或构建；未验证就明确说明，不声称已验证。
- 需求与代码均无明显问题时直接执行，不刻意挑错。

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
14. 联网检索：会话可见 zhipu-web MCP（webSearchPrime / webReader，
    智谱联网搜索/网页读取）时，外部联网检索优先调用它；不可用或
    失败时退回客户端原生 search / open_page；外部检索引用一律带
    来源与时点。

废止：binding / design review 轮次 / 审核 failover / 四级完成等级 /
五层 E2E / 唯一 writer 官僚 / opencode 双 writer 变体。
审查机制归属（owner 2026-09-04 拍板，扩展 D-027）：设计门与外部
agent 审计对 CC 与 ZCode 会话同等执行；codex-open 不设设计门、不做
外部审计，全部自己完成。
复评第一层（CC/ZCode 按需动词）：/review（实名 skill，自固定比较点；
Spec 轴源指向 docs/design/、NOW.md、commit message 引用，不依赖
issue tracker）。
外部审视（按需动词，无常驻设施；CC/ZCode 同担）：评审者固定
scripts/codex_open.sh
（评审者链，D-045：cmd·deepseek-v4-pro 主 → glm·glm-5.3 替补，主不可用自动
fallback 并横幅+fallback.tsv 落账；换/加评审者只改该脚本 profile 表，本文件
不写死）。三触发、每触发一次：
核心设计稿动代码前（设计门，规则5 那类，非核心豁免）；吓人 diff 合入前
（按规则5 核心判据：durable state/公共入口/大删大改/契约变更）；同一
问题 ≥2 次修复未果（外援，执行会话直调无需用户中继，替补链 glm·glm-5.3
即异构第二源，双模并行校准程序不变）。每次给
评审者冻结 packet：设计稿或 diff+提交清单+固定四问（契约破坏？durable
state 时序/幂等？引用闭包漏删？相对直接 Agent 退化？），packet 骨架与落
盘约定固定 docs/design-gate-packet-template.md；评审只产发现，裁决归执
行会话逐条落稿；裁决记录附 elapsed_seconds/发现数/采纳数/驳回数（含部分
采纳，逐条一句理由）与实际服务的评审者（横幅/fallback.tsv 为证）。
评审侧不用长驻会话，靠 packet 固定前缀吃 provider 缓存；仅外援出现
一次单发 packet 反复漏上下文的真实事故才许引入 resume 会话。
升级防线：外部审视每周真在手动用才许包脚本（规则11）；想接回自动
触发/强制轮次必须先指认一次真实漏网事故（一次性设计门不在此列
——它是门不是轮次）。

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
