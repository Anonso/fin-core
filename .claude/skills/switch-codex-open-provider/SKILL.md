---
name: switch-codex-open-provider
description: Switch the CC reviewer entry scripts/codex_open.sh (codex-open) between known provider profiles — codex-glm·glm-5.3 and opencode-go·deepseek-v4-pro — covering credential file, model catalog entry, the ~/.local/bin/codex-open symlink check, owner-only backup, and the exec verification ladder. Since D-045 (2026-09-05) the gate reviewer chain is cmd·deepseek-v4-pro primary with these codex profiles as fallback — switch the primary via DEFAULT_PROFILE in the script. Also run the four-step wire-protocol feasibility precheck before onboarding ANY new provider (2026-09-05 commandcode case: chat-only upstream cannot attach to codex harness). Use when the user asks to 换/切/改回/评估 codex-open 的 provider/模型/API/key; not for FIN product Codex routes (use manage-fin-codex-routes) nor personal ~/.codex/config.toml profiles.
---

# Codex-open provider 切换

`scripts/codex_open.sh` 是 CC 外部审视的评审者入口（家规「外部审视」节）。
**2026-09-05 链重构（D-045）**：入口改为评审者链——cmd·deepseek-v4-pro 主
（Command Code CLI，非 codex harness，见 run-design-gate skill）→ glm·glm-5.3
替补（自动 fallback 横幅 + fallback.tsv）。本 skill 的两个 profile 是**替补/
历史路径**：Profile A（codex-glm）= 现替补，Profile B（opencode-go）= 休眠。
换主评审者改脚本 `DEFAULT_PROFILE` 一行；评审者链语义见脚本头注释与 D-045。
换 key 改凭据文件、脚本不动（见「只换 key」节）；`~/.local/bin/codex-open`
是它的符号链接（单一事实源）。本 skill 记录已验证 profile 的可粘贴片段、踩过
的坑、验证阶梯与回滚路径。

## 第 0 步：先查包装入口（吃过的亏）

2026-08-31 事故：`~/.local/bin/codex-open` 曾是 8-30 的独立旧拷贝（非
symlink），手动 `codex-open` 走旧配置而仓脚本已是新配置。切换前后都先确认：

```bash
ls -la ~/.local/bin/codex-open   # 必须是 symlink -> /home/ypk/fin-core/scripts/codex_open.sh
```

若发现是普通文件：先 owner-only 备份进备份目录，再 `ln -sf` 指回仓脚本。
**不要**在 `~/.local/bin` 留第二份拷贝。

## 两个已验证 profile（只替换脚本中两段片段，勿动 TTY/exec 处理逻辑）

### Profile A（2026-09-05 起为替补；09-04 曾现役）：codex-glm · glm-5.3（官方 Responses 端点）

凭据段：

```bash
CODEX_GLM_AUTH_FILE="/home/ypk/fin-data/codex-routes/codex-glm/auth.json"
# 检查：非 symlink、regular file、stat '%u:%a:%h' == "$(id -u):600:1"
CODEX_GLM_KEY_VALUE="$($JQ_BINARY -er \
    '.glm_api_key | strings | select(length > 0)' \
    "$CODEX_GLM_AUTH_FILE")" || { printf 'codex-open: codex-glm credential is invalid\n' >&2; exit 78; }
export CODEX_GLM_API_KEY="$CODEX_GLM_KEY_VALUE"
unset CODEX_GLM_KEY_VALUE
```

run_codex 段：

```bash
    -c model_provider=codex_glm_review \
    -c 'model_providers.codex_glm_review.name=GLM Review' \
    -c model_providers.codex_glm_review.base_url=https://open.bigmodel.cn/api/v1 \
    -c model_providers.codex_glm_review.env_key=CODEX_GLM_API_KEY \
    -c model_providers.codex_glm_review.wire_api=responses \
    -c "model_catalog_json=${MODEL_CATALOG}" \
    -c model_reasoning_effort=max \
    -m glm-5.3 \
```

- 凭据：复用问询链 codex-glm 路由的 auth（`~/fin-data/codex-routes/codex-glm/auth.json`，
  0600，键 `glm_api_key`）——评审者与问询链同源同键，换 key 一处生效。
