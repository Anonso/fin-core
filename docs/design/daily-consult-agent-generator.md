# Daily 推送改由问询环境生成（短设计）

日期：2026-09-01 · 状态：待施工 · 级别：核心（数据管线/公共入口，规则 5）

## 目标 / 非目标

**目标**：Daily 四班推送的生成器从 L1 直调换回问询环境（consult-agent）产出，
投递层（outbox/obligation/claim/幂等）与 durable schema 零改动。

**非目标**：
- 不做飞书请求面/旧 Hermes 网关恢复（仍属 agent 调用待办，本期只做生成器后端）。
- 不动手动 CLI（finqa/finqai/finqac 保持直连，不参与自动兜底）。
- 不重设计 durable 状态机。

## 兜底链（只生效于推送）

问询环境生成失败时按序降级，全部在 Daily 生成器内部完成：

`CC(glm) → codex(ds deepseek-v4-pro) → L1 直调 → 降级通知`

- 前两档是问询环境（consult-ask 契约，CC 默认、codex 备选）。
- L1 直调仅作为推送兜底：问询环境全挂时保推送不断，产物 `generated_via=l1-direct-v1`。
- 手动 CLI 不经过此链：finqa/finqai/finqac 各自直连单旋钮模型。
- 兜底顺序/超时进配置：`config/daily-workspace.yaml`
  （`consult_agents: [cc, codex]`、`fallback_l1: true`、`per_agent_timeout_seconds`）。

## 接缝与实现

1. `consult-ask`（新，`~/fin-data/consult-agent/bin/consult-ask`）：
   - `consult-ask --agent cc|codex [--session <id>] [--timeout <秒>] <问题>`
   - 输出 JSON：`{"ok":true,"agent":"cc","session_id":"...","text":"..."}`
   - CC = `claude -p --resume <id>`；codex = `codex exec --json resume <id>`
     （旧 runtime Phase 3D 已验证形态）。key 从 llm.env / auth.json 读，不入文件。
2. `ConsultAgentWorkspaceGenerator`（新，`fin_analyse/operations/daily_consult_generator.py`）：
   - 实现同一 `_WorkspaceGenerator` 协议；输入/材料/`_render_prompt` 复用现有 L1 生成器
     的只读投影，checkpoint 问题与 turn key 派生不变（幂等键连续）。
   - 调 consult-ask 得到答案 → 投影 `fin.daily_workspace_product/v1`，
     `generated_via="consult-agent-{agent}-v1"`、`runtime_invoked=true`。
   - 问询档全挂 → 内部调现有 `L1DirectWorkspaceGenerator` 兜底；再挂 → unavailable，
     不落 product（诚实缺口语义不降级）。
3. 装配：`scripts/run_daily_workspace_checkpoint.py` PREPARE 一处换类，其余不动。
4. 配置：新增 `config/daily-workspace.yaml`，缺失时用安全默认（cc→codex→l1）。

## 风险与缓解

- 历史：旧版咨询链委托时代 Daily 曾 2/4 检查点 codex_timeout——agent 生成慢且不稳
  是当年脱钩原因。缓解：per-agent 超时进配置、L1 兜底保投递、交付层等待语义已有
  （owner 09-01：agent 预算 30 分钟，结果一出立即投递）。
- GLM 当前关闭（D-028）：CC 档今天会失败，按链自动落到 codex(ds)，不影响推送。

## 验证与上线顺序

1. 单元测试：新 `tests/operations/test_daily_consult_generator.py`
   （兜底顺序/投影 provenance/unavailable/L1 兜底/config 解析）。
2. 隔离 state root 全链演练（prepare→finalize→claim→FakeSender→settle）。
3. 真实班次对照一次（内容不降级、投递不中断）。
4. 生产 cutover 单独做：checkout SHA + uv sync + 重渲染/重启 8 个 unit + reconcile
   对账，全部通过后才算上线。
