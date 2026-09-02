# FIN 决策日志（append-only）

> 本文件记录**决策级**演进：方向、定位、架构判决、流程立废、原则采纳、改变路线的实验结果。
> 它是"为什么"的历史，**不是当前状态源**——当前状态与队列只看 `docs/pm/NOW.md`；任务级过程由 Git 提交史保存；长期产品哲学在 `docs/architecture/user-design-principles.md`。
>
> 规则（防止本文件变成第二个病）：
> 1. **只追加，不重写**。决策被取代时不删旧条，只在旧条加一行 `→ 已被 D-XX 取代`。
> 2. **只记决策级**。凡不值得问"半年后为什么这么做"的不入册；任务完成、bug 修复不入册。
> 3. 每条 ≤10 行：决策一句话、为什么两三句、**否决了什么及理由**（这是最值钱的部分——防止被否决的路被重新提议）。
> 4. 入册时机 = 用户拍板的那一刻，由当时会话写入；回填条目标注 `[回填]`。
>
> **跨会话 / 跨 agent 维护协议**（CC / codex / opencode / 未来 harness 通用）：
> 5. **落盘即 commit**：本文件与 docs/ 下其他决策文档写入后立即单独 commit（docs-only 提交永远安全，不等任务收尾）。未跟踪 = 对并行会话不存在、无任何安全网（2026-08-27 rebaseline 文档曾被删且 git 无法恢复，即为此教训）。
> 6. **追加流程**：读文件尾 → 取下一 D 号 → 文件末尾追加 → 立即 commit。两分支同时追加产生 git 冲突时：两边条目都保留，后写者顺延重号；纯追加冲突永不需要改写对方内容。
> 7. **条目所有权**：条目由见证拍板的会话写入；其他会话不得改写他人条目，只能 (a) 追加新条目 (b) 在旧条目 status 行加取代指针。拿不准是否"决策级"时不写，宁缺勿滥。
> 8. 本文件为 append-only 共享目标，不受"并行会话文件集不相交"规则约束——任何会话在用户拍板后都可追加。

## 条目格式

```
## D-NNN · YYYY-MM-DD · 标题
- 决策：
- 为什么：
- 否决了什么：
- 状态：active / → 已被 D-XXX 取代 · 证据：<路径>
```

---

## D-001 · ~2026-06 · 项目启动的初衷 `[回填]`
- 决策：代码 + LLM 做 A 股决策辅助；会话对应 Agent 会话；FIN 注入上下文；自动化以后再说。
- 为什么：所有者要一个懂自己持仓和老师认知的投研助手。
- 否决了什么：一上来就自动化交易。
- 状态：→ 定位部分被 D-016 修订（会话载体从飞书改 CLI）；证据：docs/RETROSPECTIVE.md

## D-002 · 2026-08-04 · 第一次坍塌复盘与 14 条永久反制 `[回填]`
- 决策：收敛文档（AGENTS 唯一合同 + NOW 唯一状态）、终态即回收、复杂度必须付费、直接 Agent 不退化。
- 为什么：53 worktree/138 分支/41 release；定时抓取整月不可用但文档宣称部分完成——完成口径错误。
- 否决了什么：observer/authority/migration state machine 式补丁文化。
- 状态：active（部分条款）；**教训：8/23 后建成的重机器（receipt/审核轮次/四级完成度）正是对本复盘的"响应"，药变成了第二次病**（见 D-016）。证据：docs/RETROSPECTIVE.md

## D-003 · 2026-08-13 · P0 下注计分切片授权 `[回填]`
- 决策：commit/push/上线授权、按推荐处理、非必要不停。
- 状态：→ 被 D-016 的家规 v2 吸收。证据：CC memory bet-p0-authorization-20260813

## D-004 · 2026-08-20 · 默认自动交付 `[回填]`
- 决策：验收四项过审后自动 add/commit/push/上线；同时立"自动合并、冲突按冻结范围处理、新实现全替代且无调用方的旧代码自动删除"。
- 为什么：减少人工等待。
- 状态：→ 被 D-016 的"部署=checkout+sync+重启"简化形态取代。证据：AGENTS.md 历史版

## D-005 · 2026-08-21/22 · 风险分级：low 由 CC 直接完成 `[回填]`
- 决策：只读调查后 CC 自评风险；low 跳过 design review 与 candidate audit 直接交付；high 才进审视闭环（最多 3 轮）。
- 为什么：全量审视对小修是固定税。
- 状态：→ 被 D-016 整体废止（审核机构取消，降为按需动词）。

