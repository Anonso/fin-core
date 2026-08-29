"""Strict-G 深化积压有界排空（backlog drain）测试。

覆盖 `_collect_deep_read_backlog_ids` 的过滤、限额、去重与失败安全语义，
以及与既有当轮生成路径共享的 strict-G 判定一致性。
"""

from pathlib import Path

import pytest

import fin_analyse.cognition.deep_read_artifacts as deep_read_module
from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper


def _make_entry(tmp_kb: Path, article_id: str, **overrides) -> dict:
    entry = {
        "id": article_id,
        "column": "星大派锐评",
        "source_classification": "teacher_original",
        "is_qa": False,
        "path": str(tmp_kb / "articles" / f"20260101_zsxq_{article_id}.md"),
    }
    entry.update(overrides)
    return entry


def _touch_article(tmp_kb: Path, article_id: str) -> None:
    articles = tmp_kb / "articles"
    articles.mkdir(parents=True, exist_ok=True)
    (articles / f"20260101_zsxq_{article_id}.md").write_text(
        f"# {article_id}\n", encoding="utf-8"
    )


class _FakeService:
    """is_fresh 可编程的替身，记录调用顺序。"""

    def __init__(self, stale_ids: set[str]):
        self.stale_ids = stale_ids
        self.checked: list[str] = []

    def is_fresh(self, article_id: str, article_path: Path) -> bool:
        self.checked.append(article_id)
        return article_id not in self.stale_ids


@pytest.fixture()
def scraper(tmp_path: Path) -> CdpBridgeScraper:
    instance = CdpBridgeScraper(knowledge_base_root=tmp_path)
    return instance


def test_collect_returns_only_stale_strict_g_articles(scraper, tmp_path, monkeypatch):
    stale_id = "zsxq-22258825284858211"
    fresh_id = "zsxq-45548881121555858"
    for aid in (stale_id, fresh_id):
        _touch_article(tmp_path, aid)
    scraper._index = {
        fresh_id: _make_entry(tmp_path, fresh_id),
        stale_id: _make_entry(tmp_path, stale_id),
    }
    fake = _FakeService(stale_ids={stale_id})
    monkeypatch.setattr(deep_read_module, "DeepReadArtifactService", lambda root: fake)

    assert scraper._collect_deep_read_backlog_ids(limit=3, exclude=set()) == [stale_id]
    # 确定性顺序：index 字典序，'2' < '4'
    assert fake.checked == [stale_id, fresh_id]


def test_collect_skips_ineligible_and_unresolvable_entries(scraper, tmp_path, monkeypatch):
    qa_uncertain = "zsxq-a"
    plain_qa = "zsxq-b"
    escaping = "zsxq-c"
    for aid in (qa_uncertain, plain_qa, escaping):
        _touch_article(tmp_path, aid)
    scraper._index = {
        # 好问题但没有确认 Q&A provenance → 不合格
        qa_uncertain: _make_entry(tmp_path, qa_uncertain, column="星大派好问题", is_qa=False),
        # 普通栏问答 → 非 strict-G
        plain_qa: _make_entry(tmp_path, plain_qa, column="普通", is_qa=True),
        # path 指向 articles 根之外 → 不可解析
        escaping: _make_entry(tmp_path, escaping, path="/etc/passwd"),
    }
    fake = _FakeService(stale_ids={qa_uncertain, plain_qa, escaping})
    monkeypatch.setattr(deep_read_module, "DeepReadArtifactService", lambda root: fake)

    assert scraper._collect_deep_read_backlog_ids(limit=5, exclude=set()) == []


def test_collect_respects_limit_and_exclusion(scraper, tmp_path, monkeypatch):
    ids = ["zsxq-d4", "zsxq-b2", "zsxq-c3", "zsxq-a1"]
    for aid in ids:
        _touch_article(tmp_path, aid)
    scraper._index = {aid: _make_entry(tmp_path, aid) for aid in ids}
    fake = _FakeService(stale_ids=set(ids))
    monkeypatch.setattr(deep_read_module, "DeepReadArtifactService", lambda root: fake)

    drained = scraper._collect_deep_read_backlog_ids(limit=2, exclude=set())
    assert drained == ["zsxq-a1", "zsxq-b2"]

    drained_minus_excluded = scraper._collect_deep_read_backlog_ids(
        limit=5, exclude={"zsxq-a1"}
    )
    assert drained_minus_excluded == ["zsxq-b2", "zsxq-c3", "zsxq-d4"]


def test_collect_freshness_check_failure_is_skipped_not_fatal(scraper, tmp_path, monkeypatch):
    good = "zsxq-fresh1"
    broken = "zsxq-broken"
    for aid in (good, broken):
        _touch_article(tmp_path, aid)
    scraper._index = {
        broken: _make_entry(tmp_path, broken),
        good: _make_entry(tmp_path, good),
    }

    class _ExplodingThenFake(_FakeService):
        def is_fresh(self, article_id: str, article_path: Path) -> bool:
            if article_id == broken:
                raise OSError("unreadable")
            return super().is_fresh(article_id, article_path)

    monkeypatch.setattr(
        deep_read_module,
        "DeepReadArtifactService",
        lambda root: _ExplodingThenFake(stale_ids={good}),
    )

    assert scraper._collect_deep_read_backlog_ids(limit=3, exclude=set()) == [good]


def test_collect_non_positive_limit_is_noop(scraper, tmp_path):
    assert scraper._collect_deep_read_backlog_ids(limit=0, exclude=set()) == []
