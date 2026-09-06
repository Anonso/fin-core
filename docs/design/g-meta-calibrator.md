# G 元认知调节器（g-meta-calibrator）· 短设计 v1

**日期**: 2026-09-06 · **状态**: 设计稿 v2（设计门 cmd·ds-pro 12 发现全采纳已落稿；adjudication 见台账） · **owner 依据**: 拍板「这个二阶认知很有用，得想办法用起来在问询里，可以是 G 认知的调节器」+ UL 收录「元认知层/元认知调节器」（同日）。
**设计门**: cmd·deepseek-v4-pro 正常完成（无 fallback）· 12 发现（P2×8/P3×4）全采纳 · 台账 `~/.local/state/fin-analyse/design-gate/g-meta-calibrator-20260906/`。
**家规 11 举证**: 用户要求（本条）+ 已发生代价（2026-06 末若 agent 携「G 默认为对」先验读慢牛/算力上游叙事，7 月科创50 -25.9% 即为实际下行；回放线 2026-09-06 批次账面在案）。

## 1. 问题与目标

问询链现有 G 注入是**裸的**：`read_g_context`（HARD RULE：分析/观点类问题先读）把 G 主线交给 agent，但 agent 不知道「该老师哪类判断历史上可信」。元认知层（回放线）已存在，但消费端只有 owner——二阶认知没有进入问询。

目标：agent 在问询中消费元认知层，作为 G 主线的**采纳强度调节器**——按判断类型调权，不产生一阶方向，不改 G 原文，不阻塞问询。

非目标：不做数值权重（0.3x 类假精度）；不做 G 总分；不改 readmodel schema（per-unit replay_state 仍列 v2 待使用证据）；不改 persona 文件（机制进代码，措辞进 owner 终审的 profile）；不自动更新画像（更新=验证批次 owner 扫批，与回放线正文同闸）。

## 2. 设计

### 2.1 画像 artifact（唯一的 durable 新增）

`$STATE/fin-analyse/cognition-replay-evidence/meta-profile.v1.json`（0700/0600，复用既有 evidence 根与写原语）：

```
{schema_version: fin.g-meta-profile/v1, as_of, version, note,
 dimensions: [{class: point_time|direction|narrative|observation|discipline,
               reliability: high|mid|low|unverifiable,
               basis: <回放线节/批次引用>, note: <一句口径>}]，
 injection_template: <注入文案，含边界条款>}
```

- **class 闭集**五值对齐回放线 claim_type 归并（点位/时点、方向外推、资金叙事、当日观察、纪律行动）；
- **独立 writer 声明**：meta-profile 独立文件、独立更新时机，**不参与批 manifest 与 snapshot 内容等价键**；留痕=profile 内 version+as_of+basis 引用（回放线 dated 节为文本面账本）；
- **class↔claim_type 归并映射**（loader 校验依据）：observation←current_observation；point_time←forecast/scenario·(range_check, window_event)；direction←forecast/structural_analysis·relative_spread 及 structural_analysis 方向主张；narrative←structural_analysis/historical_analysis·external_facts（资金/意图）；discipline←action_layer；mixed_published_report 不入画像。**basis 引用语法**：`batch:<YYYYMMDD>#<check_id>` 或 `section:<回放线节标题>`，loader 校验非空与格式前缀；
- **max_age_days** 进 profile 字段（缺省 90，会变的选择不硬编码）；
- **写入权**：只有验证批次扫批后的 owner 终审（会话代笔、owner 确认）；
- **无 profile / as_of 超 90 天**：不注入，行为退化为现状（fail-open，绝不阻塞问询）。

### 2.2 注入 seam（v1 唯一：`read_g_context`）

装配点=**provider 层** `read_g_context` 方法（`production_capability_provider.py`；server.py handler 是薄壳不动）：value 组装后按 `meta_profile_path`（wiring 传入，state 根先例同款）加载块，存在则 `value["meta_calibration"]={as_of, version, dimensions[], boundary}`。loader 独立模块 `meta_calibration.py`（家规 6，只 import 既有原语）。

