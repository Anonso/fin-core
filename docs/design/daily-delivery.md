# daily-delivery · 设计页（Daily Workspace 生成与投递）

> 依据：rebaseline-20260827.md 附录 C.4 映射——`docs/architecture/internal-module-catalog.md` “Semantic Research State”的 daily 子集 +
> 代码只读核对（页内断言带 file:line）+ §12 假设 D 裁决（durable 不变量逐条列明、owner 清单、崩溃交错、重放验收）。
> 定位：W2'（新仓移植 + Daily 脱钩）的硬前置施工输入。本文只冻结设计事实，不改代码。

## 目标 / 非目标

**目标：**

- 记录 Daily Workspace 生成→投递链的 durable 设计不变量，供 W2' 移植与脱钩按契约施工。
- 冻结生成器替换边界：只把生成器的 **consultation-chain 委托**换成 **L1 直调**，状态机本体（repository/outbox/obligation/run ledger）一律不动。
- 明确投递链的重放验收口径（脱钩前后内容不降级、投递不中断）。

**设计门盲评裁决（2026-08-28 · deepseek-v4-pro，证据 `$STATE/w2-daily-decoupling-design-gate-20260828/`）：**
12 条发现逐条对代码核后的采纳结果已并入下文（崩溃窗口、L1 投影、隔离库验收、并发与措辞修正）。
裁决要点：P0-1（claim 后 outbox 前断电）为真缺口但是现状既有行为，非脱钩引入——只落事实不修状态机；
P0-2（L1 结果投影未定义）为真——施工按本页「L1 直调投影」节执行；
P1-3（同库重放被幂等短路假绿）为真——验收改隔离 state root 两臂重放；
P1-4（prepare_degraded reason_code 拒绝）为误报——生产调用点唯一且固定传
`daily_workspace_prepared_product_missing`，prepare 阶段失败直接上抛不经过它，本页原措辞已修正；
P1-5 部分采纳（scheduled 链确实只经 generator，但 daily open/ask 交互面也是 consultation 消费点，已列入③归档边界输入）。

**非目标：**

- 不重设计 durable schema/状态机，不新增表/列/durable claims（O4 约束，见目录条目）。
- 不做 8 模板盘点与精简重设计（属 W2' 施工时另行进行，rebaseline §6 W2②）。
- 不覆盖咨询主链本体（其生成器是唯一依赖点，其余链将被归档）。
- 不承诺全量测试；本页只描述验证口径与回归入口。

## 数据与 schema 事实（durable store owner 清单）

Daily 链涉及四个独立持久化边界，owner 各一：

| store | owner | 路径 | 事实 |
| --- | --- | --- | --- |
| semantic-research-v1 SQLite | `ResearchStateRepository` | fixed-XDG state root 下 `state.sqlite3`（`scripts/run_daily_workspace.py:114-118`；同目录 `continuation-token-secret.bin`、`installation-identity.hex`，权限不合 0700/0600 即 fail，`scripts/run_daily_workspace.py:67-83`） | 表：chains/products/jobs/continuations/`daily_workspace_obligations`/`daily_workspace_run_ledger`/`idempotency` 等；迁移 v1→v8 顺序追加（`fin_analyse/guo_teacher_research/semantic_state.py:1217-1358`） |
| daily outbox SQLite | `SqliteDailyWorkspaceDeliveryOutbox` | state root 同级 `daily-workspace-delivery-v1/outbox.sqlite3`（`scripts/run_daily_workspace.py:124-127`） | 表 `daily_workspace_delivery_outbox`，state ∈ {DISPATCHING, DELIVERED, FAILED}（`fin_analyse/operations/daily_workspace_delivery.py:1153-1163`） |
| runtime-truth public-entry ledger | `PublicEntryLedger` | `~/.local/state/fin-analyse/runtime-truth-v1/public-entry.sqlite3`（`scripts/run_daily_workspace.py:120-123`） | B0 dispatch acceptance 持久 owner（{platform, message_id, observed_at}）；outbox 缺 acceptance port 即 fail closed（`daily_workspace_delivery.py:209-213`） |
| Hermes 平台投递 ack | Hermes/飞书 | 平台侧 | 非 FIN-owned；FIN 侧以 obligation 终态 + ledger acceptance 记录证明 |

