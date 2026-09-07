---
name: manage-llm-pool
description: Operate the FIN LLM pool — enable/disable a model node or harness leg (启用/禁用 LLM 节点/腿/渠道, 翻 enabled, 渠道恢复/限额解除/429 好了, 换 DS 槽位, 加/撤一个模型), or diagnose "pinned leg refused: pool disabled" and probe a channel before flipping. Covers the two-table flag map (config/llm.yaml api vs harness entries + finqa_nodes.yaml), credential prerequisites, dual-client channel probes, the post-commit snapshot render, the verification ladder and 落账 surfaces. Not for launcher/runtime code changes (see fin-release-launcher-chain) or capture scheduling (see manage-zsxq-capture).
---

# Manage LLM Pool（节点/渠道启用 · 禁用 · 恢复）

不变量与设计事实看 `docs/design/llm-pool-layering.md` 与
`docs/architecture/llm-route-topology.md`；本 skill 只管操作程序。
翻开关=改生产行为：**只在 owner 明确要求时执行**。config-only 走本 skill；
要动 launcher/runtime 代码才走 `fin-release-launcher-chain`。

## 开关地图（动手前必读）

- **`config/llm.yaml` `models:` 段**两类条目，各带独立 `enabled`：
  - **api 型**（直调池：glm53/qwen/deepseek_flash_opencode…）→ 供提取链
    cognition / t0 / t1 / vision（`priorities:` 段定序）。
  - **harness 型**（问询腿连接池：commandcode-pro / zcode / codex-opencode /
    claude-cc…）→ 供 finqa 问询链经 `finqa_nodes.yaml` 的 `conn_ref` 引用。
- **`config/finqa_nodes.yaml`**：节点级 `enabled`（是否入链序）+ `conn_ref`。
  链序 = 声明序 ∩（节点 enabled ∩ 池 enabled）。
- **失败语义（设计内，不是故障）**：池禁/conn_ref 悬空 ⇒ 链序静默跳过
  （fail-visible 横幅）；**钉腿（`--node`，含 -i）遇池禁 = 拒绝 rc=2**
  （`finqa_chain.py`：静默换腿会把池禁伪装成腿失败，污染对照归因）。
  `finqa-x` 报 `pinned leg refused: codex: pool disabled: codex-opencode`
  就是这个，别试图修它——翻池条目即可。
- 恢复语义惯例（finqa_nodes 头注）：节点本体常态 `true`，**恢复只翻池条目一处**。
- 分辨两类禁用缘由（注释/NOW.md 里都记着）：**渠道故障**（429 限额、key 失效
  ——渠道恢复即可翻）vs **角色决定**（弱腿不入生产链序——需 owner 拍板）。
  2026-09-07 实证：一次 opencode-go 恢复被拆成「上午翻提取链节点、下午 owner
  拍板才翻问询腿」两步——先分辨，别替 owner 拍板。

## 操作程序

1. **定位禁用点与缘由**：
   ```bash
   grep -n "enabled: false" ~/fin-core/config/llm.yaml ~/fin-core/config/finqa_nodes.yaml
   ```
   读条目上方注释（惯例记 owner/日期/缘由/恢复方式），NOW.md 生产声明交叉核对。
2. **渠道健康探针（翻开关前必做）**：先核凭据前提——llm.env
   （`~/.config/fin-analyse/llm.env`，owner-only）的对应 `*_API_KEY/BASE_URL`
   在位、auth.json 条目在位（如 `/home/ypk/.local/share/opencode/auth.json`）。
   然后**双客户端**探 `GET /models` + 小额 chat：
   ```bash
   curl 一发（绕开误报）+ .venv/bin/python 用 openai.OpenAI(api_key=…, base_url=…) 复验
   ```
   **坑：Python urllib 会被 Cloudflare 客户端指纹封禁（403 + `error code: 1010`）
   假阴性；curl 200 也不等于生产通——必须用生产同款客户端（venv OpenAI SDK）
   复验，两者都 200 才算渠道恢复**（2026-09-07 实证）。
3. **翻开关 + 注释落账**：`enabled: false→true`；条目注释按惯例写 owner/日期/
   缘由/证据（如「渠道复测探针 200」）。若恢复语义本就写好在注释里（「恢复=
   只翻此处」），照做，不加戏。
4. **同步面**（漏一面就是漂移）：
   - `config/finqa_nodes.yaml` 相关注释（节点/生产链序描述）；
   - `docs/architecture/llm-route-topology.md`（改链序/增删条目必同步，头部规则；
     「已知跨表动作」恢复注记更新状态）；
   - `docs/pm/NOW.md` 生产声明（日期一行落账：缘由+拍板+翻了几处）；
   - `~/.bashrc` 入口注释（finqa-x 等标着「休眠」的，复活后同步）。
5. **测试**：`.venv/bin/python -m pytest tests/test_llm_config.py tests/claims -q`。
6. **提交**（只 add 本任务文件；提交即生效）：post-commit hook 自动渲染快照
   `~/.local/share/fin-analyse/runtime-configs/<完整SHA>/config/llm.yaml`、刷新
   消费单元 `LLM_CONFIG_PATH` 并 daemon-reload。**timer 单元下次唤醒自动用新
   配置，无需手动重启**；`/tmp/fin-render/hook.log` 出现 `llm snapshot <sha>`
   即成（注意：日志里是短 SHA，目录是完整 SHA）。
7. **验证梯（诚实分级，逐级不冒充）**：
   a. 快照已渲染（hook 日志 + 文件存在）；
   b. 生产 loader 复载快照：`load_llm_config(snap)` 后核 enabled=True、
      `_resolve_env` key 可解析、链序符合预期（需 `FIN_LLM_ENV_FILE=
      ~/.config/fin-analyse/llm.env`——runtime-configs 路径没有 project .env）；
   c. **实弹探针走真实边界**：`finqa --node <leg> "自检探针:只回复两个字——正常"`
      无头一发，rc=0 + 回复正常 + tsv 落账 `served-by` 才叫「跑通」
      （家规 7；提取链节点用其真实消费入口验证）。