**注入文案边界（防层级错误，硬约束）**——模板必须含以下五要素，loader 逐一存在性检查，缺一不注入：
1. 「仅用于调节你对 G 内容的采纳强度与表述确定性，不得作为市场方向依据」；
2. 「低可靠度≠反向信号，不改变 G 原文的一阶含义」（防反着做）；
3. 「不得在回答中向用户引述可靠度分级表本身」；
4. 「unverifiable 维度的内容不得作为事实转述」；
5. 「本画像为工作假说级校准记录，基于有限验证批次，随新批次修订（数据截至 {as_of}）」。

**v2 留位**：runtime_context 注入 seam（FIN 生产问询的深缝）、per-unit replay_state——均待 v1 使用证据。

### 2.3 画像初稿（owner 终审对象，依据=回放线 2026-09-06 账面）

| class | reliability | basis |
| --- | --- | --- |
| observation（当日观察） | high | batch:20260904#CHK-0811-02-range；section:8/20 窗口表 CU-0811-04 行 |
| point_time（点位/时点） | high | batch:20260904#CHK-0811-02-range；batch:20260904#CHK-0825-01-floor；batch:20260904#CHK-0828-02-monthly |
| discipline（纪律行动） | mid | 十六字/不追涨，无执行记录可验，无失败记录 |
| direction（方向外推） | low | section:8/20 窗口表 CU-0624-01 行（diverges）；batch:20260904#CHK-0827-01-spread；batch:20260904#CHK-0827-02-spread（滚动值偏负、窗口未到期） |
| narrative（资金叙事） | unverifiable | section:8/20 窗口表 CU-0819-04 行（external-facts-unverified） |

## 3. 全场景覆盖表

| # | 场景 | 处理 |
| --- | --- | --- |
| 1 | 正常问询（分析/观点类） | read_g_context 带 meta_calibration 块 |
| 2 | profile 缺失/损坏/过期 | 不注入，现状行为（fail-open） |
| 3 | 纯事实查询/用户 opt-out | 本就不读 G，不涉及 |
| 4 | 验证批次后画像更新 | owner 扫批 → 更新 profile + manifest 留痕 |
| 5 | 画像与 G 内容冲突（如 G 新帖与 direction=low） | 不拦截不降权内容本身；调权发生在 agent 表述层（模板管措辞），机制面不碰 G 主线 |

## 4. 施工三步

1. profile schema/校验器 + loader（复用 cognition_replay_lib 原语，fail-open 读）；
2. server.py handler 注入块 + wiring 传路径 + 边界条款检查；
3. 测试（注入/缺档退化/边界缺失不注入/过期不注入）+ 实弹探针（问询真实调用读块）。

## 5. 验收探针

- 注入：构造 profile → read_g_context 返回含块且含边界条款；
- 退化：无 profile/损坏 JSON/目录不可创建 → 返回无块、零报错，且 value 与现状**逐字节等价**（断言）；
- 过期：as_of>90 天 → 不注入；
- 文案：模板缺五要素任一 → 不注入（loader 拒绝）；
- 不弱于对照：注入成功探针 v1 只证机制；「不弱于直接 Agent」对照挂靠下一次问询验收（盲评/双腿既有机制）；
- 画像初稿经 owner 确认后才写入真实 state（施工只交机制+draft 文件在 `docs/design/` 附件，不预写 state）。

## 6. 开工四句话

- **改哪些文件**：新增 `guo_teacher_research/meta_calibration.py`；`production_capability_provider.py`（read_g_context 装配块+构造参数）、`read_capabilities/wiring.py`（传路径）、新增测试、GLOSSARY 一行；设计稿合入后删。
- **影响哪个入口**：`read_g_context` 返回结构新增可选块——唯一生产入口变更；无 profile/加载失败时逐字节等价于现状（含目录创建失败面）。
- **怎么验证**：§5 探针 + 既有 read_capabilities 测试回归。
- **为什么不是别的做法**：改 persona 文件违反机制/措辞分离与人格治理；动 runtime_context 深缝是无使用证据的重手术（v2）；数值加权是假精度。