## D-006 · 2026-08-23 · 治理完成 + 发布/审核机器建成 `[回填]`
- 决策：P0-P3 治理收口；同期建成 frozen-sync receipt（schema 3）、CAS 激活、四级完成等级、binding、Sol/xhigh 审核 hook、codex 路由链。
- 为什么：治理复盘认为缺"用户结果约束"，响应方式是更精细的完成度机器。
- 否决了什么：当时无（这正是问题——没有否决"用加法响应加法病"）。
- 状态：→ 已被 D-016 整体退役（发布机器退役、审核取消、四级完成度废除）。**本条是 D-016 存在的直接原因。**

## D-007 · 2026-08-23 · Codex 路由链 cutover（proxy-a/b/open）`[回填]`
- 决策：多路由 failover 链 + 独立 CODEX_HOME + 探活/冷却。
- 状态：→ 已被 D-016 取代（问询大脑回归客户端；L1 管线小池保留）。

## D-008 · 2026-08-24 · R1 严格对照失败（0:3）→ 修 prompt 而非加机器 `[回填]`
- 决策：账户链全胜证明持仓上下文有增益；可比链 0:3 的差距归因 prompt/投递质量，修 advisory prompt 与 Agent seam。
- 为什么：后续 Slice 5 盲评 5/5 全胜证实杠杆在人格层。
- 状态：active（本结论是 D-016 方向③的核心实证依据）。证据：$XDG_STATE_HOME/fin-analyse/r1-direct-ab-20260824/
  → 2026-08-27 评审勘误：Slice 5 为 5/5 胜中 3/5 严格可比；其证明力是"缺陷修复后回归集全胜"，人格单因果与跨模型迁移未被识别，因果措辞以此为准。

## D-009 · 2026-08-24 · FIN 减法重设计 Slice 1：三字段 product contract `[回填]`
- 决策：咨询产物收敛为 consultation_product/v1 三字段；退役 candidate/binder/presentation 改写。
- 状态：→ 咨询主链整体被 D-016 归档；"答案唯一 owner 是 Agent"的精神保留进顾问人格。

## D-010 · 2026-08-25 · 开发阶段简化上线流程 `[回填]`
- 决策：切换路径砍掉正式 canary/回退演练/飞书 E2E；结果上限 development/partial。
- 为什么：正式仪式对开发阶段过重。
- 状态：→ 被 D-016 的便宜部署形态取代。

## D-011 · 2026-08-25 · Slice 3：G/共享知识从预取注入改为按需读取 `[回填]`
- 决策：Agent 按需经 capability 工具获取 G 与共享脑，不再预塞 prompt。
- 状态：active（D-016 的 consult-agent 延续此模式）。
  → 勘误：共享知识（shared_knowledge）capability 已于提交 `8c0718a3` 移除、不在咨询授权集；按需读取的"active"仅指 G 线（teacher_cognition）。

## D-012 · 2026-08-25/26 · Slice 5 大删除（累计净删约 3900 行）`[回填]`
- 决策：退役 candidate prompt 路径、binder 机械（1313→276 行）、research prompt 路径（-624 行）等。
- 状态：active（D-016 是同方向的结构性放大）。

## D-013 · 2026-08-26 · 外部吸收两阶段路线（A1-A4 / B1-B2）`[回填]`
- 决策：先按需增强 FIN，跑通后再评估移植到外部宿主（LangAlpha/Vibe-Research）。
- 状态：→ 被 D-016 重构：CC/codex CLI 本身即宿主；外部项目降为组件/方法粒度吸收，带三道闸（吸行为不吸机制/举证倒置/先旁路后接线），推至 W4+。

## D-014 · 2026-08-27 · 删除优先原则 `[回填]`
- 决策：旧/坏/可丢弃数据一律删除或归档（备份+manifest），不写兼容修复、检测逻辑或迁移。
- 为什么：零工具线程案例——写检测/转换逻辑的成本和风险高于直接删。
- 状态：active；已吸收进家规 v2 第 3 条与 D-016 的归档策略。证据：docs/pm/NOW.md 2026-08-27 条

## D-015 · 2026-08-27 上午 · "gateway 并发卡死"复核结案：误判 `[回填]`
- 决策：三层独立证据（ledger/状态库/官方历史）推翻卡死结论；不修不存在的 bug。
- 为什么：日志静默是该配置常态；慢延迟缺进展可见性易诱发误判重启。
- 状态：active（教训：判卡死先查 state.db 与官方历史）。证据：$XDG_STATE_HOME/fin-analyse/gateway-incident-recheck-20260827/

