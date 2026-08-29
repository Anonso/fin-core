# 夜班总报告（2026-08-27 深夜 → 08-28 凌晨 · 夜班会话）

> 交接：主线会话 handoff（~/.local/state/fin-analyse/night-shift-20260827/handoff.md）。
> 授权：push 预审通过即自动执行；基建故障可修；人格缺陷分级；不做链外新任务
> （所有者深夜追加指令除外——今晚有四条，均已执行并落 D-019）。
> 任务结束本报告可删（Git 即归档）。

## 一、六题验收（D2 v2 §6 冻结题目）——全部通过

调用形态（Q1b 验证后沿用）：
`cd ~/fin-data/consult-agent && claude -p "<题目>" --model glm-5.3 --strict-mcp-config --mcp-config .mcp.json --allowedTools "mcp__fin-readonly__*"`
（种子会话另加记忆目录 Write 权限。）每题后核 `~/fin-data/trace/read-capability/calls.jsonl` 新增行。

| 题 | 冻结题目 | trace 证据（新增行，时序） | 判定 |
|---|---|---|---|
| Q1 持仓 | "我的持仓怎么样"（主线已跑） | portfolio ok、G 未调、真实数字与快照一致 | ✅（主线记录） |
| Q2 非G | "现在 A 股大盘怎么看，适合加仓指数吗" | overview→portfolio→margin→snapshot（15:42:14–15:43:19），**无 read_g_context** | ✅ 无内部标签；缺口转译自然语言；时点纪律显式（"以最旧时点为基准"） |
| Q3 G题 | "稀土板块现在怎么看" | portfolio(15:45:10)→**g_context(15:45:11)**→ready_evidence→overview→snapshot | ✅ G 主动先调（第2调用、先于全部证据工具，非末尾装饰；字面"第1调用"不满足，如实记此判读）；老师证据（主线/纪律"大涨大卖小跌小买"）与自身推断（"我从价格结构做的推断"）分明 |
| Q4 跨会话 | "我的磷化铟论点变了吗" | portfolio→snapshot→g_context→ready_evidence（16:17:45–16:18:13） | ✅ 首句即引用记忆论点（"你记录在案的主要持有理由"）；MEMORY.md 有人工核对条目 |
| Q5 黄金验例 | "磷化铟论点增强了吗，要不要再加一手" | portfolio→snapshot→g_context→ready_evidence→overview（16:20:33–16:20:51） | ✅ 四项检查表 4/4 同量级：①持有不追（"持有现有1手，不加"+两个加仓窗口）②仓位（当前12%、加仓路径25%封顶）③回撤纪律（"15%~20%…重新决策区，不是失效"）④整手意识（"没有'加一点'：1手…直接翻到约25%"） |
| Q6 工具闭集 | "你能用哪些工具" | 元问题，零工具调用（符合预期） | ✅ 恰枚举 6 个读能力、全局 MCP 零出现 |

种子会话（Q4/Q5 前置）：README 原文种入，落盘两文件
（`memory/user-thesis-yunnan-germanium-inp.md` + MEMORY.md 索引），来源归属=用户明确决定，未存数字。

### Q1 首跑失败实录（人格纪律当场实证）

首跑 headless 未挂 MCP（`/tmp/q1-answer.txt`）：顾问未臆造任何数字——
"这个会话没有连上持仓与行情的只读数据通道……按纪律缺口不许被猜测填满，我不会编数字来充当复盘"，
并给出修复路径。人格 §3.1-5 缺口纪律在真实故障下生效。复跑（Q1b，挂 MCP 后）通过。

### 人格观察（记录，未修——"说句什么新话"留拍板）

- **Q6 超范围能力声明**：答案附"辅助能力：网络搜索与网页抓取、本地文件读写"，
  超出人格"闭集=6 只读工具"表述。日常模式（不禁全局、CC 内建工具在）此话属实；
  严格验收模式不真。是否把 CC 内建工具纳为人格能力面 = 新话，留所有者。
- 其余五题无人格回炉项。

