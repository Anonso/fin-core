"""BUG-002 回归：on-demand 行情三层执行器预算必须配平最坏嵌套需求。

2026-08-28 诊断：每个 symbol worker 最坏向 detail executor 提交 3 个任务
（quote + 日线 + 30 分钟线）。旧配置 detail max_outstanding=10 < 5×3=15，
盘前上游慢（10s 超时）时自挤占，标的被误标 ON_DEMAND_MARKET_CAPACITY_EXHAUSTED
（trace 三次全中、延迟整齐 12-14s）。此测试钉死配平不变量，防参数再漂移。
"""

from __future__ import annotations

import fin_analyse.market.on_demand_tactical_context as odtc


def test_detail_executor_budget_covers_worst_case_nesting() -> None:
    worst_case_submissions = odtc._MARKET_SYMBOL_EXECUTOR.max_workers * 3
    assert odtc._MARKET_DETAIL_EXECUTOR.max_outstanding >= worst_case_submissions


def test_symbol_executor_budget_covers_max_symbols() -> None:
    assert odtc._MARKET_SYMBOL_EXECUTOR.max_outstanding >= odtc._MAX_SYMBOLS