## D-016 · 2026-08-27 · Rebaseline：CLI-first、方向③原地减法（当前最高决策）
- 决策：定位 1-5 人自用；"所有者在用"=唯一最高验收。FIN 收缩为数据资产+stdio-MCP 读能力（provider DI 缝上薄 server）；问询 = fin-data/consult-agent/ 工作区（人格自 user-design-principles 移植，模型可配置默认 glm-5.3）；咨询主链与发布机器归档；部署=checkout+uv sync+重启；三泳道隔离；审核降为按需动词；AGENTS 177 行→家规 v2 约 20 行。
- 为什么：两次坍塌同根——为交付确定性优化而非使用频率、边界数超出 solo 可验证范围、免疫系统成本超病原体。8/24 与 Slice 5 证明杠杆在人格层=新架构最便宜的层。
- 否决了什么：**整体重做**（重付爬虫/确认链学费，且重做是 AI 超产引信）；**GitHub 宿主迁移**（LangAlpha 的 checkpoint/writer-fence 恰是要删的机器；差异化资产无 OSS 对应物；CC/codex CLI 已是事实宿主）；"开发 CLI 委托问询 agent"（重建嵌套模式）。
- 状态：active · 证据：docs/pm/rebaseline-20260827.md（含 P0-P5 排期、风险登记、实验预注册、防第三次坍塌三防御）
  → 2026-08-27 双评审（GPT + DeepSeek 对抗）裁决后修订为 v1.6：方向③确认成立；执行重构为"建新入口（只读薄 server/类型叶子化/Daily 生成器替换/L1 新配置）→ 自然使用观察 → keep-set 闭包惰性归档"，W2 从删除手术改为修复+建设，破坏性归档推迟；绊线基线改实测后设定。裁决全文见方案 §0.5。
  → 2026-08-27 容器拍板 ③b：新家移植——新仓 fin-core（包名不变、逻辑零重写、零 hooks/旧测试/skills 残留）、数据出仓至 fin-data、旧仓冻结运行作博物馆；移植清单=keep-set 闭包并集+附录 C；P1 spike 仍先在旧仓做（使用验证不等搬家）。同日补拍：停用旧飞书咨询入口。
  → 2026-08-27 工程层双评审（GPT+DS 并行盲评）裁决：D1/D2 重写为 v2、家规升 v2.1、行数退出门废除（使用是唯一门）；见方案 §0.5.7。

## D-017 · 2026-08-27 · 设计门：核心设计稿默认过外部对抗盲评
- 决策：核心链路设计稿（家规规则 5 触发设计稿的那类）动代码前默认过一次外部对抗盲评（双模型并行最优、单模型可）；评审只产发现，裁决归 writer 会话逐条对代码核；无轮次/binding/failover/追踪设施；非核心豁免。
- 为什么：工程层双盲评对 D1/D2/家规 v2 合计 54 条发现、零误报、11 项独立收敛；writer 单干设计稿的核心断言被证伪（G 主线工具选错、类型叶子化断不干净）——一次盲评的收益远超成本。
- 否决了什么：恢复 design review 轮次机器（一次性门≠轮次）；小改动强制盲评；评审者升级为规划者/指挥层（评审产发现，不产权衡与拍板）。
- 状态：active · 证据：docs/pm/rebaseline-20260827.md §0.5.7、docs/pm/engineering-layer-review-20260827.md、docs/pm/engineering-rules-review-gpt-20260827.md
  → 评审者收口与模型分档被 D-023 细化（默认单评审者 DS Pro max，双模仅外援校准期）。

## D-018 · 2026-08-27 · LLM 路由重排：GPT 全退、官方 bigmodel 双锚、T0/T1 顺序配置化
- 决策：所有 LLM/codex 平面去掉全部 GPT API（llm.yaml gpt5 链 proxy-a/gpt-5.6-sol、codex_routes 三条 gpt-5.6-sol 路由、hermes 图片识别 gpt-5.4）；保留硅基流动与 deepseek；官方 bigmodel 新增 glm53/glm53_flash 双锚——难题先用 glm-5.3、简单先用 glm-5.3-flash；硅基流动 GLM（GLM-5.2）删除；grok-4.6(proxy-b)/deepseek-flash/qwen 兜底序不变；T0/T1 候选顺序进 llm.yaml `priorities` 段，消费方按配置迭代、缺段回退硬编码（家规规则 6 首次落地）。
- 为什么：GPT 代理链贵且已停用；glm-5.3 为主实验模型且所有者日用（CC 同款凭据）可信；顺序写死导致每次换锚都动代码。
- 否决了什么：删除整个 gpt5 entry + 消费方元组全量重构（约 10 处硬编码属 W2'④ 结构重排，本次不扩面）；codex-open 咨询路由与旧主链 provider 不动（W2'③ 一并归档）；旧 GLM_VISION key 未换新（glm-vision 条目属 vision 链本轮不动，但其 key 已 401 失效待后续处理）。
- 状态：active · 证据：config/llm.yaml（priorities 段）；fin-data/codex_routes.yaml（digest c3121c61…，validate ok）；~/.hermes/profiles/fin/config.yaml auxiliary.vision；$XDG_STATE_HOME/fin-analyse/codex-route-operations/20260827-degpt-reorder/（before 备份 + 双门输出）；glm-5.3/glm-5.3-flash ping 200 于 open.bigmodel.cn/api/coding/paas/v4（通用 /api/paas/v4 对 coding plan 凭据报 1113，故用 coding 路径）

