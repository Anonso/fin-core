"""HTML 解析逻辑（文章列表、详情页解析）"""

import hashlib
import logging
import re
from typing import Any

from .config import COLUMN_PATTERNS, KNOWN_COMPANIES

logger = logging.getLogger(__name__)

# 过滤行
SKIP_PATTERNS = [
    re.compile(p)
    for p in [
        r"^\d+人觉得很赞$",
        r"^\d+条评论$",
        r"^展开全部$",
        r"^收起$",
        r"^查看详情$",
        r"^为我总结$",
        r"^[^\s]+觉得很赞$",
        r"^[^\s]+等\d+人觉得很赞$",
        r"^[^\s]+\s*回复\s*[^\s]*[：:]",
        r"^\d{4}[-/]\d{2}[-/]\d{2}$",
        r"^知识星球\s*$",
        r"^扫码加入星球",
        r"^查看更多优质内容",
        r"^免责声明.+$",
        # ZSXQ platform boilerplate (detail page)
        r"^收费公示$",
        r"^企业认证$",
        r"^星球榜单$",
        r"^发现星球$",
        r"^登录网页版$",
        r"^运营高品质社群$",
        r"^连接一千位铁杆粉丝$",
        r"^下载知识星球$",
        r"^内容创作、知识付费更方便$",
        r"^支持的系统版本：$",
        r"^iOS \d+",
        r"^Android [\d.]+",
        r"^发表主题，随时捕捉记录身边灵感$",
        r"^相比于在公众号几千字文章",
        r"^创建付费星球$",
        r"^一分钟轻松创建付费星球",
        r"^收款\d+日后点击提现秒到账",
    ]
]


class PostParser:
    """解析知识星球文章文本，提取元数据和结构化内容"""

    def __init__(self, author_name: str | None = None):
        self.author_name = author_name

    def parse_posts(self, full_text: str) -> list[dict]:
        """按作者名切分帖子，提取元数据"""
        if not self.author_name:
            return self._parse_single_post(full_text)

        parts = re.split(rf"{re.escape(self.author_name)}\s*\n", full_text)
        posts = []
        for part in parts[1:]:
            part = part.strip()
            post = self._parse_one(part)
            if post:
                posts.append(post)
        return posts

    def _parse_single_post(self, text: str) -> list[dict]:
        """没有作者名时，整体当作一篇处理"""
        post = self._parse_one(text)
        return [post] if post else []

    def _parse_one(self, text: str) -> dict | None:
        if len(text.strip()) < 30:
            return None
        tm = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text[:200])
        if not tm:
            return None
        date = tm.group(1)
        meta = self._extract_meta(text)
        content = self._clean_content(text)
        raw_id = self._make_post_id(date, content)
        return {
            "id": raw_id,
            "date": date,
            "score": meta["score"],
            "column": meta["column"],
            "companies": meta["companies"],
            "tags": meta["tags"],
            "is_qa": meta["is_qa"],
            "title": meta["title"],
            "content": content,
            "char_count": len(content),
        }

    def _make_post_id(self, date: str, content: str) -> str:
        stable_text = re.sub(r"\s+", "", content)[:500]
        return hashlib.md5(f"{date}_{stable_text}".encode()).hexdigest()[:12]

    def _extract_meta(self, text: str) -> dict:
        meta: dict[str, Any] = {
            "score": None,
            "column": "普通",
            "companies": [],
            "tags": [],
            "title": "",
            "is_qa": False,
        }

        sm = re.search(r"能量评分\s*(\d+\.?\d*)\s*分", text)
        if sm:
            meta["score"] = float(sm.group(1))

        for pat, col in COLUMN_PATTERNS:
            if re.search(pat, text):
                meta["column"] = col
                break

        if re.search(r"(提问|问题)[：:]", text):
            meta["is_qa"] = True

        tags = []
        seen = set()
        for t in re.findall(r"#(\S+)", text):
            t = t.rstrip(",，。.!！?？")
            if t and t not in seen and len(t) < 30:
                seen.add(t)
                tags.append(t)
        meta["tags"] = tags
        meta["companies"] = [name for name in KNOWN_COMPANIES if name in text]

        # 找标题
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        for line in lines:
            if not line or len(line) < 5:
                continue
            if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}(\s+\d{2}:\d{2})?$", line):
                continue
            if re.match(r"^(能量评分|报告能量评分|相关事件能量评分|量评分)\s*\d", line):
                continue
            if line.startswith("免责声明"):
                continue
            if any(p.match(line) for p in SKIP_PATTERNS):
                continue
            meta["title"] = line[:150]
            break
        if not meta["title"]:
            meta["title"] = (lines[1] if len(lines) > 1 else lines[0])[:150] if lines else "..."
        return meta

    def _clean_content(self, text: str) -> str:
        lines = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if any(p.match(line) for p in SKIP_PATTERNS):
                continue
            lines.append(line)
        content = "\n".join(lines)
        content = re.sub(r"\n知识星球\s*\n\s*扫码加入星球[\s\S]*$", "", content)
        content = re.sub(r"\n查看更多优质内容[\s\S]*$", "", content)
        # ZSXQ app-download footer — cut at first occurrence of any platform boilerplate
        _boilerplate_markers = [
            r"\n收费公示",
            r"\n下载知识星球",
            r"\n企业认证",
            r"\n星球榜单",
            r"\n运营高品质社群",
            r"\n发表主题",
            r"\n创建付费星球",
            r"\n一分钟轻松创建",
            r"\n内容创作、知识付费更方便",
        ]
        for marker in _boilerplate_markers:
            content = re.sub(marker + r"[\s\S]*$", "", content)
        return content.strip()