- 模型目录：`/home/ypk/fin-data/codex-routes/codex-glm/models.json`（glm-5.3 带
  instructions_template 与 effort 枚举 low/high/max；2026-09-04 验证阶梯三绿）。
- 2026-09-04 由 opencode-go·deepseek-v4-pro 429 限流切入；relay-19851117·gpt-5.6
  profile 已删（key 服务商侧 401 失效 + owner 裁定删除；备份在
  `~/.local/share/codex-open-backup-20260904/`，其 auth.relay.json 仅供追溯）。

### Profile B（2026-09-03 至 09-04 曾用；429 限流待恢复）：OpenCode Go · deepseek-v4-pro

凭据段（读 opencode 的 auth.json，键仍在原位未删）：

```bash
AUTH_FILE="${XDG_DATA_HOME:-${HOME:?}/.local/share}/opencode/auth.json"
# 检查同上：非 symlink、regular file、owner:600:1
OPENCODE_GO_KEY_VALUE="$($JQ_BINARY -er \
    '.["opencode-go"] | select(.type == "api") | .key | strings | select(length > 0)' \
    "$AUTH_FILE")" || { printf 'codex-open: OpenCode Go credential is invalid\n' >&2; exit 78; }
export OPENCODE_GO_API_KEY="$OPENCODE_GO_KEY_VALUE"
unset OPENCODE_GO_KEY_VALUE
```

run_codex 段：

```bash
    -c model_provider=opencode_go \
    -c 'model_providers.opencode_go.name=OpenCode Go' \
    -c model_providers.opencode_go.base_url=https://opencode.ai/zen/go/v1 \
    -c model_providers.opencode_go.env_key=OPENCODE_GO_API_KEY \
    -c model_providers.opencode_go.wire_api=responses \
    -c "model_catalog_json=${MODEL_CATALOG}" \
    -c model_reasoning_effort=max \
    -m deepseek-v4-pro \
```

- 凭据文件：`~/.local/share/opencode/auth.json`（0600）条目
  `opencode-go`；2026-08-31 验证过键仍存在（67 字符），但换回后必须重跑
  验证阶梯确认 key 未在服务商侧失效。
- 目录条目：`deepseek-v4-flash` / `deepseek-v4-pro` 一直在
  `~/.codex/models.json`，换回无需动目录。

## 目录（catalog）schema 的坑

- 每条模型**必须**带 `base_instructions` 或
  `model_messages.instructions_template`，否则整个 models.json 解析失败
  （codex 报 `missing both base_instructions and ... instructions_template`，
  所有模型一起失效）。新模型条目直接复制现有条目的模板字段。
- `supported_reasoning_levels` 必须包含脚本传的 effort（两 profile 的
  `max` 都已验证被接受；不要引入 codex 端未验证的枚举值）。
- 接全新 provider 前先跑「可行性预检」节（下一节），不猜协议、不先改配置。

## 接入新 provider 前的可行性预检（2026-09-05 commandcode 案例）

**预检四步**（任何一步不过即止，不改任何配置）：

1. `GET <base_url>/models`——验 key 有效 + 拿模型 ID 真实命名（聚合网关常带
   `provider/model` 前缀，如 `deepseek/deepseek-v4-pro`）。
2. `POST <base_url>/responses` 最小请求——404 = 该上游无 Responses wire。
3. `POST <base_url>/chat/completions` 默认(HTTP/2)与 `--http1.1` 各一发——
   有的网关 h2 POST 稳定 `SSL_read: unexpected eof`（HTTP 000），h1 正常。
4. 对照 codex CLI 的 wire 约束：**codex 的 chat wire 已死**——2025-12-09 官宣
   弃用（discussion 7782），2026-02 初硬移除；实测 0.95.0/0.144.1/0.152.1 均拒收
   `wire_api = "chat"`，**0.92.0 = 已验证的末代 chat 版本**（0.92+commandcode
   chat 直连基础问答+工具回路全绿；曾专装 `~/.local/share/codex-0.92`，2026-09-05
   已卸载，重装 = `npm install --prefix ~/.local/share/codex-0.92 @openai/codex@0.92.0`；
   全局 0.152.1 未动）。
   chat-only 上游接不了现役 codex——这不是配置技巧能绕的，是协议两端不兼容。

