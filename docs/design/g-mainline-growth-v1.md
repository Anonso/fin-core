# g-mainline-growth-v1 · 设计页（G 主线生长管线 v1：候选提名 → 起草机验 → 扫批入档）

> 依据：D-038（2026-09-03 owner 拍板）。定位：施工输入，合入后本页删除（Git 即归档）。
> 状态：**设计门已过 + 施工完成**（2026-09-04）。设计门：codex-open deepseek-v4-pro ·
> max · read-only，12 发现 = P0×0 / P1×4 / P2×8，**12 采纳 0 驳回**（elapsed=1360s），
> 裁决已并入下文（标「门」处）。施工：五部件落地（e5bcddf/866f454/1ca0eac/47c62bd/
> 8697c18/d8eb47e），实弹记录见文末施工记录。

## 目标 / 非目标

**目标：**

- 主线随 ZSXQ 增量 ingest 生长：机器提名（来源门选材+去重，纯读）→ CC 起草+机验
  → owner 扫批（唯一人工步，D-038 否决全自动零复核）→ 入档自动重建。
- 修缺口 B：标注文档编辑独立触发 rebuild（现状只看 WS 身份，`cognition_mainline_rebuild.py:104-108`）。
- 标注文件名去日期化（窗口滚动不再改硬编码路径，`scripts/consume_zsxq_capture_folder.py:499`）。
- 消费探针：主线投影附件出场记 unit_id 审计行（回答「接线之外有没有真被消费」）。

**非目标：**

- 不改 read-model schema（`fin.cognition-mainline-readmodel/v1` 冻结）；重建仍走 frozen CAS publisher。
- 不建 unreviewed 隔离区状态机（家规 11 无真实使用证据，D-038 已否决）。
- 不动来源门语义、fresh 工作集、PIT 投影分层。

## 数据与 schema 事实（已核实 2026-09-03；门评修订后）

