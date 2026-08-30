# FIN 术语表（指路型）

> 定位：专有名词的一句话解释 + 权威出处。给作者、读者、用户和新 AI 会话当快查表。
> 规则：**只指路，不复制定义**——深度定义以所链权威页为准，冲突时改权威页、再回改本表一行。
> 更新协议：新概念进产品时加一行；概念废弃时删一行。状态、排期、失败记录永不入表（看 [NOW](pm/NOW.md)）。

## 产品与使用

| 术语 | 一句话 | 深入 |
| --- | --- | --- |
| **FIN** | 面向 A 股决策辅助的自用 AI 投研系统（1–5 人）；一切产出 advisory-only。 | [user-design-principles](architecture/user-design-principles.md) |
| **咨询（consultation）** | 用户向 FIN 强 Agent 单轮提问的公共入口；seam 标识为 `consultation.decision_support`（字符串标识，详见 catalog）。 | [module catalog](architecture/internal-module-catalog.md) P0 节 |
| **续问 / 连续性（continuation）** | 同链追问复用 hash 校验过的先前上下文；audience 或 hash 不匹配一律拒绝。 | [catalog](architecture/internal-module-catalog.md)「Decision Guidance Service」 |
| **顾问人格** | codex / CC 客户端经 AGENTS.md 软链共用的 FIN 顾问人格（含认知权威边界、闭集工具规则）。 | [night-shift-report §七](pm/night-shift-report-20260827.md) |
| **薄 server（read-capability server）** | D1 v2 只读能力 server：账户 / G / 行情等可信上下文的唯一装配点，consult profile 挂载同一份。 | [read-capability-server-design](pm/read-capability-server-design.md) |
| **Daily（简报）/ Daily Workspace** | 每交易日四班定时生成并投递的简报，durable 状态机保证不丢不重。 | [design/daily-delivery](design/daily-delivery.md) |
| **检查点（四班）** | premarket 9:20 / morning 10:00 / close 14:20 / postmarket 15:30（Asia/Shanghai）。 | [design/daily-delivery](design/daily-delivery.md) |
| **prepare / delivery 两相位** | 简报的生成与投递分离；4 检查点 × 2 相位 = 8 个 systemd unit 实例。 | [design/daily-delivery](design/daily-delivery.md) |
| **L1 直调** | Daily 生成不再委托咨询 Agent，按 llm.yaml `priorities.t0` 截前 2 端点直接调 LLM。 | [design/daily-delivery](design/daily-delivery.md)「L1 直调投影」 |
| **投递 ACK / POSITIVE_ACK** | 飞书送达与平台确认的投递证据；ACK 只证明平台接受送达，不等于用户已读。 | [design/daily-delivery](design/daily-delivery.md) |
| **outbox / obligation / run ledger / reconcile** | durable 投递状态机四组件：待发件箱、投递义务、运行台账、日终对账。 | [design/daily-delivery](design/daily-delivery.md)「durable store owner 清单」 |

## 认知与深化（G 域）

| 术语 | 一句话 | 深入 |
| --- | --- | --- |
| **G / 老师** | 锅老师；G 认知的唯一人格来源。 | [g-cognition 设计页](design/g-cognition.md) |
| **G 认知 / G 认知材料 / G 认知单元** | 老师可复用的判断与框架 / 承载它的原文 / 可回指的最小单位。 | [UBIQUITOUS_LANGUAGE](../UBIQUITOUS_LANGUAGE.md)（认知底座节，权威） |
| **深化（deep-read）** | 对有资格文章预生成 full/compact 认知材料工件；`article_id + content_hash` 绑定，hash 变即失效重做。 | [design/deepen](design/deepen.md) |
| **深化资格（strict-G 门）** | 决定哪些文章可深化的单一资格定义（`classify_g_source`）；普通栏不入 G 认知库、无深化资格（owner 2026-08-28 晚撤项）。 | [design/deepen](design/deepen.md) + [source_contract](../fin_analyse/guo_teacher_research/source_contract.py) |
| **排空（drain）** | 每轮 ingest tail 有界排空待深化积压（≤3 篇/轮，覆盖 LLM 失败残留与 hash 漂移）。 | [design/deepen](design/deepen.md)「backlog 有界排空」 |
| **G 工作集（working set）** | 知识索引 + 优先事件 + 新鲜 deep-read 对绑成的运行证据清单；只作 operational evidence，不产认知。 | [g_working_set.py](../fin_analyse/guo_teacher_research/g_working_set.py) |
| **manifest（PARTIAL）** | 工作集清单产物；PARTIAL = 来源覆盖不完整。 | [g_working_set.py](../fin_analyse/guo_teacher_research/g_working_set.py) |
| **栏目（特刊 / 锐评 / 好问题 / 小故事 / 普通）** | G 来源账号的文章栏目；只是来源/检索线索，不是认知模式。 | [UBIQUITOUS_LANGUAGE](../UBIQUITOUS_LANGUAGE.md)（Relationships 末段） |
| **Z** | 知识库中非 G 的知识；Z 不验证、覆盖或否定 G（Z_EVIDENCE_NOT_G 边界）。 | [catalog](architecture/internal-module-catalog.md) 使用规则 8 |
| **主导 G / G 采用关系 / 认知模式** | Agent 对当前问题选定的主认知线索 / `adopted·not_applicable·not_used·no_g_available` / 单元的语义角色。 | [UBIQUITOUS_LANGUAGE](../UBIQUITOUS_LANGUAGE.md)（认知底座节、时间与采用关系节，权威） |

