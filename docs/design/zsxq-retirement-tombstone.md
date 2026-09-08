# 退役材料墓碑清扫（zsxq-retirement-tombstone）· 短设计 v1

**日期**: 2026-09-08 · **状态**: 设计稿（设计门后施工） · **owner 依据**: 拍板「修复」（NOW#36：S-0904B 退役不 durable，09-05 backfill 复活实证；A/B q05 异常归因在案）。

**设计门**: cmd·deepseek-v4-pro 正常完成 · P1×1+P2×4+P3×3 全采纳 · 台账 `~/.local/state/fin-analyse/design-gate/zsxq-retirement-tombstone-20260908/`。
**v2 施工修正（裁决落稿）**：删除闸=三条件（manifest 可解析+items 含该 article_id+现盘 sha256 一致）——typo id/备份残缺/内容漂移一律拒删；index 原子写（mkstemp+fsync+os.replace+0600）且无变更不落盘；retirement.py 零 stdout；触发面如实=「下一个成功 ingest 周期」（partial/手工 CLI 不即扫，取舍在案）；手工流程权威=退役 skill（操作 A/B 各加登记步骤+探针对账）；backup_ref 存目录名。

## 1. 问题与目标

退役流程现状 = 手工四步（备份→删 md→清索引→标注记录），**无防复活闸**：capture_ingest 内核重导入已退役文章时（09-05 实证），文件与索引条目整体复活，检索可命中退役材料（A/B q05 污染根源）。

目标：退役决定一次登记，此后任何重导入在**同一 ingest 周期内被自动清扫**，退役成为 durable 状态；清扫绝不阻断、绝不触碰 ingest 内核。

非目标：不改 capture_ingest 内核（dirfd 锁定机器，零触碰）；不做退役命令行工具（手工流程文档化在 README，频次低，家规 11）；不做 capture 源侧删除。

## 2. 设计

### 2.1 墓碑登记表（唯一新增配置）

`config/zsxq_retired.json`：
```
{schema_version: fin.zsxq-retired/v1,
 retired: [{id: zsxq-<topic_id>, retired_at, reason, backup_ref}]}
```
退役动作 = 手工四步（不变）+ **登记一行**；清扫器只认这张表。

### 2.2 清扫挂点（唯一代码面）

`scripts/consume_zsxq_capture_folder.py` 收尾「挂点永不阻断 ingest」惯例区（裁决收件箱 reconcile 同层）新增 `_sweep_retired_articles()`：
- 读 registry → 对每条 tombstone：`articles/<id>.md` 存在则删（**前提=registry 行含 backup_ref 且备份文件存在**，否则只告警不删——防无备份误删）；`index.json` 含该 id 条目则移除（total 同步）；
- 每条动作落 audit 行（复用 `_append_rebuild_audit` 惯例，`retired-sweep.v1.jsonl`）；
- 全程 try/suppress——挂点失败不影响 ingest 结果；
- 幂等：重复跑无动作即无行。

实现放 `fin_analyse/ingestion/retirement.py` 新模块（家规 6），consumer 只挂一行调用。

### 2.3 首批登记

S-0904B（`zsxq-45548825112521288`，backup=article-prune-backup-20260904T222303，09-08 已手工清扫过一轮——登记后清扫器转为持续值守）。

## 3. 验收探针

- 造假文章（tombstoned id）+ 索引条目 → sweep 后 md/索引条目消失、audit 行落账；
- 无备份的 tombstone → 不删、告警行；
- 非 tombstone 文章 → 不动；
- registry 缺失/损坏 → sweep 静默跳过（fail-open，与挂点纪律一致）；
- consumer 全量回归（tests/ 现有 consumer 测试）。

## 4. 开工四句话

- **改哪些文件**：新增 `fin_analyse/ingestion/retirement.py`、`config/zsxq_retired.json`、测试；`scripts/consume_zsxq_capture_folder.py` 挂一行；设计稿合入后删。
- **影响哪个入口**：consumer 收尾挂点——ingest 结果与语义零变化，清扫失败不影响 ingest。
- **怎么验证**：§3 探针 + consumer 回归。
- **为什么不是别的做法**：改 capture_ingest 内核=高风险重手术且闸点不唯一（手工/backfill 多路径）；源侧删除超出本仓边界；不加闸则 BUG#36 复发。
