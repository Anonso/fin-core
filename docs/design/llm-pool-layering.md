# LLM 连接池分层管理（连接层/消费层两层收敛）· 短设计

**日期**: 2026-09-06 · **状态**: 设计稿（设计门后施工） · **owner 依据**: 拍板「llm 放在一起管理连接信息，其他相当于消费者，各消费者易增删改、启用禁用节点，llm 的禁用也作用于消费者，分层管理；易维护、易扩展、易管理、逻辑清晰，满足当前所有已使用场景」

## 1. 问题与目标

现状三张连接/节点表各自为政：`llm.yaml`（models+priorities+vision.chain，已是两层）、
`finqa_nodes.yaml`（问询腿自带 model 字段）、`codex_open.sh` profile 区（评审者自带模型）。
同一连接身份散落多处（glm-5.3 三个身份、deepseek-v4-flash 两渠道），开关不互通
（glm53 禁用不影响评审替补），且存在跨表双记账（opencode-go 恢复要两处各翻）。

目标四条（owner 原话口径）：
- **易维护**：连接信息唯一事实源，改一处生效全部消费者；
- **易扩展**：新增模型 = L1 加一条；新增消费节点 = L2 加一行引用；
- **易管理**：两层开关正交——L1 连接级禁用传导全部消费者，L2 节点级禁用只裁剪本链；
- **逻辑清晰**：两层、一个引用接口（连接别名）、一条传导规则，无第三种机制。

## 2. 分层设计

```
L1 连接层  config/llm.yaml models 段（唯一连接注册表，别名全局唯一）
  api 型（现状字段不变）     {provider, model, api_key, base_url, endpoints?, enabled}
  harness 型（新增）         {type: harness, harness: <launcher argv 键>, model: <默认模型串>,
                              auth_note: <认证所在位置，纯文档>, managed: pool|self}
  managed 语义               pool = 池开关即总开关（enabled 键必有）；self = 无 enabled 键
                              （登记可见、开关不存在=无「翻了不生效」的错觉），auth 归 harness 自管
L2 消费层  只声明「引用哪些连接别名 + 顺序 + 本层旋钮」
  提取/识图  priorities{t0,t1,cognition} + vision.chain        （条目名/链序不变；见 §3 校验器豁免）
  问询链     finqa_nodes 节点：model 字段废除 → conn_ref: <别名>；session 旋钮保留
  评审链     codex_open.sh profile 条目 → conn_ref: <别名>
传导规则（唯一一条） L1 enabled=false ⇒ 所有消费链序跳过该连接（fail-visible，
  沿用 vision.chain 已实装的「缺失/禁用/未解析 ${}→跳过，保序去重」机制）；
  conn_ref 悬空/未解析 ⇒ 与池禁同义：链序 fail-visible 跳过 + 横幅落账；
  唯一例外：钉腿（finqa --node X，含 -i 交互形态）遇池禁或悬空 = 拒绝执行
  （rc≠0 显式报错）——钉腿是显式意图（对照实验/探针），静默换腿会污染实验；
  两形态（无头/交互）语义一致，无分叉。
```

设计判断三条：
- **池管连接，不管策略**：effort、session、温度等策略旋钮留在 L2（launcher 单点 max 不动）；
  认证凭据物理位置不动（api 型仍在 llm.env；harness 型仍在各 harness 家）——L1 拥有
  「注册表 + 开关」，不接管凭据文件，避免一次性大迁移。
- **CC 腿 v1 登记式**：`claude-cc` 条目 `managed: self`（auth 在 CC 内部，L1 开关对它
  记录不强制）。env 接管（ANTHROPIC_BASE_URL/KEY 注入）列为 v2 可选，需独立探针验证
  coding-plan 凭据 env 化，本期不做。
- **不复活常驻路由**：无 probe/冷却/优先级网关（那是已退役 codex_routes 的机制）；
  fallback 全部是「声明序 + 禁用跳过」的静态语义。

## 3. 全场景覆盖表（R：满足当前所有已使用场景）

| # | 现用场景 | 设计后 |
| --- | --- | --- |
| 1 | owner 单发问询（finqa，enabled 腿兜底） | 同；腿序经池解析，池禁用传导 |
| 2 | agent 钉腿（盲评双腿/回归/探针 `--node`） | 同；钉腿过池检查，池禁用=拒绝（保护对照实验） |
| 3 | 交互形态 `-i`（保留少用） | 同 |
| 4 | 提取链 cognition/t0/t1（llm 直调） | 条目名/链序不变；**闭集校验器豁免**：`claims/config_loader.py:350` 对 models 段全量 `issubset(_MODEL_KEYS)` 校验，harness 型条目会被拒（施工步 1.5 增豁免，否则提取/识图全线 LLMConfigError） |
| 5 | 识图 vision.chain | 同上（同一校验器） |
| 6 | deepseek legacy 后端（claims/industry_chain） | 同上 |
| 7 | 评审 cmd 主/glm 替补 | profile 条目 conn_ref 化，池禁用传导 |
| 8 | opencode-go 429 恢复 | **单开关**：新增 harness 型池条目 `codex-opencode`（codex CLI + consult-agent/.codex 认证，模型由该 home config 决定），节点 enabled 常态 true、与池 enabled 相与——恢复=只翻池一处；与提取链 api 型 `deepseek_flash_opencode` 身份分离（后者是 flash API 渠道） |
| 9 | flash 测试腿（回归探针） | 升为池别名 `commandcode-flash`，节点只留引用+session |
| 10 | session keep/discard | 保留节点级旋钮 |
| 11 | effort=max | launcher 单点不变（明确不进池） |

