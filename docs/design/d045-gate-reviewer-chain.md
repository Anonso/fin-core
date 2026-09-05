# D-045 · 外部审视评审者链重构（cmd 主 · glm 替补）

日期：2026-09-05 · 状态：短设计 v2（规则 5 核心：门设施/公共入口）
设计门：**PASS**（09-05，glm 评审者，elapsed 410s，发现 10〔P1×2/P2×5/P3×3〕，
采纳 10/驳回 0；台账 `$STATE/fin-analyse/design-gate/d045-gate-reviewer-chain-20260905/`）。
〔外审修正〕标注处为按发现折叠的 v2 内容。

## 背景与目标

门评审者入口 `scripts/codex_open.sh` 现为单 provider 硬编码（codex-glm·glm-5.3）。
这条链已两次因单一上游故障手工换评审者：opencode-go 429 后 09-04 手工切 glm——
无 fallback、无横幅，门挂了只能人肉救。owner 2026-09-05 拍板：**门主评审者换
cmd**（Command Code CLI 1.49.1 · deepseek/deepseek-v4-pro · Go Plan 账号），
glm（codex-glm·glm-5.3）降为替补。目标三个：易维护（评审者=profile 表项）、
稳定（主评审者不可用自动落替补并横幅示警）、易更换（换/加评审者=增改一个
profile 函数 + 一行默认值）。

## 方案

- **入口契约不变**：`~/.local/bin/codex-open -> scripts/codex_open.sh`；packet 骨架
  （design-gate-packet-template.md）不变。
- **调用语法矩阵（冻结，翻译层按此吸收/拒绝）**〔外审修正 Q1-P1〕：
  - `codex-open exec [--skip-git-repo-check] [-C <path>] "<packet>"` —— 门标准形态
    （run-design-gate 现行命令原样兼容）
  - `codex-open "<prompt>"` / TTY 交互 / stdin 无参或 `-` 传 prompt / 非 TTY 自动补
    exec / 首参 `exec|e|review` 同义 —— 全部保留现行为
  - 吸收名单：`--sandbox <v>`、`-C <path>`、`--skip-git-repo-check`（codex 专有，
    翻译层吞掉）；**拒绝名单（fail-closed，exit 78）**：`--yolo`、
    `--dangerously-skip-permissions`、`--tools-all`、`--tools-enable`、
    `--permission-mode`、其余一切未识别 `--` 旗标（不猜译）
- **脚本内 profile 表**（每评审者一个 precheck + 一个 exec 翻译函数）：
  - `cmd`：`~/.local/bin/cmd --skip-onboarding --no-auto-update --no-session -p
    --effort max -m deepseek/deepseek-v4-pro`。precheck = `--version` 精确等于
    钉定值 1.49.1 + `cmd status` 输出含认证布尔（捕获解析，不透传 stdout）
    〔外审修正 S1/S3〕。`-p` 收回 write/shell = 评审只读（09-05 四 canary 实证）。
    评审能力声明**收窄为本地读文件**（网络探针未测，不主张）〔外审修正 Q4-P3〕。
  - `glm`：现脚本逻辑原样搬入（auth.json 0600/非 symlink 校验 + model_catalog +
    responses wire + `--sandbox read-only` + effort max）。
- **fallback（替补链只两级：cmd → glm）**：两 profile **启动前都 precheck**（不
  做事后 lazy 发现）〔外审修正 S1〕；主评审者 precheck 失败或运行非零退出 →
  stdout 横幅 `⚠ REVIEWER FALLBACK: cmd → glm` → 以 glm 重发同参。主评审者
  stdout **先捕获**，成功才透传；失败即丢弃半份输出（原样留存 stderr 前缀文件
  供追溯）并追加 fallback 事件到
  `~/.local/state/fin-analyse/design-gate/fallback.tsv`（epoch/profile/终审/
  rc/失败阶段）〔外审修正 Q2-P2/S2〕。TTY 分支 fallback 仅限 precheck 阶段。
  opencode ds pro（429 长期故障）不入链。
- **评审者身份记录**：正常路径答案干净；fallback 时 stdout 横幅强制可见 +
  fallback.tsv 落账；run-design-gate 裁决记录须写实际服务的评审者。门裁决
  记录须写实际服务的评审者（AGENTS.md 外部审视节同步加「裁决记录附评审者」）。
- **换默认评审者** = 改脚本顶部 `DEFAULT_PROFILE=` 一行。

## 不变量

- 只读评审语义不变：cmd `-p` 收回 write/shell（harness 级实证）；glm 保持
  `--sandbox read-only`。
- 入口名 `codex-open`、packet 骨架、验证阶梯（bash -n → exec "Reply with
  exactly: ok" → 读文件探针 → 门形干跑）不变，每 profile 各过一遍。
- 外部审视三触发（设计门/吓人 diff/外援）与裁决四件套（elapsed/发现/采纳/驳回
  + 本设计新增评审者身份）不变。

## 验收

1. 两个 profile 各自过完整验证阶梯。
2. fallback 实测：令 cmd 不可用（临时清认证态/改 DEFAULT），横幅出现且 glm 出
   有效答案。
3. 文档同步：AGENTS.md 外部审视节（评审者链 + 裁决记录附评审者）、
   switch-codex-open-provider skill、NOW.md 生产声明一行。

## 风险与缓解

- cmd 账号会话过期 / 闭源 harness 升级改变 `-p` 权限语义 → precheck 早失败早
  换 + 验证阶梯加写拒绝探针；glm 替补兜底可用性。
- fallback 重发 = 同 packet 重复推理（计费×2）→ 仅在主评审者**失败**时发生，
  正常路径零开销；横幅保证记录不混淆。
- 两评审者答案风格差异 → 门判据是发现计数与采纳裁决，非文本风格；评审者身份
  入裁决记录后可横向追踪。

## 撤销

`git revert` 脚本变更 + `DEFAULT_PROFILE="glm"` 单行即回现状。

## 裁决摘要（AGENTS.md 外部审视节四件套）

elapsed 410s（packet.md→review.md mtime）· 发现 10（P1×2/P2×5/P3×3）·
采纳 10 · 驳回 0。逐条落点：调用矩阵冻结→§方案；run-design-gate 纳入同步面
（重写发射/看门狗/fallback 解析）→§验收+该 skill；fallback 捕获-丢弃-tsv→§方案；
GLOSSARY:61/83、daily-bar-third-source.md:4、NOW 消歧→§引用闭包；precheck
版本钉定+认证布尔解析+双 precheck→§方案；--no-session 表述收窄→§方案；网络
能力声明收窄→§方案。
