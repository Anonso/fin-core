# instrument_scores 增量接线（NOW #26 · D-044③ 施工设计）

状态：设计冻结，待 D3 建造静默结束后施工；施工前过设计门（run-design-gate §0，
R1 durable state + R2 ingest 路径命中 → 外部审计一次）。合入后本文件删除。

## 目标与不变式

注册表当前只由手动 `scripts/backfill_instrument_scores.py` 更新（09-03 快照），
增量断链。本设计把解析写入接进采集尾部：每次增量抓取后，新入库的普通栏
评分表自动进注册表，无需人工跑批。不变式：read_instrument_scores 契约不变；
注册表 schema（fin.instrument-scores/v1）不变；解析器（parser v2）不变；
backfill/manage 脚本保留不动（手动兜底 + 人工闭环）。

## 接缝

1. 新函数 `update_instrument_scores(knowledge_base_root: Path, *, saved_ids:
   Iterable[str]) -> InstrumentScoreUpdateReport`，落在 owner 模块
   `fin_analyse/ingestion/instrument_scores.py`（家规 6，中心在特性内）。
   报告字段：parsed/added/updated/skipped/warnings。
2. 处理集 = saved_ids ∪ 水位补漏（注册表现有最大 article_date 之后的
   index.json 普通栏行）。saved_ids 主路径；水位 union 只为自愈
   「sidecar 失败/进程中断」的漏批窗口，无新增状态。过滤口径与 backfill
   一致：column=普通、score≥6.0（D-037；采集端已拦 <6.0，此处双保险）。
3. 单篇解析与 backfill 完全同路：index 行 + zsxq_sources.jsonl
   （image_descriptions/published_at）+ a_share_name_map +
   normalize_inline_codes + parse_article_records。
4. 写入 `upsert_records`：原子 tmp+rename、0600/0700、record_id 幂等
   （sha256(source_id:code:sequence)）、内容不变即 no-op。
5. 调用点：`cdp_scraper.run_incremental_with_result` 尾部新 §9c（deep-read
   之后、boundary healing 之前），照 macro_index sidecar 先例：惰性 import、
   try/except → logger.warning + result.warnings.append("instrument_scores_failed:
   …")，绝不阻塞 ingest。priority-scan 尾部（all_saved 作用域处）加同一行调用。

## durable state 时序 / 并发 / 幂等

- 时序：调用点在文章落盘与 index.json 更新之后，index 行必然可读。
- 并发：自动写入方唯一（采集进程）；backfill/manage 为人工节奏，与现状
  相同不加锁（家规 11：无事故不加机制；upsert 原子写已保证不撕裂文件）。
- 幂等：record_id 唯一键 + 内容 hash，重跑/重放均为 no-op；崩溃恢复 =
  下次运行的水位 union 兜住。

## 引用闭包与退化

纯新增：一个函数 + 两处调用 + 一条惰性 import；无删除。公共入口
read_instrument_scores 不动，新鲜度提升，相对直接 Agent 无退化。

## 验收

1. 单测：临时 KB 上构造普通栏文章行 → 记录落库、needs_review 带原因；
   同参重跑 added=updated=0。
2. 探针：对当前 KB 以 09-05 saved_ids 调用 → 增量与 09-05 backfill 一致。
3. 实弹：下一次定时采集出现新普通栏评分表 → 注册表自动增长（owner 可查），
   无人工步骤。

## 已知边界（不在本设计内解决）

- 9/5 起评分表出现「项目评分/情绪热度」新列与分号单行 k:v 格式，现行
  parser v2 不认 → 落 needs_review（设计内行为：不丢行、不静默改）。
  列语义映射（项目评分→利好度？情绪热度→共识度？）与分号格式支持是
  parser v3 决策，归 owner 拍板后另行施工。
- 名称-代码冲突时 name_map 覆盖正文显式代码（9/5「利通科技(603629)」被
  映射为 920225）的行为是否合理，随 parser v3 一并裁。
