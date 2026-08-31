# Daily L1 backend 总预算短设计

## 事实

2026-08-31 隔离预演中，单个 GLM backend 连续三次 60 秒请求超时后才返回空
结果；`L1DirectWorkspaceGenerator._complete` 随后仍给第二个 backend 重开完整
预算，两个 backend 的总耗时可能超过同一项 Daily 生成预算。

## 决策

- 把本次生成的预算变成一个 monotonic 总截止点；多个 backend 按剩余 backend
  数平分剩余时间，单 backend 不改变原预算。
- backend 内既有重试、路由顺序和失败语义不改；没有剩余预算时直接保留
  `daily_workspace_l1_all_backends_failed`。
- 不新增 scheduler、状态、重试编排或 provider 配置。

## 验收

- 两个 backend 收到的 `total_timeout_seconds` 之和不超过调用预算（允许极小
  计时误差），单 backend 收到完整预算。
- 所有现有 Daily 失败/成功契约回归通过。
- 真实 LLM 预演仍只作观察证据，不写生产状态、不投递消息。
