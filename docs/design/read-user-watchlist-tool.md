# 短设计：read_user_watchlist — 问询薄 server 第 7 读工具

> 状态：设计门待过；过门后动代码，合入后本文件删除（Git 即归档）。
> 触发：2026-08-29 问询 CLI 实测「看下当前自选股」——agent 只能答持仓
> （read_actual_portfolio），自选股 store 存在（r22，8/11–8/12 加入）但
> v1 工具面未暴露（read-capability-server-design.md §1 非目标行）。
> 用户当面踩中该缺口：「理论上有自选股」。

## 1. 目标 / 非目标

**目标**：问询 agent 能读到 user-maintained 自选股列表。自选是
**user context / 注意力焦点，永非投资证据**（`user_watchlist.py` 模块
docstring 的既有语义；`SourceKind.USER_CONTEXT` 类型现成，本设计只是
把既有语义接进既有产品入口）。

**非目标**：不做自选写入口（唯一 mutation seam 仍是
`scripts/manage_user_watchlist.py` 的命令通道）；不把自选注入
read_g_context（每次问询都带的爆炸半径，未来有真实需求另立设计）；
不动 read_actual_portfolio 语义。

## 2. 契约（新增一行的工具面）

| 工具 | 入参 | 返回 | deadline | 失败语义 |
|---|---|---|---|---|
| `read_user_watchlist` | `question` 必填（同 ProductionReadRequest 校验）；无 instruments / as_of（列表是当前态，revision 戳） | `entries[{market_symbol, name, added_at}]` + `revision`；sources = 1× `CapabilitySource("user-watchlist", USER_CONTEXT, NON_G)` | 10s（同 portfolio：owner-only 本地态，无网络） | 构造失败→常驻降级 `user_watchlist_unavailable`（§6 两级不对称）；list() 异常→`user_watchlist_read_failed`；**entries 空是合法态不是 gap**（用户自选可以为空） |

工具描述写明触发场景（自选股/自选/watchlist/观察票类问题）、user-context
语义、可与 read_market_snapshot 配对取现价。空列表→答「自选为空」，
不是报错。

## 3. 改动面（4 文件 + 测试）

1. `fin_analyse/portfolio/watchlist_write.py`（推导收敛，防漂移）：
   manage 脚本里的 `_check_state_root` + `_require_production_state` +
   `_INSTALLATION_NAMESPACE` 移入本包（公开名
   `require_production_watchlist_state`），脚本改 import、行为不变
   （人用 CLI 兼容）。依赖 `runtime.state_roots` 与
   `principal_binding.LocalInstallationPrincipalProvider`，均 stdlib-only。
2. `fin_analyse/guo_teacher_research/production_capability_provider.py`：
   新增可选 `user_watchlist` store 参数 + `read_user_watchlist(request)`
   方法（模板 = read_actual_portfolio：`_bounded_inputs` → None-store
   降级 → try/except → gap）。
3. `fin_analyse/read_capabilities/wiring.py`：try/except 构造
   `require_production_watchlist_state()`（失败仅记 stderr、工具降级），
   传 provider；`READ_TOOL_NAMES` 加名。
4. `fin_analyse/read_capabilities/server.py`：deadline 表 + 描述表各加
   一行；尾行 stderr 计数从硬编码 "6" 改 `len(_TOOL_DEADLINE_SECONDS)`。

测试：provider 三态（unavailable/read_failed/success+USER_CONTEXT
source）；wiring 构造/降级；推导函数单测（tmp XDG）；脚本行为不变
（既有 tests/scripts 全绿即证）；tool_descriptions/stdio_roundtrip
计数更新；test_import_closure 断根验收保绿。

## 4. 影响入口与生效路径

问询 CLI（consult-agent/.mcp.json → fin-readonly stdio server，每会话
新起进程）：fin-core main 合入后下次问询自动生效，无单元重启；
Daily/ZSXQ systemd 单元不触。main 工作树即问询面运行源——实现一次性
完成并立即 commit，压缩 dirty 窗口。

## 5. 验证

1. `uv run pytest tests/read_capabilities tests/portfolio tests/scripts tests/guo_teacher_research -q`；
2. stdio 冒烟：initialize → tools/list=7 → tools/call read_user_watchlist
   返回 r22 全量条目；
3. 真实问询探针：consult-agent 目录 `claude -p "看下当前自选股" --model
   glm-5.3 --strict-mcp-config --mcp-config .mcp.json --allowedTools
   "mcp__fin-readonly__*"`，验新工具被调、答案基于自选列表。

## 6. 为什么不是别的做法

- 注入 read_g_context：改 G 分层契约，所有问询都背自选成本，非每问相关；
- agent 直跑 manage 脚本：CLI allowedTools 只有 `mcp__fin-readonly__*`，
  无 Bash 通道；
- 扩 read_actual_portfolio 顺带返回自选：混淆两个语义域（user-confirmed
  投资快照 vs user context），破坏既有工具契约。

净复杂度：+1 只读工具；推导逻辑从脚本收敛进 owner 包（删脚本本地副本，
单点化）；无新抽象、无新 durable state、无网络面。

## 7. 设计门裁决记录（动代码前落稿）

- elapsed_seconds：609（codex-open · deepseek-v4-pro · max，61,676 tokens）
- 发现数 / 采纳数：7 / 7（F1、F5 采纳变体；无 P0）
- F1 [P1] 采纳变体：`ProductionReadResult.sources` 确实无消费方（server 只投影
  value+data_gaps）——不设 sources、不造假承诺；user-context 语义改由 value 内
  `semantics` 字段 + 工具描述承载；不动 server 投影（避免全 7 工具响应 shape 变更）。
- F2 [P1] 采纳：wiring 构造捕获元组扩为 `(OSError, ValueError, UserWatchlistError,
  PrincipalBindingError)`（后两者是 RuntimeError 系，模板元组接不住→曾会变成 server
  启动级全灭）；验收探针=缺 identity/坏权限 root 时 server 仍起、仅本工具常驻
  unavailable。
- F3 [P2] 采纳：happy-path fixture 须置备 0700 root + 0600 64-hex
  installation-identity.hex，`test_builds_all_six_tools`→seven、shape 集合同步。
- F4 [P2] 采纳：`require_production_watchlist_state(*, home=None, environ=None)`
  透传，wiring 传 environment，测试隔离不靠 monkeypatch。
- F5 [P2] 采纳变体：推导落新 leaf 模块 `portfolio/watchlist_state.py`（不进
  write seam，职责名实相符；闭包净增为零——store/instrument_identity 链 provider
  已拉，硬断根不变）。
- F6 [P2] 采纳：四态镜像 `user_watchlist_reader_unavailable` / `read_failed` /
  `result_invalid`（含 isinstance 校验）；无 `core_incomplete` 对应物（列表无
  core/complete 概念，契约表写明）；gap 词形对齐模板 reader 中缀。
- F7 [P2] 采纳：read-capability-server-design.md §1/§3 加批注指向本设计的 git
  历史，防旧文档读错契约。
- 无发现项：Q2 时序/幂等本体（list() 只读、构造无写副作用、ro uri、空库合法态、
  CAS 未动）；Q4 无退化（数据等价 manage 脚本 list；无 Bash 通道下自选股问询从
  「拿不到」变「拿得到」）；五类型与既有 6 工具语义无破坏（加法性扩展）。
