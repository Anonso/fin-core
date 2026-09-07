"""owner 2026-09-07：汇总类帖认知链路由——coding 端点后端按 column 子串跳过。"""

from __future__ import annotations

from fin_analyse.cognition.models import ZsxqCognitionSource
from fin_analyse.cognition.thesis_extractor import LlmZsxqThesisExtractor


def _source(column: str) -> ZsxqCognitionSource:
    return ZsxqCognitionSource(
        source_id="s",
        article_path="a.md",
        article_id="zsxq-1",
        topic_id="1",
        published_at="",
        column=column,
        title="t",
        content="c",
        image_descriptions=[],
        image_ocr=[],
        source_rank="t0",
        completeness="complete",
    )


def test_route_names_fail_open_without_source_or_config(monkeypatch):
    import fin_analyse.cognition.thesis_extractor as te

    monkeypatch.setattr(te, "_content_filter_routing", lambda: ((), ()))
    ex = LlmZsxqThesisExtractor()
    names = ("glm53_flash", "deepseek_flash_cmd")
    assert ex._route_names(names, None) == list(names)
    assert ex._route_names(names, _source("星大派每日热点")) == list(names)


def test_route_names_skip_configured_backends_for_digest_column(monkeypatch):
    import fin_analyse.cognition.thesis_extractor as te

    monkeypatch.setattr(
        te, "_content_filter_routing", lambda: (("每日热点",), ("glm53_flash",))
    )
    ex = LlmZsxqThesisExtractor()
    names = ["glm53_flash", "deepseek_flash_cmd", "qwen"]
    assert ex._route_names(names, _source("星大派每日热点")) == [
        "deepseek_flash_cmd",
        "qwen",
    ]
    # 子串命中（column 带日期后缀变体）
    assert ex._route_names(names, _source("星大派每日热点（0907）")) == [
        "deepseek_flash_cmd",
        "qwen",
    ]
    # 非汇总类目走完整链
    assert ex._route_names(names, _source("星大派锐评")) == names
