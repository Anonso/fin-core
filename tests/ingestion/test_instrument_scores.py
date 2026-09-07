"""Instrument score parser/store unit tests (synthetic fixtures only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.ingestion.instrument_scores import (
    InstrumentScoreRecord,
    build_record,
    instrument_scores_path,
    load_records,
    normalize_inline_codes,
    normalize_score,
    parse_article_records,
    parse_rows_from_text,
    update_instrument_scores,
    upsert_records,
)


def test_normalize_score_rules() -> None:
    assert normalize_score("85") == 8.5
    assert normalize_score("9.5") == 9.5
    assert normalize_score("8.5分") == 8.5
    assert normalize_score("95%") == 9.5
    assert normalize_score("") is None
    assert normalize_score("abc") is None
    assert normalize_score("0") is None
    assert normalize_score("150") is None  # 15.0 > 10 → invalid


TABLE_MD = """| 公司名称（代码） | 核心业务 | 所属板块 | 利好度 | 共识度 | 预计多久启动 | 持有时间 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 紫金矿业（601899.SH） | 金属矿业 | 有色 | 8.2 | 95 | 已发 | 1-3个月 |
| 中金黄金（600489.SH） | 黄金开采 | 黄金 | 8.8 | 92 | 已发 | 1-3个月 |
"""


def test_parse_markdown_table_with_aliases() -> None:
    drafts = parse_rows_from_text(TABLE_MD)
    assert len(drafts) == 2
    first = drafts[0]
    assert first["code"] == "601899"
    assert first["name"] == "紫金矿业"
    assert first["lihao"] == 8.2
    assert first["consensus"] == 9.5
    assert first["core_business"] == "金属矿业"
    assert first["horizon"] == "1-3个月"


def test_parse_table_with_separate_code_column_reversed_order() -> None:
    text = """| 公司代码 | 公司名称 | 核心业务 | 所属板块 | 利好度 | 共识度 |
|---|---:|---|---:|---:|---:|
| 603993 | 洛阳钼业 | 铜钴钼 | 有色 | 9.0 | 85 |
| 601899 | 紫金矿业 | 铜金 | 有色 | 9.2 | 88 |
"""
    drafts = parse_rows_from_text(text)
    assert drafts[0]["code"] == "603993"
    assert drafts[0]["name"] == "洛阳钼业"
    assert drafts[0]["lihao"] == 9.0
    assert drafts[0]["consensus"] == 8.5


LIST_MD = """1. **思源电气 002428**
   * 核心业务：开关/变压器
   * 所属板块：电网设备
   * 利好度：9.5
   * 市场共识度：92
   * 预计介入时机：1-2周
   * 持有时间：3-6个月
"""


def test_parse_list_style() -> None:
    drafts = parse_rows_from_text(LIST_MD)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["code"] == "002428"
    assert draft["name"] == "思源电气"
    assert draft["lihao"] == 9.5
    assert draft["consensus"] == 9.2


V3_LIST_MD = """1. **利通电子（603629）**
   核心业务：AI算力配套柜机；所属板块：算力服务；项目评分：8.5；投产启动：1-2周；供货周期：1-2月；情绪热度：78
2. **天孚通信（300394）**
   核心业务：光引擎；所属板块：光器件；项目评分：8.0；投产启动：已发；情绪热度：85
"""


def test_parse_v3_semicolon_single_line_format() -> None:
    """9/5 实测分号单行格式（owner 09-05 裁决：项目评分=利好度、情绪热度÷10=共识度）。"""
    drafts = parse_rows_from_text(V3_LIST_MD)
    assert len(drafts) == 2
    first = drafts[0]
    assert first["code"] == "603629"
    assert first["name"] == "利通电子"
    assert first["core_business"] == "AI算力配套柜机"
    assert first["sector"] == "算力服务"
    assert first["lihao"] == 8.5
    assert first["consensus"] == 7.8
    assert first["launch_in"] == "1-2周"
    assert first["horizon"] is None  # 供货周期≠持有周期，不落 horizon
    second = drafts[1]
    assert second["code"] == "300394"
    assert second["lihao"] == 8.0
    assert second["consensus"] == 8.5


def test_v3_semicolon_split_preserves_non_field_tails() -> None:
    """分号后不是「已知字段别名+冒号」时不切，旧格式单行取值逐字节保留。"""
    text = """1. **思源电气 002428**
   核心业务：开关/变压器；也做储能
   利好度：9.5