关键 DDL 事实（`semantic_state.py`）：

- `daily_workspace_run_ledger`（:492-504）：run_id PK、checkpoint ∈ {premarket, morning, close, postmarket}、trigger ∈ {manual, schedule, recovery}、`CHECK(started_at <= completed_at)`、stage_statuses 为 typed 终态 JSON。
- `daily_workspace_obligations`（:508-540）：PK `(workspace_ref, product_version)`；artifact_hash NOT NULL；presentation_hash 在 PENDING 必须为 NULL；state 三态由 CHECK 强制与 claim/settlement 字段一致（PENDING 全 NULL；CLAIMED 有 claim_token/claimed_at/presentation_hash 且无 settlement；SETTLED 全齐且 settlement ∈ {POSITIVE_ACK, OUTCOME_UNKNOWN}）。
- `idempotency`（:542-556）：PK `(principal_id, capability, key_hash)`；`capability='daily_workspace'` 时 job_id/product_id 均为 NULL 的 CHECK。
- `products.product_json` 由 `_bind_daily_workspace_product`（定义 :5994）canonical 绑定，`artifact_hash = "sha256:" + product_hash`（调用与计算 :2851-2860）。

部署拓扑事实：4 检查点 × 2 相位 = 8 个 systemd unit 实例，单一渲染代码路径（`scripts/render_daily_workspace_services.py:7`）；unit 由 `scripts/apply_fin_hermes_external_integration.py` 统一 apply/check（rebaseline §0.5.3）。检查点目标时点为代码常量（`daily_workspace_schedule.py:26-29`：premarket 9:20 / morning 10:00 / close 14:20 / postmarket 15:30，Asia/Shanghai）。

## 关键不变量

1. **单事务原子提交**（§12 假设 D 锚点）：`finalize_scheduled_checkpoint`（`semantic_state.py:2970-3020`）→ `append_daily_workspace_version`（:2772）在同一个 `BEGIN IMMEDIATE`（`_transaction` helper，:1444-1446）内写 product 行、version 行、idempotency 行，并在 `create_delivery_obligation=True` 时同事务写 obligation 行（:2928-2966）。**product 提交与 obligation 插入之间崩溃不可能**——要么同落要么同不落（:2983 注释）。
2. **obligation artifact 绑定**：同一 `(workspace_ref, product_version)` 已有 obligation 且 artifact_hash 不同 → `daily_workspace_obligation_conflict`，绝不静默信任漂移行（:2936-2948）。
3. **presentation 延迟绑定**：PENDING 的 presentation_hash 为 NULL；claim 时才绑定真实渲染消息 hash（`claim_delivery`，:3022-3087；注释 :3030-3037）——义务永远等于真正发送的消息，不预存伪造 hash。
4. **claim fencing**：claim 生成 opaque `claim_token = secrets.token_hex(32)`（:3053）；settle 必须出示同 token 且 `hmac.compare_digest` 比对（:3116-3138），旧 claim 的迟到 ACK → `daily_delivery_claim_token_mismatch`。
5. **重放幂等**：同一 terminal settlement + 同 token 重放 = no-op（:3120-3128）；不同 settlement → `daily_delivery_obligation_settlement_conflict`。`EXPLICIT_NOT_SENT` 清空 claim/settlement 字段回 PENDING 以允许重发同一 immutable 消息（:3143-3156）。
6. **obligation 创建幂等**：`_ensure_obligation` 对已存在行幂等返回，重放不产生第二行（:3381-3453）。
7. **run ledger 幂等**：`append_run_ledger`（:3165）按 run_id 幂等；同 payload 重放静默、异 payload → `run_ledger_conflict`。
8. **检查点问题 FIN-owned**：timer 只传 checkpoint enum，问题由生成器常量持有（`daily_workspace_generator.py:22-42`）；daily 无 route/envelope，机器 turn key 由 `(principal, trading_day, checkpoint, question)` 确定性派生——scheduled 的 question 为常量故同 checkpoint 重试复用同一 key；**on_demand 的 question 来自用户输入，key 随 question 变化**（盲评 P2-9 措辞修正）。
9. **不伪造完成**：`unavailable`（或 scheduled 的 `unknown`）结果 → `DailyWorkspaceGenerationUnavailableError`，不落 product（:181-199）；窗口未到/错过以 typed 状态返回（`daily_workspace_runner.py:33-38`）。
10. **并发竞争由 claim 原子性防护**（盲评 P1-6 补）：两个进程（manual 与 scheduled 重叠等）并发 claim 同一 obligation 时，SQLite 事务串行化下恰好一个成功，另一方得 typed `daily_delivery_obligation_not_pending` → `DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN` fail-closed，绝不重复发送；无新增并发设施。
11. **finalize 的 presentation_hash 参数语义**（盲评 P1-8 澄清）：生产装配恒传 None；hash 一律在 claim 时绑定。非 None 仅服务非 PENDING 场景，误用于 PENDING 由 DDL CHECK fail-closed（IntegrityError），不存在静默伪造。

