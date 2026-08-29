# deepen · 设计页（Deep Read Artifact：生成 / 缓存 / 资格 / 有界排空）

> 依据：rebaseline-20260827.md 附录 C.4 映射——`internal-module-catalog.md` “Deep Read Artifact Service” +
> 统计口径（generated/cache_hit、hash 失效重做）+ strict-G 门 + backlog 有界排空；代码只读核对（页内断言带 file:line）。
> 定位：W2' 移植施工输入；本文不改代码。

## 目标 / 非目标

**目标：**

- 记录 deep-read full/compact artifact 的生命周期不变量：`article_id + content_hash` 绑定、同代 pair 的 `generation_id/generated_at`、fresh/cache-hit/generated/retryable 语义。
- 固定 strict-G 资格单一定义与 backlog 有界排空边界（LLM 失败残留 + hash 漂移）。

**非目标：**

- 不定义 deep-read 的 LLM 语义本体（归 `ZsxqCognitionApprentice`）；不定义抓取（见 zsxq-capture 页）、G 工作集（见 g-cognition 页）。
- 不重建生成器/审核；本页只登记“深化审计判决（样本≥10、均分>7 → 只调 prompt/参数；<7 → 重写生成核心）”是 W3-4 的准入口径，不是本页施工内容。

## 数据与 schema 事实（owner 清单）

| store | owner | 位置/schema | 事实 |
| --- | --- | --- | --- |
| full/compact artifact | `DeepReadArtifactService` | `knowledge-base/runtime/cognition/deep_read_artifacts/{full,compact}` | 独占路径/权限/identity/原子发布与稳定读取（`deep_read_artifacts.py:141` class）；writer 随机 `O_EXCL|O_NOFOLLOW` 0600 temp + fsync + dirfd atomic replace（目录条目） |
| deep-read 运行态 | `ZsxqCognitionApprentice` | caller-provided runtime root | 底层生成状态；artifact service 不替代其语义（目录条目） |
| availability 观测 | `DeepReadAvailabilityService`（只读） | 无状态 | 按 `index.json` canonical id/file 解析，不用 glob/文件名猜测（`deep_read_availability.py:92,99`） |

artifact 内容事实：full 与 compact 必须共享非空 `generation_id` 与 envelope `generated_at`，且都携带 `content_hash`（`deep_read_artifacts.py:14-17,22-26`）；`generated_at = now(UTC)`、`generation_id = uuid4().hex`（:290-297）。

## 关键不变量

1. **freshness = `article_id + content_hash` 双重匹配**：`content_hash` 在每次 `ensure_artifacts` 入口重算（`deep_read_artifacts.py:228`）；只当既有 pair 的 `content_hash` 与文章一致才返回 `cache_hit`（:245-252）。文章内容 hash 变化 → 旧 artifact 失效并触发重做（目录条目统计口径）。
2. **同代 pair 完整性**：full/compact 必须同 `generation_id`/`generated_at`；带 retryable backend/extraction warning 的完整同代 pair 可作为 generation 记录发布，但**不取得 fresh/cache-hit 身份**，后续 `ensure_artifacts()` 只重试该失败项（:63-70；目录条目）。
3. **generated 只算“新增产物”**：`generated` 仅在新写 pair 通过正常 fresh-pair validator（不允许 retryable warning）后返回；`cache_hit` 是匹配 `article_id + content_hash` 的有效复用（目录条目统计口径）。
4. **受控发布临界区**：受控 generation 在 `ExecutionFence.publication(...)` 临界区内发布并复验 full/compact pair（`deep_read_artifacts.py:326`）；deadline/cancel 已关闭时不写任何 pair（目录条目）。
5. **strict-G 资格单一定义**：`_strict_g_entry_pending_pair`（`cdp_scraper.py:3059-3084`）是“当轮新生成”与“存量排空”两条路径共用的唯一判定（`classify_g_source` eligible + safe article path）——资格语义不得有两份。
6. **backlog 有界排空**：每轮 ingest tail 由 `_collect_deep_read_backlog_ids(limit=3, exclude=saved_ids)` 选候选（`_DEEP_READ_BACKLOG_DRAIN_LIMIT=3`，`cdp_scraper.py:61,2239-2253,3084`）；确定性顺序（sorted index）、绕过 `saved_ids`、覆盖“LLM 失败留下的 retryable 残留 + 文章 hash 漂移”两类；单条检查失败只跳过不阻塞 ingest（:3084 文档与 is_fresh 的 try/except 跳过）。
7. **availability 只读口径**：`report()` 只观测，不触发生成、不写状态；状态闭集 READY/MISSING_ARTIFACT/STALE/CORRUPT/UNREADABLE/UNKNOWN，`availability_rate` 汇总（`deep_read_availability.py:46-69,99`）；STALE = artifact content_hash 与文章不符（:138）。