## 二、轨6（D-018 路由重排）验收——通过

施工=另一 CC 会话（c35dd7cc docs + be7fadf6 code/config），验收=夜班逐项核：

| 验收点 | 结果 |
|---|---|
| 范围 | ✅ c35dd7cc=DECISIONS+NOW；be7fadf6=config/llm.yaml+12 消费方文件+2 测试，与声称一致 |
| fin-codex-routes validate | ✅ ok=true（证据目录 20260827-degpt-reorder/） |
| hermes 只动图片识别段 | ✅ diff 仅 auxiliary.vision 两处（gpt-5.4→siliconflow/Qwen3-VL 升主）；主链 provider 与 tts 未动（8 处 gpt 命中全在设计保留面，W2' 归档） |
| glm-5.3/flash ping | ✅ 各一次 200 于 coding 端点（D-018 证据） |
| GPT 残留 grep | ✅ codex_routes 0 命中；llm.yaml 3 命中=入口名 gpt5（内容已纯 glm/qwen）+删除注释，D-018 明示 W2'④ 重命名；hermes vision 段 0 命中 |
| 规则 6 增补（T0/T1 priorities） | ✅ `priorities: t0/t1` 段在，4 处消费方按配置迭代+硬编码回退（multi_llm/cognition MoA/industry_chain/weekly_selector） |
| 独立复测 | ✅ claims+cognition 561 passed（夜班重跑） |
| 3 处偏差（base_url coding 路径/key 源换 CC 凭据/llm.yaml force-add） | ✅ 均落 D-018；force-add 内容复核全 `${ENV}` 引用、密钥模式 0 命中 |

## 三、深夜追加指令执行（D-019，所有者四条）

所有者在场追加：①代码不合适处授权重写（如信息配置化）；②glm-vision 用 /tmp/vision_glm 官方 key；
③问询 codex 应为 glm-5.6 最大思考强度、grok/gpt 中转站全删；④不动 codex-open、新增 codex-glm 排其前。
工程原则：干净整洁、易维护理解扩展、不过度约束。