## 4. 施工步（四步，每步独立可验；顺序即依赖）

1. **池补全**：llm.yaml models 增 harness 型 6 条——`commandcode-pro`（cmd login 会话）、
   `commandcode-flash`（deepseek-v4-flash）、`zcode-flash`（zhipu/glm-5.3-flash）、
   `claude-cc`（managed: self，无 enabled 键）、`glm-review`（codex-glm 资产，glm-5.3）、
   `codex-opencode`（codex CLI + consult-agent/.codex）。
1.5 **校验器豁免**：`claims/config_loader.py` `compile_backend_plan` 对 `type: harness`
   条目跳过 provider/adapter/闭集校验（登记性条目不进 backend plan），配套单测钉死
   「api 型校验不放松、harness 型不入 plan」。
2. **问询链接入**：finqa_chain.py 节点解析改 conn_ref→池（enabled/悬空检查 + fail-visible
   + 横幅；钉腿含 -i 一律拒绝），precheck 增池解析单测；**zcode 防漂移 precheck 改读池
   条目 model**（原读 node["model"]，字段废除后不迁移=保护静默失效）；finqa_nodes.yaml
   节点 schema 同批迁移（破坏性变更，读方闭包= launcher + `--node` 旗标消费脚本，旗标名
   不变）。
3. **评审链接入**：codex_open.sh profile 条目 conn_ref 化，enabled 检查用 **fin-core
   .venv python**（系统 python 无 yaml 是现实风险）读 llm.yaml，yaml 异常=视为禁用
   （fail-closed）+ stderr 横幅（与 cmd_version_pin sed 检查的 fail-closed 同语义对齐）；
   禁用替补=fallback 链跳过、双挂=rc 78 显式失败。
4. **同步**：GLOSSARY（连接池/连接别名/harness 型条目/managed 语义）、
   llm-route-topology.md、`.claude/skills/switch-codex-open-provider/SKILL.md`（其
   「恢复 opencode-go 两处都翻」指导随单开关作废）、`tests/scripts/test_codex_open_
   cmd_guard.py` 闭包补录、`~/fin-data/consult-agent/README.md` 节点/腿/model 旋钮
   表述、persona_regression.sh 与盲评 runner 冒烟确认（旗标未变，预期零改动）。

## 5. 验收探针（施工完成的定义）

- P1 传导：池禁 `glm-review` → 评审链 glm 替补跳过；池禁 `commandcode-pro` → finqa 链
  fail-visible 落下一腿（恢复后原位）；conn_ref 悬空 → 同池禁语义 + 横幅。
- P2 钉腿拒绝：`finqa --node commandcode-flash`（无头与 `-i` 两形态）且池禁该连接 →
  rc≠0 + 显式报错，两形态行为一致。
- P3 回归：cognition/识图链既有测试 52+102 全绿（零迁移证明）。
- P4 双挂：评审 cmd+glm 全禁 → rc 78 + 报错可读。
- P5 三冒烟：cmd 主评腿 / glm 替补腿 / finqa flash 腿各一发「就绪」。

## 6. 风险与边界

- **并行在编**：finqa_nodes.yaml / finqa_chain.py / codex_open.sh / persona_regression.sh
  有并行会话未提交改动（版本钉单源化），施工须等其落稿后基于新基线，避免同文件对撞。
- **破坏性变更面**：finqa_nodes 节点 schema（model→conn_ref）。读方闭包已核：launcher
  （同批改）、`--node` 消费脚本（旗标不变）、codex_open.sh 只读 cmd_version_pin（键不变）。
- **别名改名=破坏性**：引用闭包 grep 后同批改，禁止单侧改名。
- **诚实边界**：CC 腿 v1 池禁用不传导（managed: self，页上可见）；auth 迁移/harness
  凭据接管不在本期。
- **净复杂度账**：+1 条传导规则、+1 个条目类型、L2 三处改引用；−跨表双记账一处、−散落
  model 字段两处。无新增运行时进程。

## 7. 设计门裁决（2026-09-06，评审者 cmd·deepseek-v4-pro，elapsed 831s；台账 design-gate/llm-pool-layering-20260906/）

发现 9（P1×1 / P2×3 / P3×4 + 专项建议 3 问内并 1）——**采纳 9，驳回 0，部分采纳 0**，已全部落稿：
- P1 闭集校验器（config_loader.py:350）拒绝 harness 型 → 采纳：§3 场景 4-6 改「校验器豁免」+ 施工步 1.5；
- P2 双记账 skill 指导 / codex 节点 conn_ref 悬空 / zcode 防漂移迁移 → 采纳：步 4 补 skill+两闭包件、场景 8 改 `codex-opencode` 条目单开关、步 2 明示防漂移迁移；
- P3 conn_ref 悬空语义 / 交叉版本窗口 / 闭包清单两小件 / 交互钉腿语义 → 采纳：传导规则补悬空=跳过+横幅、§6 窗口声明（家规 9 成套部署下无支持场景；旧 launcher 读新表 zcode 校验静默失效属未声明退化面，随步 2 消除）、交互钉腿同拒绝；
- S1 钉腿拒绝语义正确（补 -i 覆盖）；S2 .venv python + fail-closed 对齐；S3 managed:self **不提供 enabled 键**（无开关=无错觉，优于「有开关不生效」）——三条全按建议改稿。
施工前置：等并行会话 finqa_nodes/finqa_chain/codex_open.sh/persona_regression 未提交改动落稿后再动工（§6 对撞风险）。
