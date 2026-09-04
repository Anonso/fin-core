# decision-journal-v1 施工交接（2026-09-04 深夜）

> 交接范围：设计门已闭环、代码研究完毕、**零代码未写**；后续施工由新会话接手。
> 契约权威：[../design/decision-journal-v1.md](../design/decision-journal-v1.md)
> （v2，commit 4edfdac，外审 345s/10 发现/10 采纳）。本文只补设计稿没有的**实现研究结论**，
> 与设计稿冲突时以设计稿为准（除 §3「对设计的两处实现修正」——那两条要在施工报告里声明）。

## 1. 当前目标与状态

- 目标：按设计稿施工决策日志 v1（owner 2026-09-04 晚授权施工，原定 23:00 自动启动，
  因 owner 要求交接改为新会话手动启动）。
- 状态：
  - 设计门 ✅ 闭环（台账 `~/.local/state/fin-analyse/design-gate/decision-journal-v1-20260904/`，
    packet/review/裁决全留证；345s、3P1+7P2 全采纳落稿）。
  - 镜像目标代码已通读（见 §2），实现方案已定型（见 §3），**未写一行代码、未改任何文件**。
  - 工作树干净（除会话前已存在的两个未跟踪 docs/pm handoff 文件）。
  - 原 23:00 cron（81fd03c1）已触发消费，无残留；本会话无后台任务/监视器在跑。

## 2. 关键文件/路径（研究结论，施工直接用）

**镜像三件套（store/service/state）**：
- `fin_analyse/portfolio/user_watchlist.py`（559 行）——store 模板：owner-only
  dir/file 校验（`_require_owner_dir/_require_owner_file`：0700/0600/uid/nlink=1）、
  WAL + `executescript(_SCHEMA)` + `BEGIN IMMEDIATE`、revision
  `r{seq}-{sha256[:16]}` 记账于 meta 表、audit 表、只读连接走
  `mode=ro` URI、`ConsultationInstrumentIdentity.market_symbol` 做 canonical。
- `fin_analyse/portfolio/watchlist_write_service.py`（247 行）——service 模板：
  `LocalWatchlistPreviewTokenManager`（进程内单次 token，TTL 15min，
  `secrets.token_urlsafe(32)`，pop 即消费，过期/principal 不符→None）；
  `preview` 零写→`confirmation_phrase`+`candidate_token`；`apply(token)` 先
  consume 后落库（fail-closed 语义就在这个顺序里）；`_rejected()` 返回
  `{"status": "REJECTED", "reason_codes": (...)}`。
- `fin_analyse/portfolio/watchlist_state.py`（58 行）——state 推导模板：
  `semantic_research_state_root(environ)` + `LocalInstallationPrincipalProvider`
  （`installation-identity.hex`，namespace `fin.local-installation.v1`）→
  (root, principal, store)；absent root 合法（空态），坏权限 fail-closed。

**接线面**：
- `fin_analyse/read_capabilities/server.py`（720 行）——工具注册核心：
  `_TOOL_DEADLINE_SECONDS` dict 是工具总登记表（现 13 条），末尾 `for _tool_name in
  _TOOL_DEADLINE_SECONDS:` 循环给**每个**工具挂 generic read handler，
  **仅 skip `update_user_watchlist`**——施工时 skip 集合要扩成
  `{"update_user_watchlist", "read_decision_journal", "record_decision"}`，
  后两者走 custom handler（镜像 `_make_watchlist_handler`，601-683 行：
  自管参数校验 `InvalidParamsError`、自调 `wiring` service、自写
  `trace.record`、自选 `_READ_ANNOTATIONS`/`_WRITE_ANNOTATIONS`）。
  启动 stderr 的 `serving {len(...)}` 自动跟随新计数。
- `fin_analyse/read_capabilities/wiring.py`（272 行）——两条装配路线并存：
  旧 7 读走 `READ_TOOL_NAMES` → `getattr(provider, tool)`（:203）；**新近 5 读
  （instrument_scores/article_search/article/macro_brain/shared_brain）走
  direct-runner**（独立 reader 构造，失败降级 `unavailable` 列表，塞
  `runners[tool] = reader.read`）。`ReaderWiring` 有 `watchlist_write` 字段
  先例——同样加 `decision_journal: DecisionJournalWriteService | None`。
  watchlist 构造块在 :142-168（directory 用 `RuntimeAshareInstrumentDirectory(
  kb_root/runtime/a_share_name_map.json)`）。