## D-019 · 2026-08-28 · 中转站全退 + codex-glm 官方 GLM 问询锚（owner 深夜拍板，修订 D-018 两项）
- 决策：①llm.yaml gpt5 级联删 proxy-b(grok-4.6) 与 opencode-deepseek(deepseek-v4-flash)，级联=glm-5.3(bigmodel)→qwen(硅基流动)——D-018"grok 兜底序保留"被本决策取代；②codex 路由链新增 codex-glm（adapter codex-provider，base_url open.bigmodel.cn/api/v1 官方 Responses 端点，模型 glm-5.3，quality pinned，catalog 默认档 max），声明序置于 codex-open 之前；问询链 reasoning.effort xhigh→max（两模型目录均支持 max，codex-open 端点/模型不动）；③glm-vision 修为 glm-4.6v-flash 官方免费档（旧 glm-4v-flash key 401 失效），新 key 入 llm.env GLM_VISION_API_KEY。
- 为什么：owner 指令"中转站的都删掉"；问询 codex 应上官方 GLM 最大思考强度。实测事实：glm-5.6 不存在（coding/通用两端点均 1214，模型清单最新 glm-5.3，故用 glm-5.3）；bigmodel 官方 Responses 端点 = /api/v1（/api/coding/paas/v4 无 /responses，404）——配置层可直接接入，零代码改动（wire_api=responses 硬编码不需动）。
- 否决了什么：wire_api 配置化代码改动（Responses 端点存在后不必要）；删除 codex-open（owner 明示不动，退居次优先级，探活 403/inconclusive 属已知非失败分类）；llm.env 旧 GPT_CODESONLINE_*/PROXY_A_*/PROXY_B_* 变量与 codex-routes/ 旧 route home 目录的删除（生产 release 旧配置仍引用，随 W2' 部署一并清理）。
- 状态：active · 证据：config/llm.yaml（commit 5a6cb6bb，claims+cognition 561 绿）；fin-data/codex_routes.yaml digest 034ff3f8…（validate ok，consult=[codex-glm, codex-open]，deployed 操作器双门通过）；codex-glm probe reachable 200 于 open.bigmodel.cn/api/v1/responses（effort max）；$XDG_STATE_HOME/fin-analyse/codex-route-operations/20260827-night-relay-cleanup/（before/after 全套）；网关未重启——运行态生效随 W2' release（与 D-018 同排序）。

## D-020 · 2026-08-28 · D3 终态验收后置：建设全完成后一次性跑三天门
- 决策：D3（三天真实运行验收）从"第 1-3 周先行"改为**最后执行**——顺序变为 W2 手术 → 外部项目吸收 → 各模块深化 → D3 终态验收 → P5（条件开放不变）。建设期内保持非正式日常使用：缺陷照记入队列、即修即排队，使用日志不停。
- 为什么：基础模块大改或逻辑变动会使已通过的三天观察作废、重跑浪费；终态一次性验收更省。rebaseline §6 原时序防的"无使用即建设"风险由非正式使用+缺陷队列承接，防坍塌语义不变。
- 否决了什么：D3 先行的原时序（仅时序，验收门本身"连续 3 天明天还用吗=是"不动）；把 P5 提前到 D3 前（仍条件开放）。
- 状态：active · 证据：docs/pm/NOW.md 队列重排；所有者 2026-08-28 拍板（"基础模块一大改又要浪费三天"）。

