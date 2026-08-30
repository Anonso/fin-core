# FIN 项目复盘：从复杂度失控回到用户结果

> 日期：2026-08-04
> 结论：这次失败主要不是模型能力不足，也不是“GPT 太厉害”。更强的模型放大了一套错误的工程激励：它不断产出局部合理的抽象、门禁和文档，但仓库没有用用户效果、WIP 数和复杂度预算来约束它。

## 发生了什么

- 用户的初衷很简单：代码 + LLM 做 A 股决策辅助，Hermes/飞书会话对应 Agent 会话，FIN 自动注入上下文，以后再自动化。
- 工程逐步变成了数百个模块、迁移权威、observer、fallback、spec/plan 和状态机；用户主链反而不稳定。
- 一份需求常被复制成 problem brief、design、plan、OpenSpec、执行 request、验收脚本、handoff、project-sync 报告和 memory，形成 6–9 份长期事实。
- 最高峰累积到 53 个 worktree、138 个本地分支、41 份 release；任务被当作“合并就完成”，没有把 worktree/分支/产物回收当成同一终态。
- 定时抓取一个月没有一天真正可用，每日决策推送也没有成功；但文档与预检多次宣称部分完成。

## 根因

### 1. 完成口径错了

项目奖励 commit、测试数、schema、门禁和部署动作，没有只奖励“用户今天实际收到了可用结果”。`code/preflight complete` 因此多次被误当为产品进展。

### 2. 每次失败都新增一层

遇到运行错误时，常见反应是增加 retry、fallback、watchdog、observer、authority 或新迁移流程，而不是回到拥有正确语义的 seam 删掉共享根因。这让“治理系统”比产品本身更完整。

### 3. 并行没有终态所有者

Agent 会话、worktree、分支、release 和 raw artifact 都容易创建，但没有人对“验收后立即删除”负责。`finish()` 删 worktree 却留分支，`supersede()` 还把残留永久合法化。

### 4. 多份指令相互冲突

`AGENTS.md`、`CLAUDE.md`、`.claude/memory`、OpenSpec、Superpowers 和旧 handoff 各自保存不同的“当前真相”，甚至对 Codex/CC 谁主导给出相反规则。新 Agent 为了安全，又会继续复制与补充。

### 5. 定时任务没有当成独立产品

定时链缺少一个 owner、一个与手动完全相同的入口、持久 run ledger、`last_success/freshness`、失败告警和幂等 replay。定时器被打开本身被当成交付，而不是开始观测。

### 6. 强模型放大了错误激励

模型能力强，意味着它能更快地生成合理的模块、测试和说明；并不意味着它会自动知道什么不该存在。没有产品出口门和删减预算时，更强模型只会更快把局部正确堆成全局错误。

### 7. 把增强层做成了答案控制层

FIN 本应给 Agent 注入可信上下文和能力，却逐渐用 schema、固定标题、状态行、来源模板和 Hermes 格式门禁接管表达。结果是安全 sidecar 越来越完整，用户答案反而比直接 Agent 更绕、更脆弱。事实、来源、权限、资源、风险和真实副作用需要确定性边界；意图理解、推理、权衡和表达不应由 FIN 再实现一遍。

### 8. 混淆了两个 Agent 的分工

Hermes Agent 本来负责飞书会话、Skill、路由和展示；FIN/Codex 分析 Agent 本来负责深度投研与最终答案。项目既没有明确写出这层分工，也没有把 Hermes session 精确绑定到 provider session，反而用历史 token、`resume --last`、强制 tool routing 和多份 Skill 补洞。问题不是“有两个 Agent”，而是两个 Agent 都部分拥有意图、会话与答案控制，却没有确定性的单答案边界。

### 9. A3 五轮审计暴露了“自检通过但证据没通过”

A3 的 findings 持续从 9 项收敛到 1 项，没有反复重开；五轮的主要浪费来自 CC 多次只完成部分修复，却把自检写成“全部通过”，以及 canary 没有真实命中目标 argv/seam、证据在候选冻结前生成或被后轮覆盖。更严重的是，审计中途新增的通用规则被倒灌为当前 slice 的新通过门，方向设计因此逐渐变成测试施工图。

