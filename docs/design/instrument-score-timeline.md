# ZSXQ 标的评分时间线：门槛 6.0 与注册表补全（设计交接 · 2026-09-03）

> 基线：`docs/design/instrument-score-registry.md`（D-033/D-036 已落地部分）。
> 触发：问询会话 `20260902`（09-03 10:14）——用户批评“7/7 老记录”被当最新；
> 复核发现根因不在引用习惯，而在注册表覆盖盲区与查询语义。
> 本文是交接稿：设计定稿；2026-09-03 已收口（af79ac2），§11 为交接记录、
> §12 为收口结果；合入后本文随代码归档（Git 即归档）。

## 1. 目标

1. 普通栏/Q&A 增量抓取门槛从“能量 ≥7”降到“能量 ≥6”（含 6.0），
   让 6~7 分区间里最新的评分表和主线问答能进 KB，支撑“评分时间线”。
2. 能量评分继续作为**元数据/解读维度**保留：抓取门槛之外，不合成加权分，
   不做“单条=最新”的假权威。
3. 同公司多次评分按**发布时间倒序**查询与解读：最新一条是当前锚，
   旧行是演变参照（7/7 与 8/29 并存时，8/29 优先、7/7 做趋势）。
4. 补上导致 8/29 长电表查不到的解析/回填缺口，并让查询方明确
   “本地没有更新 ≠ 星球没有更新”。

## 2. 问题覆盖矩阵

| 问题（会话复盘） | 根因 | 本设计对应 |
| --- | --- | --- |
| P1 注册表只有 7/7，8/29 表查不到 | 抓取门槛 7 + 回填门槛 7 + 解析器不认代码前置格式 | §5.1、§5.2 |
| P2 工具返回旧行且 `gaps=[]`，无“覆盖不完整”提示 | 注册表语义被当成完整索引 | §5.4 |
| P3 同公司多评分没有时间线语义 | 旧行/新行未并存，查询与解读都当单值 | §5.2、§5.3 |
| P4 评分口径不完整（8.6/88 未归一、未标普通栏/非 G/能量） | 人格缺“引用=日期+能量+栏目+归一”规则 | §5.5 |
| P5 外部证据无锚引用、一手/二手混淆 | 证据分层与引用模板没落地 | §5.5 |
| P6 大盘概览工具失败仍输出完整盘面结构 | 缺“工具缺口→诚实降级”规则 | §5.5 |
| P7 自查修正只联网、没回本地数据层 | 缺“评分必双查”与覆盖边界认知 | §5.3、§5.5 |
| P8 动作合同分母/退出/验证清单不闭环 | 人格纪律缺口 | §5.5 |
| P9 修正承诺没有测试与回归抓手 | 缺单测与 CLI 探针 | §6、§7 |

## 3. 数据事实（2026-09-03 本地实测）

- 近 60 天普通栏共 425 篇：能量 ≥7 有 137 篇、[6,7) 有 24 篇、
  [5,6) 有 7 篇、<5 有 4 篇、无评分 253 篇。
- [6,7) 的 24 篇中：10 篇含“利好度/共识度”评分表关键词，19 张图。
  7→6 的增量约 **0.4 篇/天**；普通栏不进 G、不做 deep-read（D-022），
  成本集中在抓取/识图/索引，量级很小。
- 关键漏网样本：`20260829_zsxq-22258828218828111`（能量 6.8）的
  「## 图片描述」是代码前置 inline 格式：
  `1. **600584 长电科技**：核心业务为HBM/2.5D/3D先进封装，所属板块为先进封装，利好度8.6，共识度88。`
  当前 `parse_rows_from_text` 对该文返回空 → 注册表只剩 7/7（9.2/9.2）。
- 现有 `read_instrument_scores` 已能按 `article_date` 倒序返回多条；
  缺口是“8/29 行不存在”和“排序只到日、未统一用发布时间”。

## 4. 决策（owner 已拍板，落 D-037）

- 增量门槛：普通栏/Q&A **能量 ≥6.0 入库；<6.0 跳过**；
  无评分/非法评分仍不跳过（D-036 语义保留）。G 栏目不受影响。
