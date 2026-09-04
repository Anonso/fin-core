# zsxq-capture · 设计页（Windows 六时点采集 → WSL 消费/入账 → G publication 完成）

> 依据：rebaseline-20260827.md 附录 C.4 映射——`internal-module-catalog.md` “ZSXQ Windows Incremental Scheduler” +
> “ZSXQ Scraper Module” + 代码只读核对（页内断言带 file:line）。
> 定位：W2' 移植施工输入；本文不改代码。

## 目标 / 非目标

**目标：**

- 记录 ZSXQ 采集→消费链的 durable 设计不变量：content_sha256 完整性、六时点窗口单一事实源、3 天 coverage 先证后写、skip-audit、EIO/瞬时故障容错、exactly-once 恢复链。
- 固定 owner 边界：Windows Task 只拥有触发拓扑，WSL poller 只是 transport/ingest 消费，不是第二 scheduler；`ScraperRuntimeRepository` 是唯一 control ledger。

**非目标：**

- 不定义 deep-read 生成与 G 语义（见 deepen / g-cognition 页）；不重建 WSL timer 之外的调度控制面；不含 OpenCLI/CDP 传输实现细节（只登记其边界与 fail-closed 性质）。

## 数据与 schema 事实（owner 清单）

| store | owner | 位置/schema | 事实 |
| --- | --- | --- | --- |
| Windows 触发拓扑 | `FIN-ZSXQ-Incremental` 单一 Windows Task | 用户级 Task Scheduler | 六时点由 `_EXPECTED_TIMES` 单一事实源渲染并验证（`scripts/zsxq_windows_incremental_scheduler.py:26-34,420,475,517`）；Task 固定 `IgnoreNew`/`StartWhenAvailable=true`/`PT25M`/least-privilege SID（目录条目） |
| 每轮 capture 产物 | Windows producer（capture-only wrapper） | 共享产物目录 | capture-only `fin.zsxq-windows-capture/v4` summary（四态：`capture_pending`/75 → `capture_hash`/70 → `capture`/`captureExit` → 成功 null/0）；producer 只原子替换 `capture.latest.json`，importer 只读、绝不 rename/unlink/write（目录条目） |
| 控制 ledger | `ScraperRuntimeRepository` | canonical runtime DB（`zsxq-scraper/runtime.sqlite3`，schema v4） | `capture_ingests` 表 phase 单向链 `CLAIMED → BUSINESS_TERMINAL → PUBLICATION_PREPARED → COMPLETE`（`runtime_repository.py:69-72,190-216`）；claim/terminal/lease release 同事务（:725） |
| capture recovery | importer（同一 ledger owner） | `capture-recovery-v1/{staged,consumed,rejected}` | 0700/0600；五类已冻结 kill point 以同一 raw/hash/owner 精确重放（目录条目；目录权限 `capture_ingest.py:214-215,466,497-498`） |
| handoff lock inode | `scheduler_handoff_lock.py` | `scheduler-handoff.lock`（由 canonical runtime DB 派生） | SHARED（scheduled run）/ EXCLUSIVE（manual ingest），flock 前后复验 parent/lock identity（`scheduler_handoff_lock.py:23-24,39,115,141`） |
| G manifest | `GWorkingSetService`（不变） | 见 g-cognition 页 | scraper completion 只是调用方，不新增/复制 state owner |
| skip-audit 记录 | WSL poller | `poller-skip-audit.v1.jsonl`（schema `fin.poller-skip-audit/v1`） | 可验证 failed v1 artifact 记录后跳过，防 wrapper 中途死亡永久阻塞（`scripts/consume_zsxq_capture_folder.py:80,111,124`） |

## 关键不变量

1. **content_sha256 完整性**：canonical hash 函数排除自引用字段后计算（`capture_artifact.py:149-156`）；校验即重算比对（:311-313）；artifact 命名 `{run_id}.{content_sha256}.artifact.json` 且重读再比（`capture_ingest.py:905-960`）；ledger 表持久 `content_sha256` 列（`runtime_repository.py:192`）。
2. **六时点窗口单一事实源**：`_EXPECTED_TIMES`（08:45/12:20/14:40/15:30/18:00/20:20，午间时点 09-04 由 13:50 改）同时驱动 Task 渲染、poller 窗口推导与 verifier（`zsxq_windows_incremental_scheduler.py:26-34`）；verifier 拒绝触发器与事实源漂移（:475,517）——时钟/拓扑不能有两份真相。
3. **3 天 coverage 先证后写**：sync 必须先证明 group timeline 三日窗口 coverage，之后才允许会写入的 priority surface（`capture_artifact.py:4` 文档；coverage 证明 `oldest_seen < cutoff` 否则 `window_coverage_incomplete`，:508-511）。
4. **exactly-once 单向恢复链**：`capture_ingests` phase 只进不退（`runtime_repository.py:190-216`）；终态发布顺序 = raw → receipt marker → 删 stage；五类 kill point 精确重放，冲突/漂移/symlink/torn marker fail closed（目录条目）。
5. **skip-audit**：wrapper 中途死亡留下的可验证 failed v1 artifact，由 poller 记 `poller-skip-audit.v1.jsonl` 后跳过；succeeded/不可读/未知载荷仍 fail-closed（`consume_zsxq_capture_folder.py:80,111,124`）。
6. **EIO/瞬时故障容错**：ingest 全程捕获 OSError 按 phase 回退（`capture_ingest.py` 多段 OSError 处理，例 :276,489,572,636）；no-clobber 原子发布用 `renameat2`（:87-102）；归档失败 → `archive_warning` + exit 70，绝不静默成功（:1471）。生产实证（NOW 2026-08-27）：`/mnt/c` 瞬时 EIO → idle/0、无数据丢失、下一窗口按设计重试。
7. **锁协议**：scheduled run 持 SHARED、manual ingest 持 EXCLUSIVE handoff flock，取得前后及退出前复验 parent/lock identity；contention 稳定返回 `coalesced` exit 75 且不移动 artifact（`scheduler_handoff_lock.py:141`；exit 码 `capture_ingest.py:60-61`）。
8. **运行时配置边界**：LLM/runtime config 只在 WSL importer 进程内解析；Windows capture 不传递、不验证、不注入该配置（目录条目）。
9. **身份/隐私**：cursor 只投影 topic ID/create time/type/老师归属；群友 identity/title/question/text、cookie、token、原始 API error 均不记录持久化（目录条目）。

