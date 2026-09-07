# fin-adjudication-mcp · 接口契约（D-053 · v1.1 活文档）

> 状态修订：本页不再是「合入即删」的一次性设计稿——Hermes 域按此契约并行施工，
> 它是双侧的**活契约**（v1 冻结于 065add8；v1.1 修订见文末，双侧会话确认于
> 2026-09-07：owner 经 Hermes 反馈「看不到冲突详情无法裁决」→ 增 score_get +
> conflict_detail）。稳定后按规则 5 归档。

> 依据：owner 2026-09-07 拍板两件——①Hermes fin profile 清零重建（Hermes 侧会话执行，
> 本仓不碰 profile）；②「Hermes 的功能让 Hermes 自己实现，FIN 只做好自己功能、提供
> 接口」。本页冻结的就是那个接口：FIN 侧唯一新面 = 一个 stdio MCP server。
> 合入后本页删除（Git 即归档）。

## 目标 / 非目标

**目标：** 给 Hermes 侧提供类型化的裁决操作接口（工具闭集），覆盖收件箱裁决与
评分 needs_review 闭环；每一变更动作落本机审计。

**非目标：** 不做咨询面（08-27 停用不翻案）；不做任何交易/资金动作；不管理
Hermes profile（skill/人格/注册全在 Hermes 侧）；推送 timer 不变（仍是 systemd +
HermesCliMessageSender，不走 MCP）。

## 接口契约（冻结 v1，修订需 FIN/Hermes 双侧会话确认）

- **启动**：`/home/ypk/fin-core/.venv/bin/python -m fin_analyse.adjudication.mcp_server`（stdio）；
  必需 env `FIN_KNOWLEDGE_BASE_ROOT`（评分 registry 定位，缺失 startup exit 2，
  对齐 read_capabilities 先例）。Hermes 侧以同一用户拉起（无 auth——本机单主体，
  飞书把门在 Hermes allowlist）。
- **工具闭集 6 个**，返回一律 `{"ok": true, ...}` 或 `{"ok": false, "error": <typed code>}`：

| 工具 | 参数 | 返回 | 语义 |
| --- | --- | --- | --- |
| `adjudication_list` | — | `items[]: {item_id, kind, title, backlog_days, resolution_hint, payload_ref}` | inbox open 集，opened_at 升序 |
| `adjudication_done` | `item_id, note?` | `status: "done"` | 经 inbox seam resolve（source=owner），已决/不存在 → `error: adjudication_item_not_open` |
| `score_list` | `limit≤50, offset` | `pending_total, rows[]: {record_id, code, name, article_date, review_reason}` | needs_review 闭集视图，article_date 降序 |
| `score_confirm` | `record_id, note?` | `status: confirmed \| already_ok` | status→ok；不存在 → `record_not_found`；幂等 |
| `score_drop` | `record_id, note?` | `status: dropped` | 删行（owner 逐条指令=逐条授权）；不存在 → `record_not_found` |
| `digest_send` | — | `disposition` | 复用 push()（指纹幂等/fail-closed 全保留）；env 缺目标 → typed error |

- **审计**：`$STATE/fin-analyse/adjudication-inbox-v1/mcp-ops.v1.jsonl`（0600），
  每个**变更**动作一行 `{at, action, target, note, via: "mcp"}`；查询不记。
  inbox 动作同时自然落 inbox.events。
- **硬边界 3 定向豁免（owner 2026-09-07 拍板）**：评分明细（代码/名称/原因）
  经 Hermes 进 owner 本人飞书私信——范围仅此流，其余用户数据仍不出本机。

## Hermes 侧对接（该侧会话消费本契约，互不等待）

1. profile 清零重建（备份协议家规 4）；config 只注册本 MCP server + 飞书通道 +
   owner allowlist；skill 极简路由（裁决类消息→工具；其余不接）。
2. 交互语法建议（非契约）：推送带 record_id/item_id；回复「确认/剔除 <id>」
   「完成 <item_id>」「更多」「推一下」。

## 决策与证据（家规 11）

- 举证 = owner 2026-09-07 明确要求（飞书闭环 + MCP 路线 + 分工指令）。
- 否决 = 文件桥方案（decision jsonl + .path consumer——MCP 少一整层，LLM 转写
  环节被类型化工具取代）；重开咨询面（范围外）。
- 净复杂度 = 1 模块 + 6 工具 + 1 审计文件；零新 durable store（复用 inbox.sqlite
  与 score registry）。

## v1.3 修订（2026-09-07 深夜，owner 飞书实测暴露复活洞）

- 新增 registry 墓碑层：`instrument_scores_tombstones.v1.jsonl`（cognition 目录，
  0600）。`score_drop`（MCP 与 CLI 同权）剔除成功即落墓碑；`upsert_records`
  唯一写入口统一过滤——新行不收、存量即清。采集水位重析/手动 backfill 重放
  均不再复活已剔行。
- 实测动因：owner 飞书剔除 ddc1c72f（港股科伦博泰）成功后，该行数小时内第三次
  复活——其文章在水位重析窗口内，每 tick 重发。墓碑前「剔了又长」，墓碑后终局。