## 接口契约

- **Repository daily seam**（`_WorkspaceVersionRepository` 协议，`daily_workspace_runner.py:164-232`）：create/find daily workspace、`finalize_scheduled_checkpoint`、`claim_delivery`、`settle_delivery`——gateway/use-case 只经这些 typed seam，不读库内表（目录条目）。
- **Outbox ports**（`daily_workspace_delivery.py:81-112`）：`_DeliveryObligationPort.claim_delivery/settle_delivery`（生产实现 = repository，`run_daily_workspace.py:142`）；`_DispatchAcceptancePort.record_dispatch_acceptance`。
- **Outbox.dispatch(product, delivered_at)**（:288）：仓库读产品 → `_render_message`（:1281）→ send → settle；`find_delivered_workspace_by_message_id` 只返回 state=DELIVERED 的 durable binding（:225-257）。
- **生成器协议**：`_WorkspaceGenerator.generate(snapshot, principal)`（`daily_workspace_runner.py:234-235`）。V1 实现 `ConsultationChainWorkspaceGenerator`（`daily_workspace_generator.py:77`）构造 `ConsultCommand(question=..., idempotency_key=确定性 turn key)` 后委托 `consultation_runner.handle(...)`（:140-186），投影为 `generated_via="consultation-chain-v1"`、schema `fin.daily_workspace_product/v1`（:235-283）。
- **产品/快照 schema**：`fin.daily-workspace-contract/v1` + `fin.daily-workspace-input-snapshot/v1`（`semantic_state.py:2991-3004`）。
- **生产装配**：`build_production_use_case`（`scripts/run_daily_workspace.py:87-207`）——consultation service 取自 `GatewayServiceRegistry`（:156-166），`generator = ConsultationChainWorkspaceGenerator(consultation)`（:168）。**scheduled 生成路径上这是脱钩要替换的唯一边界**；注意同一装配里的 `DailyWorkspaceService(consultation_runner=…)` 服务的是 gateway 侧 daily open/ask 交互面（`consultation/daily_workspace.py:559`），不参与 scheduled prepare/delivery——它是③归档咨询链时必须一并裁决的消费点（盲评 P1-5 补记）。
- **投递 transport**：`HermesCliMessageSender`（`daily_workspace_delivery.py:129-149`），目标由 `--delivery-target` 注入（`run_daily_workspace.py:127-131`）；无目标时 `_FakeSender` 拒绝发送（:210-212）。
- **COLLECT**：capture-gated——只读 `GWorkingSetService.evaluate` manifest，不驱动 live 浏览器（`run_daily_workspace.py:173-188`）。

