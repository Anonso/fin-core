from pathlib import Path

from fin_analyse.cognition.zsxq_apprentice import load_zsxq_cognition_source


def test_load_full_xingdapai_source(tmp_path: Path):
    article = tmp_path / "2026-06-18_2000.md"
    article.write_text(
        """---
id: 6421a58e9912
topic_id: 22255248888812480
date: 2026-06-18 20:00
score: None
column: 星大派特刊
companies: []
tags: []
is_qa: False
images: [images/6421a58e9912/000.png]
---

# 星大派特刊：半导体AI卡脖子材料全面分析与评估报告

星大派特刊：半导体AI卡脖子材料全面分析与评估报告。
核心机会：钼前驱体最具性价比；稀土氧化物地缘加需求双击；niche前驱体alpha最大；WF6已暴涨。
排名：钼前驱体14.5分，Y2O3/Dy2O3 14分，Niche前驱体13.5分，WF6 13分。
半导体卡脖子材料正在从芯片和设备进一步下沉到前驱体、特气、稀土氧化物等底层材料。

## 图片描述

### 000.png (LLM)

钼前驱体总分14.5，Y2O3/Dy2O3总分14。

## 图片OCR文字

### 000.png (OCR)

钼前驱体 14.5 WF6 已暴涨。
""",
        encoding="utf-8",
    )

    source = load_zsxq_cognition_source(article)

    assert source.source_rank == "t0_xingdapai"
    assert source.completeness == "full"
    assert source.article_id == "6421a58e9912"
    assert source.topic_id == "22255248888812480"
    assert source.column == "星大派特刊"
    assert "钼前驱体" in source.content
    assert source.image_descriptions == ["钼前驱体总分14.5，Y2O3/Dy2O3总分14。"]
    assert source.image_ocr == ["钼前驱体 14.5 WF6 已暴涨。"]


def test_load_external_context_when_column_is_plain_report(tmp_path: Path):
    article = tmp_path / "plain.md"
    article.write_text(
        """---
id: report1
date: 2026-06-18 08:55
column: 普通
---

# 能量评分8.7分

精读研报：某券商报告摘要。
""",
        encoding="utf-8",
    )

    source = load_zsxq_cognition_source(article)

    assert source.source_rank == "external_context"
    assert source.completeness == "full"


def test_load_fengxianjun_as_t0_teacher_cognition_source(tmp_path: Path):
    article = tmp_path / "fengxian.md"
    article.write_text(
        """---
id: fengxian1
date: 2026-07-30 14:00
column: 凤仙郡小故事
---

# 凤仙郡小故事：长期产业框架

这是一篇由老师发布的长期产业与商业框架文章。它讨论企业竞争、产业迁移和政策环境，
并明确提示：框架需要结合后续事实验证，不能直接翻译成交易指令。
""",
        encoding="utf-8",
    )

    source = load_zsxq_cognition_source(article)

    assert source.source_rank == "t0_fengxian"
    assert source.completeness == "partial"
    assert source.column == "凤仙郡小故事"


def test_short_article_is_partial(tmp_path: Path):
    article = tmp_path / "short.md"
    article.write_text(
        """---
id: short1
date: 2026-06-19 20:16
column: 星大派好问题
---

# 棒子以钼代钨

棒子以钼代钨，一个是替代的量是否大。
""",
        encoding="utf-8",
    )

    source = load_zsxq_cognition_source(article)

    assert source.source_rank == "t0_xingdapai"
    assert source.completeness == "partial"
