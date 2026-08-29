# fin-analyse — Claude Code 入口

@AGENTS.md

根目录 `AGENTS.md` 通过上述 import 在每个 Claude Code 会话启动时自动加载；它是 CC/codex/opencode 共享工程合同，详见 `docs/DECISIONS.md`。

开始非简单任务前读 `docs/pm/NOW.md`（唯一当前状态与执行队列）；方向权威是 `docs/pm/rebaseline-20260827.md`。
理解系统先读 `docs/architecture/system-overview.md`（主链一页纸）与 `docs/GLOSSARY.md`（术语指路）。

CC-led 时直接执行已导入根合同中的完整规则；本文不复制第二份参数、职责或状态机。

任务只在验收、合并、worktree 删除和已合并分支删除都完成后才算结束。`.claude/memory/`、历史 plan/spec/handoff 不是当前事实源。
