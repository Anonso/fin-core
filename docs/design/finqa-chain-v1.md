# finqa-chain v1：机器问询链（无头 · 节点配置化 · 自动 fallback）

> 状态：设计稿 v2（设计门后修订）。规则 5 判据：公共入口语义 +
> 跨 harness 接线 → 核心处理；合入后本文件删除，Git 即归档。
> 设计门：cmd·deepseek-v4-pro·max（无 fallback），elapsed ≈250s，
> P1×4/P2×6/P3×2 → 采纳 11、不采纳 1；裁决记录见 §7；台账
> ~/.local/state/fin-analyse/design-gate/finqa-chain-v1-20260905/。
> owner 设计意图（2026-09-05 会话）：节点 = 现役三条无头腿（finqa-claude /
> finqa-codex / finqa-commandcode，不改名）；探活/fallback
> 目的 = 减少人工换节点；session 语义接受「可丢」；节点优先级/开关/增删全进
> 配置。参考形态：提取链（llm.yaml 节点表+声明序+enabled 语义）× 设计门
> launcher（precheck+fallback+横幅+tsv）。

## 1. 问题与立项证据

机器用问询（效果评估、诊断、盲评 runner、未来自动化）现在要自己知道「哪条腿
能用」：09-05 opencode-go 429 长期故障，人工三处换节点（owner 拍板 + 改
codex_routes.yaml enabled + bashrc 注休眠）——家规 11 所需的「已发生故障」
举证成立。现役三条无头腿（bashrc finqa-claude/-commandcode/-codex）零治理，
坏了靠人看。本设计给机器问询一条配置化的自动 fallback 链。

## 2. 设计（一个 yaml + 一个 launcher + bashrc 薄别名）

### 2.1 节点表 `config/finqa_nodes.yaml`（仓内，家规 6）

```yaml
schema_version: fin.finqa-nodes/v1
# 声明序 = 优先序（提取链语义）；每节点独立 enabled；新增/删除/排序/开关只改
# 本文件。harness 只有三种，起法钉在 launcher 里（§2.2）——新增同型节点
# （如 claude+其他模型）零代码。
nodes:
  - id: claude            # CC harness + glm-5.3（现 finqa-claude）
    harness: claude
    model: glm-5.3
    enabled: true
  - id: codex             # codex CLI + opencode-go（现 finqa-codex，暂关）
    harness: codex
    model: deepseek-v4-pro
    enabled: false         # 429 长期故障在案；恢复翻 true 即回第二位
  - id: commandcode       # Command Code + ds-pro（现 finqa-commandcode）
    harness: commandcode
    model: deepseek/deepseek-v4-pro
    enabled: true
```

- 不存秘钥：各 harness 认证维持现状（claude=CLAUDE_CODE_CONFIG_DIR /
  cmd=cmd login 账号会话 / opencode=auth.json + llm.env 环境键），yaml 只写
  引用语义，实值解析留在 launcher 的 harness 函数里。
- `model` 可省略 = harness 钉定值；同 harness 加节点只改 yaml。

### 2.2 launcher `scripts/finqa_chain.py`（Python，~150 行）

- **施工基线 = bashrc 三腿函数体逐字**（〔外审修正 Q1-P1〕含
  CLAUDE_CODE_CONFIG_DIR 环境链、cmd 的 --skip-onboarding/--no-auto-update/
  --effort max、codex 的 GLM/OPENCODE env 链与 CODEX_HOME——§2.1 yaml 只管
  选节点与顺序，起法参数以下列清单为简写提示、以附录 B 为权威，漏任何一项
  即破坏项目级隔离或推理深度钉定）：
  1. **precheck**（每 harness 一个函数，便宜检查：二进制存在 + 认证标记在位；
     不做网络 ping，失败不发）；
  2. **起腿**：cwd=~/fin-data/consult-agent（目录即身份），无头形态 =
     各腿 bashrc 函数体原样（claude `-p` + strict-mcp-config + config dir /
     cmd `-p` 全旗标 / `codex exec --sandbox read-only` + env 链），argv =
     调用方问题参数原样透传；
  3. **判定**：rc=0 且 stdout 非空 → 透传 stdout，退出 0；rc=0 但 stdout
     空 → 按失败处理（机器消费方拿空答案无用，换下一位）；其余 → stderr
     横幅 + tsv 落账 → 下一位；
