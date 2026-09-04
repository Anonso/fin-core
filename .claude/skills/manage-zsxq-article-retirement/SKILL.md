---
name: manage-zsxq-article-retirement
description: 退役/换锚 shared KB 里的 ZSXQ 文章（删文章/下线文章/一篇换成另一篇/换版本/粉丝稿换 G 审核版）。覆盖引用闭包三件套（文章文件/index 行/标签墓碑）与认知主线换锚顺序——机验 span 锁定来源文件必须在盘，必须先重锚后删除。KB 文章域的任何新退役场景扩写本 skill，不新建。Not for capture 调度（见 manage-zsxq-capture）、抓取内容 bug、主线文档常规审校。
---

# Manage ZSXQ Article Retirement（退役 · 换锚）

不变量与裁决史：删改决策看 `docs/DECISIONS.md`；主线标注契约由
`scripts/verify_mainline_annotation.py` 与 `cognition_mainline_readmodel.py`
的校验器机械执行，本 skill 只管操作程序。

## 边界与扩展规则（新场景扩这里，不新建 skill）

- **本 skill 管**：shared KB 内**任何** ZSXQ 文章的退役类操作——删除/
  下线/换锚/换版本/多篇合并进一篇。操作对象是文章，不是文章类型。
- **主干不变**：owner 授权 → 备份 → 闭包 grep → 主线重锚（被引用时）→
  机验 → rebuild → 删除三件套 → 探针。场景差异**只**落在两处：
  ①替代源性质判定（源表第 4 格子串）；②该文实际存在的闭包面
  （deep-read 有无、timeline 有无、主线是否引用——grep 定 A/B 路线）。
- **已知场景**：操作 A（无主线引用退役）、操作 B（换锚退役；0903
  粉丝稿→G 审核版是**实弹参照，不是边界**）。
- **第三种场景**（退役 G 亲笔稿、混合研报稿退役、多篇合并、非 ZSXQ
  来源替代等）：**在本文档追加「操作 X」小节**，硬边界/闭包/探针/
  进程残余各节直接复用，禁止另立姊妹 skill。
- **另立 skill 的唯一条件**：操作对象离开「shared KB 文章」域
  （portfolio 行、wiki 结构、capture 调度归各自 skill）。

## 硬边界（先于步骤）

- 生产数据删除 = 家规 2 副作用类：只在 owner **当时明确要求**时执行，
  删除范围开工前对 owner 明说（owner 话语即授权，不外推）。
- 家规 4 备份先行：`~/.local/share/fin-analyse/article-prune-backup-<TS>/`
  目录 0700、文件 0600，`manifest.json` 记 reason + items + sha256
  （先例 `article-prune-backup-20260829T111757`、`-20260904T222303`）。
- **顺序不可反**：`verify_mainline_annotation.py` check 2 要求每个主线
  单元引句逐字存在于其来源文章文件。主线引用未重锚前删文件 = 机验永久
  fail-closed、「不入档、rebuild 不放行」。先换锚、机验、rebuild，再删。

## 引用闭包（动手前对目标 article_id 全库 grep）

1. `articles/<file>.md` — read_article/search 的本体（search 扫目录，
   **删文件才真正出检索**，只删 index 行不够）。
2. `index.json` 行 — read_article 定位 + reference 巷道候选（普通栏）；
   原子替换、`total` 同步、`updated` 刷新、保持 0600。
3. `runtime/cognition/article_tags.jsonl` — append-only：删标签写墓碑
   （`action=remove`），不物理删历史。CLI：
   `uv run fin-cognition tags remove <article_id> <tag>`（每标一条；
   输出 `added` 是 append 通用状态，以 jsonl 尾行 `action:"remove"` 为准）。
4. `manual-annotations/g-cognition-mainline.md` — 源表行 + CU 单元 +
   时间语义索引，**三处一起改**。
5. deep-read artifacts（`runtime/cognition/deep_read_artifacts/`，按文章
   content hash 绑定，有无按 `deep_read_availability` 查）。
6. 不改：`.index-recovery-*/` 快照、traces/审计面（历史记录）；timeline
   （read_instrument_scores）只收 score≥6，无分文章天然不在。

## 操作 A：简单退役（主线无引用）

1. 备份：文章文件 + index.json 全量 + manifest。
2. 删除三件套：`rm` 文章文件 → index.json 原子移除该行 → 标签墓碑。
3. 探针（见下）。

## 操作 B：换锚退役（主线有引用；实弹参照 0903 粉丝稿→G 审核版）

1. 备份：文章文件 + index.json + **g-cognition-mainline.md 全量** + manifest。
2. 主线换锚三处：
   - **源表加新源行**：`| S-XXXX | <发布时间> | \`knowledge-base/articles/<file>\` | <性质说明>；SHA-256 前缀 \`<sha256sum|cut -c1-10>\`。 |`
     nature 由第 4 格**子串**判定（`mixed_published_report` /
     `AI-assisted/content-mixed` / `spoken_fan_transcribed`，都不含则
     G_ORIGINAL）——新行措辞避开这些子串除非有意设档；旧源行保留原
     nature 标记不动，追加「owner 裁决由 S-XXXX 换锚取代」。
   - **CU 单元重锚**：`G 原文：“span1”“span2”。` 里 span 必须是替代文章
     **逐字**子串；弯引号 `“”` 是 span 分隔符，span 内不得再含弯引号
     （含引号术语拆多段 span）；来源/深化表达/验证/行动指导同步改写，
     Agent 推理/投资选择/交易策略保持「无」。
   - **时间语义索引行**：published_at / 观察基础 / 窗口表述同步
     （forecast_window 即读此处）。
   - **状态头**：追加裁决记录 + `as_of=<真实复核时点>`；校验器 regex 取
     文档**首个** `as_of=`，旧时点改写成不含 `as_of=` 前缀的散文。
3. `timeout 300 uv run python scripts/verify_mainline_annotation.py`
   → 必须 `RESULT: PASS`（fin-core 根目录跑）。
4. rebuild（三缝与 consume 脚本 `_rebuild_cognition_mainline` 一致）：
   `rebuild_if_stale(annotation_path=<canonical KB>/manual-annotations/g-cognition-mainline.md,
   readmodel_root=~/.local/state/fin-analyse/cognition-mainline-readmodel-v1,
   manifest_path=<canonical KB>/runtime/operations/g_working_set/manifest.v1.json)`
   → disposition `PUBLISHED`；核对 payload：新源 nature 正确
   （G_ORIGINAL 才入投影、SPOKEN_FAN_TRANSCRIBED 只入档）、单元已换锚。
5. 删除三件套（同操作 A 第 2 步）。
6. 探针。

## 探针阶梯（两类操作通用）

- `read_article` 退役 id → `article_not_found`；替代文章 → READY。
- `read_article_search` 主题词 → 替代文章命中、退役稿 0 命中。
- 操作 B 另核：机验 PASS + rebuild PUBLISHED + 投影 nature/单元归属。

## 常驻进程残余（不阻塞，owner 拍板）

进程级缓存只有 `read_article_search`（wiring preflight 建一次）：问询
Claude 会话的薄 server 与 gateway 的 MCP 子进程回收前可能仍返回退役稿
（正文自降权标注，无害降级）。index / read_article / readmodel 均每请求
落盘、即时生效。**不要顺手杀进程**：gateway MCP 子进程的
mcp_stdio_watchdog 只管父死清理、不 respawn，杀 = 断问询链路；问询会话
按日 `--resume` 自然换新，确需立即生效由 owner 明示后重启。
