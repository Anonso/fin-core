---
name: run-design-gate
description: Run the CC external review gate (设计门/吓人 diff/外援) through the frozen-packet procedure — assemble packet with the fixed four questions, launch codex-open reviewer with liveness watchdog, handle reviewer fallback, adjudicate findings into the design doc, and record elapsed/findings/adopted/reviewer. Use when a core design doc is about to touch code, a scary diff is about to merge, or a problem survived 2+ fix attempts; also when the user says 走设计门/外审/外部审视.
---

# Run Design Gate（外部审视执行程序）

机制归属与三触发定义看 AGENTS.md「外部审视」节（评审者链现役配置以该节为准，
D-045 起：cmd·deepseek-v4-pro 主 → glm·glm-5.3 替补，评审者入口自动 fallback）。
本 skill 只固化**执行程序与环境坑**，不复制机制条款。触发即一次，不设轮次。

## 0. 审计门触发判定（吓人 diff 合入前；owner 2026-09-05 拍板）

判据三轴 = 大改动 / 影响范围大 / 功能重要。**每次合入前跑一遍清单，命中任一
即走审计门**（本 skill 同流程，packet = diff + 提交清单，见 §1 审计门形态）：

- **R1 规模**：diff 合计变更 >300 行（增删合计）或 >10 个文件。
- **R2 门面路径**：触及 `scripts/`、`config/`、`AGENTS.md`、`.claude/skills/`、
  `.zcode/skills/`（行为载体与合同面）。
- **R3 生产语义**：触及 fin_analyse 生产模块、durable schema/迁移、systemd/
  部署单元、网关配置。
- **R4 会话裁量**：未命中 R1-R3 但功能重要（如 prompt/契约语义变更），可触发；
  记录一句理由即可，不强制。

豁免：纯文档（`docs/`、`*.md`，AGENTS.md 除外）、台账、本地态（`.claude` 本地
配置、`$STATE`）。设计门与审计门**不互相豁免**：设计门过 ≠ 免审计门（实现可
偏离设计——D-045 门形干跑实证，cmd 审实现层抓出 4 个设计门不可见的 P2）。

判定与执行顺序：合入前判定 → 命中 → 先合入后审计亦可（补跑形态，packet 标注
「补跑」与提交 sha），但裁决发现的 P1 必须落修复提交。

## 1. 冻结 packet（先于一切）

台账目录：`~/.local/state/fin-analyse/design-gate/<slug>-<YYYYMMDD>/`

```bash
mkdir -p ~/.local/state/fin-analyse/design-gate/<slug>-<YYYYMMDD>
```

packet.md 结构（固定前缀吃 provider 缓存，正文附被审对象全文）：

1. 头部：一句话背景 + 触发类型（设计门/吓人 diff/外援；审计门补跑须标注
   「补跑」+ 提交 sha）。
2. **固定四问**（逐条回答，每问 发现/无发现 + 一句依据）：
   - Q1 契约破坏？
   - Q2 durable state 时序/幂等？
   - Q3 引用闭包漏删？
   - Q4 相对直接 Agent 退化？
3. 输出格式约定：每问一节；发现按 P1（必须修）/P2（应修）/P3（可选）分级，
   附设计稿节号或 diff 文件行。
4. 评审范围约束：只评本对象与波及面。
5. 附录：**设计门** = 设计稿全文；**审计门** = 按受影响文件 allowlist 生成的
   diff（`git show <sha>` 逐提交）+ 提交清单（sha/主题/命中规则）。禁止整仓 diff。

## 2. 发射与看门狗（长时外呼纪律）

```bash
timeout 3700 scripts/codex_open.sh exec --skip-git-repo-check -C /home/ypk/fin-core \
  "$(cat <packet>.md)" > <review>.md 2> <review>.stderr
```

（`--skip-git-repo-check`/`-C` 由入口翻译层按 profile 吸收/透传，调用方不用改。）

后台跑 + Monitor 看门狗（90s 探针）：

- **超时下限 1 小时**（`timeout 3700`）：900s 级短超时会误杀健康评审
  （08-31 实证，评审正常耗时 7-8 分钟但启动+推理波动大）。
- 报警条件二选一即停：运行 ≥2400s 零产出（停滞）；或产物为空且 stderr 有
  `ERROR:` 行。
- **评审者 fallback 是入口内置行为**（D-045）：cmd 主评审者 precheck/运行失败
  时入口自动换 glm 重发同参，review.md 出现 `⚠ REVIEWER FALLBACK: cmd → glm`
  横幅 + stderr 有 `[cmd partial]` 半份输出 + fallback.tsv 追加一行都属正常
  流程，不重发、不当失败。裁决记录的评审者字段以此为准（横幅在 = glm 服务的）。
- **两评审者全挂**（入口 exit 78）：按 retry.ts 约定原 packet 重发一次；
  再挂则上报 owner 换窗口，不连续硬撞。

## 3. 裁决（评审只产发现，裁决归 CC）

1. 逐条读 review.md，每条裁决 采纳/不采纳 + 一句理由，落回设计稿对应节
   （标注〔外审修正 Q1-P1〕样式，保留原意可追溯）。
2. 设计稿升 vN 提交，commit message 记 elapsed_seconds/发现数/采纳数/评审者
   （elapsed = retry.ts 与 review.md mtime 之差；台账即证据）。
3. 施工后如需第二道（施工后 diff 复审），同 packet 流程再来一次，仅限
   设计门要求逐条落实核对时。

## 4. 环境坑（2026-09-04 双门实证 + 2026-09-05 链重构）

- packet 用 `$(cat file)` 位置传参；非 TTY 自动补 exec、stdin 喂 EOF、旗标
  翻译由 `scripts/codex_open.sh` 处理，勿绕过入口直调各 harness。
- 评审输出到 review.md 后 stderr 里的 token 计数行无害；判失败看产物是否
  为空 + ERROR 行（fallback 横幅与 [partial] 块不是失败）。
- 评审者链 cmd→glm 已异构（deepseek vs GLM），旧「双评审者同源期独立性打折」
  注记作废；外援双模并行校准需要时仍可手动并行跑两入口。
- cmd 主评审者是闭源账号客户端：版本被钉定（升级需改脚本 CMD_VERSION_PIN 并
  重跑验证阶梯）、会话过期看 `cmd login`；权限放大旗标（--yolo/--tools-all
  等）入口层 fail-closed，勿在 packet 调用里传。
