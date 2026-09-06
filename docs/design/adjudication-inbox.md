# adjudication-inbox · 设计页（裁决收件箱：跨功能「待 owner 裁决」统一清单 + 触达）

> 依据：owner 2026-09-06 会话拍板（按推荐方案施工 + 定时飞书摘要推送授权）；
> 家规 5 核心判据命中（durable state + 公共入口 + 跨功能接口契约）→ 设计门。
> 定位：只做「清单 + 状态 + 触达」的薄层，**不统一裁决执行**——内容与确认权威
> 留在各功能自己的确认面（家规 6 特性内聚）。合入后本页删除（Git 即归档）。
> r3：r1（619s，cmd·deepseek-v4-pro，5P1/2P2/1P3）+ r2 重评（1477s，替补
> claudecode·glm-5.3——cmd 两次 rc=3 自动 fallback 落账 fallback.tsv；2P1/5P2/5P3，
> 判定「修订后可施工」）全部裁决折入；两轮 packet/review 存
> `~/.local/state/fin-analyse/design-gate/adjudication-inbox-20260906{,-r2}/`。

## 目标 / 非目标

**目标：**

- 一个 durable 收件箱：所有「等 owner 裁决」的项在产生处登记，一处列出、
  一处接受裁决反馈（done/add）、一处可查历史。
- 一个每日触达：交易日上午一班飞书摘要（Hermes 固定 fin profile），治
  「忘记主动问」；积压超阈值标 ⚠ 升级。
- 注册协议：未来功能动代码前经设计门清单判据接 seam；接入本身只要求
  producer 真相处一次 reconcile。

**非目标：**

- 不做飞书入站裁决回复（咨询入口已停用，2026-08-27 拍板；写副作用必须回
  各功能确认面人工完成，硬边界 1/2）。推送是单向提醒。
- 不搬移/不改写任何既有确认流程（主线扫批起草协议原样保留）。
- **v0 不接 portfolio 持仓面**（P1-2 裁决）：核实结论 = preview→confirm 的
  唯一生产入口是旧 gateway 咨询面（2026-08-27 停用；`fin-analyse` MCP server
  非本仓实现，仓内 `save`/`confirm_latest_review` 无生产调用方；现役
  fin-readonly MCP 只读）——挂进休眠路径是死 producer，测试绿掩盖空转。
  portfolio 接线单列后续项（NOW.md），待持仓写路径回生产时挂点。
- 不自动接 advisory 信号（S-010C、research_suggestion：即用即弃无队列）、
  名单型规则裁决（article_tags 边界、macro_index excluded：无 producer 抛事件）、
  文档队列（NOW.md/BUGS.md：owner 自管，CLI `add` 手动钉长生命周期项兜底）。
- 回放提名（cognition-replay-facts）producer 尚未施工，届时按本协议接入。

## 数据面（唯一 durable state）

`$XDG_STATE_HOME/fin-analyse/adjudication-inbox-v1/inbox.sqlite`（新增
`adjudication_inbox_state_root` 于 `runtime/state_roots.py`；目录 0700/文件
0600，`-wal`/`-shm` 同权限纳入测试）。SQLite WAL；**并发硬约束**：单条 item
变更全程 `BEGIN IMMEDIATE` 事务，每事务 `busy_timeout=5000ms`（事务内无网络
调用，毫秒级），SQLite 层错误以 typed 异常外抛；不引入 per-principal flock
——SQLite 事务是唯一串行化点。时区单源：所有「日」口径 = 判定时刻
`Asia/Shanghai` 历日。

- `items`：`item_id TEXT PK`（producer 名+原生 id）、`kind`、`title`、
  `payload_ref`（指回 producer 自己的产物，**不复制内容**）、`resolution_hint`
  （怎么裁决：命令/入口一句话）、`status`（open|resolved）、`opened_at`、
  `last_seen_at`、`resolved_at`、`resolve_source`（owner|producer|auto）、
  `note`、`reopen_count`。
  **upsert 行级规则**（reconcile pending=True）：行不存在 → 插入
  （opened_at=now，event=submit）；行存在且 status=open → 内容字段与
  last_seen_at 就地更新、**不追加事件、不动 opened_at**；行存在且
  status=resolved → reopen（status=open、opened_at=now 重置、
  reopen_count+1、event=reopen）。
- `events`：append-only 台账，**只记状态转换**（submit|reopen|resolve）；
  内容不变的高频重放只更 `last_seen_at`，不产生事件（r1-S1 去抖，台账不
  膨胀）。CLI `add` 视为新待决事实，产 event=submit。