## 已知故障与设计回应

- **历史故障→回应的因果链**：product 提交后 obligation 插入前崩溃失配（handoff 11:39 P1 finding）→ 同事务化（:2983）；DO NOTHING 曾信任错误 artifact_hash（codex P1）→ 强制 artifact_hash 绑定（:2936-2948）；presentation 绑定必须是真实发送消息而非预计算 hash（codex P1）→ claim 时绑定（:3030-3037）。
- **崩溃交错分析（任一时刻断电）**：
  - 断电于 finalize 事务提交前：product/version/idempotency/obligation 全部不落；同 idempotency key 重试安全重放。
  - 断电于 finalize 后、claim 前：PENDING obligation 无 presentation_hash、未发送；claim 幂等推进 PENDING→CLAIMED（单次尝试）。
  - **断电于 claim 提交后、outbox 行写入前（盲评 P0-1 补，现状既有缺口）**：obligation 停留 CLAIMED（presentation_hash 已绑定、claim_token 仅存在于崩溃进程内存）、outbox 无行；`_recover_known_dispatch` 只恢复 outbox 已有 DISPATCHING 行的情况（`daily_workspace_delivery.py:842` 首查 `state='DISPATCHING'`），故下次 dispatch 渲染后 claim 得 typed `not_pending` → `DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN` fail-closed。**后果**：该 obligation 永久卡 CLAIMED，当日投递中断且不自动恢复、不重复发送，需人工按 reconcile 报告介入；**缓解**：窗口为毫秒级（claim 返回与 outbox `BEGIN IMMEDIATE` 之间），且失败诚实可见。本页不改状态机；恢复工具属设施，按家规规则 11 需真实事故举证后才建。
  - 断电于发送后、settle 前：outbox 行 DISPATCHING + obligation CLAIMED 均已持久；恢复走 `_recover_known_dispatch`（`daily_workspace_delivery.py:842`）与 settlement-intent/acceptance 记录（:703/:780）按 message_id 对齐；claim_token 已持久，同 token settle 幂等。
  - 断电于 settle 提交后：SETTLED 终态；同 settlement 同 token 重放 no-op。
  - **迟到 token 的两个分支**（盲评 P2-10 措辞修正）：SETTLED 态下旧 token → `daily_delivery_claim_token_mismatch`；EXPLICIT_NOT_SENT 回退 PENDING 后旧 token → `daily_delivery_obligation_not_claimed`（`semantic_state.py:3140` 分支）。验收两分支都要断言。
  - **一致面/不一致面**：四个持久边界（semantic DB、outbox DB、ledger DB、平台 ack）之间没有跨 store 分布式事务；单 store 内始终原子，跨 store 最终一致性由 outbox 恢复 + `scripts/reconcile_daily_workspace_day.py`（:9-26：`UNACCOUNTED_PENDING`/`TERMINAL_MISMATCH` 跨三库核对）显式收敛，不允许静默失配。

### 安全门拒绝与班次可见性（2026-08-29 基础设施审计裁决落稿）

