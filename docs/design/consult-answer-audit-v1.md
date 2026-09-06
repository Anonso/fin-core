# 问询答案事后审计 v1（consult-answer-audit）· 短设计

> 日期：2026-09-06 · 状态：设计冻结，动代码前走设计门（核心件）
> 核心度：核心——挂在唯一问询入口 `scripts/finqa_chain.py` 的答案路径上（门面路径，
> 审计门 R2 命中），动代码前按家规走设计门 packet（固定四问）。
> 来源：owner「预期管理/交易纪律优化」设计会话（2026-09-06），配套稿
> [behavior-probe-set-v1](behavior-probe-set-v1.md)（其 forbid 锚与本稿 F1 单源）。

## 开工四句话（家规 8）

- **改哪些文件**：新增 `fin_analyse/consultation/answer_audit.py`（纯函数 checker）+
  `config/answer_audit.yaml`（封闭词表/锚，会变项进配置）+ `scripts/finqa_chain.py`
  挂一个 fail-open 后处理钩子（≤10 行）+ 单测；`internal-module-catalog.md`
  consultation 节同步（其规则 9）。
- **影响哪个入口**：finqa **无头**路径 rc=0 之后的只读旁路——stdout 字节不变、
  rc 语义不变、交互路径与 runtime/scheduled 车道不碰。
- **怎么验证**：answer_audit 单测（正/负/边界锚）；launcher 冒烟（真腿一题：audit.tsv
  落行且 stdout 与挂钩前逐字节一致）；persona_regression 探针产物离线复跑同一
  checker 得同值。
- **为什么不是别的做法**：不挂 runtime finalize——那只覆盖 scheduled 车道，盖不住
  无头问询；不挂 MCP server——只见工具调用不见答案文本；不建拦截/定向重答——那是
  约束层+审核机器复活（D-016 废止面），家规 11 无使用证据不许；checker 不内联进
  launcher——launcher 保持 config-driven 薄层，只调纯函数+落一行 tsv。

## 摸底事实（2026-09-06 核实）

- 生产问询 = 有头交互（`finqa -i` 走 os.execvpe 进程替换，launcher 物理上无法后处理）；
  无头链流量 ≈ 测试与探针（finqa_nodes.yaml owner 定调）。⇒ **v1 审计面 = 无头
  finqa 答案**——恰好覆盖探针/测试流量，与 behavior-probe-set 组成「测流量的
  遵守率度量」。有头交互覆盖留 v2 扩展位（session 归档异步扫描），按家规 11 等
  真实使用证据另立短设计，本稿不实施。
- 无头答案 stdout 直透（`_run_node` 直接写 sys.stdout），此前无任何 FIN 侧答案文本
  审计；`scripts/fin_tool_usage_audit.py` 已确立「只读审计脚本」先例，但只见工具
  调用不见答案。
- runtime finalize 车道（codex_runtime→products 表→presentation）只在 scheduled/
  操作侧生效，与本入口解耦——两边互不依赖。

## 设计

- `answer_audit.audit_answer(text, checks) -> tuple[Finding, ...]`；
  `Finding = (check_id, severity, count, matched_tokens)`。零 IO 纯函数；
  词表/锚从 `config/answer_audit.yaml` 读（新增内部词=改配置不动代码）。
- **v1 唯一实现家族 F1 泄漏词表**（BUG-053 实锤：CU-编号/首行元叙述已在
  persona_regression 预期 FAIL 跟踪位）：内部工具名、gap code 样本、内部框架词
  （「主线环境刻度 v」「G 层」等，即 BUG-053 禁入清单）、首行元叙述锚。
  severity=leak。预留注册位（不实现，各注明前置）：F2 trace 交叉（动作方向题 ∧
  capability_trace 无 portfolio 读——BUG-024 门槛，依赖 trace 读侧）；F3 交易日历
  （BUG-030，依赖日历 artifact）；F4 算术对账（BUG-052，依赖动作合同派生字段）。
- **launcher 钩子**：`_run_node` rc=0 且 stdout 非空后 `try: audit → append
  audit.tsv; except: stderr 一行注记`——绝不影响 rc/答案。audit.tsv 与 fallback.tsv
  同目录同模式（O_APPEND 0600，单行原子，并发安全）：
  `ts/call_id/node/check/severity/count/tokens`。
- **隐私（家规 3）**：不落问题文本、不落答案原文；matched token 来自封闭词表
  （内部词本身非用户数据）。
- **失败语义/时序/幂等**：审计自身永远 fail-open；findings 无 reader、无 schema
  迁移、不进公共 payload——纯旁路落账，人工按期裁决（命中可走 BUGS 立案流程）。
- **不退化论证（固定四问第 4 问）**：答案字节不变、延迟 ms 级 regex、rc 不变、
  命中≠处置（无自动降级/拦截）——公共入口同题不弱于直接 Agent。

## 引用闭包与施工顺序

0 设计门 packet（本稿+finqa_chain 钩子片段+词表初版；固定四问）→
1 checker+config+单测 → 2 launcher 钩子+冒烟 → 3 module catalog 同步 + 记账。
引用闭包：`finqa_chain.py`（import——launcher 本就跑在 `$FINCORE/.venv/bin/python`
上，包导入安全——+ `_run_node` 尾部一处）；`tests/` 新增 answer_audit 用例；
`persona_regression.sh` 不改（其 q*.md 产物可被本 checker 离线复用）；audit.tsv 为
新文件、无既有读方。

## 风险

- 词表误报（合法术语撞词）：severity 仅落账不处置；撞词与真实泄漏的分辨权在人，
  误报驱动调表（配置迭代）。
- 交互盲区：v1 不覆盖有头生产主路径——价值定位是「无头/探针流量的遵守率度量」，
  不是生产质量闸；owner 要交互覆盖时 v2 另立设计，不预建。
- launcher 变薄违例：钩子 ≤10 行、词表在配置、checker 在包内单测覆盖。
