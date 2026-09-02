# 统一宏观查询接口 A（macro brain）· 短设计（2026-09-02 定稿）

## 目标

一条宏观查询入口 `read_macro_brain`：聚合三类宏观来源，返回结构化、带时效与
来源的宏观上下文；是 G 主线之外的宏观/外围补充，不冒充老师观点。

## 来源与边界

- 外置大脑 SharedKnowledgeBrain（`runtime/shared_brain/items.jsonl`）：
  scope ∈ {methodology_memory, external_reference, framework}，排除
  MARKET_DATA；书卡带 source ref / scope / 非 G lens。
- ZSXQ 宏观参考（reference 层）：普通栏市场复盘/宏观问答 + 星大派每日热点
  （并入，标注 ai_summary 参考）。G 层栏目（特刊/锐评/好问题等）不进。
- search_web 联网宏观检索：默认 guided（模型按 search_needed 调用、带来源
  与时点）；`web_search_mode` 可配 auto（接口内走智谱 web 桥，真实联网）。

## 条目维度

`published_at / effective_window（event|trend）/ impact_scope（global→
geopolitics→national→market→sector）/ priority`；查询按 priority 排序。

## 宏观识别（离线预计算）

- 增量自动打标：随 ZSXQ consume 只处理新增文章，产出
  `runtime/cognition/macro_index.json`（含规则版本号），不每天全库扫。
- 信号：强标签（每日热点 ai_summary、普通栏标题/正文宏观词且 companies 少）+
  正文词频 + 公司研报特征排除；宏观词表/排除规则进 config。
- 人工校准只三处：首次上线清单抽查、规则版本变更回归、反馈回流批量修。

## 接口 A 行为

返回 `{zsxq_macro[], shared_brain_cards[], search_needed, suggested_queries[]}`
（上限各 3）；read_g_context.external_brain 槽薄引用同一实现。

## 验证

- 打标器单测（事件/趋势、公司研报排除、每日热点并入）
- 首版全库候选清单交 owner 抽查校准
- 端到端：宏观问询 → read_macro_brain → search_web 补充 → 引用带来源

## 风险

- 外部联网（auto 模式）产生额度/时延，默认 guided。
- 宏观是语义判断：误判靠反馈回流校准，不在线硬修。