## D-021 · 2026-08-28 · gpt5 slot 全删：级联能力移除，兜底全归 priorities（owner 拍板，收口 D-018/D-019）
- 决策：llm.yaml `gpt5` 条目整个删除（非改名）——backend 内多端点级联能力随之移除，跨后端兜底由 `priorities` 承担（t0=[glm53, deepseek, qwen]，t1 不变）；约 14 处源码/脚本的 gpt5 引用与 preferred/fallback 元组清零（glm53 置首）；grok 全库无残留。
- 为什么：owner 追问「我保留干啥啊」后拍板「删除所有 gpt 和 grok」——名字即漂移（盘点问题 3），级联在单后端内的价值不抵名字谎言的认知税；每调用级联上限本就 ≤2 端点（rebaseline §3），跨后端序已覆盖兜底语义。
- 否决了什么：slot 改名保留条目（cascade/deep/zsxq 三案，owner 弃）；W2'③ 咨询机械里 codex-proxy-a/b 常量与旧 env 变量提前删（生产 release 旧配置仍引用，按 NOW W2① 随部署清理）；12 个测试文件里 gpt5 作为 fixture 标签的批量重命名（name-agnostic 机械不因标签破坏，留 W2'③）。
- 状态：active · 证据：config/llm.yaml；scripts/observe_provider_health.py 探针名单更新；2038 测试绿（宽扫 1291 + llm_config/moa/scripts 747）；干加载 plans=[glm53, glm53_flash, qwen, deepseek]

## D-023 · 2026-08-28 · 外部审视收敛：评审者单一脚本、模型按角色三档、评审侧不长驻会话（owner 拍板）
- 决策：家规"复评/设计门/外援"收敛为一段"外部审视"——评审者固定 scripts/codex_open.sh --sandbox read-only（当前 codex-open · deepseek-v4-pro · max，换 provider/模型/强度只改该脚本，家规不写死）；三触发、每触发一次：核心设计稿动代码前=设计门（规则 5 那类，非核心豁免）；吓人 diff 合入前（按规则 5 核心判据）；同一问题 ≥2 次修复未果=外援（加第二意见 codex-glm·glm-5.3，前两次双模并行校准独立发现占比再定转正）。每次评审冻结 packet（设计稿或 diff+提交清单+固定四问），评审只产发现、裁决归 writer 逐条落稿，裁决记录附 elapsed_seconds；评审侧不用长驻会话，靠 packet 固定前缀吃 provider 缓存，仅外援出现一次真实漏上下文事故才许引入 resume 会话；第一层 /review 保持 CC 本模自检。
- 为什么：外部评审已事实统一 DS Pro max（设计门 12/12/17 发现全裁决闭环）；"双模型并行最优"造成每次门临时决策成本、违反规则 6；长驻会话破坏盲评且上下文膨胀——Daily 门 12416/12467 prompt 命中缓存证明固定前缀已零状态拿到复用收益，vision 门 318.7s 给出时长基线。
- 否决了什么：每节点配不同模型（四套规格、无证据支持）；评审侧长驻会话；glm-5.3 无实证直接转正；在 packet 与家规里写死 provider/模型名。
- 状态：active · 证据：AGENTS.md 外部审视节；scripts/codex_open.sh（--sandbox read-only，2 测试绿）；docs/GLOSSARY.md；旧 review-failover hook 链与 binding-era Stop 权限 gate 已备份并删除（~/fin-backups/external-review-cleanup-20260828/，留存写守卫 3 测试绿）。

## D-022 · 2026-08-28 · 普通栏撤出 G 准入与深化资格 `[回填]`
- 决策：`classify_g_source` 删除「普通」映射——G 准入与深化资格整体撤销（单缝联动同时收回）；普通栏内容留 reference lane 检索，不进 G 认知库；已生成的 58 篇普通栏产物留库惰性。
- 为什么：全库 1202 篇结构审计质量优（仅 3 截断/3 缺文件），但普通栏非 QA 86% 为券商研报转载/总结，非老师原创观点；BUG-006③ 曾短暂放行为 general G，当晚经审计后撤销。
- 否决了什么：普通栏不深化造成的工作集 manifest PARTIAL 一次性成本（接受，不为普通栏单建解耦资格）；普通栏 QA 提问进 G 注入（同撤）。
- 状态：active · 证据：f2a70f06、fin_analyse/guo_teacher_research/source_contract.py

