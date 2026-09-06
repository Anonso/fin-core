# 市场实践回放线·事实层自动生长（cognition-replay-facts）· 短设计 v2.1

**日期**: 2026-09-06 · **状态**: 已施工（v2.1；合入后本稿按家规 5 删除，Git 即档案） · **owner 依据**: 会话拍板「快照肯定起作用；能自动化的就自动化（事实层自动生长）；方向/聚焦类按相对口径评，需钉窗+基准；先手动跑一批喂实模板」；动机与口径修正见同日 8 月 G 认知线验证会话。
**设计门**: glm·glm-5.3 替补（cmd 主 rc=3 fallback，排查交接见 fin-data/handoffs/20260906-cmd-reviewer-rc3-run-failure-handoff.md）· elapsed≈800s · 18 发现全采纳 · 台账 `~/.local/state/fin-analyse/design-gate/cognition-replay-facts-20260906/`。

**v2.1 施工勘误与细化**（对 v2 的小改，均记录在案）：
1. mapping 源勘误：黑话对照表=**仓内 `config/zsxq_jargon.json`**（owner 维护，schema `fin.zsxq-jargon/v1`，36 词条三档 confidence），非「owner 私有不入 git」——mapping.v1.json 导入其语义并钉 `jargon_source_sha256`，源漂移=拒绝重导入（防两源漂移）；
2. mapping schema 细化：entries 只存 term→{group_id, 语义, confidence}，codes/weights/role 收进 groups 表（组级取数一次，词条可复用组）；
3. nomination 用 **maturity（matured|open）与 proposed_relation 分离**表达「未到期」——化解 v2 里 not_matured 叙述与五值 relation 闭集的表述冲突；
4. 快照行形态定稿：组级一行、`by_code[code][date]→OHLCV`；
5. 触发面变更（owner 2026-09-06 拍板授权，解除原「不挂 scheduler」非目标）：事实层新增每日 23:00 systemd user timer（`fin-cognition-replay-daily.service/.timer` + `scripts/cognition_replay_daily.sh`）；`snapshot --require-today` 交易日门——当日无收盘行=skip rc0（节假日空转），**全组取数失败=rc1 硬错误**（网络/代理/源故障不得伪装成节假日静默丢日；守卫经单测实证 rc=1，代理×新浪兼容实测无碍——早前「代理抖动实测踩中」系会话误报，已更正）；nominate 在 skip 日同步跳过；正文吸收仍走 owner 扫批，timer 只写机器面。
6. 施工记录：`AKShareProvider.get_index_history` 增量方法（个股路径喂指数代码会静默拿错标的，指数必须走 `stock_zh_index_daily`）；8 月批次（20260904）对拍 fixture 11/11 PASS、幂等（剔 retrieved_at 内容等价）与护栏（空锚/无 pending 规则/缺基准拒绝）探针全绿、单测 30 绿（含 jargon 回归 21）。

## 1. 问题与目标

2026-09-06 的 8 月 G 认知线验证暴露三件事：
- **不可接续**：回放证据冻结在 8/19（标注文档切片 1-2），此后验证从零取数+查事件（~2h/次）；
- **映射悬空**：owner 黑话对照表存在但未接入任何流程，代号→标的映射靠会话语境推测；
- **口径混秤**：方向/聚焦类主张（「老登别碰」=聚焦主线）若按非主线资产绝对涨跌判会误判，需机会成本口径（主线篮子 vs 基准篮子相对差）+ 明示兑现窗。

目标三条：
- **事实层全自动**：行情事实快照按批生成（批=文件，整文件原子重写），下次验证从 manifest 链上次 as-of 接续；
- **裁决层机器提名、owner 扫批**：窗口到期自动算好到期摘要生成提案；不自动写回放线正文（归属权与裁决权留 owner）；
- **口径纪律**：主张类型驱动检查口径；方向类 spec 必须钉窗+基准（首立即须有锚，见 §2.2），否则 unscoreable；窗口到期即判，改窗=retire 旧 spec+立新 spec，manifest 按批留痕，不回头改。

非目标：不改写回放线正文/G 原文；不动 readmodel schema（replay_state 进 readmodel 列 v2，需使用证据）；不做 G 正误总分（分维度出数）；不自动执行「纠正 G 历史认知」；不挂 cron/scheduler（硬边界 2 + 家规 11；——**09-06 owner 拍板授权解除**：事实层每日 23:00 systemd user timer，见勘误 5；正文裁决层仍不自动化）。

## 2. 设计

### 2.1 存储契约（owner-only，Git 外）