## 接口契约

- **service seam**：`ensure_artifacts(article_id, article_path, force=False, control=None)` / `load_fresh_pair` / `load_full` / `load_compact` / `is_fresh`（`deep_read_artifacts.py:194`）；`control` 只由已有 deadline 的后台 ZSXQ 调用方注入，普通调用不变（目录条目）。
- **status 契约**：`ensure_artifacts` 返回 `cache_hit|generated|retryable|missing|error` + full/compact path + content_hash + generated_at（:205-256）。
- **availability seam**：`DeepReadAvailabilityService.report(article_ids)`；严格按 `index.json` canonical `id`/`file` 解析（生产 `articles/YYYYMMDD_<id>.md`），重复 ID/畸形 entry/绝对路径/symlink/identity 不一致 → UNKNOWN（目录条目）。
- **gateway 只读消费**：内部 `deep_read_article` 只 `load_fresh_pair()` 并经 `deep_read_public_projection` 构造只读投影；cache miss/stale/invalid → `DEEP_READ_ARTIFACT_UNAVAILABLE`，绝不隐式 `ensure_artifacts`/LLM fallback（目录条目）。

## 已知故障与设计回应

- **8/23–8/25 LLM config defect 留下 retryable 积压** → backlog drain（≤3 篇/轮、绕过 saved_ids、确定性顺序、有界排空；`cdp_scraper.py:2239-2253`）。排空路径与当轮新文章路径共用同一 `_strict_g_entry_pending_pair` 判定；`is_fresh` 失败/异常不致死循环（:3084-3111）。
- **hash 漂移造成旧 artifact 失效** → content_hash 每次重算比对；STALE 反馈可用性，重做由生成路径触发（`deep_read_artifacts.py:228,245-252`；`deep_read_availability.py:138`）。
- **backend/extraction 失败不能冒充成功** → retryable 状态单独保留记录、不计可用成功；后续只重试该失败项（`deep_read_artifacts.py:63-70`）。
- **active strict-G 缺 fresh pair** → G 工作集必须非 READY，scheduler 不得退出 0（目录条目）。
- **unsafe article id/symlink/硬链接/超大/递归 JSON** → 只读路径统一收敛 `error/False/None`；artifact owner 无效 → `deep_read_artifact_store_invalid` 且不触碰外部 victim（目录条目）。

## 验证方式

- **回归入口**：`tests/cognition/test_deep_read_artifact_service.py`、`tests/cognition/test_deep_read_availability.py`、`tests/gateway/test_knowledge_article_handler.py`、`tests/scraper/test_cdp_scrape_result_priority.py`、`tests/guo_teacher_research/test_agent_runtime_context_provider.py`、`tests/runtime/test_artifact_coordinator.py`。
- **口径验收**：同内容同 article_id → cache_hit；正文改动（hash 变）→ 重做且旧 pair 不再 fresh；新写 pair 过 validator → generated；带 retryable warning → retryable 且不计可用。
- **排空验收**：制造 retryable 残留 + hash 漂移，一轮 ingest 排空 ≤3 篇、顺序确定、与 saved_ids 无交集；单条 is_fresh 异常不阻塞 ingest。
- **同代验收**：full/compact 必须同 generation_id/generated_at，任一缺代不构成 fresh pair。
- **深化审计（W3-4，登记为口径）**：样本 ≥10，均分 >7 → 只调 prompt/参数；<7 → 外部契约冻结为算子、重写生成核心，抓取/导入/存储不动（rebaseline §6）。