## D-024 · 2026-08-29 · 文章标签读缝拍板：选项③观察期，检索缝等首条真实抱怨（owner 拍板）
- 决策：article_tags 暂不设产品读方——咨询检索缝（薄 server 加工具）与窄过滤面均不开工；标签服务 owner 库整理（审计筛 prune 候选、按类查询，CLI 已有）。升级触发器写死：owner 在真实咨询中出现一次「翻星球里某篇/某类内容」而顾问做不到，经 `finq` 记一条即为检索缝开工凭证；届时直接做检索工具（不做过滤维度）。
- 为什么：全库消费方实证只有 ingest 写钩子+自家 CLI（写而不读）；规则 10/11 准入=使用日志里的具体抱怨，现无一条检索抱怨；薄 server 没有被咨询使用的自由检索缝，「窄过滤」无从附着，且过滤属静默改答案面（需复跑六题验证不退化）。
- 否决了什么：现在建检索工具（无证驾驶）；窄过滤维度（前提缝不存在+静默改答案）；按「标签系统刚建成」的沉没成本倒推读方（规则 11 反向）。
- 状态：active · 证据：grep 全库消费方仅 `fin_analyse/cognition/cli.py`+`scraper/cdp_scraper.py`；NOW 产品面推进条（B1/B2 备料）；触发器数据源=`scripts/finq` → usage.jsonl。

## D-026 · 2026-08-29 · B1 裁量收口：不触发回炉 + A2 并入迁移第5步（owner 拍板「按推荐处理」）
- 决策：B1 Daily 脱钩盲评 7.67<9 **不触发回炉分支**，带缺陷闭环——缺陷全入抱怨清单（断料降级模板 + snapshot 材料级 gap 上报，NOW 主线#3）；基础设施审计 A2（poller 超时 15→20min + SuccessExitStatus=75）**并入 W2' 迁移第5步**统一 apply，不在老仓单独 apply；A5 Windows PS1 常量低优先、owner Windows 侧手动；BUG-004 描述语义等迁移第一刀合入后再修。
- 为什么：B1 归因二轮实证同条件 9=9 打平、差距全在带伤班次（装配 bug 已修），重跑三天门无增量；A2 提前 apply 会在迁移重指向时二次 apply，合并省一刀；BUG-004 在移植闭包内，先动会干扰闭包计算。
- 否决了什么：B1 从严回炉重跑三天门；A2 立即单独 apply；BUG-004 迁移前抢修。
- 状态：active · 证据：blind-eval-daily-l1-20260829/打分表.md 归因剖析节；NOW 待办队列。

## D-025 · 2026-08-29 · W2' 新仓移植执行拍板：主线功能在新仓施工（确认 D-016/§0.5.6 ③b）
- 决策：「继续迁新仓还是原地」二选一，owner 拍板**执行新仓**——按 rebaseline §0.5.6 ③b 启动 `~/fin-core` 移植，老仓过渡期冻结只跑生产，完全归档等 P5；施工顺序并入审计 F17/F18 纠正（release 门解耦；停班→迁移校验→重指向启用），见 docs/design/new-repo-migration.md；核心施工前先过设计门（D-023）。
- 为什么：深化重写是核心链路大改，在新仓写避免老仓写完再搬一遍；数据与代码彻底分离，绝 BUG-007 类双轨问题；W2 原地手术已在老仓跑通、生产稳定，移植不重做功能只搬成品。
- 否决了什么：「原地继续」——承认现状、不建新仓，把干净化无限期推迟。
- 状态：active · 证据：rebaseline §0.5.6；infra-audit A3；docs/design/new-repo-migration.md。

## D-027 · 2026-08-30 · 设计门/外部审计为 CC 专属，codex-open 全部自己完成（owner 拍板）
- 决策：设计门与外部 agent 审计仅 CC 会话执行；codex-open 不设设计门、不做
  外部审计，全部自己完成。家规 AGENTS.md 外部审视节改为 CC 专属并新增审查
  机制归属行，GLOSSARY 外部审视/设计门/外援触发三行同步。
- 为什么：owner 明确「codex-open 不需要设计门，不需要外部 agent 审计，全部是
  自己完成，只有 CC 才有设计门/审计门」。
- 否决了什么：继续把外部审视三触发无条件套在 codex-open 会话（打断其自主完成）。
- 状态：active · 证据：AGENTS.md 审查机制归属；docs/GLOSSARY.md；本条目落盘即 commit。

## D-028 · 2026-08-31 · B2 二轮复盲评闭环 + GLM 额度耗尽暂时关闭（owner 拍板）
- 决策：深化二轮复盲评（同协议新 seed，14 样本、四评审无上下文、三维 1-10）
  总均分 **7.59 > 7**，预注册闭环触发，深化主线闭环、deep-read 板 B 升「在用」；
  缺票稳健性实测最坏 7.48 仍 >7。同日 owner 裁：GLM 额度耗尽，llm.yaml
  `glm53`/`glm53_flash`/`glm-vision` 三节点 `enabled: false` 暂时关闭，
  恢复启用只需改回 `true`（priorities/vision.chain 条目保留，禁用节点消费方自动跳过）。