`$STATE/fin-analyse/cognition-replay-evidence/`（目录 0700/文件 0600，复用 `runtime/state_roots.py:62-102` 原语）。**批（batch）与文件一一对应；写=读旧→合并→临时文件→flock+os.replace 原子替换（owner_only_snapshot.py:87 先例，max_bytes 调大），无 append-only。**

| 文件 | writer | 内容与字段（内联钉死） |
| --- | --- | --- |
| `mapping.v1.json` | 导入脚本（源=owner 对照表） | `{version, source_note, entries: {代号/别名 → {codes[], role: mainline\|benchmark\|reference, group, confidence: high\|mid\|inferred}}}`；confidence 三档随提名注记透出、不做门控 |
| `specs.v1.json` | owner 终审 | `{version, specs: [{unit_id, claim_type, caliber, window{start, end\|maturity_rule}, start_anchor: 时间语义索引行引用, mainline_group, benchmark_group, status: active\|unscoreable\|retired}]}` |
| `snapshot-<batch>.jsonl` | snapshot CLI | 每批一文件；行=`{batch, as_of, group, role, codes[], weights, series[{date, raw, qfq, adjustment_basis}], source, retrieved_at, note?}`；重跑=整文件原子覆盖，幂等判**内容等价**（剔 retrieved_at） |
| `nominations-<batch>.json` | nominate CLI | `{batch, as_of, nominations: [{unit_id, spec_snapshot, members_snapshot{codes,weights,confidence}, computed, proposed_relation, evidence_refs}]}`——成员集与权重强制内嵌，窗内映射变更=拒绝或双报 |
| `manifest-<batch>.json` | 两 CLI 共同 writer | `{batch, as_of, artifacts: [{file, sha256, bytes, source, retrieved_at}], specs_sha256, mapping_sha256}`——specs/mapping 指纹随批留档（retire/改口径留痕载体）；manifest 不自哈希（显式豁免） |

### 2.2 检查规格与判定规则

claim_type → caliber（类型自标注文档认知模式闭集派生）：

| claim_type（来源模式） | caliber | 判定 |
| --- | --- | --- |
| current_observation | same_day | **PIT 规则**：只引用发文时点之前最近已收盘交易日的 raw 值；非交易日发文取其前一收盘日；执行位=nominate 校验器（same_day 行不得引用发文后的快照行） |
| forecast·绝对断言（如「业绩暴雷」） | window_event | 窗口内核对事件/数据清单（如财报披露）；未到期=not_matured |
| forecast/structural·方向/聚焦类 | relative_spread | 主线组 vs 基准组累计收益差；**raw 与 qfq 双口径：相对差用 qfq，绝对点位锚用 raw**；整窗单批重取，禁跨批拼接数值 |
| action_layer | no_check | 纪律表述，不判 |
| structural·模型/意图/比例 | external_facts | 不可机判；predicate 清单待 owner |
| mixed_published_report | excluded | 非 G 口述，不入规格 |

proposed_relation **闭集=readmodel:57 机器五值**：`supports / partially_supports / diverges / unknown / no_evidence`（no_evidence=到期但缺数）。成熟判定：窗口末日为节假日→顺延至次一交易日；数据源延迟（窗口末日行缺失）→ 记 unknown 并在补批后重算，不猜测。

护栏三条：
- **start 锚**：window.start 必须引用标注文档「时间语义索引」已声明窗口的行并交叉校验；无声明→unscoreable（防首立起点 cherry-pick）；
- **成员稳定**：nomination 内嵌成员集+权重；窗内 mapping 变更→拒绝计算或新旧双报；
- **公司事件最小策略**：窗末停牌→取最近收盘+note；起点无价（窗内新股）→剔除+note；退市同停牌；提名双报。

### 2.3 新入口（两个手动 CLI，唯一新代码面）

- `scripts/cognition_replay_snapshot.py --as-of YYYYMMDD [--dry-run|--apply]`：默认 dry-run；读 mapping/specs → **经 `fin_analyse/market` 既有 provider 层（AKShareProvider）取数**，环境 proxy 默认透传，`--no-proxy` 仅显式运行旋钮（仓内实证两种端点行为分异：sina 直连可达、direct_arm 直连挂起——不预建按端点机制）；缺数 typed gap 落 manifest。
- `scripts/cognition_replay_nominate.py --as-of YYYYMMDD [--dry-run|--apply]`：扫 specs → 窗口到期者按口径计算 → 写 nominations。同日重跑=原子覆盖；同 unit 双 active specs 由校验器拒绝；跨日以最新 as-of 提名为权威、旧档留痕。
- 日志纪律：stdout=数据契约输出；诊断进 stderr；**映射内容（黑话代号）禁入 stdout/日志**。
- 触发：验证批次时手动调用；rebuild 双触发不扩。

### 2.4 与既有面的关系