**commandcode 案例结论**（`https://api.commandcode.ai/provider/v1`，key 曾在
/tmp/command）：models ✅ 67 个（deepseek pro/flash/flash-fast/vision-exp，
glm-5.3 也在）；`/responses` ❌ 404（官方文档明示仅 OpenAI chat completions +
Anthropic Messages 两形态）；chat h2 ❌ EOF×3 / h1 ✅ 实弹返回 ok。→
**codex 腿全部不可换**（finqa-x/finqa-codex、生产 codex-open 路由——后者另有
manage-fin-codex-routes 边界「codex-provider 仅 Responses-compatible」双重锁）；
`llm.yaml` deepseek_flash（openai_compatible，python 客户端默认 h1）可换。

**已落地（2026-09-05）**：key 吸收进持久双库——`~/.local/share/opencode/auth.json`
新增 `commandcode` 条目 + `llm.env` 追加 `COMMANDCODE_API_KEY/_BASE_URL`（/tmp/command
已 chmod 600 且重启即清，勿再指 /tmp，见「只换 key」教训）；opencode-go 全部使用点
现场备份在 `~/.local/share/opencode-go-backup-20260905/`（0700/0600，MANIFEST 含
sha256 与回滚说明，内附 deepseek_flash 换 commandcode 的现成补丁）。

**未上线/边界**：codex-open 路由与 finqa-x/finqa-codex 保持 opencode-go 不动
（429 只能等服务商侧恢复，或 owner 另批本地 responses→chat 翻译代理——新基建，
按家规 11 举证另立项）；flash 补丁上线 = 应用补丁 + 发版 + 重启消费单元，窗口
owner 拍板（BUG-024 owner 09-07 盘前实弹终验前不动生产提取链）。

**CC harness 接 commandcode 的预检结论（2026-09-05 追加，owner 提案「CC+commandcode
新问询腿」实测）**：`/provider/v1/messages`（Anthropic Messages 形态）**只收 Claude
系模型**——deepseek 直拒（`Model ... is not supported on this endpoint. Use
/provider/v1/chat/completions for OpenAI and OSS models`）；而 Claude 系 8 个模型
（sonnet-5/sonnet-4-6/haiku-4.5/fable-5[-1]/opus-5/4.8/4.7）当前 plan **全部 403
MODEL_NOT_IN_PLAN**（sonnet/haiku 需 Pro+，fable/opus 需 Provider+）。→ CC 直连
在当前订阅下不可行。**若 owner 升级到 Pro+，配方（10 分钟配置活）**：
`ANTHROPIC_BASE_URL=https://api.commandcode.ai/provider`（SDK 自动拼
/v1/messages）+ `ANTHROPIC_AUTH_TOKEN`（llm.env COMMANDCODE_API_KEY）+
`ANTHROPIC_MODEL=claude-sonnet-5` + `ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5-20251001`
（CC 后台模型也要在 plan 内）+ 独立 `CLAUDE_CODE_CONFIG_DIR`（与 finqa-c 共 cwd
会串项目级 settings 的模型旋钮，必须隔离）。**LiteLLM 桥替代路线**（CC 或 codex
任一 harness 吃 chat-only 上游）：LiteLLM proxy 上游面 `/v1/responses` 或
`/v1/messages`、下游桥 chat completions（codex 场景文档明示 `use_chat_completions_api:
true`）；代价 = 常驻 daemon + 第四份 key 副本 + 映射保真风险（CC 依赖
tool_use/cache_control/thinking 等协议特性，桥接风险高于 codex 侧）。触发条件
（任一满足再立项，当前不建）：① opencode-go 长期不恢复且确需非 GLM 腿；
② owner 升级 commandcode plan；③ commandcode 上线 Responses 端点。

