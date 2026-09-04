# 直播总结认知档位（source_nature 第四档）

> 状态：设计稿（待外审）。合入后本文件删除，Git 即归档。
> 规则 5 判据：认知域 durable schema（readmodel 闭集）+ 标注协议契约 → 按核心处理。
> 背景：owner 2026-09-04 提议「直播总结值得作为 G 认知、G 认知分层加一层直播总结」，
> 拿不准，指示走设计门后按推荐汇总推进。

## 1. 问题与材料事实

- 09-04 11:07 普通栏文章（`zsxq-45548825112521288`，tags=`直播总结#/粉丝提炼#`）：
  粉丝 AI 总结的老师 9/3 晚直播，含口播级要点（十六字重申、9/23 前面子行情
  看 4100、散户走机构进、算力上游偏好）。质量有实证：「2.3 万量能线前继续
  十六字」与老师亲笔锐评口径完全一致；次日锐评「符合昨晚直播科技触底的预期」
  反向印证直播内容。
- 现状错位：内容是老师口播（高权威），载体是粉丝 AI 转述（不可回验——直播
  无存档）。现归普通栏 reference 层：不进 G 提名（栏目闭集）、不进深化
  （同一闭集）、问询仅全文检索可达。
- 量级：全库直播总结 2 篇（08-13、09-04）——零星粉丝行为，非常设栏目。

## 2. 方案：分档不分层

「G 认知分层加一层」在结构上不成立（2 篇喂不起一条产线，规则 11）；成立的是
**来源档位**——readmodel 的 `source_nature` 闭集本就是分层机制（现三档
`G_ORIGINAL / MIXED_PUBLISHED_REPORT / AI_ASSISTED_CONTENT_MIXED`，各档绑
降权纪律先例），加第四档随单元走：

1. **readmodel 闭集加值** `SPOKEN_FAN_TRANSCRIBED`（老师口播·粉丝转述）：
   `SOURCE_NATURES` frozenset 与 `_CognitionSource.source_nature` Literal 两处
   同步；parser 两处映射规则（来源表 cell 与单元「来源性质」行）加分支——
   cell 含 `spoken/fan-transcribed` → 新档。**关键正确性点：无标记来源默认
   `G_ORIGINAL` 的现状不变**，新档只有显式标记才进（防静默升格）。
2. **标注协议两行降权纪律**（草案随起草交付，owner 下批终审后入
   manual-annotations 头部纪律段，比照 mixed_published_report 既有写法）：
   `spoken_fan_transcribed` 的方向/选择/行动指导不称作 G 的选择或行动指导；
   引用一律标「直播口播·粉丝AI转述」，不写「老师认为」；不单独支撑动作；
   与亲笔冲突以亲笔为准；无法与亲笔对表的孤证在「验证」行标 unverifiable。
3. **人格一句**（consult-agent/CLAUDE.md，备份 r11）：引用直播总结类材料
   （tags 含 直播总结/粉丝提炼）按上述降权纪律执行。
4. **特批入档走现行协议**：candidates 提名口不开；owner 从 index 直接勾选
   （协议既允许），CC 起草 09-04 这篇的候选单元草稿交付 owner 下批
   （起草→机验→owner 终审，批次既有流程）。
5. **波及面核对**：source_nature 无 readmodel 之外的代码消费
   （runtime_context/read_capabilities 零分支）；纪律执行面 = 标注散文 +
   人格，无注入逻辑改动。

## 3. 明确不做（防蔓延）

- 不建独立「直播层」产线（提名通道/工作集/注入纪律全套）——2 篇/3 月，
  等第三篇出现再按使用证据开 candidates 对 `直播总结#` tag 的提名口。
- 不动 G 栏目闭集与 `classify_g_source`（栏目分类保持，直播总结仍是
  普通栏 reference 检索面）。
- 不动 read_g_context 注入逻辑与 G 工作集。
- 不改既有三档语义与默认 `G_ORIGINAL` 回落。

## 4. durable state / 并发 / 幂等

- 改动面 = readmodel 生成器闭集 + parser 规则；下游 read_g_context 只读
  payload 的 units/sources 投影，新字段值对不认识它的读者是透传字符串，
  无破坏（schema_version 不动，值域扩一档，闭集校验放行）。
- 重建幂等：annotation 指纹变化触发 rebuild gen+1（既有机制）；已发布
  三档单元不受影响（值域只增不改）。
- 标注 md 是 owner durable 数据：纪律两行与特批条目均走「CC 起草 →
  owner 终审」批次流程，本施工不直接写 md。

## 5. 验收

1. 单测：新档 parse（来源表 + 单元行两条路径）、validate 放行、无标记
   来源仍默认 G_ORIGINAL（回归）、重建 gen+1 携带新档。
2. 人格 r11 落盘（含并行会话当日新增条款共存核对）。
3. 起草草案交付（09-04 直播总结候选单元 + 协议两行纪律文本）。
4. owner 终验 = 下批标注终审该单元入档；问询实弹引用时降权纪律被执行。
