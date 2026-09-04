# 直播总结认知档位（source_nature 第四档）

> 状态：设计稿 v2（外审后修正）。合入后本文件删除，Git 即归档。
> 外审记录：codex-glm·glm-5.3·max（packet 冻结评审，09-04），3P1/2P2/3P3
> 全采纳；台账 `$STATE/fin-analyse/design-gate/spoken-grade-20260904/`。
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
- 量级（口径=tags 含 直播总结/粉丝提炼）：全库 2 篇（08-13、09-04）——零星
  粉丝行为，非常设栏目。〔外审修正 Q3-P3〕冻结后又现一篇 17:15 同题普通栏
  **无 tags** 文章——口径不含它；若后续 tags 缺失常态化，量级论据需重跑。

## 2. 方案：分档不分层

「G 认知分层加一层」在结构上不成立（2 篇喂不起一条产线，规则 11）；成立的是
**来源档位**——readmodel 的 `source_nature` 闭集本就是分层机制（现三档
`G_ORIGINAL / MIXED_PUBLISHED_REPORT / AI_ASSISTED_CONTENT_MIXED`，各档绑
降权纪律先例），加第四档随单元走：

1. **readmodel 闭集加值** `SPOKEN_FAN_TRANSCRIBED`（老师口播·粉丝转述）：
   `SOURCE_NATURES` frozenset 与 `_CognitionSource.source_nature` Literal 两处
   同步；**来源表 parser 加一条映射分支（canonical 标记冻结为
   `spoken_fan_transcribed`，cell 含此串 → 新档）**。〔外审修正 Q2-P1a〕
   来源表是**唯一映射源**——单元段「来源性质」行不参与映射（现状代码中
   该分支写入的值从未进入 built unit，属死代码，保持不动并在本设计记录）；
   标记串全链统一 `spoken_fan_transcribed`（v1 的 `spoken/fan-transcribed`
   写法作废）。**关键正确性点：无标记来源默认 `G_ORIGINAL` 的现状不变**，
   新档只有来源表显式标记才进（防静默升格）。
2. **标注协议两行降权纪律**（草案随起草交付，owner 下批终审后入
   manual-annotations 头部纪律段，比照 mixed_published_report 既有写法）：
   来源表标记 `spoken_fan_transcribed` 的方向/选择/行动指导不称作 G 的
   选择或行动指导；
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
- 不动 read_g_context 注入逻辑与 G 工作集。〔外审修正 Q1-P1/Q4-P3〕本设计
  为 **archive-only**：新档只入 durable readmodel，projector 沿用既有行为
  只投影 `G_ORIGINAL`、候选过滤排除其他档位（新档不进 read_g_context——
  v1 §4「透传字符串」表述有误，投影层是显式过滤不是透传）；**用回归测试
  锁死新档不投影**，防未来误开（现行 renderer 以「G 原文」开头，误投会把
  转述冒充亲笔口播）。
- 不改既有三档语义与默认 `G_ORIGINAL` 回落。

## 4. durable state / 并发 / 幂等

- 〔外审修正 Q1-P1〕archive-only：新档不进 read_g_context 投影（projector
  只投 `G_ORIGINAL`，其余档位被候选过滤排除）——检索引用面 =
  read_article_search（tags 可见、整体 `source_trust=non_g`，足以支撑降权
  纪律），不是 G 注入。
- 〔外审修正 Q2-P1b，硬顺序〕**新 parser 先合入部署生效，owner md 后写标记
  入档**——反序会造成 durable 污染：owner 先写标记 + 旧 parser 先 rebuild
  → 未知标记回落 `G_ORIGINAL` 发布且 sidecar 记为 current，之后新代码上线
  fingerprint 不再变化、不自动纠正。起草草案必须带此顺序说明。
- 重建幂等：annotation 指纹变化触发 rebuild gen+1（既有机制）；已发布
  三档单元不受影响（值域只增不改）。
- 标注 md 是 owner durable 数据：纪律两行与特批条目均走「CC 起草 →
  owner 终审」批次流程，本施工不直接写 md。
- 〔外审修正 Q2-P3〕该文 `published_at=2026-09-04 11:07` 晚于当前锚
  `as_of=2026-09-04T00:00`——入档批次须按既有时间校验同步滚动 as_of，
  否则整份 rebuild 被拒。

## 5. 验收

1. 单测：新档来源表 parse、validate 放行、无标记来源仍默认 G_ORIGINAL
   （回归）、**projector 不投影新档**（archive-only 锁死）；
   verify_mainline_annotation 对 spoken 单元（无 evolution 行）info-only
   不误报〔外审修正 Q3-P2〕。
2. 人格 r11 落盘（含并行会话当日新增条款共存核对）。
3. 起草草案交付（canonical 标记 `spoken_fan_transcribed`、as_of 滚动提醒、
   「代码已生效后才入档」顺序说明、09-04 候选单元 + 协议两行纪律文本）。
4. owner 终验 = 下批标注终审该单元入档（代码先行已满足硬顺序）；
   问询侧验收 = Agent 经 read_article_search 检索引用该文时降权纪律被执行
   〔外审修正 Q4-P2：引用路径是检索面，不是 read_g_context〕。
