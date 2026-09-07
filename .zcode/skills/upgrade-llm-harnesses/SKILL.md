---
name: upgrade-llm-harnesses
description: Upgrade the WSL LLM harness CLIs behind the 问询链 (finqa-chain legs) and 设计门/审计门 reviewer chain (codex_open.sh) — codex (standalone + npm copy), cmd (Command Code), claude (CC), opencode. Use when the owner asks to 升级/更新 harness 或 CLI 到最新, when a precheck/probe suddenly reports 版本钉 mismatch or the cmd reviewer silently falls to glm 替补, or for post-upgrade verification. Covers install roots, per-harness upgrade channels, the cmd-update wrong-prefix trap (update lands in ~/.hermes/node while the PATH symlink stays old = 假升级), cmd_version_pin hard-equality discipline, and the finqa_chain --node probe ladder. Not for enabling/disabling legs or channels (see manage-llm-pool), launcher/runtime code changes (see fin-release-launcher-chain), or zcode itself.
---

# Upgrade LLM Harnesses（问询链/评审门 CLI 升级）

不变量：升级=改生产行为，**只在 owner 明确要求时执行**。zcode 不在本 skill
范围（它是执行会话自身运行时，且仅弱测试腿不入生产链序）。config/钉值操作
走本 skill；要动 launcher/runtime 代码才走 `fin-release-launcher-chain`。

## 1. 盘点：四个 harness 的实体与升级通道

| harness (PATH 入口) | 实体 | 升级通道 |
|---|---|---|
| codex（standalone，问询链生效份） | `~/.local/bin/codex` → `~/.codex/packages/standalone/current/bin/codex` | `codex update` |
| codex（npm 副本，hermes 侧） | npm 全局 prefix = `~/.hermes/node` | `npm i -g @openai/codex@latest` |
| opencode | `~/.local/bin/opencode` → `~/.hermes/node/bin/opencode`（同 prefix） | `npm i -g opencode-ai@latest` |
| claude | `~/.local/bin/claude` 纯透传 shim → `~/.hermes/node/bin/claude`（同 prefix） | `npm i -g @anthropic-ai/claude-code@latest` |
| cmd（问询主腿 + 评审门 harness） | `~/.local/bin/cmd` symlink（指向见 §3） | `cmd update` + §3 陷阱处理 |

动手前先对照最新版：`npm view <包名> version` 查四个 npm 包；已最新的
（如某次 claude）直接跳过，不重装。npm 三件套可并一条命令装。

## 2. 版本钉纪律（cmd 专属，硬约束）

`config/finqa_nodes.yaml` 顶层 `cmd_version_pin` 是单源，读方两个：
`scripts/codex_open.sh` precheck_cmd（**硬等于**比对）与
`scripts/persona_regression.sh`。实际版本 ≠ 钉值 ⇒ cmd 评审者不可用，
fail-closed 落 glm 替补——评审链静默降级，不报错，容易 unnoticed。

所以 **cmd 升级与钉值 bump 必须同 stroke**：升完立即改钉值，行尾注明日期
与缘由，单独成 commit（显式路径 `git add config/finqa_nodes.yaml`）。

## 3. cmd update 陷阱：装对地方才算升级

`cmd update` 装进 npm 默认全局 prefix（`~/.hermes/node/lib/node_modules/
command-code`）且不建 bin 链接；若 PATH symlink 还指着旧树（历史安装位
`~/.local/share/command-code`），`cmd --version` 会继续报旧版——**假升级**。
升完必须核对：

```sh
cmd --version   # 与 update 输出比对，不一致即中陷阱
ln -sfn /home/ypk/.hermes/node/lib/node_modules/command-code/dist/index.mjs \
    /home/ypk/.local/bin/cmd
```

旧树保留不删（数据是产品代码是工厂；回滚 = symlink 指回旧树即可）。
以后 `cmd update` 都落 hermes prefix，与 symlink 指向自洽。

## 4. 验证阶梯（逐级全绿才算完）

1. 版本核对：动过的每个 harness `--version`；`cmd --version` == 钉值。
2. `cmd status` → `✔ Authentication verified`（评审链 precheck 第 4 条）。
3. 生产腿探针各一发（一律无头）：

   ```sh
   scripts/finqa_chain.py --node commandcode "连通性探针:只回复两个字——正常"
   scripts/finqa_chain.py --node codex       "连通性探针:只回复两个字——正常"
   scripts/finqa_chain.py --node claude      "连通性探针:只回复两个字——正常"
   ```

   判据：正文「正常」+ `served-by <leg>` + `rc=0`。stderr 见
   `audit skipped: No module named 'fin_analyse'` 属正常——链内审计步
   fail-open（系统 python 直跑 launcher 时 import 不到仓包），不影响答案
   字节与 rc，判据不看它。
4. 评审链 precheck 逐条核（bin 可执行 / 池 model 可读 / 版本==钉值 /
   auth 四条件）——不必真跑整轮评审（成本重），四条件全真即过。

## 5. 落账

钉值 bump 单独 commit，message 含旧→新版本、symlink 处置与三腿探针结果
（served-by + rc），不另建台账。样例见 commit 414577e。