- 为什么：6.83<7 → 01/03/05 prompt 调优后同协议复评，7.59 缺口闭合且非踩线；
  GLM 空响应实为额度耗尽（02 号 flash 票多次空响应），继续挂着会拖慢/污染后续链。
- 否决了什么：缺票不补（额度耗尽后无法取得，且最坏情形仍闭环）；删 GLM 配置条目
  （暂时关闭≠永久删除，保留 `enabled` 开关零成本恢复）；调低评审超时线
  （owner 08-31：最少 300s，不踩线）。
- 状态：active · 证据：`$STATE/fin-analyse/deepen-blind-eval-20260901-b2-2/`
  （打分表2.md、verbatim_check2.json、eval2_results.json）；config/llm.yaml；
  本条目落盘即 commit。

## D-029 · 2026-09-01 · 星大派每日热点/星大派人脉 纳入严格 G 闭集（owner 拍板）
- 决策：两栏目进 `classify_g_source` 精确标签与 G 工作集严格闭集；每日热点=锐评同档
  （recent_change_risk、commentary 2 交易日窗口），人脉=特刊同档（systematic_framework、
  special 30 天，owner 类比特刊）；抓取层 COLUMN_PATTERNS 与深化层 T0 rank 集合同步补列；
  存量 7 篇从「普通」纠正并重做深化（含 3 篇历史空壳）。
- 为什么：两栏目均为 teacher_original 老师原创；08-28 撤普通栏后新栏目被系统性挡在
  G 库外，每日热点连续 6 篇、人脉为方向判断——漏 G 即漏老师最新认知。
- 否决了什么：「版本强势英雄」等未确认栏目纳入（无授权、无样本确认）；每日热点按特刊档
  （每日内容时效强，commentary 档已够）；等新栏目出现再处理（漏网已发生，立即修复）。
- 状态：active · 证据：source_contract.py / g_working_set.py / zsxq_apprentice.py /
  scraper/config.py；7 篇 deep-read 产物；备份 `~/fin-data/backups/g-new-columns-20260901/`。
- 追加（2026-09-01 方案A）：每日热点 usage 细化为 `ai_summary_reference`——
  老师 AI 汇总的参考信息，非老师看法；时效档（commentary）与列闭集不变。

## D-030 · 2026-09-01 · Daily 四班推送停用，聚焦手动 CLI（owner 拍板）
- 决策：停用 fin-daily-workspace-prepare/delivery 共 8 个 systemd timer
  （`systemctl --user disable --now`），单元文件与 durable 状态机保留、可一键恢复；
  ZSXQ 采集不动。推送改由问询环境生成的设计已归档（D-031 待办），后续再做。
- 为什么：当前推送由 fin-core L1 生产管线生成，不是问询环境产出；owner 近期聚焦
  手动 CLI（finqa/finqai/finqac），先停推避免噪音与错误面（今日 close/postmarket
  已 failed）。
- 否决了什么：删除单元或状态机（保留恢复能力）；立即实现问询环境生成器（归入待办）。
- 状态：active（停用中）· 证据：`systemctl --user list-timers` 已无 fin-daily 条目；
  NOW.md 生产声明；本条目。

## D-031 · 2026-09-01 · Daily 推送改由问询环境生成（设计已定，实施待办）
- 决策：Daily 生成器从 L1 直调换回问询环境（CC 默认/codex 备选），兜底链
  `CC(glm)→codex(ds)→L1 直调→降级通知` 只生效于推送；手动 CLI 不参与自动兜底；
  投递/durable 层零改动。
- 为什么：owner 认为推送应由问询环境产出；兜底链保证 glm 关闭时推送不断。
- 否决了什么：删除 L1 兜底（会断推送）；手动 CLI 接入自动兜底（保持直连单旋钮）。
- 状态：待办（owner 09-01 指示先聚焦手动 CLI，实施顺序见设计稿）· 证据：
  docs/design/daily-consult-agent-generator.md（commit 87d0f1d）。

## D-032 · 2026-09-01 · P5 飞书家人候选方案 A：Hermes 直接当问询 agent（owner 拍板）
- 决策：P5 飞书家人主路线定为候选方案 A——Hermes 作为问询 agent 的第三个客户端角色，
  直接工作在 consult-agent 目录（同 CC/codex 的「目录即身份」）；落地时人格/工具/记忆
  三缝同源（SOUL 引用或等价 CLAUDE.md、同 MCP 冻结契约、记忆规则同款），接入飞书前按
  P1 六题级验收。方案 B（Hermes 只当传输层、用 consult-ask 调问询 agent）降为备用。
