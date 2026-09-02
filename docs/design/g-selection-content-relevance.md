# G 选择层内容相关性（3.3b）· 短设计（2026-09-02）

## 现象

8/13《科技半导体三大板块》特刊在 manifest 且 compact artifact 含
先进封装/封测/通富/长电等结构字段，但问“封测/长电科技”时未被选入
fresh_g——被更新的特刊（Trump Zone/9 月展望）挤掉。

## 根因（已实证）

1. 意图 token 仅来自 `_TOPIC_KEYWORDS` 有限词表与 request 结构化字段；
   问句里的“封测/先进封装/长电科技”不落 token。
2. `_candidate_relevance_score` 对公司/主题做精确等值匹配；主题含“先进
   封装”遇 token“半导体”只算包含性缺失，多个特刊同分 → 按 recency 排序。
3. 公司名不从问句解析（无 company identity），只能靠 request.company。

## 方案（推荐组合）

- B：相关性打分改“双向包含”（token ∈ 候选 or 候选 ∈ token），并给
  compact 信息单元更高权（对齐 `_agent_visible_relevance_score`）。
- C：manifest/compact 建立可检索关键词面（title + tags + related_topics +
  related_companies + theme names），问句 token（含 2 字滑窗）落面即计分；
  解决“封测/先进封装”这类非词表词。
- A（辅助）：`_TOPIC_KEYWORDS` 补常用主线词（先进封装/封测/HBM/存储等），
  仅缓解，不做主方案。

## 验证

- 问“封测行业点评”/“长电科技 封测”应把 8/13 特刊排进 fresh_g；
- 既有“锐评/每日热点各最新一条冻结”“特刊容量 N”测试不回退；
- guo 全量 + 一次真实 resolve 对照。

## 边界

- 不动 pinned/commentary 冻结语义；只改“特刊/好问题”候选排序打分。
- 内容关键词面只读 compact artifact，不新增 durable store（manifest 已有
  compact_raw_sha 可溯源）。

## 进度与交接（2026-09-02）

方案 B+C 已落地并提交（问句 2 字滑窗 + compact 关键词面双向包含；
latest-focus 句式不触发滑窗）。回归：guo 大集 542 passed。

真实 resolve 对照（`now=2026-09-02T21:30`）：
- “封测行业点评”：8/13 特刊已排进 `_resolve_fresh_g` 的 fresh_g 候选
  （rank 4/8）；
- “半导体先进封装与封测怎么看”：同样排进 fresh_g 候选（rank 3/8）。

仍未闭环：最终 `llm_context` 不含该特刊。根因是 `_apply_budget`
（`runtime_context.py`）优先级 pinned > latest_commentary >
recent_reference > fresh_g，且 semantic budget=5；“封测行业点评”时
fresh_g 名额被同分更新的特刊按 recency 吃掉，“半导体先进封装…”
时两条 recent_reference 直接把 fresh_g 名额占满。fresh_g 层已排序
正确，但装配层未复用该顺序（纯 `_candidate_relevance_score` + recency）。

下一步候选（触及装配预算契约，需 owner/设计裁决）：
A. 装配层让 question-matched fresh 特刊与 recent_reference 竞争（参考
   层不无条件占位）；
B. budget 内在 pinned/commentary 之后为 fresh G 保留最少席位；
C. 验收口径停在 fresh_g 选择层，装配层按现状不改。
