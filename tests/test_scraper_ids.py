from fin_analyse.scraper.scraper import ZsxqScraper


def test_post_id_does_not_change_when_title_rule_changes():
    scraper = ZsxqScraper()
    text = """2026-06-18 08:55
能量评分8.8分。国信证券2026年6月16日《算力芯片行业报告》核心观点清晰。
2026年5月国家安全可靠测评首次设AI芯片品类，华为海思、海光信息获Ⅰ级。
#半导体 #AI芯片
"""
    post = scraper._parse_one(text)
    title_based_id = (
        __import__("hashlib").md5(f"{post['date']}_{post['title']}".encode()).hexdigest()[:12]
    )

    assert post["id"] != title_based_id
    assert post["id"] == scraper._make_post_id(post["date"], post["content"])
