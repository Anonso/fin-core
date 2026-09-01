# g-cognition · 设计页（G 认知主线：工作集 / 长认知 read-model / 次级记忆）

> 依据：rebaseline-20260827.md 附录 C.4 映射——`internal-module-catalog.md` “G Working-Set Freshness Manifest” +
> “Cognition Memory / Teacher Persona” + g-methodology skill + PIT 语义（as_of/audit_by_ref）+ `guo:v0`≠长认知边界；
> 代码只读核对（页内断言带 file:line）。定位：W2' 移植施工输入；本文不改代码。

## 目标 / 非目标

**目标：**

- 记录 G（老师认知）三层的 durable 边界与不变量：① fresh G 工作集（teacher 原文 + priority event + deep-read pair 的 operational freshness）② 人工审核的长认知主线 read-model ③ 次级 persona/pattern/ReasoningTrace 记忆。
- 冻结 point-in-time（PIT）语义：`read_g_context` 的 `as_of`/`audit_by_ref`，以及长认知 read-model 的 `available_at/processed_at ≤ as_of`。
- 固定来源边界：只有 qualified teacher 原文能成为 G；Z/行情/新闻/研报/外部研究永远不得验证、覆盖或写入 G。

**非目标：**

- 不描述 ZSXQ 抓取（见 zsxq-capture 页）、deep-read artifact 生成（见 deepen 页）、read-capabilities 薄 server（D1 v2 已是设计页）。
- 不重设计 persona/pattern 写入链（其严格双 LLM 共识契约随迁移保留，本页只登记边界）。
- `guo:v0` 只登记其“身份标签≠长认知”的边界，不承诺其去留。

## 数据与 schema 事实（owner 清单）

| store | owner | 位置/schema | 事实 |
| --- | --- | --- | --- |
| G 工作集 manifest | `GWorkingSetService` | `knowledge-base/runtime/operations/g_working_set/manifest.v1.json`（相对路径 `g_working_set.py:34`）；schema `g-working-set-manifest.v2` | 唯一 manifest writer；父目录 0700/文件 0600、dirfd/no-follow、目录 flock（:722/:821）、随机排他 temp + fsync + atomic replace（:898） |
| 知识索引 / priority events / deep-read artifacts | 各自原 owner（scraper/cognition） | `knowledge-base/**` | manifest 服务只读绑定，不复制不改写（目录条目） |
| 长认知 read-model | `CognitionMainlinePublisher`（构建期唯一 writer）/ `CognitionMainlineReadModelReader`（只读） | Git 外 `$XDG_STATE_HOME/fin-analyse/cognition-mainline-readmodel-v1/`；schema `fin.cognition-mainline-readmodel/v1`（`cognition_mainline_readmodel.py:30`） | 26 单元人工审核认知；build 期从标注文档确定性生成；validator 闭集（7 值 cognition_mode、5 值 relation）任一失败整份拒绝；numeric-generation CAS（:794，disposition PUBLISHED/ALREADY_PUBLISHED/REJECTED） |
| 次级认知记忆 | `CognitionMemoryStoreService` | core cognition JSONL：`evidence_items.jsonl`/`reasoning_traces.jsonl`/`cognitive_patterns.jsonl`/`teacher_personas.jsonl`/`persona_analyses.jsonl`/`feedback.jsonl`/`trace_verifications.jsonl`（`cognition/memory_store.py:104-107,127-136`） | 四级 scope：`teacher_cognition`/`external_evidence`/`shared_reference`/`agent_private`（:51-71）；生产只读经 `open_existing_owner_only_read`（:149）不建目录、拒绝写；未知 kind 在任何 I/O 前 `UNKNOWN_MEMORY_KIND`（:165） |
| G 源资格（写入门） | `CognitionWriteGateService` | 无持久状态 | 统一 write gate seam；只有 qualified teacher 原文经严格 extraction 才能晋升 ReasoningTrace（目录条目） |

## 关键不变量