- `fin_analyse/read_capabilities/types.py`——`ProductionReadRequest` 白名单
  （question/instruments/article_id/date_from/date_to/as_of）；**没有
  decision_type/limit**，这是 read 工具走 custom handler 的理由（不动公共契约）。
- `guo_teacher_research/production_capability_provider.py`（2025 行）——
  **不需要改**（见 §3 修正一）。

**resolver**：`consultation/instrument_identity.py` 的
`AShareConsultationInstrumentIdentityResolver.resolve_many(targets) →
tuple[ConsultationInstrumentIdentity]`（status RESOLVED/UNRESOLVED，
`market_symbol` canonical 形如 `601899.SH`）；symbol 归一走它。

**测试先例**：
- `tests/portfolio/test_watchlist_write_service.py`（161 行）——service 测试模板：
  `_FakeResolver/_FakeDirectory`（dict 打表）、固定 clock
  `datetime(2026,9,1,12,tzinfo=UTC)`、token 过期直接改
  `service._tokens._tokens[token]["expires_at"]`。
- `tests/read_capabilities/` 三处钉死要同步：`test_tool_descriptions.py`（88 行，
  描述精确集）、`test_wiring.py`（373 行，runners/unavailable 精确集）、
  `test_stdio_roundtrip.py`（212 行，stdio tools/list 精确清单 + 真实 roundtrip
  harness——**实弹探针直接复用它**，隔离 environ，preview/apply 都能打）。
  另 `test_import_closure.py`（105 行）自动覆盖，动 import 后跑一遍确认。

**生产/运行事实**：
- 生产 state root = `~/.local/state/fin-analyse/`；journal 落
  `decision-journal-v1/{principal}.sqlite3`（与 `user-watchlist-v1` 同根）。
- thin server 入口 `python -m fin_analyse.read_capabilities.server`（模块路径冻结契约）。
- 生产人格：`~/fin-data/consult-agent/CLAUDE.md`（375 行，**git 外数据根，改前备份**；
  工具计数行在 ~282-295「数据权威面 = 12 个只读工具…外加 1 个受限写工具」）。
- 测试跑法：`uv run pytest tests/portfolio/ tests/read_capabilities/`。

## 3. 已定决策（研究后定型，设计稿没写的）

1. **read_decision_journal 走 direct-runner 路线，provider 零改动**。
   设计稿施工清单写的是「provider 加同名方法」（外审 P1-3 的原案）；研究后发现
   新近 5 读已全部走 direct-runner（§2），沿新例：wiring 里构造 service 并以
   custom handler 暴露，`READ_TOOL_NAMES` 不动，2025 行 provider 不碰。
   ——设计修正一，施工报告里声明。
2. **两个新工具都走 custom handler**（read 也一样）：`decision_type`/`limit`
   不在 `_invoke_tool` 白名单与 `ProductionReadRequest` 里，走 generic 要动公共
   read 契约；custom handler 零契约变化。`date_from/date_to` 校验逻辑可从
   `_invoke_tool`（:406-416）抄。
3. **CAS 降级为 revision 记账**。设计稿说「store 层 CAS 沿用」；但 append-only
   无 read-modify-write 竞争面，preview→apply 间插入的他人 append 不构成冲突，
   CAS 只会制造假拒绝。保留 revision bump + audit（可观测），append 不收
   `expected_revision`。——设计修正二，施工报告里声明。
4. **revert IFF 三重钉死**：表级 CHECK
   `((decision_type='revert') = (revert_of IS NOT NULL))` + `REFERENCES
   decisions(decision_id)`（foreign_keys=ON）+ partial unique index
   （`ON decisions(revert_of) WHERE revert_of IS NOT NULL`，每记录至多一次更正）；
   preview 层同校验（目标存在且未被更正），IntegrityError 转 typed
   `DecisionJournalRevertError`。
5. **时区与 id**：`decision_date` 默认值 = service clock 换算
   `ZoneInfo("Asia/Shanghai")` 日历日；`decision_id = DJ-{decision_date}-{4hex}`
   （`secrets.token_hex(2)`），PK 冲突重试（≤3 次）；`recorded_at` UTC isoformat。
6. **新增第四个文件 `decision_journal_state.py`**（镜像 watchlist_state，58 行）：
   store 不 import principal_binding 的分层是既有模式，照抄。施工清单因此 +1 文件。
