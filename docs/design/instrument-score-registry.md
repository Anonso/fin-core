# 标的评分维护列表 + ZSXQ 参考窗口分级（短设计 · 2026-09-02）

## 目标

1. 抓取时一次性提取 ZSXQ 图片中的标准化标的评分表，落成可查询的标的评分
   记录（带文章日期与 source_id），后续查标的不再回图片。
2. ZSXQ 参考注入（read_ready_evidence 家族）按文章类型分窗口 + 窗口内
   时效衰减排序，替代现“当天一刀切”。

**关键澄清（审查补）**：窗口分级的栏目分属两条车道，不能混成一张表——
锐评/特刊/好问题/每日热点是 G 层（teacher_original，走 read_g_context /
G 窗口体系），reference 车道（read_ready_evidence）按代码只收普通栏 +
Q&A + observation。窗口表拆两条：G 窗口（改 g_context_windows.json）与
reference 窗口（新 zsxq_reference_windows.json）。

## 非目标

- 不动真实交易/资金逻辑；不新增外部数据源；不做人工确认 UI（先落
  `status=needs_review` 标记，人工核对通道后议）。
- 普通栏评分表格仍是 reference 层（advisory_only），不写入 G 认知。

## 数据事实（现状）

- zsxq_sources.jsonl 共 249 篇；92 篇有 image_descriptions、98 篇有
  image_ocr；普通栏 58 篇（2026-05-25~08-28，8 月 49 篇），其中 39 篇含
  “利好度/共识度”标准化表格。
- 表格列变体：公司名称（代码）/ 核心业务 / 所属板块 / 利好度 /
  预计多久启动 / 期待周期 / 市场共识度（列名与顺序均有变体）。
- 评分尺度并存：利好度 1-10；共识度有 0-100（如 85）与 1-10 → 归一化
  >10 一律 ÷10。
- 文章“能量评分”（方向分）：官方定义=短中期内事件/政策对文章内公司的
  利好程度，1-10；5 分以上有看头、8.5 以上新人建议看。爬虫已解析
  “能量评分 X.X 分”。现门槛：普通栏非 Q&A 入库 ≥8.6（cdp_scraper；
  生产 Windows 侧脚本待定位同步）；“图片处理 ≥8.7”仅存于
  config/llm.yaml.example，fin-core 代码无引用（遗留，删除）。
- read_ready_evidence：候选源=index.json 普通栏行，现仅“当天+强相关+
  reference 层”可注入；近期实弹多次空返回。

## 决策（owner 2026-09-02）

- **G 车道窗口（owner 已确认同步改 G 体系；锐评/每日热点=交易日，其余
  自然日）**——`config/g_context_windows.json` 扩 schema 为
  `{column: {days, unit}}`：锐评/每日热点 {4, trading}、特刊/凤仙郡/人脉/
  版本强势英雄 {45, natural}、好问题 {20, natural}、其他 {60, natural}；
  旧三键（commentary 2 交易日/特刊 30/历史 60）迁移到新 schema，缺列回
  “其他 60”：
  - 星大派锐评 / 星大派每日热点：4 交易日
  - 星大派特刊：45 天
  - 星大派好问题：20 天
  - 凤仙郡小故事 / 星大派人脉 / 版本强势英雄：新类别，类特刊，45 天
  - 其他（未映射 G 列：重中之重 / 大锅饭宏观思考等）：60 天
- **reference 车道窗口**（read_ready_evidence 实际可收的集合）：
  普通栏研报 60 天、Q&A（星大派好问题/问题回答/回答问题）20 天、
  未归类 reference 60 天。
- 窗口内时效衰减：按 published_at 降序，越接近现在时效越高、关注度越高，
  排名/注入不得等权对待（**适用于全部窗口类型**）。最小实现=线性倒序；
  如后续要权重，用 1/(1+age_days)，不做更复杂模型。
- 门槛：普通栏与 Q&A 有评分的文章，能量评分 <7 一律跳过（不爬取/不处理）；
  无评分按不满足 ≥7 跳过；其余栏目不受影响。图片处理 ≥7.0；
  example 中 8.7 删除。**<7 跳过须记 skip 清单**（source_id、评分、原因），
  漏爬可审计。
