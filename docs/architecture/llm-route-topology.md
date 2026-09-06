# LLM·链路·入口拓扑（一页纸）

> 定位：**谁、用哪条链、模型从哪来**的结构与消费者投影。术语见 [GLOSSARY](../GLOSSARY.md)；
> 系统数据流见 [system-overview](system-overview.md)；当前状态见 [NOW](../pm/NOW.md)。
>
> **事实源声明**：链序/开关/模型的唯一权威是三张表——`config/finqa_nodes.yaml`（问询）、
> `config/llm.yaml`（提取/识图）、`scripts/codex_open.sh` profile 区（评审）；起法唯一声明位
> = `scripts/finqa_chain.py` `_launch_argv`。**本页只画结构与消费者，不复制可变值**
> （enabled/健康/时延/额度以表为准），防双记账漂移。
>
> **维护触发**：改节点表链序、增删 models 条目、改 launcher 起法、改评审 profile、
> 改 bashrc 入口函数——任一发生，同步本页对应小节（一格一行）。

## 总拓扑

```
 使用者                          链                      事实源
 ──── owner ───── finqa "问" ──┐
 ──── 机器/agent ─ finqa --node ─┤→ A 问询链(腿序兜底) ← config/finqa_nodes.yaml
 ──── 探针/回归 ── 钉 flash 腿 ──┘      └─ 人格 CLAUDE.md + 13+2 工具(.mcp.json)
 ──── ZSXQ/Daily 单元 ───────────→ B 提取链(llm 直调) ← config/llm.yaml
 ──── 设计门/外援/盲评 ──────────→ C 评审链(按需动词) ← scripts/codex_open.sh
```

## A. 问询链（finqa，唯一问询入口，owner 与机器共用）

- 腿序形状：**claude → (codex) → commandcode**；测试腿不入链序：zcode-flash、commandcode-flash（钉腿可达）。括号 = 可禁用腿，现值看节点表。
- 各腿模型旋钮：cmd/zcode = 节点表 `model` 字段与 `~/.zcode/cli/config.json`；**claude 腿 = CC 自管**（`~/.claude.json` bigmodel 接线，launcher 不传模型，实产 glm-5.3 有盲评佐证）；effort = launcher 单点 max。
- 会话语义：默认 keep；`discard`（无头不留档）；`-i` 交互形态保留。
- 人格与工具面：`~/fin-data/consult-agent/`（CLAUDE.md 人格 + .mcp.json 13 只读+2 受限写），cwd 即身份。
- 消费者：owner 单发、agent 会话钉腿（盲评/探针/回归）、`persona_regression.sh`（flash 腿）、D3 考试。

## B. 提取链（LLM 直调池，无问询语义；供给面 = 代码拥有控制流）

- cognition 链序形状：**glm53_flash → (deepseek_flash_opencode) → deepseek_flash_cmd → qwen**。
- 难题/吞吐池：t0 `[glm53, deepseek, qwen]`、t1 `[glm53_flash, deepseek, qwen]`（llm.yaml `priorities`）。
- 识图链：`vision.chain = [mimo-token-plan, glm53_flash, glm-vision, vision, mimo]`，全败落 OCR 终兜底。
- 消费者（代码级）：cognition（thesis_extractor/cross_article/zsxq_apprentice）、daily_workspace_generator、claims、industry_chain、scraper/downloader、vision/evidence——即 ZSXQ 采集、Daily 生成、深读流水线。

## C. 评审链（codex_open.sh，按需动词，无常驻）

- 评审者：**cmd·deepseek-v4-pro 主 → glm·glm-5.3 替补**（自动 fallback，横幅+fallback.tsv 落账）；cmd 版本钉读 finqa_nodes.yaml `cmd_version_pin`。
- glm 替补腿资产 = `~/fin-data/codex-routes/codex-glm/`（auth.json+models.json，退役后保留）。
- 触发：设计门 / 吓人 diff / 同题两修未果外援；packet 骨架 [design-gate-packet-template](../design-gate-packet-template.md)。

## 模型身份对照（同名多身份，防混淆）

| 模型 | 问询链身份 | 提取链身份 | 评审链身份 |
| --- | --- | --- | --- |
| glm-5.3 | claude 腿（CC 自管） | t0 头（glm53） | glm 替补（codex-glm 资产） |
| glm-5.3-flash | zcode-flash 测试腿 | cognition 头 + t1 头 + 识图第 2 位 | — |
| deepseek-v4-pro | commandcode 腿（节点表默认） | deepseek（legacy 池位） | cmd 主评审者 |
| deepseek-v4-flash | commandcode-flash 测试腿 | DS 槽位两渠道节点 | — |

## 命令表

| 命令 | 语义 | 链 |
| --- | --- | --- |
| `finqa "问"` / `finqa --node <id> "问"` | 问询唯一入口 / 钉腿（生产·测试·探针同机制） | A |
| `finqa --node <id> -i` | 交互式形态（保留） | A |
| `finqa-c` / `finqa-cmd` | 过渡别名，废弃待删 | A |
| `finlog`（=finq） | 问后记账，D3 供数，非问询 | 记账 |
| `codex_open.sh`（stdin packet） | 外部评审入口（GATE_PROFILE 可切替补） | C |
| `persona_regression.sh` | 人格回归探针（固定 6 题） | A·flash |

## 运行态边界（与三链无当前耦合）

`hermes-gateway-fin/fq`（旧咨询 MCP 挂 release `current`，入口停用允许报错，P5 重接时切换）；
`codex-remote-control.service`（codex CLI 自身 daemon，与 fin 无关）。

## 已知跨表动作（恢复类，防漂移）

- opencode-go 恢复（429 解除）：需**两处**各翻一个开关——`finqa_nodes.yaml` codex 节点 + `llm.yaml` deepseek_flash_opencode，缺一则两链行为分叉。

## 已退役

- codex_routes 交互路由链（2026-09-06）：`codex_routes.yaml`+codex-proxy 三件套+skill；备份 `~/fin-backups/codex-routes-retirement-20260906/`，叙事见 NOW。codex-glm 认证目录不在退役范围（C 链资产）。
