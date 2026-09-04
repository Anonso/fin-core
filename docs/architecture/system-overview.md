# FIN 系统概览（一页纸）

> 定位：给新读者、新 AI 会话的 5 分钟理解面。术语逐条见 [GLOSSARY](../GLOSSARY.md)；
> 当前状态/队列见 [NOW](../pm/NOW.md)——本文**无状态**，只在模块或链路结构变化时更新。

## FIN 是什么

面向 A 股决策辅助的自用 AI 投研系统（家规定位 1–5 人，当前单人）。两个产品面：

1. **顾问咨询**：在终端经 codex / CC 客户端向 FIN 顾问 Agent 提问；FIN 装配老师认知（G）、
   行情、持仓与可追溯证据注入，强 Agent 负责理解、反证、综合与表达。一切 advisory-only。
2. **Daily 简报**：每交易日四班（盘前 9:20 / 早盘 10:00 / 收盘 14:20 / 盘后 15:30）自动生成，
   经 Hermes 消息通道投递飞书，durable 状态机保证不丢不重。

旧飞书/Hermes 交互咨询入口已停用（2026-08-27 拍板，允许报错）。

## 分域定位：谁拥有控制流

两种 LLM 用法按域拆开，判据是**谁拥有控制流**（D-016、rebaseline §4；问询侧
哲学权威 = [user-design-principles](user-design-principles.md)「LLM-as-Kernel /
Agent-as-Product」节）：

- **问询面（顾问咨询）= LLM 为核心的 agent**：CC/codex 客户端即宿主，agent loop
  （会话/记忆/工具循环/模型）归 harness，FIN 不自持问询 LLM；FIN 只供数据器官
  （薄 server 只读工具）与领域人格。增量只许是上下文/人格/证据的投递质量，不许是
  控制流——同题不得弱于直接 Agent（家规 11）。
- **供给链（ZSXQ→入库→深化→G 工作集）= LLM 作器官的应用**：确定性代码拥有
  控制流（poller/consumer/排空），LLM 调用是带类型工件的纯步骤（deep-read、
  parser、评分），质量由盲评守门，成本走 L1 吞吐池。Daily 生成同属此侧（L1 直调），
  D-031 规划迁入问询环境（[设计骨架](../design/d031-daily-consult-env.md)）。

新能力归侧：交互问答 → 骑 agent loop，禁止新建编排层；可重复数据变换 → 管线
步骤 + 盲评。要防的回归是把问询面重新应用化（重建 router、硬编排、自持问询模型
——第一次坍塌的形态）。

## 主链图

```
  ZSXQ(知识星球)                 行情/两融/公告/持仓(用户确认)
     │ capture folder                      │
     ▼                                     │
  poller ─► consumer ─► 知识索引            │
                 │                         │
                 ▼                         ▼
           深化 deep-read            薄 server（只读上下文装配）
          (full/compact 工件)        账户·G·行情·两融·证据
                 │                         ▲  │
                 ▼                         │  │
          G 工作集 manifest ───────────────┘  │
          (index+事件+fresh pair)             │
                                              ▼
                                          问询链(codex-glm→codex-open)
                                          强 Agent 单轮咨询/续问 → 答案回终端

  Daily 简报：L1 认知链直调(llm.yaml t0 截前 2 端点) + 三份只读材料
       └► delivery ─► Hermes CLI send ─► 飞书 ACK / reconcile
```

- 左半是**知识供给链**（Windows Task 触发抓取 → poller 消费 → 入库 → 深化 → 工作集）：断了只影响知识新鲜度，不影响咨询可用。
- 右上是**上下文装配点**（薄 server）：交互咨询的可信上下文唯一装配点。
- 下半是两条 **LLM 泳道**（L1 生产管线 / 问询链，三池零共享）；Daily 由 generator 直取三份只读材料
  （portfolio / market_overview / g_reference），经 Hermes CLI 投递，durable 状态机保证不丢不重。

## 功能域分组

| 域 | 主要组成（包/模块） | 一句话作用 | 深入 |
| --- | --- | --- | --- |
| 采集与内容 | `scraper`（capture/ingest/排空）、`ingestion`、`scripts/`（consumer/poller）、`guo_teacher_research`（G 准入判定） | 把 ZSXQ 文章变成可信入库内容 | [zsxq-capture](../design/zsxq-capture.md) |
| 认知与深化 | `cognition`（deep-read 工件与可用性）、`guo_teacher_research`（G 工作集）、`knowledge`、`knowledge_brain` | 索引、深化、G 工作集与认知追溯 | [deepen](../design/deepen.md)、[g-cognition](../design/g-cognition.md) |
| 咨询与上下文 | `consultation`、`context`、`read_capabilities`、薄 server 装配 | 解析意图、装配可信上下文、驱动强 Agent | [module catalog](internal-module-catalog.md) P0 节 |
| 市场与账户事实 | `market`、`margin`、`official_records`、`portfolio`、`paper` | 行情/两融/公告/持仓的只读事实供给 | [market-data](../design/market-data.md)、[portfolio](../design/portfolio.md) |
| 交付与运行面 | `gateway`（飞书 WS 集成）、`operations`（Daily 生成/投递）、`runtime` | Daily 四班生成投递、运行证据与对账 | [daily-delivery](../design/daily-delivery.md) |
| 工程与验证 | `validation`、`engineering_validation`、`dataflow`、`claims`（配置加载） | 契约校验、连通性探针、预检 | [module catalog](internal-module-catalog.md) P2 节 |

其余包（`moa`、`graph`、`signals`、`backtest`、`vision` 等）为支撑或待归档面；
权威清单与接口以 [module catalog](internal-module-catalog.md) 和源码为准（其规则 9：改接口必须同步该目录）。

## 数据与运行住哪

- **仓库内**：只有代码、配置、文档；`knowledge-base/**` 是用户/领域数据（不入 git）。
- **`~/fin-data/`**：路由 home、llm-config 等（③b 已拍板为数据与运行时唯一家，迁移进行中；rebaseline §0.5.6）。
- **`$XDG_STATE_HOME/fin-analyse/`**：semantic-research SQLite、runtime-truth、证据目录等 durable state。
- **运行单元**：gateway 常驻 + 8 个 Daily unit 实例 + ZSXQ poller/consumer（systemd user 单元）。

## 谁该读什么

| 你是谁 | 先读 | 再读 |
| --- | --- | --- |
| 新读者 / 新 AI 会话 | 本文 | [GLOSSARY](../GLOSSARY.md) → [NOW](../pm/NOW.md) |
| 维护者 / 施工 | [module catalog](internal-module-catalog.md) 对应条目 + 对应 [design 页](../design/) | [AGENTS.md](../../AGENTS.md)（家规） |
| 想懂为什么这样设计 | [rebaseline §0.5](../pm/rebaseline-20260827.md) | [user-design-principles](user-design-principles.md)、[DECISIONS](../DECISIONS.md) |