- `sent_log`：`(day TEXT, fingerprint TEXT, at REAL, PK(day,fingerprint))`。
  `day` = Asia/Shanghai 历日；`fingerprint` = open 集 `sha256(sorted(item_id,
  title, status))`。

## 接口契约（窄 seam，`fin_analyse/adjudication/`）

- **producer 侧**：`reconcile(item, *, pending: bool)` —— producer 真相同步，
  幂等定义为「pending 内容不变时：不新增事件、不改 status 转换、opened_at
  语义稳定（重放不动 opened_at；reopen 必重置）」。`pending=False` → 若 open
  则 resolve（`resolve_source='producer'`，event=resolve）；行不存在或已
  resolved → no-op。
- **owner 侧 CLI `fin-adjudication`**：`list`（open 按 opened_at 升序 + 积压
  天数）、`show <id>`、`done <id> --note`（`resolve_source='owner'`）、
  `add --title --hint [--ref]`（手动项，id=`manual:<slug>`，event=submit）、
  `push`。`done` 对不存在/已 resolved 项 typed 报错提示用 `show` 核对
  （TOCTOU 不加锁膨胀，提示兜底）。
- **触达（push）**：读 `config/adjudication.yaml`（`escalate_after_days: 3`，
  可调项进配置家规 6）。无 open 项 no-op。有 → 计算 fingerprint：
  - 同日同 fingerprint 已在 `sent_log` → 跳过（幂等）；
  - fingerprint 变化 → 仅当**最近一次发送之后存在 submit/reopen 转换事件**才
    允许重发（防抖：首次发送前的转换已包含在上一条摘要里；纯标题抖动不当日
    重发，并入次日班）；
  - 组摘要（每项一行：kind/标题/积压天数/`resolution_hint`；超
    `escalate_after_days` 加 ⚠）经 `HermesCliMessageSender`（复用
    `operations/daily_workspace_delivery.py` 固定 fin profile，不新建通道）发
    一条；目标 = 环境变量 `FIN_DAILY_WORKSPACE_DELIVERY_TARGET`（unit
    EnvironmentFile 指既有 `daily-workspace-target.env`，不新增凭据面）。
  **读侧 fail-closed（r1-S2）**：sender 抛错、超时、**或返回 None（平台接受
  未回执）→ 一律不写 `sent_log`**，当日后续重试机会仍可重发；次日 timer 自然
  再试。发送成功（有 message_id）才落 `sent_log`；发送失败不自动重试
  （journal 留证）。崩溃窗口语义：send 已送达、进程在写 sent_log 前死亡 →
  次日重发一条重复摘要（at-least-once，接受）。

## producer 接线（v0 一处，best-effort，异常只 logging 不阻断主链）

| producer | 触发点 | item_id | pending 判定 |
| --- | --- | --- | --- |
| G 主线候选提名 | `consume_zsxq_capture_folder.py` `_rebuild_cognition_mainline`：**scan try/except 之外、audit 落盘同层、独立 suppress+logging**（r2-P3-4，防 inbox 异常被误记为 scan_invocation_failed） | `mainline.nomination` | `disposition=="SCANNED"` 且 `nominated>0` → True；`SCANNED` 且归零 → False（owner 扫批入档即完成路径，nominated 清零干净）；`SKIPPED`/FAILED → 不碰 inbox（真相未知）。`same_article` 只作 title 信息性计数「另有 m 同文」（r2-P1-1：same_article 清空只随 as_of 锚滚动，会造常开项+done→reopen 循环）。title 只含计数（scan 结果本就 content-free），payload_ref=draft_path |

契约承诺同步：consume 段注释「唯一写出是 state 下的候选草稿」修订为
「唯一写出是候选草稿 + 裁决收件箱 SQLite（best-effort）」；scan 纯读/
永不阻断 ingest 承诺不变（inbox 挂点在独立 suppress 边界内）。

**后续项（出队条件明确）**：portfolio 持仓面接线——待持仓写路径回生产
（现唯一入口已停用，见非目标）时挂 `save`/`confirm`；届时终态映射按
`ConfirmStatus` 8 值闭集（`PUBLISHED`/`UNCHANGED`→False；`NO_PENDING_REVIEW`/
`BUSY`→no-op；`CURRENT_CHANGED`/`REVIEW_EXPIRED`/`UNAVAILABLE`/
`OUTCOME_UNKNOWN` 不动 inbox）；hint 文案须含「已过期→重新保存再确认，
或 done」。设计门清单判据届时兜底强制。