- **故障事实**：2026-08-28 close/postmarket 四个定时 run 被 `DAILY_WORKSPACE_SCHEDULED_RELEASE_UNSAFE`（receipt 未 ready）拒绝，exit 3，failed unit 挂 ~17h 无人察觉；owner 收到的简报来自人工提前验证 run。安全门拒绝对（防 unsafe release 写生产）；缺陷在可见性。
- **exit code 语义表**（`run_daily_workspace_scheduled_checkpoint.py:361-387`，运维读 journal 用）：`INPUT_INVALID/2`；身份/cgroup 类/`1`；CLOCK/CHECKPOINT_REJECTED/INTERNAL/`RELEASE_NOT_CURRENT`/`RELEASE_UNSAFE`/`3`；透传 checkpoint_main 的 `0/1/2/3/75`；systemd 层 `203/EXEC`、`15/TERM`、`75/TEMPFAIL`。**fail-safe 细节**：exit 1 时 `production_scheduler=false` → reconcile 读器不认 → 交易日报表 `SILENT_GAP` 而非 `SCHEDULED_GATE_REJECTED`——方向安全（不会漏报），但排查时要知道这个映射。
- **manual DELIVERED 与 scheduled 拒绝并存**（08-28 close/postmarket 即此形态）：outbox 行与 gate 拒绝是**两个都真实的事实**，reconcile 同时报 `delivery_accounted=true` 与 `SCHEDULED_GATE_REJECTED` anomaly 是正确语义，不是双计 bug；读报告者须知道「该班有产物且投出，但定时面当时被门拒」。
- **裁决（A1，推翻 packet 原「durable 落点」预设）**：对账能力已存在——reconcile 读 journal 的 `scheduled_gate_failures`（08-28 重放实证 close/postmarket 各 2 条 gate failure + anomaly 在报）；08-28 的 17h 盲区根因是**没人跑 reconcile**，不是缺 durable 表。缺 durable 落点只影响「多日后补对账」（journal 有保留期）；门拒时入口零持久写是刻意的零副作用契约（`side_effects_unknown:false`，reconcile 读器以 `is not False` 过滤），入口写审计行会动该契约——不为想象中的晚对账需求破坏它（家规规则 11）。
- **同日对账纪律（补的是纪律不是机器）**：任何 cutover/release 切换日，当日最后一个 checkpoint 之后跑一次 `scripts/reconcile_daily_workspace_day.py --trading-day <当日>`（读态零副作用，exit 0=ok / 1=有发现），确认无 `SCHEDULED_GATE_REJECTED`/`SILENT_GAP` 意外再收工——入部署 runbook 成套核对清单。
- **非交易日行为（A7 实证 2026-08-29，全六班闭环）**：交易日门在 runner 层（`daily_workspace_runner.py` 经注入的 `_is_open_date` 读冻结日历），周六全六班实测（08:55/09:20/13:55/14:20/15:05/15:30）均 `NOT_TRADING_DAY` exit 0 干净收班，failed 残留清零；当日 `reconcile --trading-day 2026-08-29` 输出 `ok=true`、四班 `silent_gap_suppressed=true`、零 anomalies（与外部审计预期逐项吻合）。已知债两笔：①门在组装之后，非交易日 run 并非真 no-op（会打开/可迁移 state.sqlite），若库缺失会静默建新库——周末 run 前提是库在位；②日历判断在 runner 与 reconcile 两处实现（reconcile 侧 data_gaps 日 fail-open 当交易日），改语义要动两处。

## L1 直调投影（脱钩施工设计，盲评 P0-2 裁决产物）

生成器替换不是“把 L1 结果塞进 ConsultationResult”，而是**独立投影**：

- 新 `L1DirectWorkspaceGenerator` 实现同一 `_WorkspaceGenerator` 协议（`generate(snapshot, principal) -> dict`），落位 owner 包 `fin_analyse/operations/daily_workspace_generator.py`；`consultation/daily_workspace_generator.py` 的委托版随本刀删除（引用闭包=生产装配一处+对应测试，家规规则 12 同步删测试）。
- 输入：snapshot 的 checkpoint/trading_day/`daily_workspace_context` 投影/快照 receipt（与 V1 相同的校验与 `_TimingBoundGenerator` 包装不变）。
- 生成：checkpoint 固定问题 + snapshot 可用上下文渲染为单轮 prompt，经 llm.yaml `priorities.t0` 序**截前 2 端点**（现配置 glm53 → deepseek）直调，复用 `claims/config_loader.load_llm_config` + `OpenAICompatibleBackend`（`build_deepseek_guide_backend` 的 fail-closed 凭据校验模式：key/base_url 非空、无 `${ENV}` 字面量、HTTPS）；作业级重试交给 checkpoint 语义，客户端内不做长尾重试。
- unavailable 语义不降级：两端点皆失败/空响应/超时 → `DailyWorkspaceGenerationUnavailableError`（typed gap），不落 product，不伪造完成。
- 输出：直接产出 `fin.daily_workspace_product/v1` 形状 dict，同键齐备——`generated_via="l1-direct-v1"`；`agent_provenance` 保持形状但如实（runtime_invoked=false/output_used=false/generation 标 l1-direct）；`input_snapshot_receipt`、`first_screen`（top_items 单条=答案全文，不升格 summary）、`data_gaps`、`consultation_product` 段保留同形（product 字段承载结构化答案）。honesty 规则逐条保持：plain summary 不升格、unavailable 拒于落库前、context boundaries 标注不变。
- turn key 派生式不变（`_daily_consultation_turn_key` 同式），保证脱钩前后同 checkpoint 的幂等键连续。

