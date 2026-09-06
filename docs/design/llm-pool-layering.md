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
                              auth_note: <认证所在位置，纯文档>, managed: pool|self, enabled}
L2 消费层  只声明「引用哪些连接别名 + 顺序 + 本层旋钮」
  提取/识图  priorities{t0,t1,cognition} + vision.chain        （现状，零迁移）
  问询链     finqa_nodes 节点：model 字段废除 → conn_ref: <别名>；session 旋钮保留
  评审链     codex_open.sh profile 条目 → conn_ref: <别名>
传导规则（唯一一条） L1 enabled=false ⇒ 所有消费链序跳过该连接（fail-visible，
  沿用 vision.chain 已实装的「缺失/禁用/未解析 ${}→跳过，保序去重」机制）；
  唯一例外：钉腿（finqa --node X）遇池禁用 = 拒绝执行（rc≠0 显式报错）——
  钉腿是显式意图（对照实验/探针），静默换腿会污染实验。
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
| 4 | 提取链 cognition/t0/t1（llm 直调） | 零迁移（条目名不变） |
| 5 | 识图 vision.chain | 零迁移 |
| 6 | deepseek legacy 后端（claims/industry_chain） | 零迁移（池条目照旧） |
| 7 | 评审 cmd 主/glm 替补 | profile 条目 conn_ref 化，池禁用传导 |
| 8 | opencode-go 429 恢复 | **单开关**（池条目 enable），双记账消解 |
| 9 | flash 测试腿（回归探针） | 升为池别名 `commandcode-flash`，节点只留引用+session |
| 10 | session keep/discard | 保留节点级旋钮 |
| 11 | effort=max | launcher 单点不变（明确不进池） |

## 4. 施工步（四步，每步独立可验；顺序即依赖）

1. **池补全**：llm.yaml models 增 harness 型 5 条——`commandcode-pro`（cmd login 会话）、
   `commandcode-flash`（deepseek-v4-flash）、`zcode-flash`（zhipu/glm-5.3-flash）、
   `claude-cc`（managed: self）、`glm-review`（codex-glm 资产，glm-5.3）。
2. **问询链接入**：finqa_chain.py 节点解析改 conn_ref→池（enabled 检查 + fail-visible），
   precheck 增池解析单测；finqa_nodes.yaml 节点 schema 同批迁移（破坏性变更，读方闭包
   = launcher + `--node` 旗标消费脚本，旗标名不变）。
3. **评审链接入**：codex_open.sh profile 条目 conn_ref 化，enabled 检查用 `python -c`（bash
   不解析 yaml），禁用替补=fallback 链跳过、双挂=rc 78 显式失败。
4. **同步**：GLOSSARY（连接池/连接别名/harness 型条目）、llm-route-topology.md、
   persona_regression.sh 与盲评 runner 冒烟确认（旗标未变，预期零改动）。

## 5. 验收探针（施工完成的定义）

- P1 传导：池禁 `glm-review` → 评审链 glm 替补跳过；池禁 `commandcode-pro` → finqa 链
  fail-visible 落下一腿（恢复后原位）。
- P2 钉腿拒绝：`finqa --node commandcode-flash` 且池禁该连接 → rc≠0 + 显式报错。
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