- 阈值进配置：新增 `config/zsxq_capture.json`（`score_skip_min=6.0`），
  代码常量只做缺省回退，改行为不动代码。
- 回填/注册表口径对齐 **≥6.0**；存量 <6 不清、不进注册表，全文检索仍可见。
- 同公司多评分 = **时间线**：默认窗口内按发布时间倒序返回；
  `include_history=true` 翻全量。最新一条做当前锚，旧行做演变参照。
- 能量评分用途 = 抓取门槛元数据 + 解读强度提示，**不做加权合成**。
- 最新锚能量在 [6,7)：标“近但弱”，进动作前需 G 或外部一手证据补强。
- 查询输出必须诚实：本地增量自本决策起只收能量 ≥6；
  “本地没有更新”不是“星球没有更新”，引用要带覆盖边界。

## 5. 方案

### 5.1 抓取门槛与配置

- 新增 `config/zsxq_capture.json`：
  ```json
  {
    "schema_version": "fin.zsxq-capture/v1",
    "score_skip_min": 6.0,
    "skip_unscored": false
  }
  ```
- `fin_analyse/scraper/cdp_scraper.py` 的 `_score_skip_enabled` 改为
  读取该配置（文件缺失/非法 → 回退常量 6.0）；调用点不变。
- 保留 skip-audit（D-033 语义）：被门槛跳过的文章记录
  `source_id + 评分 + 原因`，可审计。
- 单测口径：`5.9 → 跳过`、`6.0 → 保留`、`None/非法 → 不跳过`。

### 5.2 解析器与注册表

- `fin_analyse/ingestion/instrument_scores.py` 扩展列表解析：
  支持“代码 名称：…核心业务为…，所属板块为…，利好度 X，共识度 Y”
  的 inline 图片描述格式（8/29 即此类）。缺失字段仍走
  `needs_review`，不静默填。
- 共识度 >10 一律 ÷10：8/29 原文“共识度88”落库为 **8.8**。
- parser_version 升 **v2**，与 v1 行区分；upsert 幂等语义不变。
- 回填默认 `--min-score` 从 7.0 改 6.0；对已入库 KB 重跑一轮
  （dry-run 先核对候选/记录数，再 `--write`）。预期补入 8/29 长电
  （8.6/8.8、energy 6.8）等 [6,7) 评分行。
- 时间排序补全：回填把 `published_at`（有完整时间则用，否则用
  index 的完整日期时间）写入记录；查询排序键 =
  `published_at or article_date`，避免同日多条排序不稳定。

### 5.3 查询语义：时间线

- `read_instrument_scores` 返回结构不变（多条记录、日期倒序、
  needs_review 计数不入列表），只修排序键与覆盖提示描述。
- 不新增服务端“合成分/最新摘要”字段——聚合与解读归 Agent/人格层，
  避免新抽象。
- Agent 解读协议：
  1. 先读全部返回行，按日期排出时间线；
  2. 最新行 = 当前锚；
  3. 旧行 = 演变参照（利好/共识的变化本身就是信号）；
  4. 锚能量 <7 且 ≥6 → 明说“近但弱”，需要 G/外部补强后才进动作；
  5. 默认窗口无行或最新行明显过期 → 触发 `read_article_search` 双查。

### 5.4 覆盖边界与诚实降级

- 工具描述固定声明：注册表只覆盖“已入库且能量 ≥6 的普通栏评分表”；
  返回空/旧 ≠ 星球无更新。
- Agent 引用评分时给出“本地覆盖到哪一天”：
  例如“本地最新评分表 = 8/29（能量 6.8，普通栏，非 G）；
  能量 <6 的增量不入库，未核验是否还有更晚表”。
- 大盘概览不可用同理：明说工具缺口，不把推断伪装成数据。

### 5.5 人格与证据纪律（consult-agent/CLAUDE.md）

1. 评分引用格式：日期 + 能量 + 栏目 + 来源 + 归一化分。
2. 时间线优先：最新为锚、旧行为趋势，不引用单条充当全貌。
3. [6,7) 弱锚规则：可展示，不可单独支撑动作。
4. 双查规则：标的评分问题必调 `read_instrument_scores` +
   `read_article_search`；都无更新则给覆盖边界，不写“最新=”。