7. **source='owner_stated' 服务端强制**：preview 入参不收 source 字段，service 塞常量
   （镜像 watchlist assistant provenance 强制法）。
8. **工具计数 13 → 15**（13 读 + 2 写）；server.py 模块 docstring 的
   "Seven read tools" 一并改准确（顺手小修，属本Feature 面）。

## 4. 已跑命令与验证结果

- 本会话**未跑任何测试、未跑构建**（零代码阶段）。已跑的只有读代码/grep 与设计门外审。
- 设计门外审：`timeout 3700 scripts/codex_open.sh exec --skip-git-repo-check
  -C ~/fin-core "$(cat packet.md)"`，345s 完成，产物在台账 review.md。
- 新会话动工后第一验证：`uv run pytest tests/portfolio/ tests/read_capabilities/ -q`。

## 5. 剩余任务与风险

任务序列（原会话任务清单，session-local 已带不出来，以此为准）：

1. **store + service + state 三件套**：`portfolio/decision_journal.py`、
   `decision_journal_write_service.py`、`decision_journal_state.py`（§2 模板 +
   §3 决策 + 设计稿数据模型节）。service 提供 list/preview/apply/query（query 供
   read 工具与 record_decision-list 共用；limit 默认 50 上限 200，排序
   decision_date DESC, recorded_at DESC，回填 `reverted_by`）。
2. **接线**：wiring.py（ReaderWiring 字段 + 构造块镜像 watchlist :142-168）、
   server.py（deadline 表 +2、描述 +2、注册循环 skip 集合、两个 custom handler、
   docstring 计数）。
3. **测试**：`tests/portfolio/test_decision_journal.py`（store：IFF 三重、权限
   0700/0600、空表、id 冲突重试、CST 默认日、revision/audit）+
   `test_decision_journal_write_service.py`（token 单次/过期/重启失效、闭集拒绝、
   confirmation_phrase 逐字全字段、apply 失败零行落库 token 不复活〔用坏 store
   stub 让 append 抛〕、query 过滤）+ read_capabilities 三处钉死同步。
4. **实弹探针**：复用 `test_stdio_roundtrip.py` harness 打真 server（隔离 environ）：
   preview→apply→read 全链 + 复盘问询探针（read 命中 + G-first 未被取代）。
   测试绿≠跑通，分开报。
5. **人格 + 收口**：`~/fin-data/consult-agent/CLAUDE.md` 备份后增补（不催条款、
   复盘=日志查事实/G-first 判断、计数行 12+1→13+2——计数行属 owner 会签项，
   改后明确报「待签」）；**README 冻结行一律不动**（owner 会签项）；
   **吓人 diff 外审一次**（durable state+公共入口命中规则 5 判据；复用
   run-design-gate skill，packet 换成 diff+提交清单+固定四问，声明 §3 两处修正
   请评审）；通过后收口：合 main、**删设计稿**（Git 即归档）、NOW.md 板 B 加
   `cap:decision_journal` 能力行、§3 修正与设计稿「非目标 why」提炼进
   DECISIONS.md、commit 史即完成叙事。

风险/注意：
- `test_stdio_roundtrip.py`/`test_wiring.py` 是精确集钉死，新工具上线必破，
  属预期——同步而非绕过。
- 写 token manager 测试过期用例直接改内部 dict（watchlist 先例 ：152-153），别绕。
- 外审若对 §3 两处修正有异议，按裁决流程落稿，别先斩后奏。
- 本仓 namespace 包无 `__init__.py`：一次性诊断脚本一律文件模式跑（NOW.md 遗留观察 3）。
- 提交后钩子自动重渲染 systemd 单元；合 main 前留意 hook.log（部署默认直上）。

## 6. 不要动的东西

- `production_capability_provider.py`——修正一路线＝零改动（provider 若真要改，
  说明路线走偏了，停下重对 §3-1）。
- `read_capabilities/types.py` 与 `_invoke_tool` 白名单——custom handler 路线＝零改动。
- README 冻结契约行（工具计数）——owner 会签，不自动改。
- G 域（guo_teacher_research 的认知面）——owner 决策非 G 来源，journal 不入 G。
- `READ_TOOL_NAMES` / `WRITE_TOOL_NAMES` 元组——direct-runner 路线不需要动它们
  （WRITE_TOOL_NAMES 若测试钉死其长度，保持原样，新写工具不进该元组）。
- 既有 13 工具的描述文本/语义/timeout——除 docstring 计数外零触碰。
