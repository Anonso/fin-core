from fin_analyse.scraper.scraper import ZsxqScraper


def test_title_skips_energy_score_boilerplate():
    scraper = ZsxqScraper()
    text = """2026-06-18 08:55
能量评分8.8分。（中东战争升级期间，非军工，紧缺资源类的研报和信息能量评级自动下降0.5分，ai科技类下降1分。）国信证券2026年6月16日《算力芯片行业报告》核心观点清晰。
2026年5月国家安全可靠测评首次设AI芯片品类，华为海思、平头哥、海光信息获Ⅰ级。
#半导体 #AI芯片 #精读研报
"""

    post = scraper._parse_one(text)

    assert post["title"].startswith("2026年5月国家安全可靠测评")


def test_extracts_known_companies_from_content():
    scraper = ZsxqScraper()
    text = """2026-06-18 08:55
能量评分8.8分。华为海思、平头哥、海光信息、壁仞、摩尔线程等9款获Ⅰ级。
#半导体 #AI芯片
"""

    post = scraper._parse_one(text)

    assert set(post["companies"]) >= {"华为", "海光信息", "壁仞", "摩尔线程"}


def test_parse_posts_filters_non_article_without_date():
    scraper = ZsxqScraper()
    scraper.author_name = "三线文案大锅饭"
    text = """三线文案大锅饭
创建1700天
大锅饭与小伙伴的进步空间
苹果用户请通过微信小程序知识星球加入或续费
三线文案大锅饭
2026-06-18 08:55
能量评分8.8分。华为海思获Ⅰ级。
#半导体
"""

    posts = scraper.parse_posts(text)

    assert len(posts) == 1
    assert posts[0]["date"] == "2026-06-18 08:55"
