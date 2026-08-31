# Daily 盘后材料投影短设计

## 事实

2026-08-31 15:30 的 postmarket product 记录 `data_gaps=[]`，但用户收到的正文只
看见指数涨跌幅和空仓快照。当前 `daily_workspace_generator` 把完整市场概览
JSON（实测约 13K 字符）直接切到 4000 字；切点可能位于 JSON token 中间，后半段
的概念榜、成交榜和限制说明因此不可可靠消费。市场指数的原始报价还包含点位，
但 `MarketIndexObservation` 没有投影该字段。

## 决策

1. 市场概览在 Daily prompt 中改为 FIN-owned 的有界纯文本投影，不再截断 JSON。
   投影固定保留：交易日/观察模式、四大指数（点位、涨跌幅、成交额）、宽度或
   明确不可用、行业/概念强弱与成交榜、成交额个股榜及限制摘要。
2. `MarketIndexObservation.level` 作为可选字段从既有 Eastmoney/Tencent 原始
   `f2`/报价点位透传；Tencent 的 amount 字段按既有适配器单位换算为元；
   缺失时保持 `None`，不推断。
3. 盘后/闭市读取优先使用既有 Eastmoney 指数端点（仍保留 Tencent 失败回退），
   以获取指数点位、成交额和涨跌家数；盘中仍保持 Tencent 实时优先，避免把延迟
   完成日数据冒充盘中数据。
4. prompt 明确：有任何核心市场事实时，先给至少两条带数字的当日事实；provider
   限制只列影响判断的少数项，不得把“部分缺口”自动写成唯一待处理事项。

## 不做

- 不新增确认池数据源、scheduler、第二状态 owner 或交易建议。
- 不把缺失的市场宽度/确认池行情补成估算值；仍按真实 gap/未知呈现。

## 验收

- 投影结果始终是完整文本、长度有界，不含半截 JSON。
- 有效 fixture 的指数点位、行业/概念榜和成交榜均能进入 prompt。
- 无宽度或无点位时明确写“不可用/未知”，不编造。
- `uv run pytest -q tests/operations/test_daily_workspace_market_material.py tests/market/test_current_market_overview.py`
  通过；随后跑默认测试集。
