# 主线4：Daily gap 记账哑两刀（v2·裁决后）

> 规则 5 短设计（核心链路：Daily 简报产品入口）。合入后删除，Git 即归档。
> 触发：B1 归因 L3（打分表.md:74-78）+ D-026 + owner 2026-08-30 拍板。

## 设计门裁决记录（2026-08-30）

评审者 `codex_open.sh --sandbox read-only`（deepseek-v4-pro·max），elapsed 606s，
发现 13 条（P0×3/P1×2/P2×8），采纳 13/13。v1 前提被两条 P0 打穿：

- **发现1/5（P0/P2）**：`is_verified_daily_workspace_advisory` 对 `degraded is True`
  直接判非 advisory（contracts.py:150）；系统已有 `deterministic-degraded-v1`
  降级通道（runner `_degraded_product_payload`：degraded=True、
  consultation_status=partial、consultation_product=None，契约
  `is_explicit_daily_workspace_availability_notice` 认可的唯一非 advisory 通知）。
  在 l1-direct-v1 产品上加 degraded=True = 与通知类争语义，双重拒收。
- **发现2（P0）**：runner `_TimingBoundGenerator` 无条件 `**generated, "degraded": False`
  ——生产路径根本不会出现 degraded=True 的 L1 产品。
- **发现4（P2）**：prior 基线缺失已有 `daily_workspace_same_day_parent_*` 码族经
  `context.data_gaps` 记账，新增码=双轨重复。
- **发现11（P2）**：any-gap 触发降级过宽（g_reference 断也压成工单）。
- 其余（6/7/9/12/13）：additive 半边成立、幂等同 key 漂移可接受、on_demand 语义
  一致性、测试断言修正、二分只覆盖 repr 形态——均落稿或注明。

## v2 设计：材料断供 = 既有 unavailable 语义，零新机制

全部改动收敛在 `daily_workspace_generator.py` 单文件 + 测试：

1. **gap 细分**：`_material_gaps` 两态——值为 str 且含 `_OBJECT_REPR_MARK` →
   `l1_material_<key>_unrenderable`（在料序列化坏）；否则 → 现码
   `l1_material_<key>_unavailable`（收紧为缺料语义，码名不变）。局限（发现13）：
   序列化异常在 provider 层已被捕获落 None → 记缺料，repr 泄漏是唯一可判形态，
   如实接受并注明。
2. **降级触发**：`generate()` 中材料装配后（仅 `material_provider is not None` 时），
   `portfolio` 与 `market_overview` **双双重伤**（任一两态）→
   `raise DailyWorkspaceGenerationUnavailableError((*材料 gap 码,))`，不调 LLM。
   既有处理器（daily_workspace.py:410/568）转 `deterministic-degraded-v1` 通知，
   gap 码原样进 `data_gaps`/`unknowns` 并投递（delivery 已有降级投递测试）。
   材料死 ≡ backend 不可用，全链语义统一。
3. **单面伤**（一断一活）：正常产品 + gap 码照记；模板已排除断料、unknowns 已流。
4. **不加** `degraded` 字段、不动 runner/contracts/delivery/presentation、
   不新增 prior 码（既有码族覆盖）、on_demand 同语义（材料死=诚实 unavailable）。

## 对 B1 规格原文的三点偏差（owner 可见）

- 触发收紧：any-gap → 双核心面（portfolio+overview）皆断（发现11）。
- 降级产物：确定性通知（既有通道）而非 LLM 渲染模板——A 班「排查工单」形态
  正是该通道语义；通知的缺口事实+未知清单由 gap 码投递链渲染。
- 基线指针不入通知（store 中上一班产品仍可查，指针可推导）；若 owner 要通知
  文案带指针，另起 delivery 渲染小改。

## 测试（tests/consultation/test_daily_workspace.py 邻域，密闭 fake）

1. 双面断（portfolio=None + overview=对象repr）→ `DailyWorkspaceGenerationUnavailableError`，
   codes 含 `l1_material_portfolio_unavailable`+
   `l1_material_market_overview_unrenderable`；backend_factory 伪件零调用。
2. 单面断（overview 死、portfolio 活）→ 正常产品，gaps 含 overview 码，无 degraded 字段。
3. repr → unrenderable、None/空 → unavailable 两态判定。
4. `material_provider=None` 不触发（对齐现 gap 记账的既有守卫）。
5. 既有全部用例不动仍绿 + 全量 pytest 绿。

## 备选与否决

- v1 原案（L1 产品加 degraded=True）：门评证伪，见裁决记录。
- 给 L1 加检索补偿：违背脱钩设计（归因 L4 已明示）。
- any-gap 触发：过宽（发现11）。