- 为什么：Hermes 调问询 agent 的嵌套模式问题多（旧 fin profile 前台委托 Skill 质量/
  超时不稳）、使用不便；Hermes 直接当问询 agent 少一跳，飞书多轮会话天然就是问询会话，
  且与「目录即身份」同构。
- 否决了什么：Hermes 当前台 + consult-ask 嵌套委托的默认路线（问题多、使用不便）；
  在 CLI 使用质量/效果未跑透（NOW 主线1/2、D3 未过）前开工 P5（仍按 D-020 条件开放）。
- 状态：active（候选方案 A，大概率采用；P5 前再设计细化）· 证据：本条目；
  NOW.md 板 A P5 行。
- 追加（2026-09-01）：飞书传输直接复用 Hermes 既有 gateway（本体一直运行、未停），
  P5 不新建传输面；新工作只在问询脑侧同源化（profile 人格/MCP/记忆）。

## D-033 · 2026-09-02 · ZSXQ 标的评分注册表 + 参考/G 窗口分级落地（owner 拍板）
- 决策：①普通栏研报图片评分表（利好度/共识度等）在 ingest/回填时解析一次，
  落 `instrument_scores.jsonl` 维护列表（带文章日期/source_id/方向分，
  >10 归一化 ÷10；缺字段→needs_review 人工闭环）；回填范围=能量评分≥7 +
  60 天窗口（137 篇，产出 407 条）。②新增 thin-server 工具
  `read_instrument_scores`（默认窗口内，历史由“历史/演变”触发）与
  `read_article_search`（KnowledgeQueryService 全文检索，补行业点评）。
  ③G 车道窗口新值：锐评/每日热点 4 交易日、特刊与新类别（凤仙郡/人脉/
  版本强势英雄）45 天、好问题 20 天、其他 60 天；reference 车道普通 60 天、
  Q&A 20 天，取代“当天才注入”。④门槛：普通栏/Q&A 能量评分 <7 跳过
  （无评分按不满足处理）；fin-core cdp_scraper 已改，**Windows 侧待 owner
  通知后实施**。
- 为什么：问询实弹暴露 read_ready_evidence 对三只标的/行业点评取不到料——
  评分只在图片里（0/39 正文含分）且通道只收当天；结构化注册表 + 分层窗口 +
  全文检索共同补上“评分/行业文章可查”。
- 否决了什么：把 QA 变体表头（利润率/财务评分/竞争评分/市场认可度）猜映射
  进利好度/共识度（语义有歧义，留 read_article_search）；回填全库 241 篇
  （窗口外历史交给增量与自然滚动）；查询默认不限窗口（与注入时效口径分裂）。
- 状态：active（fin-core 侧已交付，全量 3009 绿；Windows 增量门槛待 owner
  通知）· 证据：config/g_context_windows.json + config/zsxq_reference_windows.json；
  fin_analyse/ingestion/instrument_scores.py、knowledge/article_search.py、
  read_capabilities；`instrument_scores.jsonl`；design/instrument-score-registry.md。

## D-034 · 2026-09-02 · 宏观统一接口 A：ZSXQ 宏观 + 外置大脑 + search_web 补充（owner 拍板）
- 决策：宏观查询走统一入口 `read_macro_brain`——聚合 SharedKnowledgeBrain
  书卡（methodology_memory/external_reference/framework，排除 MARKET_DATA）、
  ZSXQ 宏观参考（普通栏市场复盘/宏观问答 + 每日热点标注 ai_summary，G 层
  栏目不进）、search_web 联网补充（默认 guided=模型执行并带来源，配置可切
  auto=接口内走智谱 web 桥）。条目带 effective_window/impact_scope/priority，
  按 priority 排序。宏观识别=离线增量打标 macro_index + 规则版本，人工校准
  仅首次/规则变更/反馈回流三处；read_g_context.external_brain 槽复用同实现。
- 为什么：external_brain 空槽与“外置大脑书本卡片没接入”合并为一个诉求；
  宏观是语义判断，需要维度化 + 自动增量 + 校准闭环，不能靠在线每次全库猜。
- 否决了什么：把 ZSXQ 普通栏全部当宏观（个股/行业点评走 read_article_search/
  read_article）；每天人工校准；接口内默认 auto 联网（额度/时延不可控）。
- 状态：active（设计定稿；候选清单校准 → 打标器 → 接口依次施工）· 证据：
  docs/design/macro-brain-interface-a.md；NOW 主线 3.1-3.3。
