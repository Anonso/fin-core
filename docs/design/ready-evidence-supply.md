# 设计：read_ready_evidence 供料换 canonical 索引（BUG-012 遗留决策 B）

2026-08-30 · owner 已裁方向 B（扩展供料）· 一项一份，合入后删除（Git 即归档）

## 1. 问题

`read_ready_evidence` 的 same-day reference lane 从 `priority_events.jsonl`
取候选，而该缓存按设计只记 T0/T1 推送候选事件（近期行全部 G 级
teacher_original）→ `_is_reference_eligible` 恒滤空 → 缝对几乎所有提问诚实空
（7 天 12 调 0 ok）。工具描述已在 BUG-012 第一刀专项化；本设计修供料。

## 2. 目标 / 非目标

目标：lane 候选源换成 canonical `index.json`（G 工作集 manifest 同一底座，
08-29 实测 1200 行、每日更新、含普通栏 712 行），让当天普通栏/观察类材料
按既有契约注入。

非目标：不改 G lane；不改 priority cache 写方；不动下游 15 道 strict
provenance 校验；无 LLM、无新 durable state、无写路径；好问题/锐评/特刊等
G 级材料不入 reference lane（仍走 G lane，Z/G 边界不变）。

## 3. 方案（单点换源）

`_resolve_recent_reference` 的候选来源从 `_read_cache_candidates(kb_root)`
换成新函数 `_read_index_reference_candidates(kb_root)`：

- 读取：复用 `_read_bounded_owned_regular_file_at(kb_root, "index.json",
  _MAX_PRIORITY_CACHE_BYTES)`（有界、owner-only、与 `_read_g_index_articles`
  同款安全读取）。
- 投影（index row → lane candidate dict，吸收形状差异的唯一新增面）：

| index 字段 | candidate 字段 | 说明 |
| --- | --- | --- |
| `id` | `article_id` | 材料解析按 id 找 exact markdown/深读 envelope，不变 |
| `title` | `title` | |
| `date` | `published_at` | `_candidate_time` 首选槽；同日过滤用 |
| （无） | `source_classification` | 置空串——资格判定落到既有列检查 |
| `column` | `column` + `metadata.column` | 普通栏经 `_candidate_column` 资格放行 |
| `tags` | `keywords`（≤8 项、各 ≤16 字，超界截弃） | 相关性门的确定性命中项 |
| `companies` | `companies` | 相关性门 ticker/company 重叠路径 |
| `path` | `metadata.path` | 材料解析可用 |

- 过滤链不变：排除集（pinned/fresh-G）→ 分类排除 → `_is_reference_eligible`
  → `_is_same_day` → `_reference_is_relevant`。效果：当天普通栏（及标题带
  提问的 QA 行）高相关项入选；G 级列与版本强势英雄等一律不入。
- 失败语义：索引缺失/损坏 → 返回空候选 + 新 typed gap
  `recent_reference_index_unavailable`（诚实空，不伪装、不崩溃）。
- `_recent_reference_to_candidate` 及其下游全部不动（材料解析按 article_id
  自找 exact 文件并绑 SHA-256，幻觉防线保留）。

## 4. 测试

1. 当天普通栏高相关行 → 产生候选且材料解析成功（端到端 reader 单测）。
2. 非当日行 / G 级列行 / 低相关行 → 不入选。
3. `index.json` 缺失与损坏 → typed gap、不抛异常。
4. 回归：本 lane 不再读 priority_events.jsonl（该缓存回归 G/fresh 专有）。

## 5. 风险与回滚

- 只读路径、单 commit 可回滚；无并发/时序面（无写方、无共享状态变更）。
- 供料变宽的可控面：同日普通栏行数（9–15/天）× 相关性门，注入量天然有界
  （`_MAX_ITEMS` 上游已有预算与尺寸门）。
- 与 BUG-007 教训对齐：单一 canonical 根，无新旧双轨。

## 6. 验收

- 全量测试绿；新增测试过。
- 实弹探针（提问涉当日普通栏内容）→ 工具被调、items 非空、gaps 无
  ready_evidence 码；公告类问题 → 正确走 read_external_evidence。

## 7. 设计门裁决记录（2026-08-30 · deepseek-v4-pro/max · 0P0/2P1/6P2）

| # | 发现 | 裁决 |
| --- | --- | --- |
| P1-1/2 | classification 置空串会被 `_project_item` 分类校验全拒（只认 market_observation/observation 或 teacher_original+QA 列），缝仍恒空 | **采纳修正**：普通栏行如实投影 `source_classification="observation"`（既有 reference 级分类；owner 08-28 已定普通栏为 reference tier，语义成立） |
| P2-3 | 列排除是 denylist，未来新增 G 列会静默放行 | **采纳**：投影即 allowlist——只有 `column=="普通"` 的行进入候选 |
| P2-4 | date 语义与索引更新时点未声明 | **已核实**：writer `update_index` 的 date 取发布时间、ingest 当天即写（0829 行实证）；写方非原子为既有特征，torn 读 → parse 失败 → typed gap 诚实空，写方原子化不属本刀 |
| P2-5 | 新 gap 码落账未说明 | **采纳**：presentation.py GAP 中文表加 `recent_reference_index_unavailable` 条目 |
| P2-6 | 排除集 id 空间一致性未核实 | **已核实**：index.id 与 cache article_id 同源（article frontmatter id）；补去重测试 |
| P2-7 | `_read_cache_candidates` 闭包去留 | **已核实**：fresh-G 路径（runtime_context.py:926）仍调用，保留，无死代码 |
| P2-8 | 券商名不在 companies，company 重叠通道对券商问题失效 | **记录不修**：既有缺口（tags/标题通道可用），BUGS 残余记档 |

四问结论：① 契约无 P0 破坏（P1 修正后消除）；② 无写入成立，读侧 torn 风险已记；③ 换源干净无死代码；④ 注入有界、不劣于直接 Agent，残余偏差已记。
