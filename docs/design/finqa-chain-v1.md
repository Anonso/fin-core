# finqa-chain v1：机器问询链（无头 · 节点配置化 · 自动 fallback）

> 状态：设计稿 v1（待 owner 确认 + 设计门）。规则 5 判据：公共入口语义 +
> 跨 harness 接线 → 核心处理；合入后本文件删除，Git 即归档。
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

- 读 yaml → 过滤 enabled → 依声明序逐节点：
  1. **precheck**（每 harness 一个函数，便宜检查：二进制存在 + 认证标记在位；
     不做网络 ping，失败不发）；
  2. **起腿**：cwd=~/fin-data/consult-agent（目录即身份），无头形态
     （claude `-p --strict-mcp-config --mcp-config .mcp.json` /
     `cmd -p --no-session` / `codex exec --sandbox read-only`），argv =
     调用方问题参数原样透传；
  3. **判定**：rc=0 且 stdout 非空 → 透传 stdout，退出 0；rc=0 但 stdout
     空 → 按失败处理（机器消费方拿空答案无用，换下一位）；其余 → stderr
     横幅 + `fallback.tsv` 落账（0700/0600，state 目录）→ 下一位；
- 全部失败 → **exit 78**，stderr 列各腿 rc/失败阶段（调用方自行裁决）；
- `--node <id>`：单腿钉定（探针/对照/调试），跳过链；
- session 语义：**无状态无头**——一律 no-reserve/no-resume，每轮自足；
  fallback 换腿重发同参，丢的是上一腿会话（per-backend thread 本就不可跨腿）。
- 无熔断/TTL（见 §3；人频次现探现走，设计门口径）；timeout 每节点固定
  （900s，env 可覆盖）。

### 2.3 bashrc 收编（薄别名，函数体删除）

```
finqa            → finqa_chain.py "$@"            # 链（机器/脚本用）
finqa-claude     → finqa_chain.py --node claude "$@"
finqa-commandcode→ finqa_chain.py --node commandcode "$@"
finqa-codex      → finqa_chain.py --node codex "$@"
```

- `finqa-x` / `finqa-codex` 原函数**保留定义、暂时关闭**（休眠在案；恢复 =
  yaml enabled 翻 true，或直接用原函数）——不删除；
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
- llm.yaml（提取链）、read_market_snapshot 等工具面、MCP 配置零变化；
  本链只是「起哪条腿」的治理，不碰答案内容。
- 非 v1（明确不做）：TTY 交互模式、熔断/TTL、--resume 续问、节点健康度
  打分、并发探活。

## 4. 验收

0. 消费方迁移进验收：盲评 runner（state 台账脚本）改调 `finqa` 链入口——
   链自第一天起有真实读方，防家规 10 空转；fallback.tsv 即使用证据。
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
