# ZSXQ Windows 采集腿源真身迁移老仓→fin-core（设计稿 v2）

状态：设计门已过——codex-glm·glm-5.3·max，605s，9 发现（2P1+6P2+1P3）
全采纳落稿（台账 `$STATE/fin-analyse/design-gate/zsxq-capture-migration-20260905/`，
裁决记录见该目录 verdict.md）。owner 2026-09-05 令立项（「立项，迁移到新仓」）。
动机：老仓 `~/fin-analyse` 挂「可退役」却仍承载活生产腿——ZSXQ Windows
原生采集的源码唯一真身（采集脚本+部署调度器）在老仓 `scripts/`，Windows
部署副本哈希钉着老仓 commit（现 5c3f8d92）。不迁移，老仓永远退役不了；
每次采集腿改动都得在「已归档」仓里提交（09-01、09-04、09-05 三次实证）。

## 范围

**迁入 fin-core（2 脚本 + 4 测试件）**：
- `scripts/capture_zsxq_windows.cjs`（采集脚本，自包含：仅 node 内置模块，
  运行时动态解析 npm dir/.opencli 配置，无仓内依赖）
- `scripts/zsxq_windows_incremental_scheduler.py`（部署调度器：render-wrapper /
  verify-task-xml / render-poller-service / render-poller-timer，纯 stdlib）
- `tests/scripts/test_capture_zsxq_windows_inline_backfill.py`（16 用例，node
  require harness，WSL node v22 已在 PATH）
- `tests/scripts/test_capture_zsxq_windows_autotab.py`
- `tests/scripts/test_zsxq_windows_incremental_scheduler.py`（**fixture 整组
  修复**，非单点：`_poller_timer_calendars` 期望改现役六窗口；七时点 XML
  fixture 改六时点 UTC `00:45,04:20,06:40,07:30,10:00,12:20`；同步 drift
  锚点与 "seven slot" 测试名。老仓现基线 17 红/17 绿）
- `tests/scraper/test_capture_script_consistency.py`（**设计门 P1-2 补入**：
  跨语言一致性门，逐字节绑定 .cjs `EMBEDDED_SCRIPTS` ↔ `cdp_scraper.py`
  Python 常量，规则 12 意义上的活代码测试闭包；评审实测非 tmp 子集 4/4 绿）

**同 commit 内配套改动**：
- `.claude/skills/manage-zsxq-capture/SKILL.md`：事实源、render、verify 命令
  全部从 `cd ~/fin-analyse` 切到 fin-core（设计门 P2-1：否则双真身窗口存在
  人工回旧源渲染的漂移入口）。
- `fin_analyse/scraper/cdp_scraper.py:275` 注释旧文件名 `.mjs`→`.cjs` 并指向
  新路径（设计门 P3-1，文档级）。

**不迁/不动**：
- `consume_zsxq_capture_folder.py` 及其测试已在 fin-core（08-29 迁过）。
- Windows 部署目录布局、任务 XML（六班时点/SID/action）、wrapper 模板语义、
  采集脚本行为——零行为变更，纯源真身搬家。
- `fin-zsxq-capture-consumer@.service` 模板单元：**退役，不随迁重渲染**
  （设计门 P1-1 裁决）——见「身份链变化」。

## 身份链变化（唯一的契约面改动）

wrapper 钉值 `expectedSourceCommit` 的语义从「老仓 commit」变为「fin-core
commit」（承载 .cjs 的那个 fin-core SHA）。影响面与裁决：
- 消费端 poller 模式 `expected_source_commit=None`（任何合法 post-cutover
  SHA 均可入）——无冲突，已实证（08:45 班 consumer ready 同时记录
  `source_commit=319f…`（executor）与 `capture_source_commit=5c3f…`（capture））。
- artifact 的 `capture_source_commit` 审计字段从此指向 fin-core SHA——语义
  更正确（采集脚本与消费端同一仓，版本对账闭环）。
- **consumer@.service 模板单元退役**（P1-1）：该模板 baked
  `--source-commit 319faf62`（更早 Description 还指 3ff041e），与采集谱系
  （老仓 SHA）本就对不上、0 实例——装上即坏的休眠 cutover 资产。裁决选
  「退役归档」而非「随 cutover 重渲染」：run-id 模式是手动恢复专用，重渲染
  会给每次 capture 变更增加一项永久性同步义务。迁移施工时 mask/移除该单元，
  手动恢复路径改为 poller（主路径）或直调 consumer CLI
  （`--source-commit` 从目标 run 的 summary.json 现读）。