1. **活跃窗口**：锐评/每日热点 = commentary 交易日档，其余严格 G = special 档（`config/g_context_windows.json`，经 `window_config.py` 单源）；严格 G 列闭集 = 星大派锐评/特刊/好问题/每日热点/人脉/凤仙郡小故事（`g_working_set.py:42-43`；2026-09-01 owner 拍板新增每日热点、人脉）。
2. **来源门**：`classify_g_source`（`source_contract.py:46`）只接受精确 星大派（特刊/锐评/每日热点/人脉/老师原答的好问题）与凤仙郡小故事；泛化“星大派/好问题/合格好问题”→ typed gap；“重中之重”只是检索标签，不改变来源权威/时效。
3. **PIT 语义（read_g_context）**：`request.as_of` 非空时 `audit_by_ref = _g_audit_by_ref(resolved.audit_context)`（`production_capability_provider.py:235-244`，投影 :1504）；as_of 非空但审计不可得 → `g_context_point_in_time_unavailable`；评估只输出与审计时点一致的分层投影（`runtime_context.py:499` 注释：generation 语义与 `fin.read_g_context` 一致）。
4. **PIT 语义（长认知 read-model）**：revision/节点两层 `available_at` 且 `processed_at ≥ available_at`（`cognition_mainline_readmodel.py:194-195,258-260`）；evolution `available_at` 单调非降（:272）；投影只消费 `G_ORIGINAL`，任何 `available_at/processed_at > as_of` → typed gap（`project_cognition_mainline` :992）。
5. **READY 证据不可自洽伪造**：`canonical_sha256` 排除 `evaluated_at`（同内容稳定）；`evaluate` 按 manifest as-of 从同一 source snapshot 重新选择逐篇 index/event binding 后再查 deep-read——单凭自洽重哈希的 manifest 不能成为 READY evidence（`g_working_set.py:946` evaluate；目录条目）。
6. **混代禁止**：cache-backed runtime 在 deep-read enrichment 前把 article/index-entry SHA-256/priority-event SHA-256/manifest 逐项精确回绑；任一 snapshot 漂移 → `g_working_set_sources_changed`（`g_working_set.py:975`），不得拼装“新 event + 旧 deep-read receipt”。
7. **guo:v0 ≠ 长认知边界**：`guo:v0` 只是旧 Persona 对象的身份/版本标签，不能代表 6-8 月长认知时间线或演化（`docs/architecture/fin-private-advisory-decision-framework.md:72-80`）；长认知正文由 read-model 承载，旧 Persona 快照不得冒充。
8. **双工具分工**：`read_g_context`（`production_capability_provider.py:202`）= G 认知主线（分层投影 + 审计时点）；`read_teacher_cognition`（:252）= 次级 persona/pattern 记忆。**已核实生产事实**（rebaseline §0.5.7 P0-1）：teacher_cognition 读取目录模式 775 → `open_existing_owner_only_read` 失败 → 恒 unavailable；薄 server v1 已把 `read_teacher_cognition` 移出，修复权限且有真实需求再纳入。
9. **预算与用途边界**：主线/方法论投影为有界纯函数（`project_mainline` `g_mainline_projection.py:51`、`project_methodology` `g_methodology_projection.py:61`），4 KiB item 上限、与 G refs 共享 ≤32 引用预算、`usage_boundary=background_guidance_only_no_confidence_boost`（目录条目）——不提升置信度、不冒充当前观点。

## 接口契约

- **工作集 seam**：`GWorkingSetService.reconcile`（`g_working_set.py:551`）→ `READY|STALE|MISSING|PARTIAL`；`evaluate`（:946）、`read`（:916）；写入口只经 `prepare_publication`（:695）+ `compare_and_publish`（:710，返回 PUBLISHED/ALREADY_PUBLISHED/REJECTED）+ `verify_published_plan`（:811，shared lock 严格读取真实 manifest 回绑 plan）；`reconcile_and_publish`（:855）只是兼容 facade。
- **runtime 只读 seam**：`AgentRuntimeContextProvider.resolve(...)`（`runtime_context.py:305,352`）——生产 composition 必须显式传入唯一 `kb_root`（`GatewayServiceRoots.kb_root`），不从源码/cwd 推断。
- **能力 seam**：`read_g_context`（`production_capability_provider.py:202`）以 `agent_id="guo_teacher"`、`max_g_events=_MAX_G_ITEMS` 解析；输出分层投影（pinned/framework/facts/associations/external_brain）。
- **长认知 seam**：`CognitionMainlinePublisher`（构建期）/ `CognitionMainlineReadModelReader`（只读当前 revision，missing/corrupt/schema_drift/hash_drift typed failure，不隐式创建）/ `project_cognition_mainline`（PIT 注入）。
- **记忆 seam**：`CognitionMemoryStoreService.handle(CognitionMemoryRequest) -> CognitionMemoryResult`（14 操作）；scope 合同四值；生产 consultation composition 注入已打开的 owner-only read view，unsafe/missing root 只降级 reader unavailable。

## 已知故障与设计回应

- **混代 G**（新 event/index 配旧 deep-read receipt）→ 逐项回绑 + `g_working_set_sources_changed` fail（`g_working_set.py:975`；runtime_context 精确回绑）。
- **旧文章被新 event 重新变鲜 / future time 进入 LLM** → 活跃窗口按 index 文章时间选择（`select_active_g_working_set` :393），future/invalid candidate time 不进入（目录条目）。
- **自洽重哈希 manifest 冒充 READY** → `evaluate` 必须从同一 source snapshot 重选绑定后再查 deep-read（:946）。
- **teacher_cognition 恒 unavailable**（目录 775）→ 已核实为生产死线；v1 移出工具面，回应当前不做权限静默改权（只读路径不自动改权，须显式审计迁移，目录条目）。
- **symlink/多根混拼** → 知识根或其 material 父路径为 symlink 时 dirfd/no-follow reader 拒绝读取（目录条目）。
- **NO_CHANGE 时间连续性**：prior owner evidence 的 `evaluated_at` 必须 ≤ `run.started_at`，晚到 prior → 非 READY completion（目录条目）。

## 验证方式

- **回归入口**：`tests/guo_teacher_research/test_g_working_set.py`、`tests/guo_teacher_research/test_agent_runtime_context_provider.py`、`tests/guo_teacher_research/test_cognition_mainline_readmodel.py`、`tests/cognition/test_memory_store_service.py`、`tests/cognition/test_write_gate_service.py`。
- **PIT 验收**：同一 `as_of` 两次 `read_g_context` 审计时点与 refs 一致；`as_of` 早于 read-model `available_at` → typed gap 零阻断；`as_of=None` 不给审计（当前时点语义）。
- **来源门验收**：泛化标签/外部证据不得进入 teacher 投影；`write_gate` 只放行 qualified teacher 原文，external/reference/apprentice interpretation 不得写 persona/pattern。
- **混代与漂移验收**：改动 index/priority event/deep-read 任一 snapshot 后重跑 → `g_working_set_sources_changed`/`g_working_set_deep_read_changed`，不静默混代。
- **READY 证据验收**：同一 publication 重复 reconcile 时 `canonical_sha256` 稳定；仅重哈希不改内容不算 READY 新证据。