5. 外部证据分级：公告/官方新闻 > 券商研报 > 媒体 > 星球转述；
   每条可行动事实带“日期+来源+公告号/URL”，否则标“未核验”。
6. 工具缺口降级：`read_market_overview` 失败就明说，快照与推断分列。
7. 动作合同：仓位分母统一（可用现金 vs 总账户）；试仓/持有必须有
   失效退出（不是“不再加仓”）；监控触发器带日期、数据源与阈值。

## 6. 文件改动清单（交接）

| 文件 | 改动 |
| --- | --- |
| `config/zsxq_capture.json` | 新增：`score_skip_min=6.0`、`skip_unscored=false` |
| `fin_analyse/scraper/cdp_scraper.py` | `_SCORE_SKIP_MIN` 7→6 并接配置；保留 skip-audit |
| `fin_analyse/scraper/config.py` | （可选）提供配置路径/缺省常量，避免散落 |
| `fin_analyse/ingestion/instrument_scores.py` | inline 代码前置解析；parser_version v2；published_at 排序 |
| `scripts/backfill_instrument_scores.py` | 默认 `--min-score` 7→6；补 published_at |
| `consult-agent/CLAUDE.md`（`~/fin-data/consult-agent/`） | §5.3/§5.4/§5.5 规则落盘 |
| `tests/ingestion/test_instrument_scores.py` | 8/29 inline 格式样例单测 |
| `tests/scraper/test_cdp_score_skip.py` | 门槛 5.9/6.0/None/非法 四态 |
| `tests/read_capabilities/test_instrument_score_reader.py` | 多行时间线排序 + published_at 排序 |
| `docs/design/instrument-score-registry.md` | 门槛与查询语义加 D-037 取代指针 |
| `docs/DECISIONS.md`、`docs/pm/NOW.md` | D-037 与队列指针（本文交付时同步） |

## 7. 验证与探针

1. 单测：8/29 inline 格式解析出 600584/长电/8.6/8.8；v1 样例全回归。
2. 抓取门：`_score_skip_enabled(5.9)=True`、`(6.0)=False`、
   `(None)=False`、`(非法)=False`。
3. 回填 dry-run：`--min-score 6.0` 候选数可预期；`--write` 后
   注册表长电含 8/29（前）与 7/7（后）两行。
4. reader 单测：同 code 多行按发布时间倒序；同日用 published_at 决胜。
5. CLI 回归探针：问“长电科技最新 ZSXQ 评分”，trace 必须出现
   `read_instrument_scores` + `read_article_search`，回答含日期/能量/
   栏目与覆盖边界。
6. 全量 pytest 绿；核对 `instrument_scores.jsonl` 0600、无 tmp 残留。

## 8. 部署与回滚

- fin-core 提交后按运维铁律重渲染/重启绑定 HEAD 的单元；
- Windows 侧门槛先确认生产执行点（registry 页结论：capture wrapper
  为纯传输、无评分过滤；真正门槛在 fin-core cdp_scraper），如双侧
  都存在则同步，单侧则核对下一抓取窗口证据；
- 回滚 = 阈值配置改回 7.0（或 checkout 前一 SHA + uv sync + 重启），
  已补注册表行保留（多行时间线语义无害）。

## 9. 非目标与边界

- 不清理能量 <6 的存量文章；不把它们加入结构化注册表。
- 不为“能量 <6 最新表”做定向补抓/延迟识图（owner 明确：不进 KB）。
- 不做服务端加权合成分、不做“latest”摘要字段（先人格层，缺实证再加）。
- 不改 G 车道、reference 窗口配置、read_article_search 索引范围。

## 10. 实施前需现场确认

1. 生产执行点：`_score_skip_enabled` 到底在 fin-core cdp_scraper 还是
   Windows 侧脚本运行——决定 5.1 是否还要同步 Windows 副本。
2. 回填写盘时机：KB 是部署数据，`--write` 前先 dry-run 并备份
   `instrument_scores.jsonl`（0600/0700 规则）。
3. CLAUDE.md 位于 `~/fin-data/consult-agent/`（不在本仓），改动后
   需在问询环境即时生效验证，不进 git。

