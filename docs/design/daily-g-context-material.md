# daily-g-context-material · 设计稿（规则5，合入后删除）

> 起因：owner 2026-08-31 实弹反馈「简报看不出重点、没用到 G 认知」。核账：Daily
> 材料三键（portfolio / market_overview / g_reference），g_reference 明确「非 G
> 基准，仅参考」，G 认知主缝（g_context 老师工作集）从未接入 Daily 链；简报中的
> 体系先验全继承自上一检查点基线文本。行情半边同日已修（97a9c74），本稿只解决
> 「体系感缺失」。owner 拍板走 A（本稿），设计门过审后施工。

## 目标 / 非目标

**目标**：四班检查点 prompt 注入第四材料键 `g_context`（老师体系证据），简报按
体系对表，不只记账。

**非目标**：不改 delivery 状态机与 durable schema；不动 consult 链；不重构
KnowledgeReferenceReader 的检索锚点（记观察项）；不改 B1 盲评协议。

## 设计

1. **源缝**：复用 `AgentRuntimeContextProvider.resolve`（与 read_g_context 同源，
   production_capability_provider.py:210 的底层）。generator 每班构造一次
   `AgentRuntimeContextProvider(kb_root=knowledge_base_root)`，与现有
   KnowledgeReferenceReader 同构。
2. **选材锚**：`tickers` = 当班 portfolio 持仓标的（portfolio material 同源读出，
   经 position topic 推断获得主题锚）；`question` = 固定检查点问题；
   `max_g_events=8`（与 consult 对齐）；`now` = 当班 evidence cutoff（时点诚实）。
   不传 positions 全量对象——持仓细节已由 portfolio 材料承载，G 缝只做选材锚。
3. **渲染**：标签「# G 认知参考（老师体系证据）」；取 resolve 分层投影
   （置顶/框架/事实/关联）紧凑文本，上限 4000 字（与 g_reference 同截断口径）；
   items 空 → None → `l1_material_g_context_unavailable` gap（沿用
   `_material_gaps` 一码一因）。
4. **gap 语义**：resolve 自身 typed gaps（g_working_set_manifest_missing 等）原样
   并入产品 data_gaps（可见、不阻塞班次）；resolve 异常 → material None + gap，
   与 portfolio 同级 try/except，不触发班次失败。
5. **检查点覆盖**：四班全上（同一装配方，无分班特判）。
6. **键序**：`_MATERIAL_KEYS = (portfolio, market_overview, g_context, g_reference)`
   ——G 基准在非 G 参考之前；prompt 输出要求不变。

## 契约与失败面

- capability value schema 不变（g_context 只进 prompt 材料，不出新 tool）；
  delivery/outbox/ledger 零变更；无 durable state 变更。
- 新增失败面仅「resolve 慢/异常」：oneshot 单元无交互时延压力；typed gap 诚实
  显形，最坏退化 = 回到今天的简报。

## 验收

- 单测三臂：有料注入（items 非空进 prompt）/ 空（gap 显形）/ resolve 异常
  （不阻塞班次）。
- 实弹：当日 close/postmarket 班起看材料与重点变化；owner 一周日用反馈为「在用」
  证据（规则10）；盲评沿用 B1 口径，待行情+G 双修后复评。

## 待评审问题（固定四问 packet）

①契约破坏？②durable state 时序/幂等（本稿判断：零 durable 变更——请验证）？
③引用闭包/失败面漏项？④相对直接 Agent 退化？（G 注入是否让简报劣于直接问
Agent；材料贫时是否逼模型无中生有）
