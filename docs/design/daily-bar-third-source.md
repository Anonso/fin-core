# 日线第三结构化源（短设计）

> 状态：设计稿（家规 5：核心链路动代码前必备；施工合入后本稿按例删除，Git 即归档）。
> 设计门：施工前跑 `scripts/codex_open.sh exec` 外审（固定四问；评审者链见 D-045），packet 按
> `docs/design-gate-packet-template.md`。
> 立项依据：owner 2026-09-05 拍板「现在立项短设计」（09-05 审查报告 B：东财 push2his 自
> 08-02 不可达，个股/指数日线实际单源依赖腾讯 ifzq.gtimg.cn——免费非官方接口单点撑生产）。

## 背景与目标

- 现状：daily lane = [eastmoney（主源，08-02 起不可达）, tencent（事实唯一）]。东财恢复
  时间未知；腾讯一挂日线即全断（quote/两融/官方记录链不受影响）。
- 目标：日线 lane 增加第三个结构化源，恢复「任一免费源挂掉日线仍可用」的冗余；资格化
  语义与 BUG-036 修复后的对齐表一致（CN 时区 cutoff + 15:00 完成门）。
- 非目标：不动报价/两融/官方记录链；不做数据回填；不建自动健康检查之外的常驻设施。

## 候选源盘点（施工前逐项实弹核验；当前均为【待核验】级，不预设结论）

| 候选 | 形状 | 复权 | 初判风险 |
| --- | --- | --- | --- |
| 新浪 CN_MarketDataService getKLineData | JSON/JSONP 日线 | 默认不复权；qfq 需另一接口 | 非官方、频控未知、JSONP 解析 |
| akshare `stock_zh_a_daily`（sina 后端） | DataFrame→需转换 | qfq 可选 | 仓内 legacy 簇带真缺陷（P3-1）需先清；akshare 版本漂移 |
| 网易 money.163 history kline | 目录式 JSON | 不复权 | 接口年久、断供风险 |
| 腾讯同族第二接口（qt.gtimg kline） | 同 provider | 不复权 | 与现有 tencent 同源单点，冗余价值低，仅兜底候选 |

初判优先级：新浪 qfq（若前复权可机器核验）> akshare 簇修复复用 > 网易；「同族第二接口」
不计入冗余。**出局硬门槛（任一不满足即弃）**：bar 日期为 CN 日界；cutoff 语义 = 「bar 日
15:00 CST 完成」；OHLCV 四价 + 量可无损映射；可内容寻址落 immutable artifact；畸形
payload fail-closed。

## 契约（对接面冻结版；修订需双方会话确认）

- 新增 `fin_analyse/market/qualification_sources/<source>_daily_bars.py`，实现与
  `tencent_daily_bars.py` 相同的 QualifiedDailyBarReader 协议（`read(request)` →
  `QualifiedDailyBarSeries`；adjustment / deadline_at / timeout 语义一致）。
- driver 注册进 `_DAILY_DRIVER_CATALOG` + manifest daily lane 追加第三项（timeout 配平，
  建议 8s 档）。
- fallback 顺序 = [eastmoney, tencent, new]；`_FallbackDailyBarReader` 现为二元
  primary/fallback——扩链式或二元嵌套是本设计唯一结构决定点，施工时定并补测试。
- 失败语义：单源失败 = 既有 typed gap 降级到下一源；全部失败 = daily lane unavailable +
  顶层 gap。不新增 user-facing gap 码，不建自动恢复。

## 资格化对齐表（第三源验收 = 与东财/腾讯同表，无宽免）

| 项 | 要求 |
| --- | --- |
| cutoff | CN 时区 + 15:00 完成语义（BUG-036 同款盘后/盘中边界用例必配） |
| bar 数 | ≥120 根滚动窗口（`_LOOKBACK_DAYS` 余量同理） |
| 双源交叉 | 与腾讯对账 disagreement ≤0.3% READY（沿用现有资格化，不改阈值） |
| durable | 内容寻址 artifact，重放同 sha；无新增 mutable state、无并发写点 |

## 施工与验收

- 窗口：D3 建造静默结束后（NOW #31）。
- 验收：离线 contract 测试（cutoff 边界 / 字段映射 / 畸形 payload fail-closed / deadline
  预算）+ 实弹双源对账（新源 vs 腾讯 120 根 bar 逐根 close 对比 ≤0.3%）+ 公共入口探针
  （read_market_snapshot 零新增 gap）。
- 回滚：manifest 删第三项即回现状，无 durable 清理。

## 否决了什么

- 用搜索/LLM 源补行情数字——BUG-025 事故面 + 无资格化对应物（2026-09-05 拍板记录）。
- 现在就施工——D-043 建造静默；「腾讯也挂」的立项事故证据尚未发生，本设计提前备好，
  事故发生即随修随施工。
- 整体救活 legacy akshare 簇——P3-1 缺陷清单未清前不进生产 lane；仅评估其 sina 后端作为
  候选源实现参考。
