# W2' 移植清单（出处与决策记录）

本仓 = fin-analyse keep-set 闭包的移植（rebaseline §0.5.6 ③b，D-025）。
源：`~/fin-analyse` @ `c2d4dd06`（git archive，仅跟踪内容——.env/pyc/数据天然排除）。
依据：docs/design/new-repo-migration.md（含 2026-08-29 设计门裁决，1P0/5P1/2P2 全采纳）。
包名 `fin_analyse` 不变，import 零改。

## 构成（501 文件）

| 部分 | 数量 | 说明 |
| --- | --- | --- |
| 四入口 import 闭包 | 133 | scheduled Daily 检查点 · ZSXQ consumer/poller · 薄 server（read_capabilities.server）· deepen（deep_read_artifacts/availability）；AST 闭包 walker |
| 相对/懒导入不动点补齐 | 14 | walker 只跟绝对导入的盲区：claims 门面（extractor/llm_extractor/models/claude_backend/hermes_backend）、context/projection、ingestion/runtime_health、market adapter/registry/providers（akshare/baostock/eastmoney/easyquotation）——缺一则 import 即炸 |
| 附录 C.1 包保护增量 | 54 | scraper 21 · cognition 25 · portfolio 7 · margin 1（活代码，均不在已执行 delete-list） |
| `*.v1.json` package-data | 4 | runtime_budget · hermes_managed_assets · market_evidence_plan · consultation-advisory-prompt（C.2 事故教训资产） |
| config/ 运行时资产 | 42 | llm.yaml（全 `${ENV}` 占位，零实值）· g_context_windows · position_topic_rules · a_share_calendar · capabilities.yaml + capabilities/ 35 项 · article_tag_rules · portfolio schema · 两个 .example |
| 运维脚本 | 7 | manage_actual_advisory_portfolio · manage_user_watchlist · reconcile_daily_workspace_day · alert_daily_workspace · render_daily_workspace_services（单元渲染，步 5 用）· fin_tool_usage_audit + finq（D3 供数） |
| 盲评 harness | 6 | tools/effect_evaluation（B1/B2/六题测量机器，C.2 显式随迁） |
| 文档 | 14 | AGENTS/CLAUDE/UBIQUITOUS_LANGUAGE/GLOSSARY/DECISIONS/BUGS/system-overview/design 六页/read-capability-server/consult-agent-workspace/night-shift 六题源/rebaseline |
| 测试精选 | 221 | imports 全落清单内；archive-delete-tests 已扣；conftest 精选重造（只留 LLM 阻断闸，make_signal/fixed_now/industry-chain 桩零引用即删）；pytest 配置原样 |

## 剪枝决策

- **依赖 drop**：`numpy`/`pandas`（清单代码+测试零直接 import；akshare 传递自带）。
- **依赖补声明**：`baostock`（baostock_provider 直接 import，原靠传递）。
- **保留易误判项**：`anthropic`（claims/claude_backend:17 懒加载，llm.yaml adapter 可达）、
  `easyquotation`（market provider 直接 import）、`yfinance`（akshare provider 懒加载可达）。
- **[project.scripts] 只留 fin-cognition**：其余五个入口模块（knowledge/ingestion/admin/project_sync/codex_route_admin 的 cli）不在 keep-set。
- **留馆（老仓，不迁）**：release 机器（build/prepare_fin_release、canary）、gateway 与 Hermes 集成（apply_fin_hermes_external_integration、heal_gateway_once、codex_*.sh）、Windows capture 链（zsxq_windows_incremental_scheduler 等）、旧咨询链（consultation agent_module/gateway handlers/moa/signals/analysis 栈）、workflow 机器（dev-orchestrator/opsx/fix-bug、worktree 工具）、repo `knowledge-base/`（F1：68K 无读方旧副本，步 6 绝根；canonical KB=190M XDG-shared 根，`knowledge_root.py` fail-closed）。

## 施工步 4 记录（2026-08-29，测试全绿 = 2839 过 / 2 跳过 env 门控 / 35 去选 marker）

- **依赖钉版**：`mcp>=1.0,<2`（2.x 改名 FastMCP，API 破坏）；`playwright==1.60.0`
  （对齐老仓 lock，浏览器 build 匹配）；`baostock` 补声明。
- **测试精选二次修剪**（测的是留馆机器，静态 import 判不出来）：
  删 12 个测试文件（release 机器 205 败、codex proxy/runbook/Windows capture、
  CLI 子进程类、单入口整仓卫生读 Makefile/catalog）+ `test_capture_ingest` 内
  引用留馆 `scripts/import_zsxq_capture.py` 的一个函数；
  两个 migration 卫生测试做「文件在场才断言」适配（护栏对象部分留馆）。
- **fixture 数据**：tests/ 下 16 个非 .py 文件必须随迁（replay/raw 类测试吃数据）。
- **delete-list 复活一件**：`_semantic_snapshot_child.py` 被老仓 delete-list 误标死，
  实为 semantic_state 沙箱运行时必需（老仓在用）——随迁。
- **umask 陷阱（步 5 必读）**：agent shell 快照默认 `umask 077`，`uv sync`/tar 解包
  产物全 600/664 → semantic_state 沙箱检查（拒组可写）、playwright driver 执行位
  全被误拒。venv 与拷贝树必须归一 644/755（本仓已做）；步 5 单元渲染时对
  fin-core 树做同样核对。

## 过渡期事实

- 状态源：本仓 `docs/pm/NOW.md` 为过渡快照，cutover（迁移步 5）前权威在老仓
  `~/fin-analyse/docs/pm/NOW.md`。
- 数据根零移动：durable store 全在 XDG/fin-data 固定路径（代码决定），不随 checkout 走。
- 调度重指向（步 5）：systemd daily 8 timer + 2 模板 + zsxq poller/consumer、
  Windows capture 七时点、`~/.config/fin-analyse/*.env`、consult-agent/.mcp.json——
  前后逐条登记，ZSXQ 水位线（`--not-before-run-id`/`--source-commit`）原样延续。
