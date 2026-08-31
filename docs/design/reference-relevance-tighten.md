# read_ready_evidence 选材门收紧短设计

## 事实

2026-08-30 晚实弹探针显示，保偏光纤/CPO 问题下目标帖未浮出，同日无关帖
（INTP 心理帖、地产宏观帖）反而进入 reference lane。离线重放同样问题：
`_reference_is_relevant` 对 9 个同日候选返回 6 个 True，其中 3 个是泛词
误匹配（标题里“主线”“公司”“什么”等 2 字子串），目标帖被空事实帖挤掉。

## 决策

1. 标题子串匹配 `_has_common_substring` 的最小长度从 2 提高到 4；结构化
   keywords/theme_clusters 匹配保持不变（它们是领域标签，不是自由文本）。
2. 空 mapping facts 的候选（index companies/tickers 全空）不再与有事实候选
   同权重：选材后按“有公司/链事实优先、其余按原顺序”排序，避免空帖先占槽位、
   投影阶段再被丢弃。
3. 不新增配置项、不改 ready_evidence 投影契约、不改 G 准入。

## 验收

- 泛词标题（含“主线”“公司”“什么”但无领域词）在无关问题下不再通过。
- 目标帖（标题含 4 字领域词或 companies 重叠）仍能进入。
- 空事实帖排在带 facts 帖之后。
- 现有 reference 端到端回归 + 默认套件通过。
