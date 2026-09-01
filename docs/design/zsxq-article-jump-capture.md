# zsxq 文章跳转链接补抓 · 设计页（短设计 · 核心数据管线）

> 动因：2026-09-01《Trump Zone 现象研究报告》只抓到「目 ...」开头。根因不是
> 文章无正文，而是这类「群页只渲染预览 + 跳转链接」的文章在 Windows capture
> 的 F-07 补抓里被丢弃，且 WSL 侧对已入库 topic_id 无条件跳过，截断文无法升级。
> 本文与 zsxq-capture.md 互补；合入后按规则 5 删除。

## 根因（读码+生产 artifact 双重钉死）

1. `capture_zsxq_windows.cjs` F-07 在内存里回填 `topic.content_text`，但
   `collectCursorCoverage` 最终 `pages.push({... output})` 推的是补抓前的原始
   cursor 输出——回填全文被静默丢弃（生产 artifact `bec2857f` 实证：cursor
   output 仍是「目 ...」）。
2. F-07 只在群页 `[data-topic-id]` card 内查 `articles.zsxq.com` 锚点；卡片未
   渲染/锚点为相对 href/跳转靠 JS 时取不到链接，且无第二退路。
3. WSL `run_incremental` 保存循环对已索引 topic_id 无条件 `continue`；既有的
   `_should_recapture`（完整性修复）从未接线，是死代码。

## 方案

### Windows capture（老仓留馆资产 `scripts/capture_zsxq_windows.cjs`）

- 把 F-07 回填从 `collectCursorCoverage` 抽成 `backfillTruncatedInlineArticles`
  （可注入 deps 供测试），回填完成后 `output = JSON.stringify(parsed)` 再 push。
- 链接提取三退路：群页 card 锚点（按 `a.href` 绝对化判定）→ topic 详情页
  「查看详情」展开 + `articles.zsxq.com` 锚点 → 详情页正文兜底。
- 有界：每 cursor 页最多补抓 5 条（对齐 F-06）；任一补抓失败保留原文，
  绝不使整页作废；结束后恢复群页。

### WSL 消费（fin-core `cdp_scraper.py`）

- cursor 教师 talk 正文尾截断（`…`/`...`）→ `incomplete=True` +
  `incomplete_reason=cursor_content_truncated`（诚实降级，frontmatter/index
  落字段，与 `derive_quality` 截断语义一致）。
- 保存循环遇到已索引 topic_id 改用 `_should_recapture`：读现有文章文件正文尾
  是否截断 && 新正文严格更长 → 原位覆盖（id=`zsxq-<topic_id>` 稳定，文件/index
  同位替换）；否则跳过。deep-read 按 content hash 自然失效重生，G 工作集按
  manifest 重算。

## 契约与边界

- cursor projection schema v4 五键/八键不变，只改 `content_text` 值——不涉及
  跨侧接口契约修订。
- 不采集非 teacher 正文；不新增持久化状态；回填只升级正文，不改来源资格。
- 未跑真实 Windows Chrome 前只能称「测试绿 + 回放验证」，真实 canary 是产品
  完成硬门禁（家规 7）。

## 验证

- 老仓：`tests/scripts/test_capture_zsxq_windows_inline_backfill.py`（node 注入
  fake deps：回填持久化、三退路、budget 5、无链接/更短不覆盖）+ 现有
  consistency 套件。
- fin-core：`tests/scraper/test_cdp_incomplete_repair.py` 扩展（截断标记 +
  `_should_recapture` 文件尾判据 + 保存循环升级路径）+ 默认套件。
- 生产：部署后下一真实班次重放 3 日窗口，验 Trump Zone md 全文 +
  deep-read 重建 + working set READY 无新 gap。
