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

- 使用实况（owner 2026-09-06 定调）：真实问询=有头手动（终端 CC / Windows ZCode 连 WSL），无头链流量≈测试——**生产链序仅 commandcode（ds-pro）一条**，其余皆测试腿。
- 腿序形状（生产）：**commandcode** 单腿；测试腿（enabled:false，仅 --node 钉腿可达）：**A** commandcode-flash（ds-flash，人格回归探针）、**B** zcode（glm-5.3-flash，单旋钮锚定）、**C** claude（glm-5.3-flash，备用，`--model` 逐次指定实测生效）。
- 各腿模型旋钮：zcode = `~/.zcode/cli/config.json` 全局单旋钮（现 zhipu/glm-5.3-flash）。**zcode 机制边界（2026-09-06 实测钉死）**：无逐次模型旗标；帮助文本所列 `--settings` 在 0.16.5 未实现（parser 拒绝）；`ZCODE_HOME` 仅遥测、不重定位配置；模型在会话创建时钉定（全局证伪：假模型名 rc=1）。claude/CC = `--model` 逐次指定（实测 served 实证），接线自管。
- effort = launcher 单点 max。
- 会话语义：默认 keep；`discard`（无头不留档）；`-i` 交互形态保留。
- 人格与工具面：`~/fin-data/consult-agent/`（CLAUDE.md 人格 + .mcp.json 13 只读+2 受限写），cwd 即身份。
- 消费者：owner 单发、agent 会话钉腿（盲评/探针/回归）、`persona_regression.sh`（flash 腿）。

## B. 提取链（LLM 直调池，无问询语义；供给面 = 代码拥有控制流）

- cognition 链序形状：**glm53_flash → (deepseek_flash_opencode) → deepseek_flash_cmd → qwen**。
- 难题/吞吐池：t0 `[glm53, deepseek, qwen]`、t1 `[glm53_flash, deepseek, qwen]`（llm.yaml `priorities`）。
- 识图链：`vision.chain = [mimo-token-plan, glm53_flash, glm-vision, vision, mimo]`，全败落 OCR 终兜底。
- 消费者（代码级）：cognition（thesis_extractor/cross_article/zsxq_apprentice）、daily_workspace_generator、claims、industry_chain、scraper/downloader、vision/evidence——即 ZSXQ 采集、Daily 生成、深读流水线。

## C. 评审链（codex_open.sh，按需动词，无常驻）

- 评审者：**cmd·deepseek-v4-pro 主 → claudecode·glm-5.3 替补**（自动 fallback，横幅+fallback.tsv 落账）；两者模型/开关自连接池解析（commandcode-pro / claude-cc），池禁=评审者不可用传导；cmd 版本钉读 finqa_nodes.yaml `cmd_version_pin`。
- 触发：设计门 / 吓人 diff / 同题两修未果外援；packet 骨架 [design-gate-packet-template](../design-gate-packet-template.md)。

## 模型身份对照（同名多身份，防混淆）

| 模型 | 问询链身份 | 提取链身份 | 评审链身份 |
| --- | --- | --- | --- |
| glm-5.3 | 问询链无此档（测试腿 B/C 均 flash） | t0 头（glm53） | **glm 替补（claudecode·5.3，在役）** |
| glm-5.3-flash | zcode 测试腿 B + claude 测试腿 C（单旋钮/--model） | cognition 头 + t1 头 + 识图第 2 位 | — |
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

- opencode-go 恢复（429 解除）：`llm.yaml` api 条目 `deepseek_flash_opencode`（提取链 DS 槽位）已于 **2026-09-07 翻回 enabled**（渠道复测 curl/生产 OpenAI SDK 探针均 200，owner 令）。`codex-opencode` 池条目（问询链 codex 腿）仍 enabled=false：渠道已恢复，但 09-06 腿角色重排后 codex 腿休眠属角色决定——翻回会令其按声明序插回生产问询链（commandcode→codex→claude），需 owner 拍板。渠道凭据前提：llm.env 的 OPENCODE_GO_* 与 auth.json 的 opencode-go 条目在位（缺失=启用后 fail-visible 跳过，需先补 key）。

## 已退役

- codex_routes 交互路由链（2026-09-06）：`codex_routes.yaml`+codex-proxy 三件套+skill；备份 `~/fin-backups/codex-routes-retirement-20260906/`，叙事见 NOW。codex-glm 认证目录不在退役范围（C 链资产）。
