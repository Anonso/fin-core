# daily-g-context-material · 设计稿（规则5，合入后删除）

> 起因：owner 2026-08-31 实弹反馈「简报看不出重点、没用到 G 认知」。核账：Daily
> 材料三键（portfolio / market_overview / g_reference），g_reference 明确「非 G
> 基准，仅参考」，G 认知主缝（g_context 老师工作集）从未接入 Daily 链；简报中的
> 体系先验全继承自上一检查点基线文本。行情半边同日已修（97a9c74），本稿只解决
> 「体系感缺失」。owner 拍板走 A（本稿）。
> 设计门（codex-open · deepseek-v4-pro · max，2026-08-31）：8 发现（P1×3/P2×5，
> 无 P0），**8 采纳 0 驳回**，elapsed≈570s；裁决已并入下文（标「门」处）。

## 目标 / 非目标

**目标**：四班检查点 prompt 注入第四材料键 `g_context`（老师体系证据），简报按
体系对表，不只记账。

**非目标**：不改 delivery 状态机与 durable schema；不动 consult 链；不重构
KnowledgeReferenceReader 的检索锚点（记观察项）；不改 B1 盲评协议（输出要求
变更后复评须同条件，见验收）。

## 设计

1. **源缝**（门 P1-1/P2-2）：复用 `AgentRuntimeContextProvider.resolve`，**flat
   渲染，不引 read_g_context 私有分层构建器**。`AgentRuntimeContextProvider`
   构造放当班 try 内（与 portfolio/overview/reference 同位，构造失败=
   material None + gap，不击穿班次）；懒 import 于函数内（沿
   daily_workspace_generator.py 既有模式，operations 不顶层依赖 guo 链）。
2. **选材锚**（门 P2-1）：`tickers` = 当班 portfolio 持仓标的；`question` = 固定
   检查点问题；`now` = 当班 evidence cutoff。**不传 max_g_events**——
   `_resolve_budget` 只收不放，有效上限即 FIN 默认 5（与 consult 链实际一致）。
3. **渲染**（门 P1-1/P1-2/P2-4）：标签「# G 认知参考（老师体系证据）」。**strict-G
   过滤**：只取 `source_bucket ∈ {pinned_source, fresh_g, latest_commentary}`
   （recent_reference 等非 G 桶绝不入本材料，守住 G/Z 边界）。逐条自包含渲染：
   `published_at 标题：guidance_brief（usage_boundary；source_ref）`，字段映射
   冻结为 resolve 条目六字段（title/guidance_brief/source_ref/published_at/
   usage_boundary/why_available）。4000 字上限按**整条**丢弃（不半条切断），
   弃条数记班次日志。items 空或全被过滤 → None。
4. **gap 语义**（门 P1-3）：**resolve 层 typed gaps 不进 product data_gaps**——
   与既有三材料键同规（一码一因，`_material_gaps` 只产 `l1_material_*`）；材料
   缺席即 `l1_material_g_context_unavailable`；resolve 层细节（manifest_missing
   等）记班次日志可见。随本刀修订 daily-delivery.md 两臂比对口径：「材料键集合
   变更时允许新增 `l1_material_<key>_*` 码」，并同步更新
   tests/operations/test_daily_workspace_generator_gaps.py 断言（家规12）。
5. **输出要求**（门 P2-5）：追加对表指令——「最值得处理」判定须对照 G 认知参考
   （一致或不一致都点名；材料缺席时明说「无体系对照」）。输出结构不变
   （1-3 项 + 较此前变化 + 哪里未知）。
6. **键序**：`_MATERIAL_KEYS = (portfolio, market_overview, g_context, g_reference)`
   ——G 基准在非 G 参考之前。

## 契约与失败面

- capability value schema 不变（g_context 只进 prompt 材料，不出新 tool）；
  delivery/outbox/ledger 零变更；无 durable state 变更（设计门核读证实 resolve
  调用图全只读）。
- 新增失败面仅「构造/resolve 慢或异常」：oneshot 无交互时延压力；typed gap
  诚实显形，最坏退化 = 回到今天的简报。

## 验收

- 单测：有料注入（strict-G 过滤后非空进 prompt、含对表指令）/ 空（gap 显形、
  输出要求带「无体系对照」）/ 构造或 resolve 异常（不阻塞班次）+ 既有 gap
  断言更新。
- 实弹：当日 close/postmarket 班起看材料与重点变化；owner 一周日用反馈为
  「在用」证据（规则10）；盲评沿用 B1 口径同条件复评（行情+G+对表指令三变更
  后）。

## 设计门裁决记录

- packet：本稿 v1 + 固定四问；评审者 scripts/codex_open.sh --sandbox read-only
  （deepseek-v4-pro · max）；elapsed≈570s；发现 8（P1×3/P2×5/P0×0）；采纳 8、
  驳回 0。逐条落点：P1-1→§1/§3、P1-2→§3、P1-3→§4、P2-1→§2、P2-2→§1、
  P2-3→§4、P2-4→§3、P2-5→§5。