"""
    drafts = parse_rows_from_text(text)
    draft = drafts[0]
    assert draft["core_business"] == "开关/变压器；也做储能"
    assert draft["lihao"] == 9.5


def test_explicit_code_beats_name_map() -> None:
    """owner 09-05 裁决：正文显式代码优先——利通科技（603629）不得被名册覆盖为 920225。"""
    article = {
        "source_id": "zsxq-55521145582888484",
        "column": "普通",
        "title": "t",
        "article_date": "2026-09-05",
        "published_at": None,
        "article_score": 6.8,
    }
    md_text = (
        "## 图片描述\n"
        "1. **利通科技（603629）**\n"
        "   核心业务：AI算力配套柜机；所属板块：算力服务；项目评分：8.5；情绪热度：78\n"
    )
    records = parse_article_records(
        article=article,
        md_text=md_text,
        source_record=None,
        name_map={"利通科技": {"ticker": "920225"}},
    )
    assert len(records) == 1
    assert records[0].code == "603629"
    assert records[0].name == "利通科技"
    assert records[0].status == "ok"
    assert records[0].lihao_score == 8.5
    assert records[0].consensus_score == 7.8


INLINE_MD = """1. **600584 长电科技**：核心业务为HBM/2.5D/3D先进封装，所属板块为先进封装，利好度8.6，共识度88。
2. **002156 通富微电**：核心业务为AMD高端封测+存储封测，所属板块为先进封装，利好度8.4，共识度86。
4. **688200 翔宇微电子**：核心业务为2.5D/3D多芯片集成，所属板块为先进封装，利好度8
"""


def test_parse_code_first_inline_rows() -> None:
    """8/29 实测代码前置 inline 格式（D-037）：88 → 8.8。"""
    drafts = parse_rows_from_text(INLINE_MD)
    assert len(drafts) == 3
    first = drafts[0]
    assert first["code"] == "600584"
    assert first["name"] == "长电科技"
    assert first["core_business"] == "HBM/2.5D/3D先进封装"
    assert first["sector"] == "先进封装"
    assert first["lihao"] == 8.6
    assert first["consensus"] == 8.8

    article = {
        "source_id": "zsxq-22258828218828111",
        "topic_id": "22258828218828111",
        "column": "普通",
        "title": "8月下旬科技修复行情",
        "article_date": "2026-08-29",
        "published_at": "2026-08-29 12:19",
        "article_score": 6.8,
    }
    records = parse_article_records(
        article=article,
        md_text=f"## 图片描述\n{INLINE_MD}",
        source_record=None,
    )
    by_code = {record.code: record for record in records}
    assert by_code["600584"].status == "ok"
    assert by_code["600584"].lihao_score == 8.6
    assert by_code["600584"].consensus_score == 8.8
    assert by_code["600584"].article_score == 6.8
    assert by_code["600584"].parser_version == "v4"
    assert by_code["688200"].status == "needs_review"
    assert by_code["688200"].review_reason == "missing_fields:consensus"


def test_parse_inline_rows_normalizes_a_share_suffix() -> None:
    text = (
        "301631.SZ 壹连科技：核心业务为电芯连接组件，所属板块为新能源设备，"
        "利好度9.3，共识度86\n"
        "688008.SH 澜起科技：核心业务为内存互连芯片，所属板块为半导体，"
        "利好度9.5，共识度95\n"
        "1651.HK 津上机床中国：核心业务为高端数控机床，所属板块为高端制造，"
        "利好度9.0，共识度83\n"
    )
    drafts = parse_rows_from_text(text)
    assert [(draft["code"], draft["name"]) for draft in drafts] == [
        ("301631", "壹连科技"),
        ("688008", "澜起科技"),
        ("1651.HK", "津上机床中国"),
    ]
    assert drafts[0]["consensus"] == 8.6
    assert drafts[1]["consensus"] == 9.5


def test_normalize_inline_codes_uses_name_map() -> None:
    name_map = {
        "壹连科技": {"ticker": "301631"},
        "301631": {"ticker": "301631"},
        "澜起科技": {"ticker": "688008"},
        "万  科Ａ": {"ticker": "000002"},
        "华泰证券": {"ticker": "601688"},
        "广联达": {"ticker": "002410"},
    }
    text = (
        "688008 壹连科技：核心业务为电芯连接组件，利好度9.3，共识度86\n"
        "688041.SH 海光信息：核心业务为CPU，利好度8.5\n"
        "1651.HK 津上机床中国：核心业务为机床，利好度9.0\n"
        "688008.SH/6809.HK 澜起科技：核心业务为内存互连芯片\n"
        "300002 万 科Ａ：核心业务为地产\n"
        "1. **688268 华泰证券**：核心业务为证券\n"
        "| 广联达（688548.SH） | 数字建筑 | 软件 | 9.4 | 82 | 一周 | 3个月 |\n"
    )
    normalized, count = normalize_inline_codes(text, name_map)
    assert count == 5
    assert "301631 壹连科技：" in normalized
    assert "688008 澜起科技：" in normalized
    assert "000002 万 科Ａ：" in normalized
    assert "688041.SH 海光信息" in normalized
    assert "1651.HK 津上机床中国" in normalized
    assert "601688 华泰证券：核心业务为证券" in normalized
    assert "| 广联达（002410.SH） |" in normalized


def test_parse_article_records_name_map_fixes_drafts() -> None:
    """v3 语义：名册只在缺码时补（赛微电子行）；显式代码不被名册覆盖（源杰科技行）。"""
    article = {
        "source_id": "src-2",
        "column": "普通",
        "title": "t",
        "article_date": "2026-06-27",
        "published_at": None,
        "article_score": 8.0,
    }
    md_text = (
        "| 公司名称 | 代码 | 核心业务 | 所属板块 | 共识度 | 利好度 | 预计多久启动 | 期待周期 |\n"
        "| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |\n"
        "| 源杰科技 | 688515.SH | 光芯片 | 光芯片/半导体 | 87 | 8.5 | 一周 | 半年 |\n"
        "| 赛微电子 | 前道设备 | 半导体材料 | 78 | 7.5 | 1周 | 1-3个月 |\n"
    )
    name_map = {
        "源杰科技": {"ticker": "688498"},
        "赛微电子": {"ticker": "300456"},
    }
    records = parse_article_records(
        article=article,
        md_text=md_text,
        source_record=None,
        name_map=name_map,
    )
    by_name = {record.name: record.code for record in records}
    assert by_name["源杰科技"] == "688515"
    assert by_name["赛微电子"] == "300456"


def test_missing_consensus_marks_needs_review() -> None:
    article = {
        "source_id": "zsxq-article-1",
        "topic_id": "topic-1",
        "column": "普通",
        "title": "研报",
        "article_date": "2026-08-01",
        "published_at": None,
        "article_score": 7.5,
    }
    md = """## 图片描述
