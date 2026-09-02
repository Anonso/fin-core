"""增量能量评分门槛单测（owner 2026-09-02 修订口径）。

只有“有评分且 <7”的文章跳过；无评分/评分非法不跳过——无表帖（书单/宏观/
问答）仍是有内容价值的参考材料，不能因评分缺失被门拦掉。
"""

from __future__ import annotations

from fin_analyse.scraper.cdp_scraper import _score_skip_enabled


def test_unscored_articles_are_not_skipped() -> None:
    assert _score_skip_enabled(None) is False


def test_scored_below_threshold_is_skipped() -> None:
    assert _score_skip_enabled(6.9) is True
    assert _score_skip_enabled("6.5") is True


def test_threshold_and_above_are_kept() -> None:
    assert _score_skip_enabled(7.0) is False
    assert _score_skip_enabled(8.6) is False


def test_invalid_score_is_not_skipped() -> None:
    assert _score_skip_enabled("无评分") is False
    assert _score_skip_enabled(object()) is False
