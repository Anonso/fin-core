# 用户理念与产品设计原则

> 状态：active
> 用途：保存跨需求长期成立的产品边界，不保存排期、实现历史或当前运行状态。

## 产品北极星

FIN 是面向用户 A 股决策的 AI 投研助手，不是资料检索站、MCP 工具目录或自动交易机器人。

用户体验应当像与一个熟悉其持仓、老师认知、最新市场和历史对话的强 Agent 交流：

```text
用户入口（无关化：当前 CLI consult-agent；飞书/Hermes 待 P5 才加薄壳）
  → 确定性绑定同一 FIN/Codex 分析 Agent 会话
  → 提供紧凑能力地图，分析 Agent 按需读取专属数据与知识
  → 分析 Agent 理解、反证、综合并生成自然答案
  → 逐字展示可追溯、说人话的决策辅助
```

用户应得到更专业、更省心、更可追溯的判断，而不是看到内部工具、等级、状态机和工程错误。

## LLM-as-Kernel / Agent-as-Product

- **最高原则：FIN 是辅助与增强层，不是约束层。** FIN 可以让直接 Agent 更可信、更懂上下文，但绝不能让它更难用。任何相关设计先说明给 LLM 增强了什么、限制了什么；不能由事实、来源、权限、资源、风险或真实副作用证明的限制，不得进入实现。
- **Agent 是产品**：Agent 有身份、目标、知识边界、工具边界、连续对话和安全边界，不是 persona prompt 包装器。
- **LLM/MoA 是认知内核**：理解、推理、迁移、反证、综合和表达尽量交给模型。
- **程序是能力放大器和护栏**：它向 Agent 提供领域上下文、工具、状态、记录与可靠边界，不用规则树、固定模板或字段填空取代高价值判断。
- **单强 Agent 优先**：先让一个 Agent 把主链做好。只有真实对照显示它在具体场景不足时，才加 MoA 或第二 Agent；多 Agent 不是成熟度象征。

这里的“单强 Agent”特指唯一投研推理者，不是说系统中只能有一个 Agent 进程。旧飞书/Hermes 拓扑曾采用 **双 Agent、单投研答案**：Hermes Agent 拥有前台会话、Skill 和路由；FIN/Codex 分析 Agent 拥有投研推理与最终自然答案。**该分工随旧拓扑退役（2026-08-27 rebaseline，D-016）**；当前用户直接对话的那个强 Agent 即唯一投研推理与答案 owner，没有第二个 Agent 生成第二份投资判断。

## 轻量 Skill 与会话绑定

- 一个 Hermes 会话确定性绑定一个 FIN semantic chain 与一个精确的分析 Agent provider session；追问恢复精确 session id，不使用全局最近会话。
- Hermes 侧 Skill 是很薄的提示词包：只判断何时调用唯一 `fin_consultation`、保留用户原话、禁止自行拼持仓/G/行情，并原样展示分析 Agent 答案。
- Skill 不保存 continuation、账户或领域状态，不复制 FIN 合同，不规定标题、段落、结论类型或固定措辞。正式身份与 session binding 由非 LLM transport/FIN server 注入。
- 数据采集、清洗、去重、时效、来源、检索能力、权限和副作用边界属于代码；是否读取、如何理解、推理和表达属于 FIN/Codex 分析 Agent。Agent 本来就会做的事，不写成 Skill，也不把全部专属数据预塞进每轮 prompt。
- 同一前台目的只保留一个 Skill；stock/portfolio/router/runtime 等差异由用户原话与 FIN 内部深模块处理，不复制成多份重叠提示词。

FIN 领域内核与强 Agent 的 owner 边界见 `fin-domain-kernel-agent-runtime.md`。

## FIN-owned 产品接口

普通咨询的正式 interface 应足够小：原始问题、可信 session、可授予的按需能力、opaque continuation、总 deadline，以及 Agent 的完整自然答案和 machine-owned tool trace。FIN 不要求 Agent 复制内部 context ID、来源回执、profile、claim taxonomy 或安全声明，也不用 ProductContract 规定自然答案形状。

持仓写入、真实交易和其他副作用各自使用独立的窄 typed contract；不能因为未来可能写入，就让所有只读咨询承担写操作的完整性和确认门禁。Hermes Agent 只委托与逐字展示，不成为第二个投研答案 owner。

## G、Z 与来源边界

