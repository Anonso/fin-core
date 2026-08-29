"""Tests for utils/markdown.py."""

from fin_analyse.utils.markdown import parse_frontmatter


def test_no_frontmatter():
    meta, body = parse_frontmatter("# Just a title")
    assert meta == {}
    assert "# Just a title" in body


def test_basic_frontmatter():
    text = "---\nid: abc123\nscore: 8.5\ntitle: My Article\n---\n# Content here"
    meta, body = parse_frontmatter(text)
    assert meta["id"] == "abc123"
    assert meta["score"] == 8.5
    assert body.startswith("# Content here")


def test_colon_in_value():
    """Values with colons should not break the parser."""
    text = "---\nurl: https://example.com/path?x=1\ntitle: Test\n---\nBody"
    meta, _ = parse_frontmatter(text)
    assert meta["url"] == "https://example.com/path?x=1"
    assert meta["title"] == "Test"