- 评分表格提取范围：只收普通栏（研报 + 问答）的统一格式表格。
- 分级（归一化 1-10）：≥9 重点关注；≥8 及格；<8 一般。
- 标的记录含文章能量评分（方向分），一并存储；**只并列展示，不合成新
  评分**（避免新抽象）。
- 全量保留；**查询默认=窗口内**（与注入共享同一窗口语义、时效一致），
  `include_history=true` 翻全量——窗口只是默认视图，数据始终全量保留。
- **存量回填（仅此一轮）**：只处理「能量评分 ≥7 且落在当前 60 天窗口
  （2026-07-04 起）」的普通栏文章——index 内实测 **137 篇**（其中 81 篇
  含评分表格会产出记录，其余无表无产出）；超过窗口的历史文件（含未索引
  的 5 月文件）今天不回填，交给增量与窗口自然滚动。回填与增量同
  parser 版本。**回填候选判定直接读 index.json（score/date/column），
  不重解析“能量评分”文本**；解析器只负责表格。
  **回填载体三源（实测）**：137 篇中仅 40 篇在 zsxq_sources（36 篇有
  image_descriptions 字段），97 篇只在 articles/.md；81 篇含表格文章 =
  36 篇 jsonl 字段 + 35 篇 .md「## 图片描述」节 + 10 篇 .md 含表格文本但
  无标准节名。**回填以 .md 为覆盖全集（81/81），jsonl 字段用于增量侧**；
  解析器统一吃三种载体（字段 / .md 节 / 正文扫描），同一 parser 逻辑。

## Schema（草案）

```json
{
  "schema_version": "fin.instrument-scores/v1",
  "record_id": "<sha256(source_id + code + 行内序号)>",
  "source_id": "zsxq-...",
  "topic_id": "4554...",
  "column": "普通",
  "title": "…",
  "article_date": "2026-08-13",
  "published_at": "2026-08-13T09:00:00+08:00",
  "instrument": {"code": "002156", "name": "通富微电"},
  "core_business": "先进封装",
  "sector": "半导体",
  "lihao_score": 9.0,
  "consensus_score": 8.5,
  "launch_in": "1周",
  "horizon": "3个月",
  "article_score": 8.2,
  "status": "ok",
  "raw_origin": "image_descriptions",
  "extracted_at": "...",
  "parser_version": "v1"
}
```

存储：`shared/knowledge-base/runtime/cognition/instrument_scores.jsonl`
（与 zsxq_sources 同域，0600；量小，内存加载即可，不建新库）。
唯一键 = (source_id, code, 行内序号)（同篇同 code 出现多行不互相覆盖）；
同篇重复处理按 record_id 幂等；内容 hash 变化则覆盖（保留 parser_version）。

## 提取流程

1. ingest/capture 侧对普通栏文章识别评分表格（含 利好度/共识度/
   公司名（代码）列）。**评分表格只在图片里**（39 篇实证：原始正文含
   “利好度/共识度”的为 0/39）；.md 里的列表是 vision 识别结果被嵌入
   「## 图片描述」节，不是原始正文。数据源优先级：**image_descriptions
   （vision LLM 结构化，主）> image_ocr（兜底交叉校验）**；raw_origin
   记录来源 + fallback_chain（vision 模型）；两源数值一致才 `ok`，
   不一致/缺失 → `needs_review`。
2. 解析器：markdown 表格行 → 列名别名映射；代码校验
   （6 位数字，沪深京代码段；公司名复用 zsxq_adapter 静态名单+后缀兜底）；
   评分归一化（>10 ÷10，去 “%”/“分” 后缀）；字段缺失/越界/代码非法 →
   `status=needs_review`，不丢行、不静默改。
3. 落库幂等；存量 137 篇用同一解析器回填。

## 查询与注入改造

- 新只读能力 `read_instrument_scores`：
  - 入参：code/name、sector/关键词、分数下限（≥8/≥9）、是否含
    needs_review、日期/窗口（**默认=窗口内**，`include_history=true`
    翻全量）；
  - 返回：该标的最近 N 条 + 历史序列（日期降序）、文章方向分并列、
    status、来源文章（title/source_id/日期）；needs_review 默认排除但
    返回计数；
  - 工具描述必须声明：来源=普通栏研报 AI 分析（reference 层、
    advisory_only、带文章日期，非老师 G 观点）。
