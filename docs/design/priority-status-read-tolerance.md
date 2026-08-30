# BUG-009 方向1：priority_analysis_job_status 读侧宽容化 + 逐条隔离（v2·裁决后）

> 规则 5 短设计（durable 读缝的契约放宽）。合入后删除，Git 即归档。
> 触发：BUG-009（docs/pm/BUGS.md）方案三选一，owner 2026-08-30 拍板走方向1
> 并过设计门。

## 设计门裁决记录（2026-08-30）

评审者 `codex_open.sh --sandbox read-only`（deepseek-v4-pro·max），elapsed 469s，
发现 12 条（P0×0 / P1×4 / P2×8），采纳 12/12。方向（宽容+隔离+fail-closed+
`reports_feishu_push_succeeded` 不变量）零 P0 通过；P1 全在坏行计数传导链：

- **P1-1 采纳**：`check_priority_dispatch_health` 内的 `list_statuses()` 调用点
  升级为第三处改动（改调 `_with_health`），原「两调用点零改动」表述作废。
- **P1-2 采纳**：`PriorityDispatchHealth` 新增 `bad_status_entries: int = 0`
  （dataclass + `to_dict()`）；provider_health 侧零改动——字段经 `to_dict()`
  自动进 MCP payload，计数即降级标记，不另设 flag。
- **P1-3 采纳（裁决为计数可见而非保守 pending）**：坏行无法归因到 job（解析
  失败读不出 job_id），强制 `priority_dispatch_pending=True` 是捏造信号；采纳
  「计数为绊线 + docstring 钉 latest-wins 遮蔽残余风险」+ 专门测试覆盖
  「坏行恰为该 job 最新行」场景。
- **P1-4 采纳（显式范围风险）**：用户可见症状 `priority_status_outbox_unavailable`
  出自**老仓** report.py + 老 common/priority_health（独立解析器，两套代码）——
  本修后变好的是 fin-core `check_priority_dispatch_health` 与 provider_health
  MCP payload 的 `priority_dispatch` 段；老仓报告恒显 unavailable 不变，其
  迁移/退役是独立后续项，不得把本次合入当成「报告已修好」。
- P2×8 全采纳：双代际测试（20×六字段 + 19×七字段）、delivery 放行钉单一正则
  `^feishu:oc_[0-9a-f]+$`（39/39 探针验证）、坏行口径四类计入/空行与 torn 尾帧
  不计（既有行为文档化）、既有测试改名 `allows_registered_v2_extensions_and_
  rejects_unknown`、合成 oc id 入夹具（规则3）、扩展字段值形态与
  `result_classification` 不参与聚合登记、`to_dict()` 有损往返登记、行号勘误。

## 实证（2026-08-30 实施时）

- 只读探针（生产文件，新代码）：**39/39 解析，bad=0**；consumers 全为
  `priority_analysis_consumer_v2`，status 全在合法集，delivery 全命中正则。
- 单测 18/18（含遮蔽场景与 v2 双代际）；全量见提交信息。

## 现状与实证

- 生产 status 文件 39 条全拒（0/39）：`PriorityJobStatus.from_dict`
  （priority_articles.py:904）严格键集 `set(data) != REQUIRED_STATUS_FIELDS`；
  且 `__post_init__`（:861-862）consumer/delivery 白名单
  `{("hermes","feishu"), ("fin","internal")}`——v2 消费者写
  `consumer=priority_analysis_consumer_v2`、`delivery_target="feishu:oc_…"`，
  两道都杀（BUGS 台账「双杀」实证）。
- 写方已停：Hermes 侧 v2 消费者 07-13 停写、链路随 rebaseline 退役；数据是
  7 月陈记录，但 durable 文件按规则 3/4 不动。
- fin-core 活消费方 = 本模块 `check_priority_*`（:1020 起）+ 
  `tests/cognition/test_priority_job_status.py`；老仓 report.py 健康段
  未随迁（留馆），不在本设计范围。
- `list_statuses`（:946）当前对任一坏行整体抛错 → 一条坏记录毒化全部。

## 设计（全部改动收敛在 fin_analyse/cognition/priority_articles.py + 测试）

1. **from_dict 键集放宽**：拒绝条件从「键集必须恰好等于必需 10 键」改为
   「必需 10 键必须齐全」；已知 v2 扩展字段（result_status/
   article_analysis_status/data_gaps/operation_advice_blocked/
   operation_advice_block_reason/portfolio_advice_status/
   result_classification，常量化 `_V2_EXTENSION_FIELDS`）允许存在并忽略；
   出现**未知**扩展字段仍拒绝该条（对新漂移 fail-closed）。
2. **consumer/delivery 白名单扩容**：追加
   `("priority_analysis_consumer_v2", "feishu:oc_*")`——delivery_target 放行
   `feishu:` 前缀 + 非空 id 段。**语义不变量**：`is_hermes_feishu` 仍只认
   `("hermes","feishu")`，v2 的 push_succeeded 不计为 Hermes 推送证据
   （`reports_feishu_push_succeeded` 判定零改动）。
3. **list_statuses 逐条隔离**：坏行跳过不抛、计数显形——新增
   `list_statuses_with_health() -> tuple[list[PriorityJobStatus], int]`；
   `list_statuses()` 保持原返回签名、委托新函数（962/1032 两个既有调用点
   零改动）。`check_priority_*` 结果里透出坏行计数（新字段，additive）。
4. **模块 docstring 补写侧契约**：v2 六/七字段与 delivery_target 格式登记
   为「已知历史写方形态」，将来任何新写方先登记再放行。
5. **不做**：不改 events/jobs 读侧（348/348 全过，本来宽容）；不动 39 条
   durable 数据（可读后无需归档，方向2 弃）；不改 VALID_JOB_STATUSES。

## 测试（对齐 tests/cognition/test_priority_job_status.py 既有风格）

1. v2 形态（7 扩展字段 + v2 consumer + feishu:oc target）39 条生产样本形态
   全部可解析——实现时先以只读探针跑生产文件 39/39 作实证。
2. 未知扩展字段 → 该条拒绝且只拒该条；文件其余条目照常返回、坏计数=1。
3. `reports_feishu_push_succeeded` 对 v2 条目恒 False（语义不变量钉死）。
4. 既有用例全绿 + 全量 pytest 绿。

## 备选与否决

- 方向2（历史数据降维改写 durable 文件）：需动生产数据 + v2 复活即再漂移。
- 方向3（status 段退役降级）：推送健康可见性归零，违背「修好读数」的目标。
- 无界宽容（任意键全收）：对未来新漂移失明，fail-closed 原则不让。