## 触达单元（部署面）

`scripts/systemd/user/fin-adjudication-digest.{service,timer}` 静态文件入仓；
安装 = `cp` 到 `~/.config/systemd/user/` + `daemon-reload` + `enable --now`。
`OnCalendar=Mon..Fri *-*-* 09:00:00`（系统本地时区 CST；法定节假日仍会推，
v0 接受）。**偏离仓内 unit 惯例声明（r2-P2-3）**：仓内唯一既有 unit 生命周期
是 post-commit 钩子渲染 + SHA 钉单（daily 件），本对单元为静态件 + 手动
安装/维护的新约定——理由：单对无参数单元，渲染器与 daily 生命周期/SHA
校验器耦合，复用属结构改动（驳回该替代）。静态件维护责任：CLI 入口/脚本
路径变更时钩子不会更新已装副本，须手动重装（写进变更/退役面）；家规 9
部署枚举面将本单元记为「无钉单、手动维护」。v0 不做 `--expected-commit`
等价钉单校验（低风险：摘要只含元数据）。退役 = `disable --now` + 删单元。

## 注册协议（未来功能怎么接上）

1. 设计门清单判据（run-design-gate skill §0 增一行）：新增「preview→确认 /
   提名→扫批 / 需人工确认的警告面」→ 必须接 `fin_analyse/adjudication`
   reconcile seam。设计门/吓人 diff 审查命中即拦——仓库既有强制点，不新增设施。
2. 落点修订（r2-P2-4：`internal-module-catalog.md` 本仓不存在，为老仓拆解
   遗物）：system-overview 域表「交付与运行面」行补 adjudication + GLOSSARY
   增「裁决收件箱」词条。
3. 否决项：AGENTS.md 家规硬条款（owner 未选，不落）；自动扫描检测未注册
   producer（无普遍判据，纯噪音）。

## 决策与证据（家规 11）

- 增强 = 跨面清单 + 状态台账 + 每日触达；限制 = item 只存元数据与指针
  （无正文/无持仓数字，硬边界 3：飞书面只见计数与标题型摘要）；删除 = 无
  （新能力）。举证 = owner 2026-09-06 明确要求（家规 10：用户本人要求即
  使用证据）。
- D-030 边界：本推送是 owner 新请求驱动的小流量通道（无项不发、指纹幂等），
  不是恢复 Daily 四班；8 个 daily timer 维持停用。
- 设计门裁决汇总（两轮）：r1 5P1/2P2 全采纳、1P3 驳回（done 无 note 缺
  上下文——`show` 可自取）；r2 2P1 采纳（mainline 判定改 `nominated>0`、
  portfolio 降范围砍 producer）、5P2 采纳（终态映射闭集随后续项落稿、send
  None 不落账、unit 惯例偏离三句声明、注册落点改 system-overview、
  DECISIONS/NOW 治理留痕随本批 commit）、5P3 采纳（r1 裁决补 elapsed 与
  评审者、D-030 误引改正、全量测试数字实测后表述、consume 挂点钉位、钉值
  四件：busy_timeout=5000ms/add 产 submit 事件/-wal/-shm 权限测试/hint 过期
  路径）。
- 净复杂度：1 小包 + 1 sqlite + 2 静态单元 + 1 处 producer 挂点；配置仅
  `escalate_after_days` 一项（不为想象中的变化预建，家规 6）。

## 验证方式

- 单测：`tests/adjudication/` —— reconcile 幂等（**重放不新增事件**）、
  reopen 重置 opened_at、producer auto-resolve（nominated 归零路径）、
  CLI list/show/done/add（add 产 submit 事件）、push：无项 no-op / 有项一次 /
  **当日同指纹跳过** / 最近发送后无转换事件不重发 / **sender 抛错、超时、返回
  None 均不写 sent_log** / 升级标记 / env 缺失或非 `feishu:` 前缀 → typed
  失败不写 sent_log / DB 及 `-wal`/`-shm` 权限位。sender 用 fake（对齐
  daily delivery 测试的 fake sender 模式）。
- 冒烟：临时 item 上 `fin-adjudication push --dry-run`（渲染文本不发送）+
  真发一条后 `sent_log` 去重复验；`systemctl --user list-timers` 见新单元。
- producer 接线：consume 冒烟（scan 产物 → items 出现 mainline.nomination；
  入档后 nominated 归零 → auto-resolve）；全量测试施工后以实际数字表述
  （不沿用历史「231+」口径）。
