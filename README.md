# fin-analyse

FIN 是面向 A 股决策辅助的自用 AI 投研系统。两个产品面：

- **顾问咨询**：在终端经 codex / CC 客户端向 FIN 顾问 Agent 提问；FIN 装配老师认知（G）、行情、持仓和可追溯证据，强 Agent 负责理解、反证、综合和表达。一切 advisory-only。
- **Daily 简报**：每交易日四班自动生成，经 Hermes 消息通道投递飞书。

方向权威是 [rebaseline-20260827](docs/pm/rebaseline-20260827.md)（§0.5 为当前权威版本）；旧飞书/Hermes 咨询入口已停用（允许报错不可用）。

## 理解系统（5 分钟）

- [系统概览](docs/architecture/system-overview.md)：主链图 + 功能域一页纸。
- [术语表](docs/GLOSSARY.md)：问询链、深化、G 认知等专有名词的一句话解释与权威指路。
- 认知域深度词汇以 [UBIQUITOUS_LANGUAGE](UBIQUITOUS_LANGUAGE.md) 为权威。

## 当前状态

唯一当前状态与执行队列见 [NOW](docs/pm/NOW.md)；本 README 不复述状态。

## 入口

- [共享工程合同](AGENTS.md)：Codex 与 Claude Code 的唯一工程规则。
- [当前状态与执行队列](docs/pm/NOW.md)：唯一当前产品事实。
- [内部模块接口目录](docs/architecture/internal-module-catalog.md)：维护者向；改接口前按其规则核对。
- [用户理念与产品原则](docs/architecture/user-design-principles.md)。
- [FIN 领域内核与强 Agent 边界](docs/architecture/fin-domain-kernel-agent-runtime.md)。
- [Hermes 公共咨询合同](docs/hermes/fin-hermes-provenance-contract-v1.md)：旧飞书/Hermes 拓扑，咨询入口已停用（见 [NOW](docs/pm/NOW.md)）。
- [本次项目复盘](docs/RETROSPECTIVE.md)：历史教训，不是当前排期。

`knowledge-base/**` 是用户/领域数据，不是工程计划文档。历史 plan、spec、handoff 和执行日志已从当前工作树退休，需要时从 Git 历史或退休 bundle 查看。

## 开发验证

```bash
# 聚焦测试
.venv/bin/pytest <test-path> -q

# 按变更范围运行
.venv/bin/ruff check <paths>
.venv/bin/mypy <paths>
```

旧 `make daily` 和多 cron wrapper 已退休；Daily 现由 8 个 systemd unit（4 检查点 × prepare/delivery）+ durable 状态机运行，运行事实与完成口径见 [NOW](docs/pm/NOW.md)。