- 全部失败 → **exit 78**，stderr 列各腿 rc/失败阶段（调用方自行裁决）；
- **横幅/元信息一律显式 stderr**（〔外审修正 Q1-P1〕codex_open.sh 先例横幅
  走 stdout 是已知缺陷，不继承）；
- **tsv schema**（〔外审修正 Q2-P2×2〕）：`$XDG_STATE_HOME/fin-analyse/
  finqa-chain/fallback.tsv`（0700 目录/0600 文件，族约定），行 =
  `ts \t event \t node \t rc \t stage \t detail`，event ∈ {fallback,
  success, exhausted}——**success 行入账**：既是家规 10 使用证据，也是
  provenance 数据源；
- **provenance 传递**（〔外审修正 Q4-P1〕）：每次调用生成 call_id，结束在
  stderr 输出一行元信息 `finqa-chain: served-by <node> call_id=<id> rc=0`
  （stdout 答案不掺；调用方重定向 stderr 即丢弃）；盲评/效果评估类按腿
  比对的消费方**必须** `--node` 钉腿，或对账 tsv success 行，禁止裸走链后
  按预期腿记账；
- `--node <id>`：单腿钉定（探针/对照/评估钉腿），跳过链；
- session 语义：**可丢 = 不 resume，非不留档**（〔外审修正 Q1-P3〕撤
  `--no-session`，与原函数逐字一致，会话文件留档可审计，跨调用零依赖）；
  fallback 换腿重发同参，丢的是上一腿会话（per-backend thread 本就不可跨腿）。
- 无熔断/TTL（见 §3；人频次现探现走，设计门口径）；timeout 每节点固定
  （900s，env 可覆盖）。

### 2.3 bashrc 收编（薄别名，函数体删除）

〔外审修正 Q1-P1/P2〕**只新增一个 bashrc 函数**：

```
finqa() { /home/ypk/fin-core/.venv/bin/python /home/ypk/fin-core/scripts/finqa_chain.py "$@"; }
```

- 三腿原函数（finqa-claude/finqa-commandcode/finqa-codex）**原样保留不动**
  ——「保留原函数」与「收编别名」二选一，裁决取保留（消解同名两立；人钉腿
  用原函数、机器钉腿用 `--node`，两者调用面等价）；`finqa-x` 暂关语义不变
  （链内对应 codex 节点 enabled:false）；
- **机器消费方一律直调 `scripts/finqa_chain.py`**（cron/subprocess 非交互
  shell 看不见 bashrc 函数；alias 另有 `"$@"` 空展开坑）——bashrc 函数只是
  人终端便利层；
- 交互式 `finqa-c` / `finqa-cmd` **不动**（人用交互会话不在本链范围）。

## 3. 契约与边界

- **无熔断的含义**：链不记录节点失败状态。熔断器（提取链在用）是「节点连挂
  N 次后开闸冷却一段时间，期间直接跳过、连试都不试」——省的是每次调用先撞
  一次失败的延迟，代价是要维护状态（存哪/多久过期/恢复后会不会被旧状态误
  跳过）。本链每次调用都从首位真试起：故障期间每次多付几秒失败延迟，换来
  零状态、节点恢复即刻回位、无旧状态误判。人/机器低频问询下这笔交换划算；
  将来真有高频消费方再按提取链形态加熔断，不预置。
- 对调用方：stdin/argv 问题进 → stdout 答案出，rc 语义 0/78；横幅只走
  stderr 不污染答案。
- **codex_routes.yaml 本期不动**：其现役消费方（效果评估追问轮）依赖
  thread resume，与本链「可丢」语义不同——两链并存是已知代价，合并等真实
  需求驱动（消费方迁移另立项）。