## 11. 实施交接（2026-09-03 · 暂停时记录）

> 状态：已恢复并收口（af79ac2，详见 §12）。下方快照与待办是恢复前的
> 历史交接记录，仅作审计用。

### 状态快照

- 本会话接手基线 `438ab7c`（docs-only）时工作区干净；实施期间出现并行
  macro 改动，已由并行会话提交为 `858b325` / `05cbefa` / `2a7eb22`
  （当前 HEAD = `2a7eb22`）。
- 分段暂存与并行提交交错：`858b325` 里**混入了本任务的 cdp_scraper 两个
  hunk**（`.config` import `score_skip_min` + `_score_skip_enabled` 改读
  配置阈值），但支撑它的 `config/zsxq_capture.json` 与 `config.py` 加载函数
  仍在工作区未提交。**HEAD 此刻不是可运行状态**：导入 cdp_scraper 需要
  `score_skip_min`，而 HEAD 的 `fin_analyse/scraper/config.py` 尚无该函数。
- 本会话没有产生 commit、没有执行回填 `--write`、没有部署/重启。

### 当前工作区（全部未提交）

- 新增 `config/zsxq_capture.json`（`score_skip_min=6.0`、
  `skip_unscored=false`）。
- `fin_analyse/scraper/config.py`：`score_skip_min()` 配置加载 + 缺省 6.0。
- `fin_analyse/scraper/cdp_scraper.py`：只剩过滤日志/注释 hunk 未提交
  （import 与判定函数已在 `858b325` 的 HEAD 内）。
- `fin_analyse/ingestion/instrument_scores.py`：parser v2、8/29 代码前置
  inline 解析、共识度 88→8.8、reader 按 `published_at or article_date`
  排序（含窗口判定）。
- `fin_analyse/read_capabilities/server.py`：`read_instrument_scores`
  描述补“能量 ≥6.0 覆盖边界 + 时间线 + 双查”语义。
- `scripts/backfill_instrument_scores.py`：`--min-score` 默认 7→6；回填
  `published_at` 优先 source_record，缺省用 index 的完整日期时间。
- 测试：`tests/ingestion/test_instrument_scores.py`、
  `tests/read_capabilities/test_instrument_score_reader.py`、
  `tests/read_capabilities/test_tool_descriptions.py`、
  `tests/scraper/test_cdp_score_skip.py`。
- 本文档（本次交接追加）。

### 已完成并验证

- 定向测试 25 passed；全量 pytest 3042 passed / 2 skipped（跑于 macro
  三连合入前；恢复后需在最终 HEAD + 剩余未提交改动上重跑全量）。
- 真实 KB dry-run（只读，未写盘）：`candidates=161`（含 [6,7) 24 篇）、
  `records_total=445`（ok 370 / needs_review 75）；8/29 长文解析出
  600584 长电 8.6/8.8、`raw_origin=article_md.image_desc_section`、
  `parser_version=v2`。
- 生产执行点已确认：Windows `capture-zsxq.cjs` 无评分/跳过过滤（纯传输），
  systemd 消费端跑 fin-core `capture_ingest → cdp_runtime`，门槛只在
  fin-core 单侧；无需同步 Windows 副本。
- `~/fin-data/consult-agent/CLAUDE.md` 已按 §5.5 落盘（双查、时间线、
  [6,7) 弱锚、引用格式、覆盖边界、证据分层、大盘降级、动作合同分母/
  退出/监控触发器）；**尚未在问询环境做即时生效验证**。

### 恢复后待办（按序）

1. 只提交本任务的剩余文件 + cdp_scraper 过滤日志 hunk，不碰 macro 三连
   （历史已被混入的部分是否拆分为独立 D-037 commit，需 owner 拍板，默认
   不 rewrite 已合入 main 的历史）。
2. 在最终 HEAD 上重跑全量 pytest。
3. 回填：先按 §7 探针 3 复核 dry-run 数字，备份 `instrument_scores.jsonl`
   （0600/0700），owner 授权后 `--write`；预期补入 8/29 长电两行时间线
   （8/29 前、7/7 后）。