## 验证方式

- **回归入口**：`tests/guo_teacher_research/test_semantic_state_daily_workspace.py`、`tests/guo_teacher_research/test_daily_workspace_obligation.py`、`tests/consultation/test_daily_workspace.py`、`tests/operations/test_daily_workspace_generator.py`（新）、`tests/operations/test_daily_workspace_delivery.py`、`tests/operations/test_daily_workspace_runner.py`、`tests/operations/test_daily_use_case.py`、`tests/scripts/test_reconcile_daily_workspace_day.py`、`tests/scripts/test_run_daily_workspace.py`。
- **脱钩前后内容不降级（隔离库两臂验收，W2② 施工时执行——盲评 P1-3/P2-12 裁决后口径）**：同库同键重放会被 idempotency 短路直接返回旧 product、新 generator 不被执行，故验收**不在生产 state root 重放**：
  1. 基线臂：脱钩前对固定交易日+checkpoint 的 owner 判定 baseline（已存 `$STATE/w2-daily-decoupling-design-gate-20260828/baseline-20260827/`，8-27 三个完整 checkpoint 产物）。
  2. 新臂：临时隔离 state root + 固定时钟，同一 checkpoint 输入跑新 generator 全链（prepare→finalize→claim→FakeSender dispatch→settle）。
  3. 比对：脚本断言结构契约（schema/键集/必填段非空、`generated_via` 换值合法、data_gaps 不新增缺口码；材料键集合经设计门变更时允许新增 `l1_material_<key>_*` 码——2026-08-31 daily-g-context-material 设计稿 P2-3 裁决修订）+ owner 对两臂样本盲比内容语义（理解准确/密度/去噪不降）。
  4. 投递不中断：隔离库 obligation 全链 PENDING→CLAIMED→SETTLED 且无失配；生产侧以当日真实 checkpoints 兑现（至少一条真实 Hermes 对照）。
  5. 状态机不动证明：脱钩 diff 只允许触碰 generator（迁址+重写）与装配一处（`scripts/run_daily_workspace.py:156-168`），repository/outbox/delivery 零改动。
- **重放幂等验收**：同 idempotency key 重跑不新增 product_version；同 claim token 重放 settle no-op；旧 token 迟到 settle 在 SETTLED 态报 `daily_delivery_claim_token_mismatch`、在回退 PENDING 态报 `daily_delivery_obligation_not_claimed`（盲评 P2-10 两分支都要断言）。
- **崩溃演练**：注入“发送后、settle 前”断电，重启后 outbox 恢复/reconcile 对齐，不产生重复投递或失配终态；另断言“claim 后、outbox 行前”崩溃态（手工构造 CLAIMED 无 outbox 行）下 dispatch fail-closed 报 `OUTCOME_UNKNOWN` 且 reconcile 可见该失配（盲评 P1-7/P0-1 裁决：验证其诚实失败，不建自动恢复）。

> L1 路由细节：见 `docs/pm/l1-route-chain-survey-20260827.md`（已完成盘点）与生产事实 `config/llm.yaml` `priorities` 段（2026-08-28 配置化，`t0: [glm53, deepseek, gpt5, qwen]`）；两处不一致时以生产配置为准。