## 接口契约

- **Windows→WSL 边界**：producer 原子发布 `capture.latest.json` + v4 summary；poller（oneshot，30 分钟窗口，`_POLLER_WINDOW_MINUTES` :35-36）选最旧 pending/retryable 合格 artifact 逐个消费（目录条目）。
- **入账入口**：`consume_zsxq_capture_folder.py` → `import_capture`（reuse importer ledger/handoff lock 保证幂等）。
- **运行契约**：`ZsxqRunRequest`（intent/trigger/deadline/request_id）；`ZsxqRunResult`（schema v3，failure_reason 冻结 allowlist）；`ZsxqHealth`（contract v1）；`fin.zsxq-scheduled-run/v3` stdout receipt 内嵌 `fin.zsxq-g-working-set-publication/v1`（目录条目）。
- **操作入口**：`python -m fin_analyse.scraper.scheduled_run`（人工重放唯一单链）、`python -m fin_analyse.scraper.live_proof`（本机只读在线证明，绑定 clean HEAD 的九字段 v2 proof）。
- **G completion**：成功/NO_CHANGE 必须经 frozen G plan；失败 terminal 不得进入 G publication（目录条目）。

## 已知故障与设计回应

- **wrapper 中途死亡留下半截 summary** → v4 四态链 + poller skip-audit，不永久阻塞队列（`consume_zsxq_capture_folder.py:80`）。
- **`/mnt/c` 瞬时 EIO 写 result** → OSError phase 回退 + 下一窗口重试，无数据丢失（生产实证，见关键不变量 6）。
- **旧 result 覆盖成功** → capture/summary/source/run/hash/result identity 任一失败非零且不得被旧 result 覆盖为成功（目录条目）。
- **capture_pending + artifact 瞬时窗口** → 判 exit 70、不持久化，下轮重试（目录条目）。
- **interop 故障** → 不阻断采集/消费（wrapper 不调 wsl.exe/systemctl/importer）。
- **NO_CHANGE 时间连续性** → producer/observer 以 crawl `started_at` 作为 prior evidence 闭区间上界；晚到 prior 不能形成 READY（目录条目）。
- **排空期 exit 2 = 预期，勿当故障（2026-08-29 基础设施审计 F9 裁决；deadline 2026-09-02 900→1200s）**：深读排空跨多窗口，单 run 撞协作 deadline（`--deadline-seconds 1200`）属正常收口——链路为 `deadline_exceeded → ingest exit 2`（`capture_ingest.py:64`）→ consume result `status="failed"` 非 retryable（`consume_zsxq_capture_folder.py:460-468` 的 status 表，2 不在 {0,4,70,75}）→ unit failed。**这是已知噪音**：剩余 backlog 下次 timer 窗口自然续排（每 run 截 3 篇），unit failed 态本身不代表数据丢失；判真故障看 run payload 的 `status`/`changed_count`，不看 unit 态。不并入 75（75 已有 coalesced 语义，且 consumer 单元的 75=unavailable 真失败，语义不可复用）。

## 验证方式

- **回归入口**：`tests/scripts/test_zsxq_windows_incremental_scheduler.py`、`tests/scripts/test_consume_zsxq_capture_folder.py`、`tests/scraper/test_capture_ingest.py`、`tests/scraper/test_zsxq_scheduled_run.py`、`tests/scraper/test_zsxq_live_proof.py`、`tests/scraper/test_zsxq_scheduler_handoff_lock.py`、`tests/scraper/test_zsxq_cdp_page_evidence.py`。
- **content_sha256 重放**：同 artifact 重放幂等；篡改正文 → hash mismatch 拒绝；artifact 文件名/复读 hash 与 receipt 三者一致。
- **六时点 verifier**：Task XML 的六个 daily trigger 与 `_EXPECTED_TIMES` 精确一致（`zsxq_windows_incremental_scheduler.py:475,517`）。
- **EIO 演练**：注入瞬时 OSError → 下一窗口重试、无数据丢失、无静默成功。
- **产品完成硬门禁**：真实 Windows Chrome 的只读 canary/soak（自动化测试只是预检与回归安全网，目录条目）。
