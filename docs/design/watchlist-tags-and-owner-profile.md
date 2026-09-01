# 设计稿：owner 口述三件套（watchlist 标签与写入口 / 账户可买约束 / G 置顶可观测）

> 动代码前唯一设计。owner 2026-09-01 在问询会话口述，开发域承接持久化与能力；
> 状态源见 `docs/pm/usage-profile.md` 口述补充段。合入后本页删除（Git 即归档）。

## 1. 目标 / 非目标

**目标**：
1. 自选股支持多值标签 + 添加方（owner/assistant），assistant 可新增与打标签、
   不可删除；拟删除用保留标签 `suggest_delete`，由 owner 拍板。
2. 账户可买约束入 typed 数据：仅主板（非 688/300）可买入；688/300 只研究。
3. 「置顶进没进」可事后审计：`read_g_context` 返回与 trace 都暴露 pinned 摘要。

**非目标**：不做 assistant 删除/改名；不做标签白名单（除保留字 `suggest_delete` 外自由，
仅约束格式）；不做 688/300 的机械过滤（由人格 + `read_actual_portfolio` 字段执行）；
不动 Hermes/旧仓写面（`update_user_watchlist` 仍独立）。

## 2. watchlist：schema 与操作

**schema（SQLite `entries` 增两列，新库建表带列、旧库启动迁移 ALTER）**：
`provenance TEXT NOT NULL DEFAULT 'owner'`、`tags TEXT NOT NULL DEFAULT '[]'`（JSON 数组）。
revision 内容改为含 `provenance+tags`（改标签即 bump revision，CAS 语义不变）。
audit 表不改 schema，operation 扩 `add_tags`/`remove_tags`。

**标签约束**：单标签 1–24 可见字符、去重、≤8 个/标的；`suggest_delete` 为保留字。
provenance ∈ {owner, assistant}；assistant 的 provenance 由服务端强制，客户端不可传。

**store API**：`add(identity, *, expected_revision, provenance="owner", tags=())`；
新增 `add_tags(symbol, tags, *, expected_revision)` / `remove_tags(...)`（后者仅 owner CLI
调用，服务端写工具不暴露）；`remove` 不变。

**写面两路，共享 resolve/CAS（沿用 watchlist_write.py 的 seam）**：
- owner CLI：`manage_user_watchlist.py` 增 `--provenance`、`--tag`（可重复）、
  `tag`/`untag` 子命令；`remove` 仍 owner-only。
- 问询侧：薄 server 新增第 8 工具 `update_user_watchlist`（仅 `list`/`preview`/`apply`，
  operations 只接受 `add` 与 `tag`，禁止 `remove`）。两段式：preview 解析 + 返回
  确认短语与本地随机 token（进程内存、TTL 15 分钟、单次使用）；apply 只收 token，
  逐条 CAS。headless/一次性会话人格禁止 apply（只给预览），交互会话须用户逐字确认。

**read 投影**：`read_user_watchlist` entries 增 `provenance`、`tags`；语义仍
user context / never investment evidence。

## 3. 账户可买约束

`actual-advisory-portfolio.v1.json` 增可选字段 `buyable_board_rule`：
`null`（缺省/未知）或 `"main_board_only"`。旧快照缺字段继续合法（解析为 None）。
`ActualAdvisoryPortfolioSnapshot` 增字段并进 `to_safe_dict`；operator preview/publish
走同一 `_parse` 校验（额外字段必须合法）。`read_actual_portfolio` 返回该字段；
问询人格据此只对主板给买入建议与整手换算。

## 4. G 置顶可观测

`read_g_context` 的 `attestation` 增 `quality`：`{pinned_injected, pinned_candidate_seen,
pinned_layer_count, pinned_data_gaps}`（来自 `AgentRuntimeContextResult.quality_flags`
与 pinned 层投影，纯增量不改既有字段）。server trace 对 `read_g_context` 追加可选
`summary` 字段（`g_pinned` 同上四值）；trace schema_version 保持 1，旧行无 summary。
判定链不变：trace 无调用=漏调；有调用带 `pinned_source_*` gap=门/数据源跳过；
ok 无 gap 且 pinned_layer_count>0=已注入，答案没用上属模型侧。

## 5. 问询人格同步（consult-agent）

CLAUDE.md（AGENTS.md 软链共用）：
- 主线边界：候选/研究覆盖默认只 AI 泛产业链；非主线仅用于判断市场环境，不给买入建议。
- 自选写协议：可 add/tag、禁 remove；`suggest_delete` 提删；apply 前必须用户确认；
  headless 只预览。
- 主板约束：688/300 只研究；买入建议/整手换算只给主板。
- 工具闭集改述为 7 读 + 1 写（当前文案仍写 6 只读，顺带修正）。

README MCP 表同步。变更前按现有备份协议备份 CLAUDE.md 并更新 MANIFEST。

## 6. 数据落盘

- 存量 watchlist DB：启动迁移自动加列，存量行 provenance 默认 owner、tags 空。
- 快照文件：经 operator 流程把 `buyable_board_rule=main_board_only` 与当前确认时点
  落进 `~/.config/fin-analyse/actual-advisory-portfolio.v1.json`（用户数据不入 git）。

## 7. 验收

- 单测：store 标签/迁移/CAS；write seam tag 语义；CLI tag/untag；
  薄 server 写工具 preview→apply 回环 + remove 拒绝 + 过期/复用 token 拒绝；
  read 投影含 provenance/tags；actual_advisory 新字段与旧快照兼容；
  read_g_context attestation.quality；trace summary。
- 实弹：`finqac`/CLI 一次问询，trace 出现 summary；`manage_actual_advisory_portfolio.py
  show` 返回 buyable_board_rule。

## 8. 风险

- 写工具入只读薄 server 是设计修订（原非目标=不做写入口）：范围锁死 add/tag 且
  preview/apply 两段式，删除仍不可能；stdio 本地单 principal，风险面=本机所有者。
- token 进程内存：server 重启即失效，行为=拒绝 apply，无持久 pending，安全侧闭合。