**Kilo CLI 试点（2026-09-05，owner 拍板 opencode-go 为长期故障后的换车候选）**：
`@kilocode/cli` 7.5.13（opencode 内核）三关全绿——基础实弹 ✓、人格加载 ✓（在
consult-agent cwd 自动读 AGENTS.md->CLAUDE.md，advisory_only/13 工具纪律复述准确）、
MCP 工具闭环 ✓（模型正确传参调 read_user_watchlist 返回 30 标的）。**2026-09-05
已卸载**（owner 裁定环境清洁；主腿定 cmd），重装一条命令：
`npm install --prefix ~/.local/share/kilo @kilocode/cli@7.5.13` + symlink。配方：
二进制 `~/.local/share/kilo`（npm --prefix）+ symlink `~/.local/bin/kilo`；
provider 放**用户级** `~/.config/kilo/kilo.json`（600）：`provider.commandcode =
{npm: "@ai-sdk/openai-compatible", options: {baseURL: "https://api.commandcode.ai/provider/v1",
apiKey: "{env:COMMANDCODE_API_KEY}"}, models: {deepseek/deepseek-v4-pro, ...}}`——
注意**项目级 kilo.json 禁止 {env:} 引用**（`kilo config check` 会拒），所以项目级只放
MCP 面（`"mcp": {"fin-readonly": {type: local, command: [...], environment: {...}, enabled: true}}`，
已落在 consult-agent/kilo.json）；模型选择 `kilo run -m commandcode/deepseek/deepseek-v4-pro`，
无头一次性 = `kilo run`。已观察瑕疵：偶发 `Connection reset by server`（重试即过）+
模型偶发漏必填参数（read_user_watchlist 的 question 必填，显式指定后正常）。
**dsh（@deepseek-ai/dsh 0.1.2-rc.1）定性：观察位，不上腿**——node 22 可跑（包未声明
engines，社区传的 Node 24+ 未在元数据落实），但形态是 Cordis 插件栈引导的**纯浏览器
UI**（profiles 只有 web），无终端无头形态，对问询族是形态级不匹配。

**Command Code 自家 CLI（`command-code`/`cmd` 1.49.1）定性：三关全绿，已上线为非
GLM 问询腿（finqa-cmd / finqa-commandcode，2026-09-05 owner 拍板换腿）**——
2026-09-05 owner 完成 `cmd login`（Go Plan）后试点：基础 `-p -m deepseek/deepseek-v4-pro`
✓、consult-agent 人格加载 ✓（纪律复述含「跨会话记忆不存数字」）、MCP 闭环 ✓
（read_user_watchlist 返回 30 标的，与 Kilo 一致）。要点：二进制 `~/.local/share/
command-code` + symlink `~/.local/bin/cmd`（重装 = `npm install --prefix
~/.local/share/command-code command-code@1.49.1`，精确版本钉定，升级需同步
脚本 CMD_VERSION_PIN 并重跑验证阶梯）；`cmd mcp add-json <name> <json> --scope
project`（在目标工作区执行）**直接写共享的 .mcp.json**（与 CC 同一面，会给条目补
transport/enabled 字段，CC 忽略无碍）；`--skip-onboarding` 关 taste、`--no-auto-update`
钉版本；-p 前必须已认证（无 env-key 旁路）。UNLICENSED 闭源薄客户端（dist 3.3M，
逻辑在厂商后端），68 模型随 Go Plan 菜单切换。与 Kilo 二选一：cmd = 集成最薄、
账号额度直付、闭源绑定；Kilo = 开源 harness + Provider API key 直连，依赖最薄。

**pi（@earendil-works/pi-coding-agent 0.85.0，MIT）定性：三关全绿，第三条可用路线
（2026-09-05 已卸载，重装 = `npm install --prefix ~/.local/share/pi @earendil-works/pi-coding-agent@0.85.0`）**——
earendil-works 是 pi 的在维护 upstream（badlogic/pi-mono 原作 2026-05 起停更，fork 于
2026-09-04 仍有发版；node ≥22.19）。二进制 `~/.local/share/pi` + symlink `~/.local/bin/pi`。
配方：① provider 写**用户级** `~/.pi/agent/models.json`：`providers.commandcode =
{baseUrl: "https://api.commandcode.ai/provider/v1", api: "openai-completions",
apiKey: "$COMMANDCODE_API_KEY"（原生 $ENV 引用，零密钥副本）, compat:
{supportsDeveloperRole: false, supportsReasoningEffort: false}, models: [deepseek 系,
reasoning: true]}`；② MCP 走官方适配器 `pi install npm:pi-mcp-adapter`，适配器**原生读
项目 .mcp.json**（与 CC/cmd 共享同一面，consult-agent 零配置即用）；无头 =
`pi --no-session -p --provider commandcode --model deepseek/deepseek-v4-pro`。
2026-09-05 试点：基础 ✓、consult-agent 人格加载 ✓、read_user_watchlist 30 标的 ✓。