- needs_review 闭环：`scripts/manage_instrument_scores.py`
  （list / confirm / drop），owner 手工确认，防脏记录永久堆积。
- `read_ready_evidence` / runtime_context `_resolve_recent_reference`：
  当天门 → 类型窗口门（新配置 `config/zsxq_reference_windows.json`，
  映射 column/类别 → {days, unit: trading|natural}，缺文件/非法回内建
  默认，规则 6）；窗口内排序键加 recency（published_at 降序），注入预算
  优先近期。

## 验证

- 解析器单测（真实样例 fixtures）：39/137 篇回填全跑；断言 四方达 9.0、
  85→8.5、共识度缺失→needs_review、列名变体（预计介入时机/持有时间）、
  同 code 多行不覆盖。
- 窗口单测：各类型边界（窗口第 N+1 天不注入、窗口内倒序、交易日语义）。
- 端到端：read_instrument_scores 对 002156/601138/603993 返回记录；
  read_ready_evidence 对封装/封测问题在窗口内有候选时非空。
- 全量 pytest + 部署成套（含 Windows 侧抓取脚本门槛同步，先定位）。

## 排期（owner 2026-09-02 定稿；当前未施工）

1. **存量回填**：解析器（image_descriptions 主 + image_ocr 兜底校验，
   列名别名含 预计介入时机/持有时间 等变体；>10 ÷10；缺字段→
   needs_review）→ 回填 137 篇 → `instrument_scores.jsonl` + 单测。
2. **查询工具**：`read_instrument_scores`（按 code/name，窗口过滤 +
   日期降序 + status/来源）接入问询 CLI。
3. **参考窗口分级**：`config/zsxq_reference_windows.json` + runtime_context
   当天门→类型窗口门 + 全窗口 recency 衰减排序。
4. **G 窗口改值**：`config/g_context_windows.json` 迁移新 schema 并落
   新值（锐评/热点 4 交易日、特刊与新类别 45、好问题 20、其他 60）。
5. **增量门槛（fin-core 侧已完成，Windows 侧待 owner 通知）**：
   fin-core cdp_scraper 普通栏/Q&A 评分 <7 跳过（新语义已落地）；
   Windows capture-zsxq.cjs 侧改动暂停——wrapper sha 钉死 + 需下一抓取
   窗口验证，owner 通知后再做。
6. **行业点评全文检索**（owner 确认需要）：`read_article_search` 包一层
   现有 knowledge.query（按关键词/板块/栏目检索历史文章，含来源与时点）。
7. **收尾**：全量 pytest、部署成套、回填与增量结果对账。

执行顺序：1→2→3→4→7 一条链；5、6 与其余无耦合，可并行或后置。

## 风险 / 边界

- 生产抓取在 Windows 侧：7.0 门槛与图片处理门槛需定位 Windows 脚本后
  双侧同步，否则 fin-core 改动不生效。
- image_descriptions 是 vision 生成，错格漏字不可避免 → needs_review
  显形，不静默。
- 凤仙郡/人脉/版本强势英雄按“类特刊 45 天”处理（owner 口径近似）；
  “其他 60 天”= 普通栏研报 + 未归类问答。
- 回填与增量解析须同 parser_version，后续改版用 version 字段区分。
- 评分表格只在图片里，原始正文无表（0/39 实证）：解析正确性依赖
  vision 质量，needs_review 与人工闭环是最后防线；行业点评全文检索由
  排期 6 的 read_article_search 承担，与本表互补。
- `g_context_windows.json` 迁移的消费方需同步：window_config.py +
  runtime_context（G 工作集准入层与选择层）+ daily G 材料；实现双读兼容
  （新 schema 优先、旧键回退），特刊 30→45 语义变化回归测试。
- 排期 5 前置：Windows 抓取脚本位置与 8.6/8.7 门槛是否在生产侧执行尚未
  定位（fin-core cdp_scraper 的 8.6 可能不是生产路径）——先定位再排期。
