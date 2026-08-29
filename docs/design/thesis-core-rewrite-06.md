# thesis 生成核心重写（06 零提取）· 短设计（2026-08-29）

规则5 核心链路短设计。来源：B2 盲评预注册 <7 判决 + 08-29 判别实验
（`$STATE/fin-analyse/exp-sample06-zero-extract-20260829/`：清洗输入后四论点
逐字在场仍零提取 → 病根在生成核心；输入存量与兜底门静默为并存缺陷）。
owner 2026-08-29 直接授权施工（含 durable 数据手术，硬边界4 备份+manifest 照走）。

## 问题（实证）

1. **生成核心缺陷**：主提取 prompt 的 confidence 语义混淆「观点为真的概率」
   与「引用忠实度」——老师以推测口吻（"我更相信"）表达的实质论点被整体
   跳过；单遍整文全局判定，宁可 `{"units": []}` 也不逐段尝试（样本06：
   社保抢筹/躺好/砸盘抢筹/洗出主线四论点逐字在场，A/B 双支零提取）。
2. **主链路 evidence 无确定性校验**：LLM 单元 evidence 不验 verbatim 直写
   durable 库（B2 样本08 的罐头幻觉正是同类通路；central-idea 缝有
   `_evidence_in_source`，主缝没有）。
3. **输入存量**：435/1356 篇 6-21 旧导入脏文（评论串/UI chrome 嵌正文，
   行锚定清洗救不了非独立行形态）；205 篇 teacher 栏文件缺
   `source_classification`（现行采集默认写 `teacher_original`，老文件没有）
   → `classify_g_source` provenance unconfirmed → central-idea 兜底拒判。
4. **兜底门静默**：`_central_idea_gate` ineligible 返回 `(False, None)`，
   跳过不留任何警告——08-28 空壳 artifact 无诊断线索即此故。

## 改动

**A. `fin_analyse/cognition/thesis_extractor.py`：**

1. `_LLM_EXTRACTION_PROMPT` v2，三处语义修正（结构对齐实证病灶）：
   - **quote-driven 两步**：先从原文逐字摘录承载实质观点/规则/判断的句子
     （3–10 条），再对摘录句构造单元，evidence ⊅ 摘录集——先摘后判，
     消除全局拒判的形态；
   - **confidence 重定义**：衡量「该表述在原文中的明确程度 + 引用是否逐字」，
     非观点为真概率；推测口吻照提，语气记入 interpretation；
   - **空返回须附因**：`{"units": [], "empty_reason": "..."}`，纯闲聊无实质
     是唯一合法空返回。
   既有七类 unit_type、凤仙郡叙事规则、操作纪律规则、时点分离规则全部保留。
2. `extract()`：解析 `empty_reason`（拼为 `"LLM found no extractable units:
   {reason}"`——前缀保持，`replace_central_idea_warnings` 的 substring
   匹配不破坏）；主链路 units 加 `_evidence_in_source` 确定性校验，不过者
   丢弃并计 `"LLM evidence not verbatim: {N} dropped"`（与 central-idea
   同一判据，堵幻觉直通）。
3. `_CENTRAL_IDEA_PROMPT` 补一句忠实度语义（对齐，避免兜底同病）。

**B. `fin_analyse/cognition/zsxq_apprentice.py`：**

`_central_idea_gate` ineligible 必返原因码
（`central_idea_skipped_{data_gap|g_source_type_unknown}`），调用处必入
warnings——静默跳过绝根。

**C. 数据手术（`scripts/legacy_kb_surgery_20260829.py`，一次性，owner-only）：**

1. **备份先行**（硬边界4）：`$STATE/fin-analyse/legacy-article-surgery-20260829/`
   （目录 0700/文件 0600），备份每个被改文件 + manifest（per-file
   sha256 before/after、ops、bytes、跳过原因）。
2. **清洗 435 脏文**：确定性规则——正文首个评论/chrome 标记处截断
   （首 120 字符豁免保护标题区）+ 尾部 hashtag 行剥离；校验：无残留标记、
   清洗后 ≥150 字（保 central-idea 完整度门），不过者跳过不写并登记。
3. **backfill 205 teacher 栏文件**：frontmatter 补
   `source_classification: teacher_original`（与现行采集写入值逐字一致；
   scope = 凤仙郡小故事 6 + 星大派好问题 127 + 星大派锐评 57 + 星大派特刊 15；
   普通/版本强势英雄等非 teacher 栏不动——owner 08-28 撤项维持）。

**D. artifact 重生成**：清洗致 content_hash 变的 teacher 文章（含凤仙郡三篇）
   验收时经 `DeepReadArtifactService.ensure_artifacts(force=True)` 重生成；
   backfill-only 文件 hash 不变、旧 pair 仍 fresh 不受影响；其余 STALE 由
   backlog 排空（≤3/轮）按设计消化。

## 不变 / 非目标

- **外部契约冻结**：ThesisExtraction/InformationUnit schema、5 个 durable
  JSONL schema、deep_read_artifacts pair/freshness/retryable-warning 契约、
  语义门、非 T0 门、backlog 排空边界全部不动。
- 警告字符串前缀语义保持：`"LLM found no extractable units"` 仍为
  central-idea 可清除类；新增后缀不改变 substring 判定。
