# ZSXQ 深读并行化设计（批次 3 · 2026-09-07）

目标：缩短采集→深化→可问询延迟。单篇深读 LLM 多阶段串行 5–16 分钟，多文章
逐篇串行使整班深化时长 ≈ 篇数 × 单篇。并发化后 4 篇 ≈ 最慢单篇时长。

## 方案

- **C1 跨文章深读并行**：`_ensure_deep_read_artifacts_for_new` 的逐篇循环改
  `ThreadPoolExecutor(min(4, n))`。每篇独立提交，结果按提交序聚合；单篇异常
  逐篇捕获不拖垮整批。
- **C2 图片 OCR/vision 并行**：图片处理循环改 `ThreadPoolExecutor(3)`，逐张
  （下载→保存→OCR→vision）为独立任务；结果按 index 排序保持文件名与
  provenance 顺序；预算/储备检查移入任务起点（语义等价：过期即跳过）。
- **C3 排空限**：`_DEEP_READ_BACKLOG_DRAIN_LIMIT` 3→8（deadline 已 1h）。

## 不变量

1. G 发布仍是主线程单点收口，发生在全部任务汇合之后。
2. deadline 栅栏：worker 侧 control 只查栅栏（`checkpoint=lambda: None`）；
   **不碰 repo**——runtime_repository 的 SQLite 连接绑定主线程（无
   check_same_thread=False），worker 续租会炸。续租由主线程等待循环每 10s
   调 `_surface_checkpoint()`；过期时取消未启动任务，在跑任务由自身栅栏
   快速终止。control 为 None（无 deadline，测试场景）保持串行原语义。
3. 工件文件按 article 独立命名，无共享写；DeepReadArtifactService 实例仅持
   不可变路径。断路器 dict 竞态为良性计数损失（健康启发式，非正确性）。
4. checkpoint 多线程写 heartbeat 文件/SQLite：worker 侧已改为不触 repo
   （见不变量 2），主线程等待循环单点续租。
5. 结果顺序确定性：deep-read 结果按提交序聚合；图片结果按 index 排序。

## 回滚

`_DEEP_READ_MAX_WORKERS = 1` / 图片 workers 3→1 即回串行现状（常量一改）。

## 证据

- 并发探针：glm-5.3-flash 4 并发 vs 串行 = 3.4x，零 429 零退化（同 key）
- 约束排查：SQLite 连接主线程绑定（本设计的 worker 不触 repo 的原因）；
  DeepReadArtifactService 无共享可变状态；OpenAI 客户端线程安全
- 串行痛点：单篇 8.5min × 逐篇 = 45–90min/班，deadline 切半成多轮 churn
