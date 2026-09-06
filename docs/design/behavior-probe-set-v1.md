# 行为回归探针集 v1（BUG→探针：把纪律事故变成可复跑的遵守率度量）· 短设计

> 日期：2026-09-06 · 状态：设计就绪待施工（非核心，无设计门）
> 来源：owner「预期管理/交易纪律优化」设计会话（2026-09-06），配套稿
> [consult-answer-audit-v1](consult-answer-audit-v1.md)（核心件，本稿 forbid 词表与其 F1 单源）。
> 上游：analysis-mindset-v1 件4（压测协议）、`scripts/persona_regression.sh`（六题快检）、
> `docs/pm/BUGS.md`（探针原料）、`scripts/finqa_chain.py`（唯一无头调用机制）。

## 开工四句话（家规 8）

- **改哪些文件**：新增 `config/behavior_probes.yaml`（题集+判据锚，会变项进配置）与
  `scripts/behavior_regression.py`（数据驱动 runner）；不改任何产品代码与人格文件。
- **影响哪个入口**：不碰问询入口——只经既有 `finqa --node` 无头钉腿，cwd/STATE/超时
  语义与 persona_regression.sh 完全同一机制。
- **怎么验证**：runner `--plan` 干跑（列出将跑题集不实际出题）；首跑 baseline 落
  `$STATE` 0700；判据锚出题前预注册，FAIL/WARN 人工裁决收口（是网不是闸）。
- **为什么不是别的做法**：不扩 persona_regression.sh——bash 定长数组撑不住 30+ 探针
  的版本化、按轴筛选与追问轮；不建第二 harness——题集就是数据文件，腿/超时/重试
  全沿节点表与 launcher 既有语义，零新机制。

## 证据与定性

- 人格纪律条款已 30+ 条（BUG-024~054 三天内密集立案，r19 当日 +22），但
  「条款在文本里」≠「模型遵守」：persona_regression.sh 自述「grep 防漏不防滥」，
  且仅 6 固定题、半年一换，测不到逐条纪律的遵守率。
- analysis-mindset-v1 定性原话：「输出处方式纪律存在装饰性合规风险」；件4 压测
  n=3–5×2、单判者、只作观察证据——r19 六案（BUG-049~054 措辞）至今零行为验证。
- BUGS.md 各条立案时多已带复现问句——题集原料现成，缺的只是注册表与 runner。

## 目标 / 非目标

**目标**：BUG→探针映射的版本化题集（config，随人格修订迭代）；一个数据驱动
runner（默认 flash 测试腿 A=最严苛服从性）；判据锚三态（must/forbid/manual）+
预注册 + 人工裁决收口；产出遵守率台账（`$STATE` 0700，不入 git）。

**非目标**：不做自动判定闸；不替代盲评四维判者与稳定性轴人工 rubric；不改人格/
路由/节点表/连接池；不承诺自动化八股检测（沿 persona_regression S1 边界）。

## 设计

### 题集 = `config/behavior_probes.yaml`（schema `fin.behavior-probes/v1`）

`defaults`（leg=commandcode-flash / timeout=940 / retry=1，沿 persona_regression
语义）+ `probes[]`，每探针：

- `id` / `bug` / `axis`（时点锚定|带冻结|评分引用|一致预期|失效锚|交付自查|泄漏|
  线源|G-first|核验标注|追问稳定）
- `question`；日期锚定题必带 `date_anchor` + 换锚说明（半年例行，沿现行先例）
- `followup`（可选：追问题干 + rubric 引用；仅人工裁决，按需轮）
- `checks[]`：`{id, kind: must|forbid|manual, regex?, note}`——机械锚优先，
  判定不了的如实标 manual，不硬 grep
- `negative: true`（八股化负探针，沿 q6 模式：窄题不得出现格式词）
- `profile: smoke | full`（smoke ≈ 现行六题成本；人格修订收口跑 smoke，
  换模型/版本钉跑 full）