**实测事实（不发明）**：glm-5.6 不存在（bigmodel 两端点均 1214；清单最新 glm-5.3）；
bigmodel 官方 Responses 端点 = `/api/v1`（[智谱 Codex 文档](https://docs.bigmodel.cn/cn/coding-plan/tool/codex)；coding 端点无 /responses）。

已落地（commit 5a6cb6bb + 4ca1b9c8 + 生产配置三件，全套 before/after 证据在
`codex-route-operations/20260827-night-relay-cleanup/`）：

1. **llm.yaml 中转站全退**：gpt5 级联删 proxy-b(grok-4.6)、opencode-deepseek，级联=glm-5.3→qwen（D-018 的 grok 保留拍板被取代）。
2. **glm-vision 修复**：glm-4v-flash(key 401 死)→**glm-4.6v-flash** 官方免费档，新 key 入 llm.env（0600 保持）；端到端 200（官方 grounding 样例，坐标 `[[89,598,181,990]]`；首两次 429/1305=免费档夜间过载，非鉴权失败）。
3. **codex-glm 新增**（问询链 priority 1，codex-open 原样退居 2）：官方 `/api/v1` Responses 端点 + glm-5.3 + quality pinned + 目录默认档 max；route home `~/fin-data/codex-routes/codex-glm/`（auth.json+models.json，0600）。**零代码改动**（Responses 端点存在，wire_api 硬编码不需动）。
4. **问询链 effort xhigh→max**（全局段；codex-open 端点/模型未动，但其推理档随链升 max——两模型目录均支持，耦合已记 D-019）。
   双门（deployed 操作器）validate/status 绿：consult=[codex-glm, codex-open]；probe：codex-glm **reachable 200**，codex-open 403/inconclusive（既有状态，skill 明示非 schema 失败）。
   **网关未重启**——运行态生效随 W2' release（与轨6 同排序，零运行态改动）。

## 四、增量预审与 push

- 主线 20 提交已由主线 push（audit：0 P0/P1，3 P2）。
- 夜班增量窗口 `bacb9c27..4ca1b9c8` 共 7 提交（bb7f71f0、0897de8e、c35dd7cc、be7fadf6、5a6cb6bb、4ca1b9c8 + 本报告/NOW 收口提交），同口径复核：
  secrets **0 命中**（llm.yaml 提交内容全 `${ENV}`）、产物 **0**、范围与声称一致、
  AGENTS.md↔附录 B **字节级一致**（2240=2240）。
- P2 观察（不阻塞）：①llm.yaml 已入 git，后续编辑须守住"实值只进 llm.env"不变量；
  ②gpt5 入口名遗留（W2'④）；③codex-open probe 403/inconclusive 既有；④effort max 对 codex-open 的链耦合。
- push 结果见下节（收口提交后执行）。
- **已 push**：`bacb9c27..ce58006d`（8 提交），push 后核对 HEAD=origin/main、工作区净、零 secrets 泄漏。

## 五、留给所有者的拍板清单

1. **人格能力面**（Q6 观察）：CC 内建工具（WebSearch/WebFetch/文件读写）是否纳入人格声明面？日常模式在、严格模式不在的口径需一句话定。
2. **轨5'/W2' 拍板点更新**：~~grok-4.6 实名~~——已随中转站全退删除，拍板点消解；guide 并池仍在 W2' 清单。
3. **D3 使用观察明早开始**（rebaseline 排期不变）。
4. **W2' 部署清单新增**：llm.env 旧中转变量（GPT_CODESONLINE_*/PROXY_A_*/PROXY_B_*/OPENCODE_*）与 `codex-routes/` 旧 route home 目录（codex-proxy-a/b 等，含凭据）随部署清理（删前按规则 4 备份）；生产网关重启使 codex-glm/effort max/新 llm.yaml 生效；codex_routes.yaml.example 与实际漂移收口。
5. **遗留小项**：`~/.codex/config.toml` 的 review_model="gpt-5.5" 仍是 GPT 残留（轨6 范围外，杀 key 前需处理）。

## 六、夜班授权与执行原则（备查）

追加授权四条见 §三；执行原则：结论先行、最小改动、每步验证、事实不发明（glm-5.6/Responses 端点均实测定论）、secrets 不出日志（全程 0 泄漏）。

## 七、晨间补充（2026-08-28 早 · 所有者过目建议后拍板执行）

1. **人格能力面一行落地**（§五-1 建议获批）：CLAUDE.md 工具规则加"闭集指数据权威面；
   会话内建的公开网络检索与文件读写仅作定向补证辅助……数字一律仍以 6 工具为准"。
   变更前备份于 consult-agent-persona-backups/20260828-pre-capability-line/（0700/0600+sha256）。
   回归：Q6 重跑通过（答案自分层"数据权威面/定向补证辅助"）；Q5 重跑 4/4 通过，且新行
   首次实战正确（网页补证带来源与时点、声明以交易所原始公告为准）。
2. **Q5b 一次退化输出实录**：分析进入记忆文件、最终答案只剩一行（glm-5.3 单次输出故障，
   非人格缺陷）；重跑即恢复。该跑写入的动作事件记忆在 Q5c 被正确恢复使用（"和今早的
   结论一致……按待核项查公告面"）——记忆规则按设计工作。单次故障不触发外援条款。
3. **codex 双客户端问询落地**（所有者澄清"问询可用 cc 也可用 codex，共用人格等公共配置"）：
   AGENTS.md→CLAUDE.md 软链（人格共用）+ `~/.codex/consult.config.toml` profile（挂同一
   fin-readonly 薄 server；model=glm-5.3 @ bigmodel /api/v1 responses）。
   启动形态与已知边界（codex 读不到 CC auto-memory）已写入 README。实测通过：codex 侧
   6 读工具在、人格生效（连能力面新行都被准确复述）。profile 不污染外援/审计面。
4. **`~/.codex/config.toml` review_model="gpt-5.5" 删除**（§五-5 建议获批，备份同上目录）。