## 采集与数据

| 术语 | 一句话 | 深入 |
| --- | --- | --- |
| **ZSXQ** | 知识星球；G 认知原文的来源平台。 | [design/zsxq-capture](design/zsxq-capture.md) |
| **capture folder** | ZSXQ 抓取落地的目录，消费链入口。 | [design/zsxq-capture](design/zsxq-capture.md) |
| **consumer** | `consume_zsxq_capture_folder.py`：对 capture 产物做校验、恢复与入库委托。 | [consume_zsxq_capture_folder.py](../scripts/consume_zsxq_capture_folder.py) |
| **poller** | `fin-zsxq-capture-poller.service`：WSL 侧 transport/ingest 消费单元，timer 触发后逐次消费最旧 pending capture 并委托入库（深化排空随 ingest tail）；capture 触发拓扑归 Windows Task。 | [design/zsxq-capture](design/zsxq-capture.md) |
| **knowledge index** | 知识索引 `index.json`，工作集与检索的地基。 | [design/g-cognition](design/g-cognition.md) |
| **knowledge-base/** | 用户/领域数据根；规划随容器判决迁 `~/fin-data/knowledge`（数据出仓）。 | [rebaseline §0.5.6](pm/rebaseline-20260827.md) |

## 链路与路由（LLM 泳道，三池零共享）

| 术语 | 一句话 | 深入 |
| --- | --- | --- |
| **L1 生产管线（认知链）** | 批量管线（深化、Daily 生成）的 LLM 直调链；路由权威 `config/llm.yaml`；挂了停知识更新与 Daily 生成，不影响交互咨询（泳道独立）。 | [design/daily-delivery](design/daily-delivery.md)「L1 直调投影」 |
| **问询链（= 咨询链）** | 交互咨询的 codex 路由链；路由权威 `~/fin-data/codex_routes.yaml`；故障退避不影响 L1。 | [l1-route-chain-survey](pm/l1-route-chain-survey-20260827.md) §1 |
| **priorities t0 / t1** | llm.yaml 分层：t0=难题质量锚 `[glm53, deepseek, qwen]`；t1=简单任务吞吐（glm53_flash 优先）。 | [config/llm.yaml](../config/llm.yaml) |
| **codex-glm / codex-open** | 问询链两条活路由：官方 glm-5.3 Responses 端点 / 既有 codex 路由（priority 1 / 2）。 | 生产 `~/fin-data/codex_routes.yaml`（D-018/D-019 后形态） |
| **probe / 冷却** | 路由探活（probe TTL 1800s）与故障退避（step 900s / max 3600s / half-open 300s）。 | [l1-route-chain-survey](pm/l1-route-chain-survey-20260827.md) §1.1 |
| **熔断** | 按命名空间（如 `vision:`）的故障隔离，防单点拖垮整链。 | [backend_health.py](../fin_analyse/claims/backend_health.py)（BackendCircuitBreaker） |
| **vision chain** | 识图链：glm53_flash → glm-4.6v-flash → 硅基流动 → mimo，已配置化。 | [config/llm.yaml](../config/llm.yaml) `vision.chain` 段 |
| **复评第一层** | `/review`（实名 skill，自固定比较点）；Spec 轴源指向 docs/design/、NOW.md、commit message，不依赖 issue tracker。 | [AGENTS.md](../AGENTS.md) 复评第一层节 |
| **外部审视** | CC 专属统一外部评审机制：CC 的评审者固定 scripts/codex_open.sh --sandbox read-only（当前 codex-open·deepseek-v4-pro·max，换规格只改脚本）；三触发各一次=设计门/吓人 diff/外援；packet 冻结四问；评审只产发现、裁决归 CC；裁决记录附时长。codex-open 不设设计门/外部审计，全部自己完成。 | [AGENTS.md](../AGENTS.md) 审查机制归属 |

## 运行与发布

| 术语 | 一句话 | 深入 |
| --- | --- | --- |
| **gateway** | Hermes/飞书 WS 平台集成常驻进程（旧咨询入口）；scheduled Daily 不经它——投递走 `hermes` CLI 子进程。 | [fin-domain-kernel-agent-runtime](architecture/fin-domain-kernel-agent-runtime.md) |
| **release / current 指针** | 不可变 release 目录 + 符号链接切换；current 指向即在线版本。 | [AGENTS.md](../AGENTS.md) 规则 9 |
| **cutover** | 切 current 的切换动作；成套部署 = SHA + lock digest + 已装依赖 + PID + 公共入口五件核对。 | [AGENTS.md](../AGENTS.md) 规则 9 |
| **runtime-truth / public-entry ledger** | 投递接受 `{platform, message_id, observed_at}` 的持久证据库，dispatch 前置 owner。 | [design/daily-delivery](design/daily-delivery.md) |
| **systemd units** | `fin-daily-workspace-{prepare,delivery}@{四班}` ×8 + `fin-zsxq-capture-consumer@{run-id}`、`fin-zsxq-capture-poller.service`（各带 timer）+ `hermes-gateway-fin`；渲染/apply 脚本统一装。 | [design/daily-delivery](design/daily-delivery.md) |

## 工程与治理

| 术语 | 一句话 | 深入 |
| --- | --- | --- |
| **家规 v2.1** | 根 `AGENTS.md` 共享工程合同（CC/codex/opencode 同一约束）。 | [AGENTS.md](../AGENTS.md) |
| **NOW.md / DECISIONS.md** | 唯一当前状态与执行队列 / 决策史；共享追加目标，按各自文件头协议维护。 | [NOW](pm/NOW.md)、[DECISIONS](DECISIONS.md) |
| **rebaseline-20260827** | 方向权威（§0.5 为当前版本）：CLI-first、Daily 脱钩、实验预注册、容器判决 ③b。 | [rebaseline](pm/rebaseline-20260827.md) |
| **容器判决 ③b** | 新代码住 `~/fin-core`（新 git 历史）、数据住 `~/fin-data`、旧仓冻结不删。 | [rebaseline §0.5.6](pm/rebaseline-20260827.md) |
| **阶段（P0–P5）** | rebaseline §6 刻度 + D-020 时序调整：P0 止血 → P1 CLI 首链（薄 server/consult-agent）→ W2 手术 → 外部项目吸收 → W3-4 深化 → D3 终态验收（最后）→ P5 条件开放。 | [rebaseline §6](pm/rebaseline-20260827.md)、[D-020](DECISIONS.md) |
| **设计门** | CC 外部审视三触发之一：核心设计稿动代码前一次盲评（规则 5 那类，非核心豁免）。 | [AGENTS.md](../AGENTS.md) 外部审视节 |
| **外援触发** | CC 外部审视三触发之一：同一问题 ≥2 次修复未果时加第二意见模型（当前 codex-glm·glm-5.3），前两次双模并行校准独立发现占比再定转正。 | [AGENTS.md](../AGENTS.md) 外部审视节 |
| **keep-set 闭包** | 薄 server + Daily + ZSXQ + 深化四入口的 import 闭包并集；新仓移植与归档的准入线。 | [rebaseline §0.5.6](pm/rebaseline-20260827.md) |
| **工作树（worktree）** | 并行开发隔离单元；独立功能并行、半成品不进 main、终态回收。 | [AGENTS.md](../AGENTS.md) 规则 13 |
| **advisory_only** | 研究咨询默认边界；真实交易/资金副作用必须人工确认（家规硬边界 1）。 | [AGENTS.md](../AGENTS.md) 硬边界节 |
