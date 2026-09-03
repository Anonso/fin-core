---
name: switch-codex-open-provider
description: Switch the CC reviewer entry scripts/codex_open.sh (codex-open) between known provider profiles — relay-19851117·gpt-5.6 and opencode-go·deepseek-v4-pro — covering credential file, model catalog entry, the ~/.local/bin/codex-open symlink check, owner-only backup, and the exec verification ladder. Use when the user asks to 换/切/改回 codex-open 的 provider/模型/API/key; not for FIN product Codex routes (use manage-fin-codex-routes) nor personal ~/.codex/config.toml profiles.
---

# Codex-open provider 切换

`scripts/codex_open.sh` 是 CC 外部审视的评审者入口（家规「外部审视」段）。
换 provider/模型改该脚本一个文件，**换 key 改凭据文件、脚本不动**（见
「只换 key」节）；`~/.local/bin/codex-open` 是它的符号链接（单一事实源）。
本 skill 记录两个已验证 profile 的可粘贴片段、踩过的坑、验证阶梯与回滚路径。

## 第 0 步：先查包装入口（吃过的亏）

2026-08-31 事故：`~/.local/bin/codex-open` 曾是 8-30 的独立旧拷贝（非
symlink），手动 `codex-open` 走旧配置而仓脚本已是新配置。切换前后都先确认：

```bash
ls -la ~/.local/bin/codex-open   # 必须是 symlink -> /home/ypk/fin-core/scripts/codex_open.sh
```

若发现是普通文件：先 owner-only 备份进备份目录，再 `ln -sf` 指回仓脚本。
**不要**在 `~/.local/bin` 留第二份拷贝。

## 两个已验证 profile（只替换脚本中两段片段，勿动 TTY/exec 处理逻辑）

### Profile A（2026-08-31 起，现役）：relay 19851117 · gpt-5.6

凭据段：

```bash
RELAY_AUTH_FILE="${XDG_DATA_HOME:-${HOME:?}/.local/share}/codex-open/auth.json"
# 检查：非 symlink、regular file、stat '%u:%a:%h' == "$(id -u):600:1"
RELAY_KEY_VALUE="$($JQ_BINARY -er \
    '.["relay-19851117"] | select(.type == "api") | .key | strings | select(length > 0)' \
    "$RELAY_AUTH_FILE")" || { printf 'codex-open: relay credential is invalid\n' >&2; exit 78; }
export RELAY_API_KEY="$RELAY_KEY_VALUE"
unset RELAY_KEY_VALUE
```

run_codex 段：

```bash
    -c model_provider=relay_19851117 \
    -c 'model_providers.relay_19851117.name=Relay 19851117' \
    -c model_providers.relay_19851117.base_url=https://www.19851117.xyz/v1 \
    -c model_providers.relay_19851117.env_key=RELAY_API_KEY \
    -c model_providers.relay_19851117.wire_api=responses \
    -c "model_catalog_json=${MODEL_CATALOG}" \
    -c model_reasoning_effort=max \
    -m gpt-5.6 \
```

- 凭据文件：`~/.local/share/codex-open/auth.json`（0600，目录 0700），
  条目名 `relay-19851117`。
- 目录条目：`gpt-5.6` 已在 `~/.codex/models.json`（官方 ctx 1,050,000 /
  max input 922,000 / effort 支持 none→max；prompt 模板沿用 deepseek 条目）。
- 该端点 `gpt-5.6` 是 GPT-5.6 Sol 的别名；另有 `gpt-5.6-sol/-terra/-luna`
  变体，换变体只改 `-m` 一行。

### Profile B（2026-08-30 及以前）：OpenCode Go · deepseek-v4-pro

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
- 接全新 provider 时不猜协议：先 `GET /v1/models` 确认模型 ID，再
  `POST /v1/responses` 最小请求探测 wire_api（通则 responses，不通试
  chat），context window 取官方文档数，不编。

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