- 单元重写不改 unit_id 生成式（`stable_id(source_id, unit_type, title,
  thesis)`）——重生成文章的新单元是新行，旧行随 hash 失效路径走既有语义。
- 采集端不动（现行已清洗、已写 provenance）；`_merge_extractions` 不动。
- B2 复盲评（预注册口径：样本≥10 均分>7）为后续独立步，本刀验收 =
  样本06 夹具 + 无退化对照。

## 验收

1. `tests/cognition/` 全绿 + 受影响面 focused 全绿。新增测试：样本06 清洗
   正文夹具（fake backend 正常产出四锚词单元）；空返回带 empty_reason 的
   警告形状；evidence 造假单元被确定性丢弃并计数；gate ineligible 警告形状。
2. **真后端**：样本06 清洗正文 → ≥3 单元、四锚词命中 thesis/evidence 且
   evidence verbatim；a972 复跑不退化；普通栏文章照旧政策跳过（不回退）。
3. 手术 manifest：435 目标实际清洗/跳过计数、205 backfill 计数、抽样 10 篇
   人审 diff、全库无残留标记断言。
4. 凤仙郡三篇 artifact 重生成后 units>0 且证据可溯源。

## 设计门（外部审视·一次）

packet = 本稿 + 固定四问（契约破坏？durable state 时序/幂等？引用闭包
漏删？相对直接 Agent 退化？）。评审渠道 `scripts/codex_open.sh
--sandbox read-only`（codex-open · deepseek-v4-pro · max）。

## 设计门裁决（2026-08-29 · deepseek-v4-pro · max · attempt2 成功）

**0 P0 / 5 P1 / 8 P2，全部采纳、0 驳回**（台账
`$STATE/fin-analyse/thesis-core-rewrite-gate-20260829/`）。按裁决修正施工：

1. **P1-1 采纳（D 节事实错误）**：backfill 改 frontmatter 即改文件字节即改
   content_hash——「backfill-only 不受影响」不成立。修正：force 重生成范围
   = 全部被手术触碰且已有 pair 的 teacher 文章（清洗 ∪ backfill，实测含
   88 对 backfill-only pair），非仅清洗篇。
2. **P1-2 采纳（校验域冻结）**：主链 evidence 校验域 = LLM 实际所见全集
   （content + image_ocr + image_descriptions），非 content-only；v2 prompt
   明令 evidence 只准出自正文/图片OCR/图片描述。补 OCR-evidence 夹具。
3. **P1-3 采纳**：4 处既有夹具（test_thesis_extractor.py:215/333/368/389）
   evidence 改逐字，测试意图不变。
4. **P1-4 采纳（v2 全文施工前冻结）**：保留全部被断言字面（「低于0.7的
   不提取」「时点分离」「老师时点判断」「泛化」「不得因涉及买卖表述而
   跳过」「不主动给出"买入"建议」「老师原文 G 文章」、凤仙郡/特刊/锐评
   关键词、无「星大派老师」）；0.7 门槛句改写为忠实度语义但保留字面。
5. **P1-5 采纳（范围口径修正）**：127 篇好问题中 71 篇 is_qa:False——
   不翻真值（不可确定性核实），其 central-idea 门维持拒判但**有原因码可
   诊断**；解锁口径 = 134 篇（205−71）。71 篇的 G 工作集路径本不依赖
   is_qa，不受损。
6. **P2-6 采纳**：手术于采集窗口外执行（当晚，下窗 08:20）；术后全量
   is_fresh 复核入验收。
7. **P2-7 采纳**：被 force 覆写的旧 full/compact pair 先入备份+manifest。
8. **P2-8 采纳（表述修正）**：删「旧行随 hash 失效路径走既有语义」——
   durable JSONL 无失效机制，旧单元/链/簇行留存属已知累积（咨询主路经
   fresh pair 隔离），明确接受不建 tombstone。
9. **P2-9 采纳**：原因码直接嵌完整 data_gap 值
   （`central_idea_skipped_g_source_original_provenance_unconfirmed` 等），
   classification=None 且无 gap → `central_idea_skipped_g_source_type_unknown`。
10. **P2-10 采纳（规则冻结）**：标记表/正则/豁免口径全部冻结为手术脚本
    常量；「首 120 字符豁免」废除，改**密集跑判据**（锚点后 400 字符内
    ≥2 个其他评论标记才算评论块起点）+ 行级 chrome 剥离 + 尾部点赞脚注
    剥离（dry-run：296 截断 / 113 脚注 / 947 净）；断言口径=清洗后正文
    无任何冻结标记。
11. **P2-11 采纳**：v2 保留「每篇文章通常有 1-5 个信息单元」上限（3-10 是
    摘录句素材非单元数）；无退化对照扩为样本集（凤仙郡三篇 + a972 +
    特刊/锐评各 1，比对新旧单元数/类型）。
12. **P2-12 采纳**：无 empty_reason 时保持裸字符串
    `"LLM found no extractable units"` 逐字节不变，仅 reason 非空拼后缀。
13. **P2-13 采纳（幂等）**：重跑跳过已达目标态文件（无标记/字段已存在，
    以 before-hash 判定）；manifest 每次全量重建记录终态与本轮 ops。