**Cursor（cursor-agent CLI 2026.09.02）定性：否决，不评估入腿**——它是 Cursor 私有
agent 后端（api2.cursor.sh）的闭源客户端：`--endpoint` 覆盖实测不能换成 OpenAI 兼容
端点（指到 commandcode 后仍做 Cursor 平台 key 校验直接拒绝），模型全是 Cursor 托管
id；认证强制 Cursor 账号/平台 API key。IDE 侧的 BYOK base_url 覆盖只作用于 GUI 聊天
面，不是无头 agent 回路。闭源 + 账号依赖 + 协议锁死三重不可行。教训入库：**评估第三方
harness 先看它原生讲谁的协议、认证绑谁**——`--endpoint` 这类旗标常被误读成「通用
OpenAI 兼容口」。

## 切换流程（两个方向通用）

1. **备份先行**（secrets 不入 git，备份留本机 owner-only）：
   ```bash
   BK=~/.local/share/codex-open-backup-$(date +%Y%m%d)
   mkdir -p "$BK" && chmod 700 "$BK"
   # 拷入：codex_open.sh、两份 auth.json、~/.codex/models.json，均 chmod 600
   # 写 MANIFEST.txt：日期、原 provider、原位路径、sha256sum、回滚方式
   ```
2. **改脚本**：只替换上面对应的两段片段；TTY/exec 自动补 `exec`、
   STDIN_NULL 逻辑一律不动。`~/.local/bin/codex-open` 是 symlink，无需
   其他改动。
3. **验证阶梯**（按序，绿了才算）：
   ```bash
   bash -n scripts/codex_open.sh
   scripts/codex_open.sh exec --skip-git-repo-check "Reply with exactly: ok"
   scripts/codex_open.sh exec --skip-git-repo-check "Read scripts/codex_open.sh and answer in one sentence: ..."   # 验证沙箱内读仓
   ```
   再人工开一次 TUI 确认横幅 Model/Provider 行已切换。
4. **同步文档并提交**：AGENTS.md 与 docs/GLOSSARY.md 的
   「当前 codex-open · <model> · max」描述改掉；DECISIONS.md 历史记录
   不回改。commit 只含脚本与文档，**key 永不入仓**。

## 只换 key（provider/模型不动）——2026-09-01 教训

key 属于凭据文件，**永远不要把脚本/入口指到临时文件**。2026-09-01 曾误把
`scripts/codex_open.sh` 与 `~/.bashrc` 的 finqa-codex/finqa-x（旧名
finqac/finqai）的
`OPENCODE_GO_API_KEY` 直接改为读 `/tmp/open.txt`（/tmp 重启即清空、且绕过
auth.json 单一事实源，误提交 commit e0c7269），同日回退。正确流程：

1. 新 key 从外部来源（如 `/tmp/open.txt`）**取出值**，写入对应 profile 的
   auth.json 条目——Profile B = `~/.local/share/opencode/auth.json` 的
   `opencode-go.key`；Profile A = `~/.local/share/codex-open/auth.json` 的
   `relay-19851117.key`；保持 0600，先 `cp -a` 备份原文件。
2. 一致性校验（不打印值）：新 key 值与 auth.json 条目值 sha256 一致。
3. 入口零改动：scripts/codex_open.sh 仍读 auth.json；`~/.bashrc` 的
   finqa-codex/finqa-x、llm.yaml 的 `AUTHJSON:opencode-go` 降级链、
   codex_routes.yaml 的 codex-open 路由同源生效——只改 auth.json 一处即
   全覆盖。
4. 重跑验证阶梯第 1 条（`bash -n`）+ 一次 `exec "Reply with exactly: ok"`。
5. 用毕删除临时 key 文件（或保留 0600，/tmp 重启自动清）。

## 回滚

`git checkout <上一版脚本所在 commit> -- scripts/codex_open.sh` +
备份目录中对应凭据/目录文件拷回原位（保持原权限），重跑验证阶梯。

## 边界

- FIN 产品的 Codex 路由链（多路由/探活/cooldown）归
  `manage-fin-codex-routes`，与本 skill 无关。
- 本 skill 只管 CC 评审者入口 `scripts/codex_open.sh` 及其包装 symlink。