4. 问询环境验证 CLAUDE.md 新纪律 + `read_instrument_scores` 时间线回答。
5. 按运维铁律部署（checkout 最终 SHA + uv sync + 重启绑定 HEAD 的单元，
   核对 SHA/lock/PID/公共入口）；下一抓取窗口核对 6.0 门槛实际生效。

### 风险提示

- HEAD（`2a7eb22`）依赖未提交的 config 改动才能导入 cdp_scraper；恢复后
  第一步应先落库剩余文件，不要只 checkout HEAD 就部署。
- `858b325` 的提交信息是 macro_index，实际混入 D-037 的两个 cdp hunk；
  任何按 commit message 回滚 macro 的操作都会把门槛代码一起滚掉。

## 12. 实施收口（2026-09-03）

- 落库：`af79ac2` 提交剩余 D-037 文件（config/zsxq_capture.json、
  config.py `score_skip_min()`、cdp_scraper 过滤日志 hunk、parser v2、
  reader/backfill/server 描述、4 组测试），858b325 混入 hunk 的支撑文件
  补齐，HEAD（af79ac2）可直接导入 cdp_scraper。
- 验证：全量 pytest 3043 passed / 2 skipped；真实 KB dry-run
  candidates=161、records_total=445（ok 370 / needs_review 75）。
- 回填：备份 `instrument_scores.jsonl.bak-20260903`（0600）后 `--write`
  执行，added=38、updated=407；600584 长电 8/29（8.6/8.8、v2）与 7/7
  （9.2/9.2）两行并存；文件 0600、无 tmp 残留。
- 问询探针：09-03 12:49 CST 实弹“长电科技最新评分”，trace 出现
  read_instrument_scores ok → read_article_search ok → read_article；
  回答含 8/29 锚、7/7 参照、8/08 口径例外、本地覆盖边界与“近但弱”。
- 部署：post-commit 钩子已将 zsxq poller/consumer 单元重渲染并绑定最终
  HEAD 6273ab8（LLM_CONFIG_PATH 指向 runtime-configs/6273ab8…）；
  09-03 12:54 手动触发 FIN-ZSXQ-Incremental 完成实弹闭环：12:58 poller
  sync 成功（新增 4 篇、unit 绑定 6273ab8），macro_index 自动生成 22 条
  （12 kept + 8 每日热点 + 2 新规则命中）；首篇 [6,7)/<6 边界样本待自然窗口。
- Windows 侧单侧确认（无代码改动）：capture wrapper 纯传输，无评分过滤。
- 扩展回填（owner 09-03 指示）：备份 `.bak-20260903-60d` 后执行
  `--since 2026-05-01 --min-score 6.0 --write`，added=136、updated=445 →
  注册表 581 条；最早可解析表为 2026-06-24（index 5/13 起的老文章多数只有
  图片 OCR/旧版式，正文无可解析评分表文本）。
- 历史缺口记录：5/13–6/23 共 179 篇能量 ≥6 普通栏本地文件均含图片；其中
  24 篇正文带旧 OCR 表文本（`### NNN.jpg` 松散表格 / `公司利好度：名称（代码）…`
  / 代码前置 inline），现行 parser v2 不识别 → 未结构化；其余 155 篇表格只在
  图片里需识图。
- 定向识图收口（owner 09-03 指示）：154 篇“只有图片、无 `## 图片描述`”的
  普通栏 ≥6 文章已全部识图转录（scripts/backfill_old_score_images.py；
  A 股代码经名册归一）；注册表扩到 1348 行，覆盖 2026-05-13 起（新增
  767 行、ok 1225 / needs_review 123）。原 24 篇旧 OCR 文本文章仍由
  parser v2 不识别，走全文检索即可。
- 存量质量待清理：注册表另有 30 行 A 股代码↔名称错位（2026-06/07/08，
  非本次识图批引入），需按名册替换 record_id 修正；待 owner 确认后执行。
- vision 链修正：mimo-token-plan model 从 mimo-v2.5-pro 改为 mimo-v2.5
  （llm.yaml，fc8f4fe）；实测 404 消失，函数级调用返回
  mimo-token-plan/mimo-v2.5 ok。
