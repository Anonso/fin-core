---
name: run-design-gate
description: Run the CC external review gate (设计门/吓人 diff/外援) through the frozen-packet procedure — assemble packet with the fixed four questions, launch codex-open reviewer with liveness watchdog, handle 429 backoff, adjudicate findings into the design doc, and record elapsed/findings/adopted. Use when a core design doc is about to touch code, a scary diff is about to merge, or a problem survived 2+ fix attempts; also when the user says 走设计门/外审/外部审视.
---

# Run Design Gate（外部审视执行程序）

机制归属与三触发定义看 AGENTS.md「外部审视」节（评审者现役配置以该节为准，
当前 codex-glm · glm-5.3 · max）。本 skill 只固化**执行程序与环境坑**，
不复制机制条款。触发即一次，不设轮次。

## 1. 冻结 packet（先于一切）

台账目录：`~/.local/state/fin-analyse/design-gate/<slug>-<YYYYMMDD>/`

```bash
mkdir -p ~/.local/state/fin-analyse/design-gate/<slug>-<YYYYMMDD>
```

packet.md 结构（固定前缀吃 provider 缓存，正文附被审对象全文）：

1. 头部：一句话背景 + 触发类型（设计门/吓人 diff/外援）。
2. **固定四问**（逐条回答，每问 发现/无发现 + 一句依据）：
   - Q1 契约破坏？
   - Q2 durable state 时序/幂等？
   - Q3 引用闭包漏删？
   - Q4 相对直接 Agent 退化？
3. 输出格式约定：每问一节；发现按 P1（必须修）/P2（应修）/P3（可选）分级，
   附设计稿节号。
4. 评审范围约束：只评本对象与波及面。
5. 附录：设计稿全文，或 diff + 提交清单。

## 2. 发射与看门狗（长时外呼纪律）

```bash
timeout 3700 scripts/codex_open.sh exec --skip-git-repo-check -C /home/ypk/fin-core \
  "$(cat <packet>.md)" > <review>.md 2> <review>.stderr
```

后台跑 + Monitor 看门狗（90s 探针）：

- **超时下限 1 小时**（`timeout 3700`）：900s 级短超时会误杀健康评审
  （08-31 实证，评审正常耗时 7-8 分钟但启动+推理波动大）。
- 报警条件二选一即停：stderr 出现 `429 Too Many Requests` / `401` / `ERROR:`
  且产物为空；或运行 ≥2400s 零产出（停滞）。
- **429 退避 10 分钟重发**（owner 2026-09-04 口径）：`sleep 600` 后原 packet
  重发一次；再 429 则上报 owner 换窗口，不连续硬撞。
- **401 = key 失效**：走 switch-codex-open-provider skill 换 profile，不在
  本 skill 内修凭据。

## 3. 裁决（评审只产发现，裁决归 CC）

1. 逐条读 review.md，每条裁决 采纳/不采纳 + 一句理由，落回设计稿对应节
   （标注〔外审修正 Q1-P1〕样式，保留原意可追溯）。
2. 设计稿升 vN 提交，commit message 记 elapsed_seconds/发现数/采纳数
   （elapsed = retry.ts 与 review.md mtime 之差；台账即证据）。
3. 施工后如需第二道（施工后 diff 复审），同 packet 流程再来一次，仅限
   设计门要求逐条落实核对时。

## 4. 环境坑（2026-09-04 双门实证）

- packet 用 `$(cat file)` 位置传参；codex 非 TTY 自动补 exec、stdin 喂 EOF
  由脚本处理，勿绕过 `scripts/codex_open.sh` 直调 codex。
- 评审输出到 review.md 后 stderr 里的 token 计数行无害；判失败看产物是否
  为空 + ERROR 行。
- 429 与 401 的处置路径不同（退避 vs 换 profile），看门狗区分开。
- 双评审者同源期（如现役 glm 且外援第二意见也是 glm）在裁决记录里注明，
  独立性打折是已知状态，恢复异构后自消。
