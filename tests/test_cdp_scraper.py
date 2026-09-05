from pathlib import Path

from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, GroupTimelineLoadResult


def test_parse_post_cuts_like_footer_and_comments():
    scraper = CdpBridgeScraper()
    text = """2026-06-24 08:55
能量评分8.6分
PCB覆铜板产业链跟踪
这是一段足够长的正文，讨论PCB覆铜板、铜箔、玻纤布、树脂材料、AI服务器需求和产业链供需变化，应该完整保留下来用于后续知识库分析。
第二段继续说明投资逻辑、估值变化、产能释放节奏和风险提示，保证正文长度超过解析阈值。
爬楼梯、lucky boy🍉等11人觉得很赞
评论 3
慢一点，差评
"""

    post = scraper._parse_post(text)

    assert post is not None
    assert "PCB覆铜板" in post["content"]
    assert "觉得很赞" not in post["content"]
    assert "慢一点，差评" not in post["content"]


def test_save_article_writes_image_sections(tmp_path):
    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    post = {
        "date": "2026-06-24 08:55",
        "score": 8.6,
        "title": "激光通信产业链",
        "tags": ["激光通信"],
        "content": "正文内容足够长，用于测试图片描述和OCR能随文章进入markdown。",
        "char_count": 42,
    }
    image_texts = [
        {
            "filename": "000.png",
            "path": "images/demo/000.png",
            "llm_desc": "图1列出利好的公司：光迅科技、中际旭创。",
            "ocr_text": "利好的公司 光迅科技 中际旭创",
        }
    ]

    scraper._save_article(post, image_texts=image_texts)

    saved = next((Path(tmp_path) / "articles").glob("*.md"))
    text = saved.read_text(encoding="utf-8")
    assert "image_count: 1" in text
    assert "images: [images/demo/000.png]" in text
    assert "## 图片描述" in text
    assert "图1列出利好的公司" in text
    assert "## 图片OCR文字" in text
    assert "光迅科技 中际旭创" in text
    # New fields default correctly when post dict lacks them
    assert "column: 普通" in text
    assert "is_qa: False" in text
    assert "type: talk" in text


def test_images_by_date_from_page_parses_cdp_json(monkeypatch):
    scraper = CdpBridgeScraper()

    def fake_js(script):
        assert "images.zsxq.com" in script
        return '[{"date":"2026-06-24 08:55","src":"https://images.zsxq.com/a.png","index":0}]'

    monkeypatch.setattr(scraper, "_js", fake_js)

    images = scraper._images_by_date_from_page()

    assert images == {
        "2026-06-24 08:55": [
            {"date": "2026-06-24 08:55", "src": "https://images.zsxq.com/a.png", "index": 0}
        ]
    }