- G 是老师/星大派认知主线。回答“老师怎么看”时，主结论只能来自合格 G 证据或对 G 逻辑的显式推断。
- Z 是工具线，只围绕 G 已提及或逻辑暗含的信息分析行情、估值、资金、信号、触发器和风险。
- Z 不引入 G 外新推荐，不验证、覆盖或否定 G；G 不直接改 Z score、交易动作、仓位或 hard blocker。
- 研报、新闻、公告、行情、书本、课程、外部 research 与 MoA 产物是 evidence/reference，不得冒充老师观点或污染 Persona/ReasoningTrace。
- 展示必须让用户分清：老师原始证据、FIN Agent 推断、Z 工具分析、外部参考和正式账户事实。

只有用户明确问“老师怎么看”或答案实际依赖 G 时，G 证据不足才需要在答案中说明；普通问题没有相关 G 时直接按强 Agent 基线回答，不能用参考资料补写老师观点，也不显示内部 G 缺口。

## 知识与时间

- 最新星大派是 G 线最高优先级的实时输入。“最新”提升处理优先级，不自动提升置信度或交易强度。
- 书本、课程和长期方法论进 shared brain 时保留 source refs、time horizon、usage policy 和 confidence boundary；它们是分析 lens，不是老师最新观点。
- SharedKnowledgeBrain 是 FIN 的通用认知模块，G 是老师专属认知模块。相关且已复核的 shared brain
  方法可以塑造分析问题和验证顺序，但必须保持 `non_g/reference_only`，不得写成老师观点、当前事实或交易信号；
  无相关卡片或读取失败时直接强 Agent 基线不得退化。
- analysis、MoA result、source trace 和工程报告是记录，不是可无条件复用的结论缓存。

## 投资判断与低买高卖

- 投资收益的核心是识别尚未被市场充分计价的未来价值，在预期差收敛前以有利价格承担风险，
  并在价值兑现、赔率消失或 thesis 被证伪时退出；不把“等待所有事实确认”冒充判断能力。
- “低”是当前价格相对未来价值与市场预期分布具有正向不对称，不是低于某条均线；“高”是
  上行空间或预期差已被价格消化，不是站上某条均线。好公司、好产业和好催化不自动等于好下注。
- 回答“能买吗/谁更值得”必须连接当前价格、市场已计入的预期、情景上行与下行、催化和失效条件；
  缺价格或估值锚时只能给研究优先级，不能冒充投资排序。
- 技术信息是市场参与者交易行为的证据，可更新 thesis 概率、提供领先或反证信号，并帮助设计
  路径、时机、仓位和退出；它不能单独证明便宜、保证盈利或把单一均线变成买卖许可。
- A 股分析先判断市场可能在正式业绩验证前交易什么，以及标的处于潜伏、扩散、加速、拥挤还是
  兑现/验证阶段；再检查下一批边际买家、已计入预期和基本面最终能否承接。均线门不能换成财报、
  订单或一致预期门。
- 确认会降低不确定性，也通常要求付出更高价格。Agent 应比较提前介入、分阶段试错、等待和 PASS
  的预期收益与机会成本；不确定性优先改变下注大小与失效条件，不能自动推出无动作。

## 风险与交易边界

- 研究层可以给出复核优先级、风险理由、观察触发器和非执行型 decision draft，但不产生真实订单语义。
- 交易草案只能进独立 execution contract；真实执行还必须有 RiskGuard、实时持仓验证、权限、审计和人工确认。
- 持仓复核优先看 thesis 的兑现、偏离、证伪或超预期；价格/ATR 可作独立 RiskBackstop，不冒充 thesis，也不被 Agent 关闭。
- 无有效 runtime 结果时明确 unavailable；无合法账户写入上下文时禁止写入。普通 advisory 缺少行情、G 或其他增强数据时，应给出条件化判断、关键变量和验证方法，而不是以“无动作”替代分析。

## 从简与真实完成

1. 先问“如果今天从零设计，这层仍必要吗”。没有已发生的问题或用户效果证据，就不增加抽象、缓存、fallback、兼容层或控制面。
2. 先复用已有深模块和小 interface；调用方需要理解内部细节时，深化 owner 模块，不再加 pass-through 包装。
3. 定性程序守 source、risk、权限、schema、资源和 write effect；模型在护栏内保留完整推理和表达空间。
4. 结构化输出用于互操作、审计和安全，不得成为信息上限。fallback 要诚实标注能力下降。
5. 测试、fixture、schema 和部署动作只是 preflight。只有所有者在用的真实用户结果和必要的连续运行证据才能将一项能力称为产品完成。
6. 定时器被激活不是完成。手动同入口、run ledger、last success、freshness、alert、replay 和连续真实投递是定时产品的最小合同。