改正方式不是增加更多 hook 或表单：实现前只冻结用户结果、硬边界、必要语义 seam 和高风险失败路径；局部设计与测试组织留给 writer。自检必须给 reviewer 可复算的直接证据；连续两轮未过，第三轮前先简化、缩小或重定基线，而不是继续堆补丁。新工作流经验默认从下一个 slice 生效。

## 本次改正

- 退休前建立完整 Git bundle、dirty patch、WIP tar、registry/run 和 release receipt 存档，验证可恢复后才删除。
- 本地开发收敛到一个 `main`；生产只保留 current + 一个 rollback release。
- user crontab、Hermes cron 与 systemd timer 的旧任务已全部退休；退休前发现 7 条 cron 会直接执行 dirty checkout，证明“代码身份固定”必须是生产验收的前置条件，而不是文档约定。
- 项目工程规则收敛到 AGENTS，当前事实与队列只由 NOW 拥有；架构文档与本复盘分别保存长期边界和历史教训，不承载当前排期。
- post-commit 不再生成 memory、报告或索引；project-sync 报告只写 `$XDG_STATE_HOME/fin-analyse/`。只有 operator 显式运行 capability generation 才更新唯一 `config/capabilities.yaml`。
- 并行工具只有删除 worktree 和已合并分支后才返回 `integrated`；有 superseded 残留时拒绝新任务。

## 永久反制

1. **一个用户结果，一个 owner，一条主链。** 调用方只消费 FIN 正式 interface，不在 Hermes 或 cron 里拼出第二条路径。
2. **单 Agent 先证明不够。** 没有对照证据，不增加 Agent/MoA/编排层。
3. **一个需求最多一份活动设计主页。** 完成后删除；Git 就是 archive。
4. **工作流职责由入口作用域决定。** Codex-led 与 CC-led 不共享一套默认分工；CC-led 应去重职责、复用同一 Codex thread，并避免同步等待与重复全量审计。A3 证明方向设计不能下沉为测试施工图、自审表不能替代可复算证据、验收门不能在 slice 内新增后追溯。当前操作、审计和熔断合同只见根目录 `AGENTS.md`，复盘不维护第二份规则。
5. **终态即回收。** 未删 worktree/已合并分支/临时文档的任务不是 done。
6. **产品证据高于工程证据。** 测试和 preflight 是上线前证据；真实入口的连续成功才是完成。
7. **复杂度必须付费。** 每个新抽象、状态、fallback 或文档都要说明已发生的问题和删除了什么；不能说明就不加。
8. **定时产品有 SLO。** 手动与定时同入口、run ledger、last success、freshness、alert、replay 和连续真实运行是最小合同。
9. **冻结期不偷渡功能。** 新想法只 parked，直到 NOW 退出门达成且用户明确解冻。
10. **安全形容词必须有可执行后置条件。** 只写 `owner-only` 不足以约束 Shell、Write/Edit 与不同进程的默认 umask；创建时显式收紧、结束前机械验收，具体规则只见根 `AGENTS.md`。
11. **调度入口必须枚举为零或一。** 每次部署/退休都同时核对 user crontab、Hermes profile/global cron 与 systemd timer；任一任务指向 mutable checkout 即停止上线。
12. **直接 Agent 不退化。** FIN 是辅助与增强层，不是约束层。每项 Agent/ContextPack/ProductContract/presentation/Hermes 变更先写清“增强了什么、限制了什么、为什么该限制是必要边界”，并从公共入口证明回答不比直接 Agent 更难用；答不出来就不做。
13. **双 Agent、单投研答案。** Hermes Agent 只做前台会话、轻量 Skill、路由与展示；FIN/Codex 分析 Agent 是唯一投研推理与答案 owner。一个 Hermes session generation 精确绑定一个 provider session；禁止 `--last` 和历史文本猜会话。
14. **Skill 是小卡片，不是工作流平台。** 同一用户目的只保留一个 Skill；它只说明何时委托和如何使用结果，不保存状态、不复制领域合同、不规定答案格式。数据、身份与副作用仍由代码拥有。

## 不再重复的做法

- 不再用更多 observer/authority/migration state machine 代替一个真正工作的 scheduler。
- 不再用 fresh 静默伪装 resume，也不吞掉能定位失败阶段的私有诊断。
- 不再把旧会话交接、每提交报告和 raw run 作为长期项目记忆。
- 不再因为代码已经很多，就把它当成必须继续维护的需求。
