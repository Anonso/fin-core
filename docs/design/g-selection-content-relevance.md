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

## 装配预算裁决与闭环（2026-09-02，方案 A 落地）

现象分两层，已在装配层全部闭环：
1. 条目化候选丢选择层相关性信号——`_resolve_fresh_g` 产出 entry 时不再携带
   `_enriched_*`（compact 抽取的问句关键词面），`_apply_budget` 只剩标题等
   浅字段、按 recency 重排 → 更新的无关特刊挤掉问句命中特刊。修复：entry
   透传 `_enriched_*`（私有字段，不进 llm/audit 投影）。
2. `_apply_budget` 静态巷道优先级 pinned > commentary > reference > fresh_g：
   reference 无条件占位。裁决取方案 A——pinned/commentary 冻结语义不动，
   reference 与 fresh_g 按问句相关性竞争（等分时 reference 仍优先，保留原
   巷道意图）；稳定排序继承各巷内顺序（fresh=选择层 rank，reference=
   fact-first）。B 第二刀（泛问题 fresh 特刊 ≤2）语义不变。

真实 resolve 对照（`now=2026-09-02T21:30`）：
- “封测行业点评”：llm_context = pinned + 锐评 + 每日热点 + 大金融人脉 +
  8/13 特刊（原先两条 fresh 名额被 Trump Zone/人脉按 recency 吃掉）；
- “半导体先进封装与封测怎么看”：llm_context 含 8/13 特刊（原先两条
  recent_reference 占满名额，特刊被挤出）。

回归：guo 大集 546 passed（新增装配竞争/冻结/端到端 4 测）；全仓 3024
passed。剩余观察：问句含公司名碎片（“长电科技 封测”）时 2 字滑窗把“科技”
灌成主题，泛“科技主线”特刊仍压过 8/13 特刊——选择层排序质量问题，非装配
契约，见 BUG-023（公司名不从问句解析是方案 C 既知边界）。