| 事实 | 值 / 位置 |
| --- | --- |
| 标注文档 | `<KB根>/manual-annotations/g-cognition-mainline.md`（2026-09-04 去日期化改名，原名 `g-cognition-mainline-2026-06-to-2026-08-19.md` 见家规4迁移史 81c28d4 与 09-04 备份 manifest）：538 行 / 26 个 CU 单元（另有「主线变化证据」「预测窗口与观察状态」2 个非单元三级头）/ evolution 4 节点，文档头 as_of=2026-08-20；owner durable 数据 |
| read-model 工件 | `$XDG_STATE_HOME/fin-analyse/cognition-mainline-readmodel-v1/readmodel.v1.json`：generation 41；重建审计 `cognition-mainline-rebuild.v1.jsonl` 最近 PUBLISHED→ALREADY_CURRENT |
| rebuild 触发条件 | 仅 WS READY + `pit_working_set_identity` 变化（`cognition_mainline_rebuild.py:97-121`）；标注编辑不触发（缺口 B 本体） |
| 工件内已有 hash 字段 | readmodel 顶层 `content_hash` = payload canonical hash（构建期占位后按最终 canonical 计算，`cognition_mainline_readmodel.py:655-675`），**非标注 hash**；schema `_ReadModel` `extra="forbid"` 冻结（:185），不加字段——缺口 B 基线走 sidecar，不复用（门评 P1 定案，见部件3） |
| annotation 缝 | `default_knowledge_base_root()/manual-annotations/`（`scripts/consume_zsxq_capture_folder.py:499`）；库函数 `generate_cognition_mainline_readmodel(annotation_path, ...)` 路径已是参数（`cognition_mainline_readmodel.py:620`） |
| 投影消费点 | `runtime_context.py:578-652`：mainline / methodology / cognition_mainline 三附件构建处（探针落点，见部件5） |
| 来源门 | `classify_g_source`（`source_contract.py:46`，:55）闭集：星大派特刊/锐评/每日热点/人脉/**星大派好问题** + 凤仙郡小故事（门：列名纠正——「老师原答好问题」在代码中不存在）；每日热点 usage=`ai_summary_reference`（AI 汇总边界信号，mainline 候选排除，见部件1） |
| index 选材键（门评补充） | index 1242 条全用 `date`，**无 `published_at` 字段**；provenance 覆盖现状：星大派好问题 36/36 无 `is_qa`、星大派特刊 12/17 无 `source_classification` |

## 设计（五部件）

1. **候选扫描器**（新模块 `fin_analyse/guo_teacher_research/mainline_candidates.py`）：
   consume ingest 后与 rebuild_if_stale 同位触发（typed 审计行、永不阻断 ingest，沿用现有
   rebuild 调用模式）；输入 = index 中 `date > 标注 as_of` 的文章（门：index 无
   `published_at`，选材键=date），经 `classify_g_source` 选材，**参数与 G 工作集同源取**
   （teacher_original=True、`is_qa=entry.get("is_qa") is True` 等，对齐
   `g_working_set.py:453-456`；门：参数来源定案——不新建 provenance 通道，好问题 36/36
   无 is_qa 等现状=v1 自然不提名，属已知限制，补数走既有采集/标签侧 enrichment，不在本稿）；
   **usage=`ai_summary_reference`（每日热点列）排除出 mainline 候选**（门：AI 汇总非老师
   看法，对齐 owner 2026-09-01 边界拍板；人脉列=特刊档，保留提名资格）。
   去重（门：链路定案）：index `path` 经 `_normalize_article_ref` 同语义归一为 canonical
   repo-relative ref（`cognition_mainline_readmodel.py:380`）后，与 readmodel
   `sources[].article_ref` 精确匹配（unit.`source_ref` 是 S-短 ID，不可直接对照）；粒度=
   article 级，**同文已入档者不静默丢弃**——单独列「同文已入档（潜在新增段落候选）」段
   （实证 S-0730G/S-0730M 同文双段，标注 :38-39），勾选权留 owner。输出候选草稿 markdown
   到 `$XDG_STATE_HOME/fin-analyse/`（固定名 + 0600，幂等重写：内容由 index 确定性导出、
   未入档候选每次全量重算，as_of 不推进不丢——门评 P2 定案）。只读既有读路径，不复制
   index（各 owner 边界，g-cognition.md 数据表）。
2. **起草协议**（CC 会话内流程，非代码）：候选 → owner 勾选 → CC 按标注契约生成
   单元块（G 原文摘录/深化表达/来源性质逐段归属/Agent 推理分栏）→ **机验：摘录 span
   逐字 ⊆ 原文**（小脚本，失败=不入档 fail-closed）→ owner 终审追加进标注文档。
   混合发布材料（mixed_published_report）必须逐段归属标注。
   （门补硬约束，机验脚本预检，违反=整份拒绝：a) as_of 必须 ≥ 本批新单元最大
   published_at（`_check_time_ordering` :258：processed_at ≥ available_at，否则整份
   拒绝、旧工件保持）；b) evolution 节点只能追加表尾、日期前缀非降（:263-270）。
   每批新单元**同步追加/并入对应 evolution 节点行**（前缀=新单元 published_at 日期）
   ——投影 usable_unit_ids 全部来自节点 unit_refs（`cognition_mainline_readmodel.py:1023-1031`），
   无节点行=投影不可见，实弹验收会假失败；机验顺带校验 node→unit 引用完整性。）
3. **缺口 B 修复**：rebuild_if_stale 增加标注内容 sha256 比对——标注变更即
   generation+1 重建，不再等下次 ingest 顺带生效；与现有三条件（READY/全量校验/
   身份变化）合并为四条件，失败仍 fail-closed 保持旧工件。
   （门定案：基线走 sidecar `<readmodel根>/annotation.sha256`（0600），在 publisher
   flock 临界区内（`cognition_mainline_readmodel.py:816-852`）随 PUBLISHED 成功原子
   写入；**不复用 `content_hash`**（payload hash，schema 冻结不加字段）。sidecar 缺失/
   不可读=基线未知→触发一次重建后写入（自愈，至多一次性 generation+1）。
   RebuildResult/审计行加 typed trigger 字段（identity_changed / annotation_changed /
   both）——内部类型非 frozen read-model schema，允许（门评 P2：无触发原因则缺口 B
   生效无从排查）。第四条件 hash 输入含文档路径名：改名后即使内容不变也视为变化，
   配合部件4 迁移末尾强制重建，防 `annotation_ref` 悬挂旧名（`annotation_ref` 由
   path.name 构建（:661）且无运行时读方校验——门评 P2）。）
4. **去日期化**：文档改名 `g-cognition-mainline.md`（家规 4 迁移：owner-only 备份+
   manifest→迁移→删旧名）；consume 读点、测试锚点（`test_cognition_mainline_readmodel.py`
   :23 `annotation_ref` 与 :207 `ANNOTATION_DOC`）、`annotation_ref` 同步；文档头改为
   「窗口 2026-06-01 起 · as_of=<最后复核日>」，as_of 滚动只进不退（对齐 evolution
   available_at 单调不变量）。（门补同步清单：`docs/architecture/fin-private-advisory-decision-framework.md:77,417`
   两处文档引用；迁移末尾强制一次 rebuild 刷新工件 `annotation_ref`，机制见部件3。）
5. **消费探针**：`runtime_context.py` 投影附件构建处追加 typed 审计行
   （unit_ids + generation + as_of 检查结果），落 read-capability 审计侧；不进 PIT
   工件，不改变投影内容。（门定案写路径：探针数据由 `_build_cognition_mainline_projection`
   构建结果直接携带（命中 unit_ids / generation / `g_cognition_pit_*` gap 检查结果），
   随 resolve 返回，**server 层**每请求恰好一行并入既有 trace（`CallTrace.record` 是
   唯一 trace 写点，`read_capabilities/server.py:277`）；OSError suppress；**禁止在
   `_projection_attachments()` 里写任何东西**——它在 D3 尺寸逐出 while 循环内反复调用
   （`runtime_context.py:675-677`），写在那里会一请求多行；库层零新增 IO，read_g_context
   30s 预算（`server.py:87`）不受影响。）

## 开工四句话（规则 8）

改哪些：新增 mainline_candidates.py + 机验脚本；改 cognition_mainline_rebuild.py、
consume 读点、runtime_context.py 探针、server 层探针行落点、测试锚点；标注文档改名
（家规 4 流程）；新增 sidecar `annotation.sha256`。（门：补 server 层与 sidecar。）
影响入口：ZSXQ ingest 管线（附加扫描步）、read_g_context（探针审计行，投影内容不变）。
怎么验证：单测（扫描选材/去重/缺口 B 触发/改名路径/探针行）+ 实弹（ingest→候选→
起草 1-2 单元→入档→generation+1→投影可见新单元+审计行）；1211 绿基线不回退。
为什么不是别的做法：全自动入档已被 D-038 否决（混合材料归属机验不了）；隔离区
转正状态机被家规 11 否决；扫描器写 KB 被否决（裁决权留 owner）。

## 验证方式

- 单测：候选选材/去重/纯读、缺口 B（标注变→PUBLISHED，标注不变→ALREADY_CURRENT）、
  去日期化路径解析、摘录机验（正/反例）、探针行 schema。
- 实弹验收：一次真实 ingest 全链走通；read_g_context 三字段探针照旧全过。
  （门：「三字段探针」=NOW.md 问询探针口径——工具被调、`data_gaps` 空、`status` 正常。）
- 回归入口（门：点名补全）：`tests/guo_teacher_research/test_cognition_mainline_readmodel.py`、
  `test_cognition_mainline_rebuild.py`、`test_g_mainline_projection.py` 及
  consume/g-cognition 既有测试。

## 设计门裁决录（2026-09-03 · codex-open deepseek-v4-pro · max · read-only · elapsed=1360s）

12 发现 = P0×0 / P1×4 / P2×8；**12 采纳 0 驳回**（其中 5 条在评审给定选项内做了方案选择）。

| # | 级 | 发现 | 裁决 / 落点 |
| --- | --- | --- | --- |
| 1 | P1 | 缺口 B 基线无处存放：content_hash=payload hash，schema `extra="forbid"` 不加字段 | 采纳→sidecar `annotation.sha256`（flock 临界区、PUBLISHED 后写、缺失自愈），不复用 content_hash（部件3） |
| 2 | P1 | 去重链路未闭合：source_ref=S-短 ID 不可对照 index；article 粒度会误杀同文新增段 | 采纳→path 归一化后对 sources.article_ref 精确匹配；同文已入档单独列示不静默丢弃（部件1） |
| 3 | P1 | classify_g_source 参数来源未定；好问题 36/36 缺 is_qa、特刊 12/17 缺 source_classification（writer 实测核同） | 采纳→与 G 工作集同源取参；现状不提名=已知限制，不建新 provenance 通道（部件1） |
| 4 | P1 | 探针写路径与触发次数未定；`_projection_attachments` 在逐出循环内反复调用；库层无 trace 写点 | 采纳→数据随 resolve 返回、server 层每请求一行、OSError suppress、禁在附件构建处写（部件5） |
| 5 | P2 | 改名后内容不变则 annotation_ref 悬挂旧名（:661 构建自 path.name，无运行时读方）；同步清单漏 architecture 文档与测试 annotation_ref | 采纳→迁移末尾强制 rebuild + 路径名入第四条件 hash；补同步清单（部件3/4） |
| 6 | P2 | as_of「只进不退」不足：硬约束=processed_at ≥ available_at（max published_at）+ 节点前缀非降，违反整份拒绝 | 采纳→起草协议两条硬约束 + 机验预检（部件2） |
| 7 | P2 | 起草协议漏 evolution 节点行：投影 usable_unit_ids 全来自节点 unit_refs，无节点行=不可见 | 采纳→每批新单元同步追加节点行 + 机验校验 node→unit 引用（部件2） |
| 8 | P2 | 候选草稿路径/权限/覆盖语义未定；覆盖式会丢未审候选 | 采纳→固定名+0600+幂等全量重算重写（部件1） |
| 9 | P2 | 事实两处错误：index 无 published_at（全 date）；「老师原答好问题」应为「星大派好问题」 | 采纳→事实表与部件1 选材键已改 |
| 10 | P2 | 每日热点 usage=ai_summary_reference（AI 汇总边界信号）照闭集会提名进 G 主线，冲突单一来源边界 | 采纳→mainline 候选排除 ai_summary_reference（对齐 owner 09-01 拍板）；人脉列保留（部件1） |
| 11 | P2 | 两种触发原因在 RebuildResult 不可区分，缺口 B 生效无从排查 | 采纳→typed trigger 字段（内部类型，非 frozen schema）（部件3） |
| 12 | P2 | 回归入口漏两测试文件；「三字段探针」未定义；26 单元口径经核无误（26 CU + 2 非单元头） | 采纳→点名三测试文件 + 探针定义（验证方式）；口径已写入事实表 |

评审者承重事实经 writer 逐条复核全部属实（generation 41、538 行、`extra="forbid"`、
flock :816-852、is_qa 覆盖实测 36/36 与 12/17、S-0730G/M 同文双段、逐出循环 :675-677、
30s 预算 :87）；仅一处行号偏差（S-0730 来源表实为标注 :38-39，评审所称 :17-18），
不影响裁决。

## 施工记录（2026-09-04）

- 提交链：e5bcddf（缺口B）→ 866f454（探针）→ 1ca0eac（扫描器）→ 47c62bd
  （机验脚本）→ 8697c18（去日期化）→ efdda8f（机验 span 口径）→ d8eb47e
  （reader 装配修复）。guo:v0 清运（83af6f4，NOW #17）由并行会话同期落地，
  文件集不相交。
- 缺口B 实弹：改名迁移末尾强制重建 generation 42（PUBLISHED，
  trigger=annotation_changed），annotation_ref 刷新新名，sidecar 0600 落位，
  幂等复跑 ALREADY_CURRENT。
- 机验 span 口径（实弹首跑 26 失败逐条诊断后修正，efdda8f）：弯引号是标注
  包装、只有引号**内**是摘录主张；「无可分离的纯 G 口述…」是来源性质免责语
  （info 不计失败）。修正后真实 26 单元全 span 逐字可回指，RESULT: PASS。
- **部件5 实弹发现并修复装配缺口**：生产 composition 从未注入
  cognition_mainline_reader，cognition 投影在真实入口恒为 unavailable——
  「接线之外有没有真被消费」首答为「从未」。修复后真实探针：6 单元投影
  （generation 42，framework 层可见），生产 trace 行
  `summary.cognition_mainline_consumption` 落盘，问询探针三字段全过。
- 扫描器实弹：1242 扫描 / 123 过 as_of / 16 提名 / 8 排除 ai_summary_reference
  / 99 闭集未命中；草稿 `~/.local/state/fin-analyse/mainline-candidates.md`。
- 未竟半链：候选 → **owner 勾选** → CC 起草 → owner 终审入档的首次走通待
  owner 勾选（管线唯一人工步）；自然 ingest 窗口将自动产增量草稿。