### 000.jpg (LLM · fake)
1. **示例公司 600000**
   * 核心业务：示例业务
   * 所属板块：示例板块
   * 利好度：8.0
"""
    records = parse_article_records(article=article, md_text=md, source_record=None)
    assert len(records) == 1
    assert records[0].status == "needs_review"
    assert records[0].review_reason == "missing_fields:consensus"


def test_same_code_two_rows_both_kept() -> None:
    text = """1. **甲公司 600111**
   * 核心业务：业务A
   * 所属板块：板块A
   * 利好度：8.0
   * 共识度：80
2. **甲公司 600111**
   * 核心业务：业务B
   * 所属板块：板块B
   * 利好度：9.0
   * 共识度：90
"""
    drafts = parse_rows_from_text(text)
    assert len(drafts) == 2


def test_cross_carrier_conflict_marks_needs_review() -> None:
    article = {
        "source_id": "zsxq-article-2",
        "topic_id": "topic-2",
        "column": "普通",
        "title": "研报",
        "article_date": "2026-08-02",
        "published_at": None,
        "article_score": 8.0,
    }
    source = {
        "image_descriptions": [
            "| 公司名称（代码） | 利好度 | 共识度 |\n|---:|---:|---:|\n| 乙公司（600222.SH） | 9.0 | 90 |"
        ],
        "image_ocr": ["乙公司（600222.SH）\n利好度：8.0\n共识度：90"],
    }
    records = parse_article_records(
        article=article, md_text=None, source_record=source
    )
    assert len(records) == 1
    assert records[0].status == "needs_review"
    assert records[0].review_reason == "cross_source_conflict"
    # v1.1：双源数值全量保留——裁决必须能看到两边各说了什么（2026-09-07）
    detail = records[0].conflict_detail
    assert detail is not None and len(detail) == 2
    assert {(entry["origin"], entry["lihao"]) for entry in detail} == {
        ("zsxq_sources.image_descriptions", 9.0),
        ("zsxq_sources.image_ocr", 8.0),
    }


def test_upsert_store_is_atomic_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "instrument_scores.jsonl"
    article = {
        "source_id": "zsxq-a",
        "topic_id": "t",
        "column": "普通",
        "title": "标题",
        "article_date": "2026-08-03",
        "published_at": None,
        "article_score": 7.0,
    }
    record = build_record(
        draft={
            "code": "600000",
            "name": "示例公司",
            "core_business": "业务",
            "sector": "板块",
            "lihao": 8.0,
            "consensus": 8.0,
            "launch_in": None,
            "horizon": None,
        },
        article=article,
        raw_origin="test",
        provenance=None,
        sequence=0,
    )
    added, updated = upsert_records(path, [record])
    assert (added, updated) == (1, 0)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".tmp").exists()
    added2, updated2 = upsert_records(path, [record])
    assert (added2, updated2) == (0, 0)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["record_id"] == record.record_id


def test_upsert_records_removes_obsolete_ids(tmp_path: Path) -> None:
    path = tmp_path / "instrument_scores.jsonl"
    article = {
        "source_id": "src-1",
        "column": "普通",
        "title": "t",
        "article_date": "2026-08-01",
        "published_at": None,
        "article_score": 8.0,
    }
    record = build_record(
        draft={
            "code": "688268",
            "name": "华泰证券",
            "core_business": "证券",
            "sector": "非银",
            "lihao": 8.0,
            "consensus": 8.0,
            "launch_in": None,
            "horizon": None,
        },
        article=article,
        raw_origin="test",
        provenance=None,
        sequence=0,
    )
    upsert_records(path, [record])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    upsert_records(path, [], remove_record_ids={record.record_id})
    assert path.read_text(encoding="utf-8").strip() == ""


def _seed_kb(tmp_path: Path, articles: list[dict]) -> None:
    (tmp_path / "articles").mkdir(exist_ok=True)
    for article in articles:
        md = tmp_path / "articles" / f"{article['id']}.md"
        md.write_text(
            "## 图片描述\n"
            f"1. **{article['name']}（{article['code']}）**\n"
            "   核心业务：测试业务；所属板块：测试板块；项目评分：8.5；情绪热度：78\n",
            encoding="utf-8",
        )
        article.setdefault("path", str(md))
    (tmp_path / "index.json").write_text(
        json.dumps({"articles": articles}), encoding="utf-8"
    )


def test_v4_anchor_without_fields_is_dropped() -> None:
    """v4 守卫：散文「公司（代码）」行首写法不是评分表——8/30 空壳事故。"""
    text = """壁垒在“上游配额 + 下游锁定”。