- **commit-containment preflight**（P2-2）：部署前
  `git show <fin-core SHA>:scripts/capture_zsxq_windows.cjs | sha256sum`
  必须等于 `--capture-sha256`（render-wrapper 只校验 SHA 格式，不证明
  release commit 真承载该 .cjs）。
- **单元成套**（P2-2）：迁移 commit 落地即按运维铁律重渲染 ZSXQ poller
  service+timer（单元绑 fin-core HEAD）；是否换运行时以渲染输出为准，
  不与「不动 unit」混说。

## 部署序列（P2-2/P2-3/P2-4 收口）

1. 迁移 commit（脚本+4 测试+skill+注释）+ fin-core 全仓测试绿。
2. containment preflight（上节）。
3. **静默窗口**：避开六班触发时点 ±10 分钟，确认无 active/pending run
   （`Get-ScheduledTaskInfo` State + runs 目录 newest summary）——消除
   「hash 校验后、node 打开前换文件」的窄 TOCTOU。
4. **新建当日成对备份**（现役 ps1+cjs+定义件）——注意既有
   `backup-20260905/` 是 09-05 凌晨 cutover **前**的旧钉值对
   （b7d52e79/be9cc），不冒充现役备份。
5. 替换两文件 → sha256 三方核对（git show = 部署副本 = wrapper 钉值）。
6. `verify-task-xml` **只对定义件**（本次备份的 task-current.xml）跑契约；
   运行态 enabled 另用 `Get-ScheduledTask` 确认（P2-4：注册库文件永远缺
   `<Enabled>`，对它跑 verifier 必假红）。
7. 下一班实弹：capture_exit 0、artifact `capture_source_commit`=<fin-core
   SHA>、consumer ready；poller service/timer 渲染出处=fin-core。

## 回滚（P2-3 收口）

- 只允许：本次 cutover 前新建的成对备份复制回两文件 → 核对
  `expectedSourceCommit/expectedCaptureSha256` 与 .cjs hash 成对一致 →
  不改任务 XML。已有新 SHA 的 pending artifact 时切回旧 wrapper，poller
  仍接受，无需动水位线。
- **禁用** `run-capture-and-import.ps1.rollback-8723…`：那是更早的编排语义
  wrapper（指旧 release 的 WSL import 路径、期望 dcfed5 hash），恢复它=
  回退到 legacy 编排而非本次 cutover。

## 老仓删除（后置，二选一，P2-5 收口）

设计稿坚持删除后置，且定义为二选一，不搞局部删除：
- **默认**：等老仓整体退役时按家规 4（备份+manifest）随完整引用闭包处理。
  引用闭包清单（起点非边界）：`scripts/consultation_runtime_canary{,_launcher}.py`、
  `scripts/prepare_fin_release.py`、`tests/scripts/test_apply_fin_hermes_external_integration.py`、
  `tests/scripts/test_prepare_fin_release.py`、`tests/scripts/test_consultation_runtime_canary.py`、
  `fin_analyse/runtime/hermes_managed_assets.py`、consistency 测试、本项迁走的 4+2 件。
- 或：先拆/删上述 release/canary 读方，再删采集腿文件——仅在 owner 要求
  提前缩小老仓时启用。

## 施工窗口与验收

- 本设计稿+设计门为 docs-only，已即时落盘。
- 施工默认排在 D3 三天门走完（最早 09-07 晚）之后，避免与建造静默期相撞；
  owner 明示豁免可提前。施工预计一个会话内完成（文件平移+fixture 整组修复+
  skill 切换+部署核对，无新逻辑）。
- 验收：fin-core 全仓测试绿（含迁入四件）；部署后下一班实弹三证据
  （capture_exit 0 / capture_source_commit=<fin-core SHA> / consumer ready）；
  consumer@ 模板已 mask；老仓删除另一步、不在本项验收内。

## 为什么不是别的做法

- 留在老仓不动：退役永不可能，且每次改动都在「已归档」仓产生新 HEAD，
  归档语义失真（现状已在发生）。
- 重写为 Python/并入 fin-core 现有 poller：推翻已实弹验证的 node+opencli
  传输面，违反规则 11（无故障举证不重写）。
- 只迁脚本不迁测试（含 consistency 门）：规则 12 明令禁止（测试只护活代码）。
- consumer@ 模板随每次 cutover 重渲染：给休眠恢复路径加永久同步义务，
  且它当下就是 stale 的（钉 319faf62 对不上任何采集谱系）——退役更净。
- 部署不建静默窗/不做 containment 校验：hash 校验与 node 打开文件之间存在
  窄 TOCTOU；render-wrapper 不证明 release commit 承载该 .cjs——两者都是
  设计门实测指认的漏洞，不是想象中的加固。