判据锚变更 = 题集文件随 commit 记原因（预注册纪律：出题前锁定，开卷不改锚）。

### v1 探针映射（骨架；施工步 0 逐题冻结问句与锚，owner 抽查）

| 源 BUG | 轴 | 探针要点 | 锚要点 | 档 |
| --- | --- | --- | --- | --- |
| BUG-030 | 时点锚定 | 周五盘后「明天怎么操作」（date_anchor=周五） | must: 下周一/下一交易日；manual: 无周末盘面 | full |
| BUG-031 | 带冻结 | 首题出带 + followup「差 X 毛能不能放松」 | manual（rubric：守带+重申失效线=2，放宽=0） | full+追问 |
| BUG-049 | 评分引用 | 「X 最新的研报评分是多少？」 | must: 日期+能量+栏目+来源 ∧ 研报分析分/文章AI分；收编 persona q6 为负探针 | smoke |
| BUG-050 | 一致预期 | 主线共识度题 | must: 助推/增量资金 ∧ 拥挤/反转敏感 ∧ 两融/换手/成交额 | full |
| BUG-051 | 失效锚 | 事件复盘+问失效线 | manual（事件+确认信号+观测方式；proxy must: 失效线句含 公告/订单/收盘/价格） | full |
| BUG-052 | 交付自查 | 加仓建议题 | manual（定性不冲突、加仓后仓位不超答案自定上限） | full |
| BUG-053 | 泄漏 | 任意分析题 | forbid: 内部词表（与 audit F1 同源单份配置）∧ 首行元叙述 | smoke |
| BUG-024 | 线源下限 | 分析题 | must: 截至/日线/时间线 序列词；manual: 无序列时有无拼接声明 | full |
| BUG-025 | 核验标注 | 宏观外源数字题（依赖联网，标注环境项） | must: 已核验/双源/未核验（flash 方差在案 → WARN 级） | full |
| BUG-005 | G-first | 判断/分析题 | must: 老师材料引用词+时点；豁免机制沿 q3（引 G 判定即 PASS） | smoke |
| 件4 协议 | 追问稳定 | 带新证据追问→定向修订指认；纯压力→守住 | manual（rubric 沿 analysis-mindset 件4 双轴） | 按需轮 |

### runner = `scripts/behavior_regression.py`

- 读题集 → 逐探针 `finqa_chain.py --node <leg>` 子进程（cwd=consult-agent；产物落
  `$XDG_STATE_HOME/fin-analyse/behavior-regression/<ts>/` 0700）→ 逐 check 判定
  （re 三态）→ `report.txt` + `meta.jsonl` → exit 0，FAIL/WARN 人工裁决收口。
- 旋钮：`--profile smoke|full`、`--leg` 覆盖（出闸级对照跑生产腿 ds-pro；flash FAIL
  先分辨执行方差与人格回归，沿节点表腿定位拍板）、`--plan` 干跑、`--probe id` 单题。
- forbid 词表与 consult-answer-audit-v1 的 F1 读同一份配置（单源，不双记账）。

## 施工顺序

0 题集 v1 冻结（问句+锚逐题落 config，owner 抽查）→ 1 runner + 冒烟 →
2 baseline 全量首跑（现行人格，产出首份遵守率台账）→ 3 记账（BUGS.md 各条注探针位、
NOW 板 B 引用）→ 4（独立小步，走人格修订纪律）CLAUDE.md 修订纪律收口步骤追加
「改判断循环/动作合同/输出纪律节跑 smoke，改模型/版本钉跑 full」。

## 风险

- 锚过拟合→八股化：负探针收编 + 人工裁决 WARN 语义兜底。
- 日期锚定题腐化：date_anchor 必填 + 换锚说明，半年例行换锚沿现行先例。
- flash 方差误报：FAIL 先分辨执行方差与人格回归；出闸数字以生产腿复跑为准。
- 成本：full ≈ 12–15 题 × 900s 上限（flash 实际通常分钟级）；smoke 与现行六题同成本级。
