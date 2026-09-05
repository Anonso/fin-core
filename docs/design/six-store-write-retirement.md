# 六仓停写 + cli deep-read 退役 · 短设计（BUG-047 B2-P1③ 裁决施工）

日期：2026-09-05 23:00 · 裁决：BUGS BUG-047 B2-P1③ owner「按推荐处理」（fce6a99，勘误 79f5812）。
动 durable state 写入面，按规则 5 出本页；合入后删除（Git 即档案）。

## 改哪些文件

1. `fin_analyse/cognition/zsxq_apprentice.py`
   - 删 `deep_read()` 内 `source_repo.upsert(source)`（:316）与 downstream 持仓块
     （:427-446：unit/chain/cluster/clock/suggestion 五类 upsert + cluster 的 find-merge
     读）。in-run 计算（assign_theme_clusters/evaluate_dynamic_clock/generate_research_
     suggestion）全保留，结果照常进 `ZsxqApprenticeResult` → 工件 payload；
     `_merge_theme_cluster`（唯一调用点在被删块，历史簇合并行为随停写一并消失）。
   - 删 `__init__` 六个 repo 属性与 `runtime_root` 参数（参数只喂 repo；构造点仅
     deep_read_artifacts:323 与已退役 cli）。
2. `fin_analyse/cognition/cli.py`：删 `deep-read`（:89-140）与 `refresh-clocks`
   （:181-192±）两命令及孤立 import。
3. `fin_analyse/cognition/dynamic_clock.py`：删 `refresh_all_clocks`（:104-150，唯一
   外部读方随 cli 退役）与 `refresh_clock`（唯一调用方是前者）；`evaluate_dynamic_clock`
   引擎内使用，保留。
4. 测试：`test_zsxq_apprentice_pipeline.py` 持久化断言改为「六仓不落地」+ 结果内容
   断言保留；`test_dynamic_clock.py` 删 refresh 相关测试；其余闭包 grep 后同步。

**不动**：`deep_read_artifacts.py` 主流程（仅构造点去参）、六仓数据文件（留存）、
memory_store 五仓（另一体系，guo:v0 后生产只读）。

## 影响哪个入口

- 生产消费链：consume→深化排空→`DeepReadArtifactService`→`deep_read()`——写入面从
  「工件+六仓」变「仅工件」；读方（fresh pair READY、read_g_context 注入）零变化。
- poller/consumer 为 timer oneshot：下次触发自动用新码，无需人工重启；薄 server 读
  路径不触六仓，不重启也保持正确。
- 手动入口：`cognition deep-read / refresh-clocks` 消失（B2-P1③ 佐证：生产装配点仅
  cli 一处，guo:v0 时代残留）。

## 怎么验证

1. 全仓 pytest 绿（闭包内无残引）。
2. 实弹探针：取已爬文章跑 `DeepReadArtifactService.generate`（真实腿），断言
   full/compact 生成且 fresh；六个 jsonl 字节数+sha256 前后零变化。
3. grep 闭包：六个 repo 属性名 / `refresh_all_clocks` / `deep-read` 无生产残引。

## 为什么不是别的做法

- 写入加配置开关：规则 6（不为想象的需求预建配置）+ 零读方事实——开关是给不存在的
  读者预备的。
- 只停写留仓属性/参数：死代码半截，规则 12。
- 六仓为主：无任何读方证据支持（审计 P1-3 + 勘误后接线图）。

## 审计门判定（R1-R3）

规模小（净删）；非门面路径；**命中 R3**（生产 durable 写入语义变更）且 BUG-047 裁决
自带「修复 diff 命中 R3 过审计门」条款 → 施工完成后、合入前跑一次外部审视
（codex_open.sh，packet=diff+固定四问），裁决逐条落稿后收口。