利通电子（603629）
核心画像是先发加渠道，客户集中度高，弹性在三家里最陡。
协创数据（300857）
规模换话语权，交付网络铺得开。
"""
    assert parse_rows_from_text(text) == []


def test_v4_partial_field_anchor_still_kept() -> None:
    """守卫只杀零字段行：任一字段位命中即保留（走 needs_review 人工闭环）。"""
    text = """1. **示例公司 600000**
   投产启动：1-2周
"""
    drafts = parse_rows_from_text(text)
    assert len(drafts) == 1
    assert drafts[0]["launch_in"] == "1-2周"


def test_update_instrument_scores_incremental_and_watermark(tmp_path: Path) -> None:
    """NOW #26：saved_ids 入册、幂等重跑、水位自愈（saved_ids 缺席也补）。"""
    _seed_kb(
        tmp_path,
        [
            {
                "id": "src-a",
                "column": "普通",
                "date": "2026-09-05 14:34",
                "score": 6.8,
                "title": "甲",
                "topic_id": "t-a",
                "name": "利通电子",
                "code": "603629",
            }
        ],
    )
    report = update_instrument_scores(tmp_path, saved_ids=["src-a"])
    assert (report.candidates, report.parsed, report.added) == (1, 1, 1)
    records = load_records(instrument_scores_path(tmp_path))
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["code"] == "603629" and record["status"] == "ok"
    assert record["lihao_score"] == 8.5 and record["consensus_score"] == 7.8

    rerun = update_instrument_scores(tmp_path, saved_ids=["src-a"])
    assert (rerun.added, rerun.updated) == (0, 0)  # 门审 P2-1：extracted_at 不参与比较

    second = {
        "id": "src-b",
        "column": "普通",
        "date": "2026-09-06 09:00",
        "score": 7.2,
        "title": "乙",
        "topic_id": "t-b",
        "name": "天孚通信",
        "code": "300394",
    }
    _seed_kb(tmp_path, [second])
    healed = update_instrument_scores(tmp_path, saved_ids=[])
    assert healed.candidates == 1 and healed.added == 1
    records = load_records(instrument_scores_path(tmp_path))
    assert {record["code"] for record in records.values()} == {"603629", "300394"}


def test_update_instrument_scores_skips_below_threshold_and_other_columns(
    tmp_path: Path,
) -> None:
    _seed_kb(
        tmp_path,
        [
            {
                "id": "low",
                "column": "普通",
                "date": "2026-09-05 10:00",
                "score": 5.0,
                "title": "低能",
                "topic_id": "t1",
                "name": "甲公司",
                "code": "600000",
            },
            {
                "id": "qa",
                "column": "问答",
                "date": "2026-09-05 11:00",
                "score": 9.0,
                "title": "问答",
                "topic_id": "t2",
                "name": "乙公司",
                "code": "600001",
            },
        ],
    )
    report = update_instrument_scores(tmp_path, saved_ids=["low", "qa"])
    assert report.candidates == 0
    assert not instrument_scores_path(tmp_path).exists()