- 回放线正文（标注文档）：本设计零写入；owner 按 nominations 扫批后手动追加（或并入增量生长制扫批流程）。**owner 追加正文会经既有双触发在下次 ingest 重建 readmodel——既有语义，非本设计代码面改动。**
- specs.status 与标注文档窗口状态表（:866）双状态面分工：specs.status=机器面权威（裁决前）；标注表=owner 裁决后文本面；扫批动作负责对齐两者。
- readmodel / 问询链 / finqa / read_g_context：零改动。
- 对照表源文件：owner 私有不入 git（家规 3）；evidence 副本只含标的代码/角色/组/confidence。
- 既存悬挂事实（非本设计义务，报备）：标注文档 :140-142 引用的切片 receipt 目录已按删除协议清入备份 tar，路径悬挂待 owner 处置。

## 3. 全场景覆盖表

| # | 场景 | v1 处理 |
| --- | --- | --- |
| 1 | 8 月批次接续验证（本设计动因） | snapshot(20260820 批)+nominate → owner 扫批追加回放线 |
| 2 | 以后每批验证 | snapshot 从 manifest 链上次 as-of 续跑；**数值一律整窗单批重取**，跨批只传 as-of 指针不拼数字 |
| 3 | 事件 predicate（FCC/磋商/仓位等） | 快照记 checklist 状态+来源 URL+时点；检索由验证会话执行 |
| 4 | 长期 regime 单元（无窗） | unscoreable（§2.2 start 锚护栏同此） |
| 5 | 数据源故障 | typed gap + manifest 落账；缺数到期单元=no_evidence；下批补取后重算（raw 锚定，不可倒灌规则见 §2.2） |

## 4. 施工五步

0. **验收 fixture 冻结**（评审 P1-4）：黄金值+篮子成员+方法+显式覆盖单元集全部转录在案（见 §5），fixture 即对拍预期值的唯一载体；
1. 四份 schema + 闭集校验器（mapping/specs/snapshot/nomination，fail-closed；同 unit 双 active 拒绝）；
2. snapshot CLI（provider 层取数/内容等价幂等/manifest 按批留档/权限/dry-run 默认）；
3. nominate 生成器（到期判定+三口径计算+start 锚交叉校验+成员内嵌）；
4. 8 月批次 dry-run 对拍 + 回放线 owner 追加流程说明一段（不改产品）。

依赖：owner 提供黑话对照表源文件或指认路径（施工步 1 前）。

## 5. 验收探针（fixture 化）

- **对拍**（覆盖单元集显式声明：CU-0811-02 / CU-0825-01 / CU-0828-02 点位类 + CU-0827-01/02 方向类；same_day 类发文日 ≤8/19 早于首快照，**显式排除**，不做静默缩水）：
  - CU-0811-02 蹦蹦床：8/12–9/04 共 18 交易日，收盘在 3900–4000 区间 15 日；越界 3 日（8/19 收 3894.42 / 8/24 收 3882.01 / 8/25 收 3889.45）；区间上沿未破（最高收盘 3990.30）；
  - CU-0825-01 托而不举：批内最低 3850.86（8/25）、3855.35（8/24），触及 3860 下沿后未收于 3850 下方；
  - CU-0828-02 保月线：8/31 收 3986.30（+0.86%），8 月月线 raw 收 3986.30 vs 7 月收 3832.26（+4.02%）；
  - CU-0827 方向类：8/27→9/04 科创50 −6.86%（1693.48→1577.36）vs 中证银行 +3.51%（7338.47→7596.30），相对差 ≈ −10.4pct（容差 ±0.1pct）；
- **幂等**：同批重跑内容等价（剔 retrieved_at；qfq 基准位移由整批重取吸收，不做 byte 级断言）；
- **权限**：目录 0700/文件 0600 stat 抽查；根缺失/权限异常 fail-closed；
- **护栏**：无 start 锚的方向类、无窗方向类提名被拒并落账（unscoreable）；到期缺数=no_evidence 槽位。

## 6. 开工四句话

- **改哪些文件**：新增 `scripts/cognition_replay_snapshot.py`、`scripts/cognition_replay_nominate.py` 与 `$STATE` 下 evidence 目录（数据）；本设计稿合入后删（Git 即档案）；合入时 GLOSSARY 补「回放线（市场实践）」词条。
- **影响哪个入口**：零生产入口变更——两个新 CLI 仅验证会话手动调用。
- **怎么验证**：§5 fixture 化探针 + 8 月批次对拍。
- **为什么不是别的做法**：自动写回放线被 owner 否决（归属权+不可证伪风险）；扩 readmodel schema 无使用证据（家规 11）；复用 rebuild 触发会把证据层耦合进标注指纹。