def test_run_incremental_passes_matching_images_to_save(monkeypatch):
    from datetime import datetime, timedelta, timezone

    scraper = CdpBridgeScraper()
    saved = []

    now = datetime.now(timezone(timedelta(hours=8)))
    today_str = now.strftime("%Y-%m-%d %H:%M")
    text = f"""三线文案大锅饭
{today_str}
能量评分8.6分
激光通信产业链
正文内容足够长，讨论激光通信、光模块、卫星互联网、产业链公司和AI通信需求，满足投资相关过滤条件。
第二段继续讨论利好的公司 图1 和推荐标的，保证解析内容长度超过阈值。这是额外填充文本以确保超过200字符限制，因为CDP scraper的按作者名切分会跳过太短的文本片段。还需要更多字数来达到门槛。激光通信在卫星互联网和6G领域具有重要战略意义，相关产业链覆盖光模块、光芯片、光器件等多个环节。"""

    monkeypatch.setattr(scraper, "_load_index", lambda: None)
    monkeypatch.setattr(scraper, "_nav", lambda url, wait=0: None)
    monkeypatch.setattr(scraper, "_click_tab", lambda tab: True)
    monkeypatch.setattr(
        scraper, "_scroll_until_cutoff", lambda cutoff, max_scrolls=40: (0, True, True)
    )
    monkeypatch.setattr(scraper, "_extract_visible_dates", lambda: [])
    monkeypatch.setattr(
        scraper,
        "_load_group_timeline_batch_first",
        lambda cutoff: GroupTimelineLoadResult(
            full_text=text,
            reached_page_end=True,
            timeline_dates=[now],
        ),
    )
    monkeypatch.setattr(scraper, "_is_platform_chrome", lambda title, content: False)
    monkeypatch.setattr(scraper, "_is_investment_relevant", lambda title, content: True)
    monkeypatch.setattr(scraper, "_full_text", lambda: text)
    monkeypatch.setattr(
        scraper,
        "_images_by_date_from_page",
        lambda: {today_str: [{"src": "https://images.zsxq.com/a.png"}]},
    )
    monkeypatch.setattr(
        scraper,
        "_process_images",
        lambda images, post_id: [
            {
                "filename": "000.png",
                "path": "images/demo/000.png",
                "llm_desc": "利好的公司：光迅科技",
                "ocr_text": "利好的公司 光迅科技",
            }
        ],
    )
    monkeypatch.setattr(
        scraper, "_save_article", lambda post, image_texts=None: saved.append((post, image_texts))
    )

    assert scraper.run_incremental() == 1
    assert saved[0][1][0]["llm_desc"] == "利好的公司：光迅科技"


def test_parse_post_extracts_column():
    """CDP scraper should detect column from DOM text using COLUMN_PATTERNS."""
    scraper = CdpBridgeScraper()
    text = """2026-06-24 08:55
能量评分8.6分
星大派特刊
PCB覆铜板产业链跟踪分析报告内容详实
这是一段足够长的正文内容讨论PCB覆铜板铜箔玻纤布树脂材料AI服务器需求和产业链供需变化，需要至少一百个字符来通过解析的最小长度校验确保测试有效所以这里补充更多文字来满足最低要求继续填充。
"""
    post = scraper._parse_post(text)
    assert post is not None
    assert post["column"] == "星大派特刊"


def test_parse_post_extracts_column_default():
    """When no column pattern matches, column should default to '普通'."""
    scraper = CdpBridgeScraper()
    text = """2026-06-24 08:55
能量评分7.2分
随便聊聊最近的市场动向和配置
这是一段足够长的正文讨论最近的市场动向和一些配置想法字数需要超过解析阈值确保通过继续填充文本以满足一百字符的最低长度要求所以这里补充更多文字来满足最低要求。继续填充继续填充继续填充继续填充继续填充继续填充继续填充。
"""
    post = scraper._parse_post(text)
    assert post is not None
    assert post["column"] == "普通"


def test_parse_post_extracts_is_qa():
    """CDP scraper should detect Q&A articles via (提问|问题)[：:] pattern."""
    scraper = CdpBridgeScraper()
    text = """2026-06-24 08:55
能量评分7.2分
提问：AI算力板块现在怎么看后市走势
这是一段足够长的正文提问者想知道AI算力板块的配置思路老师回答从估值和基本面两个角度展开需要凑足一百个字符来通过解析的最小长度校验所以这里补充更多文字来满足最低要求。继续填充继续填充继续填充继续填充继续填充继续填充继续填充。
"""
    post = scraper._parse_post(text)
    assert post is not None
    assert post["is_qa"] is True


def test_parse_post_is_qa_default():
    """When no Q&A pattern matches, is_qa should be False."""
    scraper = CdpBridgeScraper()
    text = """2026-06-24 08:55
能量评分8.1分
PCB覆铜板产业链最新跟踪情况
这是一段足够长的正文讨论PCB覆铜板最新情况应该保持is_qa为False继续填充文本以满足一百字符的最低长度要求确保测试有效所以这里补充更多文字来满足最低要求。继续填充继续填充继续填充继续填充继续填充继续填充继续填充。
"""
    post = scraper._parse_post(text)
    assert post is not None
    assert post["is_qa"] is False


