# g-mainline-growth-v1 · 设计页（G 主线生长管线 v1：候选提名 → 起草机验 → 扫批入档）

> 依据：D-038（2026-09-03 owner 拍板）。定位：施工输入，合入后本页删除（Git 即归档）。
> 状态：**设计门未过**——开工前先送外审（scripts/codex_open.sh --sandbox read-only，
> 固定四问 packet），裁决后施工。

## 目标 / 非目标

**目标：**

- 主线随 ZSXQ 增量 ingest 生长：机器提名（来源门选材+去重，纯读）→ CC 起草+机验
  → owner 扫批（唯一人工步，D-038 否决全自动零复核）→ 入档自动重建。
- 修缺口 B：标注文档编辑独立触发 rebuild（现状只看 WS 身份，`cognition_mainline_rebuild.py:104-108`）。
- 标注文件名去日期化（窗口滚动不再改硬编码路径，`consume_zsxq_capture_folder.py:499`）。
- 消费探针：主线投影附件出场记 unit_id 审计行（回答「接线之外有没有真被消费」）。

**非目标：**

- 不改 read-model schema（`fin.cognition-mainline-readmodel/v1` 冻结）；重建仍走 frozen CAS publisher。
- 不建 unreviewed 隔离区状态机（家规 11 无真实使用证据，D-038 已否决）。
- 不动来源门语义、fresh 工作集、PIT 投影分层。

## 数据与 schema 事实（已核实 2026-09-03）

| 事实 | 值 / 位置 |
| --- | --- |
| 标注文档 | `<KB根>/manual-annotations/g-cognition-mainline-2026-06-to-2026-08-19.md`：538 行 / 26 单元 / evolution 4 节点，文档头 as_of=2026-08-20；owner durable 数据（家规 4 迁移史见 81c28d4） |
| read-model 工件 | `$XDG_STATE_HOME/fin-analyse/cognition-mainline-readmodel-v1/readmodel.v1.json`：generation 41；重建审计 `cognition-mainline-rebuild.v1.jsonl` 最近 PUBLISHED→ALREADY_CURRENT |
| rebuild 触发条件 | 仅 WS READY + `pit_working_set_identity` 变化（`cognition_mainline_rebuild.py:97-121`）；标注编辑不触发（缺口 B 本体） |
| 工件内已有 hash 字段 | readmodel 顶层 `content_hash`——语义待核对（payload hash ≠ annotation hash），缺口 B 复用或新增缓存比对，设计门定 |
| annotation 缝 | `default_knowledge_base_root()/manual-annotations/`（`consume_zsxq_capture_folder.py:499`）；库函数 `generate_cognition_mainline_readmodel(annotation_path, ...)` 路径已是参数（`cognition_mainline_readmodel.py:621`） |
| 投影消费点 | `runtime_context.py:578-644`：mainline_projection + cognition_mainline_projection 两附件构建处（探针落点） |
| 来源门 | `classify_g_source`（`source_contract.py:46`）闭集：星大派特刊/锐评/每日热点/人脉/老师原答好问题 + 凤仙郡小故事 |

## 设计（五部件）

1. **候选扫描器**（新模块 `fin_analyse/guo_teacher_research/mainline_candidates.py`）：
   consume ingest 后与 rebuild_if_stale 同位触发（typed 审计行、永不阻断 ingest，沿用现有
   rebuild 调用模式）；输入 = index 中 `published_at > 标注 as_of` 的文章，经
   `classify_g_source` 选材，对照 readmodel units 的 `source_ref` 去重；输出候选草稿
   markdown 到 `$XDG_STATE_HOME`（纯读 index/KB，不写 KB）。只读既有读路径，不复制
   index（各 owner 边界，g-cognition.md 数据表）。
2. **起草协议**（CC 会话内流程，非代码）：候选 → owner 勾选 → CC 按标注契约生成
   单元块（G 原文摘录/深化表达/来源性质逐段归属/Agent 推理分栏）→ **机验：摘录 span
   逐字 ⊆ 原文**（小脚本，失败=不入档 fail-closed）→ owner 终审追加进标注文档。
   混合发布材料（mixed_published_report）必须逐段归属标注。
3. **缺口 B 修复**：rebuild_if_stale 增加标注内容 sha256 比对——标注变更即
   generation+1 重建，不再等下次 ingest 顺带生效；与现有三条件（READY/全量校验/
   身份变化）合并为四条件，失败仍 fail-closed 保持旧工件。
4. **去日期化**：文档改名 `g-cognition-mainline.md`（家规 4 迁移：owner-only 备份+
   manifest→迁移→删旧名）；consume 读点、测试 ANNOTATION_DOC 锚点、annotation_ref
   同步；文档头改为「窗口 2026-06-01 起 · as_of=<最后复核日>」，as_of 滚动只进不退
   （对齐 evolution available_at 单调不变量）。
5. **消费探针**：`runtime_context.py` 投影附件构建处追加 typed 审计行
   （unit_ids + generation + as_of 检查结果），落 read-capability 审计侧；不进 PIT
   工件，不改变投影内容。

## 开工四句话（规则 8）

改哪些：新增 mainline_candidates.py + 机验脚本；改 cognition_mainline_rebuild.py、
consume 读点、runtime_context.py 探针、测试锚点；标注文档改名（家规 4 流程）。
影响入口：ZSXQ ingest 管线（附加扫描步）、read_g_context（探针审计行，投影内容不变）。
怎么验证：单测（扫描选材/去重/缺口 B 触发/改名路径/探针行）+ 实弹（ingest→候选→
起草 1-2 单元→入档→generation+1→投影可见新单元+审计行）；1211 绿基线不回退。
为什么不是别的做法：全自动入档已被 D-038 否决（混合材料归属机验不了）；隔离区
转正状态机被家规 11 否决；扫描器写 KB 被否决（裁决权留 owner）。

## 验证方式

- 单测：候选选材/去重/纯读、缺口 B（标注变→PUBLISHED，标注不变→ALREADY_CURRENT）、
  去日期化路径解析、摘录机验（正/反例）、探针行 schema。
- 实弹验收：一次真实 ingest 全链走通；read_g_context 三字段探针照旧全过。
- 回归入口：`tests/guo_teacher_research/test_cognition_mainline_readmodel.py` 及
  consume/g-cognition 既有测试。