## v1.4 修订（2026-09-07 深夜，owner 问「终审能通过飞书审吗」→ 闭环到最后一步）

- 新增第 10/11 工具：`g_draft_list()`（readOnly）= 列出起草会话发布的待终审
  单元（manifest `$STATE/fin-analyse/adjudication-inbox-v1/g-batch-draft.v1.jsonl`，
  未发布时 ok+空表+提示）；`g_draft_verdict(unit_ids, verdict approve|reject,
  note)` = owner 终审裁决落
  `g-batch-verdicts.v1.jsonl` sidecar，起草会话消费执行（approve→写标注文档
  →机验→入档；reject→修改或弃）。动作落 mcp-ops 审计。
- 工作流闭环：起草会话起草+机验 → g_draft_list 呈报 → owner 飞书逐单元
  approve/reject → 起草会话按裁决入档。G 写入执行仍在本地确定性链
  （Hermes LLM 不直写标注文档），飞书只承载呈报与裁决。
- 本地执行半边（2026-09-08 落地，批 9/05-09/07 首跑）：发布 =
  `python -m fin_analyse.guo_teacher_research.mainline_draft_manifest`
  （机验过的起草稿→manifest，0600，首行 _meta+逐单元 title/逐字摘录/
  topic_id）；裁决消费 = `python -m
  fin_analyse.guo_teacher_research.mainline_batch_merge`：全单元有裁决才动
  （未决即拒），reject=该单元不入档（语义改稿仍走起草会话），拼合→
  canonical 上机验→原子替换→as_of 滚至最新裁决时点→rebuild→清空 manifest
  （g_draft_list 回空表）。四点拼接确定性无 LLM；首跑即真实批次
  （25 单元，gen 59）。

## v1.2 修订（2026-09-07，owner 要求「G 批次能在 Hermes 批注，提供能判断的信息」）

- 新增第 8/9 工具：`g_batch_list()`（readOnly）= as_of 之后全部老师文章的判断
  信息面（date/column/能量 score/nominated/title/topic_id，每日热点除外，日期
  降序，含 lag_days 与 as_of）；`g_batch_select(entries, verdict)`（verdict 闭集
  keep|drop）= owner 勾选写入 sidecar
  $STATE/fin-analyse/adjudication-inbox-v1/g-batch-selections.v1.jsonl，
  本地起草会话消费；动作落 mcp-ops 审计。
- 边界不变：Feishu 只做**勾选**；起草协议/机验/终审入档仍是本地会话职责
  （G 认知主线=owner durable 认知数据，不让 Hermes LLM 直写标注文档）。

## v1.1 修订（2026-09-07，双侧确认）

- 新增第 7 工具 `score_get(record_id)`（readOnly）：返回单条记录**全字段**，
  含 `conflict_detail`（cross_source_conflict 专用：同码各载体
  `{origin, lihao, consensus}` 全量）；id = 精确或唯一前缀。
- registry schema 增列 `conflict_detail`（ingestion.instrument_scores 写入侧，
  解析时保留双源数值，替代「只留旗标丢数值」）。
- 语义澄清：cross_source_conflict 实测=**同篇同码两行不同评分**（两载体互证
  一致），即文内双表，非跨文章打架；详情见 conflict_detail。

## 契约附注（v1 冻结面细化，评审 S3 采纳）

- `digest_send` 的 `disposition` 六值闭集：`no_open_items / already_sent /
  debounced / sent / outcome_unknown / dry_run`；
- typed error 全表：`adjudication_item_id_required / adjudication_item_not_open /
  record_id_required / record_not_found / record_id_ambiguous / registry_unreadable /
  inbox_unavailable / digest_target_missing / digest_send_failed`；
- `score_confirm` 返回 `already_ok` = 该记录此前已确认（Hermes 侧不应再次提示
  确认，直接回执即可）；
- `record_id` 支持**唯一前缀**匹配（`record_id_ambiguous` = 歧义拒绝）——飞书
  手打 64 位 hex 的现实容错；歧义时 Hermes 应回列表让 owner 精选；
- `score_find(code|name)` 检索：v2 候选，当前用 score_list 分页兜。

## 设计门裁决（2026-09-07，cmd·deepseek-v4-pro，台账 design-gate/fin-adjudication-mcp-20260907/）

1P1/4P2/3P3：P1（digest_send 异常面漏 typed）采纳已修；P2 采纳五件——registry
唯一写串行化点收敛到 upsert_records（内部 flock + 唯一 tmp 名，三写者全收口）、
drop 改走 remove_record_ids（消手写整文件重写）、stdout guard/startup
fail-closed/stdio roundtrip 三测试补齐、豁免边界落模块 docstring（防设计稿删除
后蒸发）；P3 采纳——读路径 typed、record_id 唯一前缀匹配、docstring 禁语加固、
契约附注本节；P3 注记接受——审计晚于变更的崩溃窗口、digest 双实例 at-least-once。

## 验证方式

handler 级单测（对齐 decision_journal_handlers 先例）：工具闭集行为、typed 错误、
幂等、审计行、limit 钳制、startup 缺 env fail-closed；stdio roundtrip 由 FastMCP
框架承担（read server 同款不重复测）。
