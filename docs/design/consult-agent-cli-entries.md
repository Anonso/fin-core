# 问询手动 CLI 入口与模型单旋钮（短设计）

日期：2026-09-01 · 状态：施工中 · 级别：公共入口（规则 5 按核心处理）

## 需求

用户手动在问询工作区 `~/fin-data/consult-agent/` 启动交互/一次性 CLI，支持两个客户端：

- CC（Claude Code）：模型用 glm-5.3。
- codex：模型用 deepseek-v4-pro（与 codex-open 同款 ds 端点），可内部切
  deepseek-v4-flash。
- 两个客户端的模型都必须单旋钮可换，换模型只改配置不改命令。

## 现状与缺口

| 入口 | 现状 | 动作 |
| --- | --- | --- |
| finqa（CC 交互） | 有，glm 单旋钮在 `.claude/settings.local.json` | 不动 |
| finqa-x（CC 一次性） | 有 | 不动 |
| finqac（codex 一次性） | 有但模型是 glm | 改为 ds（config 单旋钮） |
| finqai（codex 交互） | 无 | 新增 |

codex 侧阻塞点：consult-agent 不是 git 仓库，交互 TUI 需要配置
`skip_git_repo_check = true`；模型 catalog 需换成 ds 双档（pro/flash），
`supports_search_tool = false` 保持 MCP 工具直接注入（与 glm catalog 同惯例）。

## 入口契约（本期）

- `finqa` / `finqa-x`：不变，CC 交互/一次性，模型读 `.claude/settings.local.json`
  的 `env.ANTHROPIC_MODEL`。
- `finqai` / `finqac`：codex 交互/一次性，模型读 `.codex/config.toml` 顶部
  `model` 一行；key 从 `~/.config/fin-analyse/llm.env`（GLM，供 zhipu-web MCP）
  与 `~/.local/share/opencode/auth.json`（OPENCODE_GO_API_KEY，供 ds provider）
  读取，不入配置文件。
- 换模型：CC 改 settings.local.json 的 `ANTHROPIC_MODEL`；codex 改
  config.toml 的 `model = "deepseek-v4-pro"` ↔ `"deepseek-v4-flash"`。

## 预留（本期不实现）

“其他 agent（含旧版 Hermes/未来飞书）调用问询 agent 并会话追问”作为待办：
统一契约暂定为 `consult-ask --agent cc|codex [--session <id>] <问题>`，输出
JSON `{session_id, text}`；CC 侧 `claude -p --resume`，codex 侧
`codex exec --json resume <id>`（旧 runtime Phase 3D 已验证的形态）。默认
agent 可配置，飞书加入后默认 cc。本期不建该脚本。

## 验证

- `bash -n ~/.bashrc` 语法通过；
- `.codex/models.json` 含 deepseek-v4-pro / deepseek-v4-flash 且
  `supports_search_tool=false`；
- `CODEX_HOME=... codex exec --help` 配置可加载；
- 真实冒烟：codex 一次性回答“只回复 OK”，确认 ds 端点、MCP、人格加载链路。