def test_parse_post_extracts_companies():
    """CDP scraper should detect KNOWN_COMPANIES in article text."""
    scraper = CdpBridgeScraper()
    text = """2026-06-24 08:55
能量评分8.6分
华为和宁德时代产业链最新跟踪
这是一段足够长的正文讨论华为的芯片布局和宁德时代的电池产能两个公司名字都应该被检测到继续填充文本以满足一百字符的最低长度要求所以这里补充更多文字来满足最低要求。继续填充继续填充继续填充继续填充继续填充继续填充继续填充。
"""
    post = scraper._parse_post(text)
    assert post is not None
    assert "华为" in post["companies"]
    assert "宁德时代" in post["companies"]


def test_save_article_includes_column_and_is_qa(tmp_path):
    """CDP scraper frontmatter should include column, companies, is_qa, type fields."""
    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    post = {
        "date": "2026-06-24 08:55",
        "score": 8.6,
        "title": "星大派特刊：激光通信",
        "tags": ["激光通信", "星大派"],
        "content": "正文内容足够长，讨论光模块、卫星互联网、产业链公司和AI通信需求...",
        "char_count": 80,
        "column": "星大派特刊",
        "companies": ["华为", "中芯国际"],
        "is_qa": False,
    }

    scraper._save_article(post)

    saved = next((Path(tmp_path) / "articles").glob("*.md"))
    text = saved.read_text(encoding="utf-8")
    assert "column: 星大派特刊" in text
    assert "companies: [华为, 中芯国际]" in text
    assert "is_qa: False" in text
    assert "type: talk" in text


def test_save_index_writes_articles_list_format(tmp_path):
    """_save_index should write {"articles": [...], "updated": ..., "total": N} format."""
    import json

    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    scraper._index = {
        "abc123": {"id": "abc123", "title": "Test", "date": "2026-06-24", "column": "普通"},
    }
    scraper._save_index()

    data = json.loads((tmp_path / "index.json").read_text())
    assert "articles" in data
    assert isinstance(data["articles"], list)
    assert len(data["articles"]) == 1
    assert data["articles"][0]["id"] == "abc123"
    assert "updated" in data
    assert data["total"] == 1


def test_load_index_handles_both_formats(tmp_path):
    """_load_index should normalize both old flat and articles-list formats."""
    import json

    # Test 1: old flat CDP format
    index_path = tmp_path / "index.json"
    old_flat = {
        "abc123": {"id": "abc123", "title": "Old Post", "date": "2026-06-24"},
    }
    index_path.write_text(json.dumps(old_flat))

    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    scraper._load_index()
    assert "abc123" in scraper._index
    assert scraper._index["abc123"]["title"] == "Old Post"

    # Test 2: articles-list format
    scraper._index = {}
    articles_fmt = {
        "articles": [
            {"id": "xyz789", "title": "New Post", "date": "2026-06-26", "column": "星大派特刊"},
        ],
        "updated": "2026-06-26T12:00:00+08:00",
        "total": 1,
    }
    index_path.write_text(json.dumps(articles_fmt))

    scraper._load_index()
    assert "xyz789" in scraper._index
    assert scraper._index["xyz789"]["column"] == "星大派特刊"


def test_load_index_fails_closed_on_corrupt_index(tmp_path):
    """索引存在但读不了必须 fail-closed：静默清空会让下一次 _save_index
    用本轮新文覆盖全量索引（BUG-027 同款爆炸半径）。"""
    import pytest

    index_path = tmp_path / "index.json"
    index_path.write_text('{"articles": [', encoding="utf-8")

    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    with pytest.raises(RuntimeError, match="不可读"):
        scraper._load_index()
    # 半截文件原样保留，未被空索引覆盖。
    assert index_path.read_text(encoding="utf-8") == '{"articles": ['