- 落账范围（〔外审修正 Q3-P2〕）：NOW/GLOSSARY 之外，`.claude/skills/
  switch-codex-open-provider/SKILL.md` 恢复流程补两链并存说明（问询链=
  finqa_nodes.yaml enabled；效果评估=codex_routes，各管各的），防操作面
  打架。
- llm.yaml（提取链）、read_market_snapshot 等工具面、MCP 配置零变化；
  本链只是「起哪条腿」的治理，不碰答案内容。
- 非 v1（明确不做）：TTY 交互模式、熔断/TTL、--resume 续问、节点健康度
  打分、并发探活。

## 4. 验收

0. 消费方迁移进验收（〔外审修正 Q3-P3〕指认具体标的）：state 台账盲评
   runner `~/.local/state/fin-analyse/consult-blind-eval-20260903/runner.sh`
   的 CC 腿调用处改调 `finqa_chain.py`（该腿无 resume 依赖，语义匹配），
   并按 §2.2 对账 tsv success 行或 `--node` 钉腿——链自第一天起有真实读方，
   防家规 10 空转。
1. 三腿实弹各一发（enabled 全 true 时链序 claude→codex→commandcode；
   现状 codex disabled，有效链序 claude→commandcode，恢复即自动回插第二位）。
2. fallback 演练：首位节点注入坏认证（临时 yaml）→ 自动落次位，stderr
   横幅 + tsv 行 + 答案仍出，rc=0。
3. 双挂演练：全部 enabled 节点坏认证 → exit 78 + 各腿失败列。
4. disabled 跳过：codex enabled:false 时链不含它，precheck 不触发。
5. `--node` 单腿：三腿各一发与旧 bashrc 函数行为一致（同 cwd/同 MCP/同模型）。
6. 家规 7 分级：以上是「测试绿/演练绿」；「在用」以机器消费方真实迁移
   （盲评 runner/效果评估改调 finqa）为准。

## 5. 风险

- launcher 吸收三 harness 参数空间（cwd/MCP/收权语义差异）——起法逐字复用
  bashrc 现役函数体，不新发明；差异面在 precheck 与 rc 判定，逐 harness
  单测演练覆盖。
- 答案质量不归链管（可用性≠质量），仍归盲评/finq。
- 两链并存期的认知成本：NOW/GLOSSARY 落账时写清「问询链=finqa-chain；
  codex_routes=效果评估专用（冻结）」。

## 7. 设计门裁决记录（2026-09-05）

- 实际服务评审者：cmd·deepseek-v4-pro·max（无 fallback），elapsed ≈250s
  （stderr 23:13 → review.md 23:17，台账为证）。
- 发现 P1×4 / P2×6 / P3×2 → **采纳 11 · 不采纳 1**。
- P1×4 全采纳：①起法清单与 bashrc 逐字基线不一致 → §2.2 锚定附录 B 为权威
  （环境链/effort 钉定全量）；②finqa-codex「别名 vs 保留原函数」两立 →
  §2.3 裁决保留原函数、只新增 finqa 一个函数；③codex_open.sh 横幅走 stdout
  缺陷不得继承 → 横幅/元信息一律显式 stderr；④provenance 无传递机制 →
  call_id + stderr 元信息行 + tsv success 行，评估类消费方强制钉腿或对账。
- P2×6 全采纳：alias 非交互 shell 不可见 → 机器直调 py、bashrc 用函数；
  tsv schema 未定义 → §2.2 定死六字段；state 路径未定 → finqa-chain/ 族约定；
  验收 0「tsv 即使用证据」不成立 → success 行入账；switch-provider skill
  旧路径打架 → 落账范围扩到 skill；验收 0 迁移对象未指认 → 指认 blind-eval
  runner.sh CC 腿。
- P3×2：采纳 1——--no-session 收窄审计面 → 撤 --no-session（可丢=不 resume
  非不留档）；不采纳 1——失败腿原始 stderr 留档（家规 3 边界 + rc/stage 已
  可定位，失败现场用 --node 重跑复现，不增设留存面）。
