"""知识星球文章爬取器 - 主控制器，协调 browser/parser/downloader 模块"""

import contextlib
import json
import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as tz_module
from pathlib import Path
from urllib.parse import quote

from . import config
from .browser import BrowserManager
from .config import (
    INCREMENTAL_WINDOW_DAYS,
    KNOWN_COMPANIES,
    MAX_SCROLLS,
    SCROLL_PX,
    SCROLL_WAIT,
    STALL_LIMIT,
    TZ,
)
from .downloader import ImageDownloader
from .parser import PostParser

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {"wx.zsxq.com", "articles.zsxq.com", "images.zsxq.com"}


class ZsxqScraper:
    """知识星球爬虫 - 主控制器"""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.author_name: str | None = None
        self._browser = BrowserManager(headless=headless)
        self._parser: PostParser | None = None
        self._downloader: ImageDownloader | None = None

    # ── 浏览器管理（委托给 BrowserManager） ──────────────────────────

    def start_browser(self):
        self._browser.start_browser()

    def stop_browser(self):
        self._browser.stop_browser()

    def login(self):
        """手动登录流程：打开浏览器等用户登完"""
        self._browser.login()
        self.author_name = self._browser._detect_author()

    # ── 导航（委托给 BrowserManager） ────────────────────────────────

    def navigate(self):
        """打开圈子页，切到精华 tab"""
        self.author_name = self._browser.navigate()
        self._parser = PostParser(author_name=self.author_name)
        self._downloader = ImageDownloader(cookies_provider=self._browser.get_cookies)

    def _detect_author(self):
        """从页面中检测作者/博主名（向后兼容）"""
        self.author_name = self._browser._detect_author()
        if self._parser is None or self._parser.author_name != self.author_name:
            self._parser = PostParser(author_name=self.author_name)

    # ── 内容提取（委托给 BrowserManager） ────────────────────────────

    def scroll_and_load(self):
        """滚动加载精华列表"""
        self._browser.scroll_and_load(MAX_SCROLLS, SCROLL_PX, SCROLL_WAIT, STALL_LIMIT)

    def get_raw_text(self) -> str:
        return self._browser.get_raw_text()

    def get_images(self) -> list[dict]:
        """提取有日期锚点的文章卡片中图片"""
        return self._browser.get_images()

    def extract_article_urls(self) -> list[str]:
        """从精华列表页提取所有文章详情页 URL"""
        return self._browser.extract_article_urls()

    def scrape_article_page(self, url: str) -> tuple[str, list[dict]]:
        """进入文章详情页，返回 (正文, 图片列表)"""
        return self._browser.scrape_article_page(url, ALLOWED_HOSTS)

    def download_and_ocr_images(
        self, images: list[dict], post_id: str = "", max_images: int = 5
    ) -> list[dict]:
        """用浏览器 Cookie + requests 下载图片并 OCR"""
        if self._downloader is None:
            self._downloader = ImageDownloader(cookies_provider=self._browser.get_cookies)
        return self._downloader.download_and_ocr(images, post_id=post_id, max_images=max_images)

    def _get_cookies(self) -> dict[str, str]:
        return self._browser.get_cookies()

    # ── 文章解析（委托给 PostParser） ────────────────────────────

    def parse_posts(self, full_text: str) -> list[dict]:
        """按作者名切分帖子，提取元数据"""
        if self._parser is None:
            self._parser = PostParser(author_name=self.author_name)
        return self._parser.parse_posts(full_text)

    def _parse_single_post(self, text: str) -> list[dict]:
        """没有作者名时，整体当作一篇处理"""
        if self._parser is None:
            self._parser = PostParser(author_name=self.author_name)
        return self._parser._parse_single_post(text)

    def _parse_one(self, text: str) -> dict | None:
        if self._parser is None:
            self._parser = PostParser(author_name=self.author_name)
        return self._parser._parse_one(text)

    def _make_post_id(self, date: str, content: str) -> str:
        if self._parser is None:
            self._parser = PostParser(author_name=self.author_name)
        return self._parser._make_post_id(date, content)

    def _extract_meta(self, text: str) -> dict:
        if self._parser is None:
            self._parser = PostParser(author_name=self.author_name)
        return self._parser._extract_meta(text)

    def _clean_content(self, text: str) -> str:
        if self._parser is None:
            self._parser = PostParser(author_name=self.author_name)
        return self._parser._clean_content(text)

    # ── 存储 ────────────────────────────────

    def save_article(self, post: dict, image_texts: list[dict] | None = None) -> str:
        """保存为 markdown 文件，图片路径记录在 frontmatter"""
        ds = post["date"].replace(" ", "_").replace(":", "") if post["date"] else "unknown"
        sc = f"score{post['score']}" if post["score"] else "noscore"
        fp = Path(post.get("path") or config.ARTICLES_DIR / f"{ds}_{sc}_{post['id']}.md")
        fp.parent.mkdir(parents=True, exist_ok=True)

        companies = ", ".join(post["companies"])
        tags = ", ".join(post["tags"])

        img_paths = []
        llm_section = ""
        ocr_section = ""
        if image_texts:
            img_paths = [item["path"] for item in image_texts]

            llm_parts = []
            for item in image_texts:
                if item.get("llm_desc"):
                    llm_parts.append(f"### {item['filename']} (LLM)\n\n{item['llm_desc']}\n\n")
            if llm_parts:
                llm_section = "\n## 图片描述\n\n" + "".join(llm_parts)

            ocr_parts = []
            for item in image_texts:
                if item.get("ocr_text"):
                    ocr_parts.append(f"### {item['filename']} (OCR)\n\n{item['ocr_text']}\n\n")
            if ocr_parts:
                ocr_section = "\n## 图片OCR文字\n\n" + "".join(ocr_parts)

        img_paths_str = ", ".join(img_paths)
        image_count = post.get("image_count", len(img_paths))

        topic_id = post.get("topic_id", "")
        # F-06（2026-08-17）：feed/详情侧截断诚实标记——正文以 …/... 结尾
        # 视为预览截断，绝不当作完整原文（下游 deep_read/工作集/置顶据此
        # 不把预览冒充全文）。
        content_text = post.get("content", "")
        truncated = bool(
            content_text
            and re.search(r"(?:\.{3}|…)\s*$", content_text)
            and not post.get("incomplete")
        )
        if truncated:
            post["incomplete"] = True
            post["incomplete_reason"] = "zsxq_content_truncated_preview"
        content = f"""---
id: {post["id"]}
topic_id: {topic_id}
date: {post["date"]}
score: {post["score"]}
column: {post["column"]}
companies: [{companies}]
tags: [{tags}]
is_qa: {post["is_qa"]}
type: {post.get("type", "q&a" if post.get("is_qa") else "talk")}
article_url: {post.get("article_url", "")}
content_source: {post.get("content_source", "")}
incomplete: {post.get("incomplete", False)}
incomplete_reason: {post.get("incomplete_reason", "")}
completeness_version: {post.get("completeness_version", 1)}
image_count: {image_count}
images: [{img_paths_str}]
---

# {post["title"]}

{post["content"]}
{llm_section}
{ocr_section}
"""
        fp.write_text(content, encoding="utf-8")
        return str(fp)

    def update_index(self, posts: list[dict]) -> int:
        """增量更新 index.json；topic_id 已存在时更新而不是重复追加。"""
        if config.INDEX_FILE.exists():
            existing = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
        else:
            existing = {"articles": []}

        articles = existing.setdefault("articles", [])
        by_id = {a.get("id"): i for i, a in enumerate(articles) if a.get("id")}
        by_topic = {str(a.get("topic_id")): i for i, a in enumerate(articles) if a.get("topic_id")}

        changed = 0
        for p in posts:
            ds = p["date"].replace(" ", "_").replace(":", "") if p["date"] else "unknown"
            sc = f"score{p['score']}" if p["score"] else "noscore"
            article_path = Path(p.get("path") or config.ARTICLES_DIR / f"{ds}_{sc}_{p['id']}.md")
            entry = {
                "id": p["id"],
                "topic_id": p.get("topic_id", ""),
                "date": p["date"],
                "score": p["score"],
                "column": p["column"],
                "companies": p["companies"],
                "tags": p["tags"],
                "title": p["title"],
                "char_count": p["char_count"],
                "path": str(article_path),
                "type": p.get("type", "q&a" if p.get("is_qa") else "talk"),
                "article_url": p.get("article_url", ""),
                "content_source": p.get("content_source", ""),
                "incomplete": p.get("incomplete", False),
                "incomplete_reason": p.get("incomplete_reason", ""),
                "completeness_version": p.get("completeness_version", 1),
                "image_count": p.get("image_count", 0),
            }
            idx = None
            topic_id = str(entry.get("topic_id") or "")
            if topic_id and topic_id in by_topic:
                idx = by_topic[topic_id]
            elif entry["id"] in by_id:
                idx = by_id[entry["id"]]

            if idx is None:
                articles.append(entry)
                idx = len(articles) - 1
                changed += 1
            else:
                articles[idx] = {**articles[idx], **entry}
                changed += 1
            by_id[entry["id"]] = idx
            if topic_id:
                by_topic[topic_id] = idx

        existing["updated"] = datetime.now(TZ).isoformat()
        existing["total"] = len(articles)
        config.INDEX_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

    def load_index_ids(self) -> set[str]:
        if not config.INDEX_FILE.exists():
            return set()
        data = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
        return {a["id"] for a in data.get("articles", [])}

    def load_index_topic_ids(self) -> set[str]:
        """Load all known ZSXQ topic_ids from index for dedup."""
        if not config.INDEX_FILE.exists():
            return set()
        data = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
        return {a.get("topic_id", "") for a in data.get("articles", []) if a.get("topic_id")}

    def _collect_topics(self, scope: str) -> list[dict]:
        """用 API 分页收集指定 scope 的 topic，遇到超出 3 天窗口则停止。

        scope: "all"（首页时间线）或 "digests"（精华）
        返回 [{topic_id, create_time, title}]，已按 create_time 倒序排列
        """
        from datetime import timedelta

        tz = tz_module(timedelta(hours=8))
        now = datetime.now(tz)
        cutoff = now - timedelta(days=INCREMENTAL_WINDOW_DAYS)

        all_topics: list[dict] = []
        seen_ids: set[str] = set()
        end_time = ""

        for _page in range(4):  # 最多 4 页 (120 条)，覆盖 3 天足够
            batch = self._browser.fetch_topics_by_scope(scope, end_time)
            if not batch:
                break

            out_of_window = False
            for t in batch:
                tid = t["topic_id"]
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    # 解析日期用于比较
                    date_str = t["create_time"]
                    try:
                        article_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
                        article_dt = article_dt.replace(tzinfo=tz)
                    except (ValueError, TypeError):
                        all_topics.append(t)
                        continue

                    if article_dt < cutoff:
                        out_of_window = True
                        break
                    all_topics.append(t)

            if out_of_window or len(batch) < 30:
                break
            # 下一页：用本批最旧的时间作为 end_time
            end_time = batch[-1]["create_time"].replace(" ", "T") + ":00.000+0800"

        logger.info("[COLLECT] scope=%s → %d topics (3-day window)", scope, len(all_topics))
        return all_topics

    # 非投资内容过滤关键词（命中任一即跳过）
    _NON_INVESTMENT_PATTERNS = [
        # 医疗/健康咨询
        "住院",
        "哪家医院",
        "脂肪液化",
        "癌症",
        "生病",
        "手术",
        "头疼",
        # 房产
        "北京房价",
        "上海房价",
        "深圳房价",
        "买房",
        "楼市",
        # 模型/工具咨询
        "大模型用的是哪家",
        "用的哪个大模型",
        # 纯个人生活
        "没有心情和资金",
        "没有资金在凤A",
        "家人生病",
    ]

    # 投资内容必须命中至少一个
    _INVESTMENT_KEYWORDS = [
        "股",
        "基金",
        "ETF",
        "板块",
        "赛道",
        "行情",
        "策略",
        "仓位",
        "研报",
        "政策",
        "技术分析",
        "基本面",
        "估值",
        "财报",
        "半导体",
        "芯片",
        "AI",
        "新能源",
        "光伏",
        "锂电",
        "储能",
        "军工",
        "航天",
        "医药",
        "消费",
        "汽车",
        "机器人",
        "去日化",
        "国产替代",
        "稀缺",
        "卡脖子",
        "产能",
        "供应链",
        "光刻",
        "材料",
        "设备",
        "封装",
        "面板",
        "存储器",
        "PCB",
        "光通信",
        "CPO",
        "DRAM",
        "HBM",
        "NOR",
        "FPGA",
        "稀土",
        "锂矿",
        "铜",
        "钨",
        "铋",
        "碲",
        "氖",
        "氟",
        "硅",
        "长鑫",
        "SpaceX",
        "英伟达",
        "台积电",
        "中芯",
        "华为",
        "宁德",
        "比亚迪",
        "隆基",
        "摩尔线程",
        "寒武纪",
        "美联储",
        "央行",
        "利率",
        "降息",
        "加息",
        "通胀",
        "IPO",
        "涨停",
        "跌停",
        "北向",
        "两融",
        "成交量",
        "K线",
        "MACD",
        "RSI",
        "均线",
        "支撑",
        "阻力",
        "经济",
        "GDP",
        "PMI",
        "CPI",
        "PPI",
    ]

    def _is_investment_relevant(self, title: str, content: str) -> bool:
        """判断文章是否与投资相关。纯生活咨询/医疗/房产等返回 False。"""
        combined = title + content
        # 命中排除模式 → 跳过
        for pat in self._NON_INVESTMENT_PATTERNS:
            if pat in combined:
                return False
        # 必须命中至少一个投资关键词
        return any(kw in combined for kw in self._INVESTMENT_KEYWORDS)

    def load_index_articles_by_topic_id(self) -> dict[str, dict]:
        """Load index entries keyed by topic_id for completeness-aware repair."""
        if not config.INDEX_FILE.exists():
            return {}
        data = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
        return {str(a.get("topic_id")): a for a in data.get("articles", []) if a.get("topic_id")}

    def _is_existing_article_complete(self, entry: dict) -> bool:
        """Return whether an existing indexed article is complete enough to skip."""
        if not entry:
            return False
        if entry.get("incomplete") is True:
            return False
        char_count = int(entry.get("char_count") or 0)
        if char_count <= 0:
            return False
        if entry.get("completeness_version") is None and char_count < 200:
            return False
        path = entry.get("path")
        return not (path and not Path(path).exists())

    def _should_process_topic(
        self, topic: dict, existing_by_topic: dict[str, dict]
    ) -> tuple[bool, str]:
        """Decide whether a topic is new or needs repair despite existing topic_id."""
        tid = str(topic.get("topic_id", ""))
        existing = existing_by_topic.get(tid) if tid else None
        if not existing:
            return True, "new"
        if self._is_existing_article_complete(existing):
            return False, "complete"
        return True, "repair"

    def _extract_text_from_topic_payload(self, topic: dict, topic_type: str) -> str:
        """Extract cleaned text from a topic payload."""
        if topic_type == "q&a":
            question_text = (topic.get("question") or {}).get("text", "") or ""
            answer_text = (topic.get("answer") or {}).get("text", "") or ""
            question_text = self._browser._clean_api_text(question_text)
            answer_text = self._browser._clean_api_text(answer_text)
            return f"问：{question_text}\n\n答：{answer_text}" if question_text else answer_text
        talk = topic.get("talk") or {}
        return self._browser._clean_api_text(
            talk.get("text") or topic.get("text", "") or topic.get("content_text", "") or ""
        )

    def _resolve_topic_content(self, topic: dict, topic_id: str, topic_type: str) -> dict:
        """Resolve full topic content across Q&A, talk, snippets, and embedded articles."""
        detail_payload: dict = {}
        with contextlib.suppress(Exception):
            detail_payload = self._browser.fetch_topic_detail_payload(topic_id)
            if detail_payload.get("type"):
                topic_type = detail_payload["type"]

        list_text = (
            topic.get("content_text")
            or topic.get("text")
            or (topic.get("talk") or {}).get("text", "")
            or ""
        )
        merged_topic = {**topic, **detail_payload} if detail_payload else topic
        text = self._extract_text_from_topic_payload(merged_topic, topic_type)
        if not text and list_text:
            text = self._browser._clean_api_text(list_text)

        images = self._browser.extract_images_from_topic_payload(merged_topic, topic_type)
        article_url = self._browser.extract_article_url_from_topic(
            topic
        ) or self._browser.extract_article_url_from_topic(detail_payload)
        content_source = "topic_detail_api" if detail_payload and text else "list_api"

        if article_url:
            article_text, article_images = self._browser.fetch_article_content(article_url)
            if article_text and len(article_text) > max(len(text), 200):
                text = article_text
                content_source = "article_html"
            if article_images:
                images = article_images

        if not text:
            title = topic.get("title") or ""
            text = f"# {title}\n\n{title}" if title else ""
            content_source = "synthetic"

        incomplete = False
        reason = ""
        if content_source == "synthetic":
            incomplete = True
            reason = "synthetic_fallback"
        elif topic_type != "q&a" and len(text.strip()) < 200:
            incomplete = True
            reason = "content_too_short"
        elif article_url and content_source != "article_html":
            incomplete = True
            reason = "article_url_fetch_failed"

        return {
            "text": text,
            "images": images,
            "article_url": article_url or "",
            "content_source": content_source,
            "incomplete": incomplete,
            "incomplete_reason": reason,
        }

    def _process_article_images(self, images: list[dict], post_id: str) -> list[dict]:
        """下载图片 → LLM 描述 → OCR 兜底，返回 [{filename, path, ocr_text, llm_desc}]"""
        from .downloader import describe_image

        if not images:
            return []

        # Step 1: 下载 + OCR（复用现有逻辑）
        if self._downloader is None:
            self._downloader = ImageDownloader(cookies_provider=self._browser.get_cookies)
        results = self._downloader.download_and_ocr(images, post_id=post_id, max_images=len(images))

        # Step 2: LLM 描述每张图片（OCR 作为兜底）
        for r in results:
            local_path = r["path"]
            # 构造绝对路径（download_and_ocr 返回相对路径）
            from .config import KB_ROOT

            abs_path = KB_ROOT / local_path

            llm_desc = describe_image(str(abs_path))
            r["llm_desc"] = llm_desc

            # OCR 兜底：LLM 描述太短或为空时，OCR 结果作为补充
            if not llm_desc or len(llm_desc) < 20:
                logger.debug(
                    "[IMG] %s: LLM 描述不足(%d chars)，使用 OCR 兜底",
                    r.get("filename", ""),
                    len(llm_desc) if llm_desc else 0,
                )

        return results

    def run_extended(self, max_articles: int = 0, ocr: bool = False):
        """通过 API 时间窗口分页获取超出 DOM 范围的历史文章"""
        logger.info("[EXTENDED] API时间窗口抓取, max=%s", max_articles or "all")

        self.navigate()  # ensure auth
        existing_ids = self.load_index_ids()
        tz = tz_module(timedelta(hours=8))
        now = datetime.now(tz)

        seen_ids = set()
        all_topics = []
        # Go back week by week
        for week_back in range(40):
            end_dt = now - timedelta(days=week_back * 7)
            end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.999+0800")
            encoded = quote(end_str, safe="")
            url = f"https://api.zsxq.com/v2/groups/15522441811252/topics?scope=digests&count=30&end_time={encoded}"
            data = self._browser.fetch_api(url)
            topics = data.get("resp_data", {}).get("topics", [])
            if not topics:
                continue
            for t in topics:
                tid = t.get("topic_id", "")
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    all_topics.append(t)

            if len(all_topics) >= 2000:
                break

        logger.info("[EXTENDED] 去重后 %d 个唯一 topics", len(all_topics))

        posts = []
        for i, topic in enumerate(all_topics):
            tid = topic.get("topic_id", "")
            date = (topic.get("create_time") or "")[:16].replace("T", " ")
            title = topic.get("title", "")[:150]
            content = (topic.get("talk") or {}).get("text", "") or ""

            if not date or not title:
                continue

            # Check if already in index
            raw_id = self._make_post_id(date, content or title)
            if raw_id in existing_ids:
                continue

            # API 元数据内容通常为空或很短，使用标题作为内容
            if not content or len(content) < 50:
                content = f"# {title}\n\n{title}"
                if topic.get("likes_count"):
                    content += f" | {topic['likes_count']}赞"
                if topic.get("comments_count"):
                    content += f" | {topic['comments_count']}评论"
            full_content = content

            companies = [name for name in KNOWN_COMPANIES if name in (full_content + title)]
            tags = [t.strip("#") for t in re.findall(r"#(\S+)", full_content + title)]
            score = None
            sm = re.search(r"能量评分\s*(\d+\.?\d*)\s*分", full_content)
            if sm:
                score = float(sm.group(1))

            post = {
                "id": raw_id,
                "date": date,
                "score": score,
                "column": "普通",
                "companies": companies,
                "tags": tags,
                "is_qa": False,
                "title": title[:150],
                "content": full_content,
                "char_count": len(full_content),
            }

            self.save_article(post, [] if not ocr else [])
            posts.append(post)

            if i % 20 == 0:
                logger.info("[%d/%d] %s %s", len(posts), len(all_topics), date, title[:50])

            if max_articles > 0 and len(posts) >= max_articles:
                break

        if posts:
            n = self.update_index(posts)
            logger.info("[EXTENDED] 新增 %d 篇, 保存 %d 个文件", n, len(posts))
        else:
            logger.info("[EXTENDED] 没有新文章")

    def run_column(
        self,
        column_name: str,
        max_articles: int = 0,
        ocr: bool = False,
        detail: bool = False,
        since_year: int = 0,
    ):
        """爬取指定栏目的文章（如「星大派好问题」），自动去重。

        返回新增文章数。
        """
        logger.info(
            "[COLUMN] 爬取栏目: %s, max=%s, since_year=%s",
            column_name,
            max_articles or "all",
            since_year or "all",
        )

        self.navigate()
        existing_ids = self.load_index_ids()

        columns = self._browser.fetch_columns()
        target_cols = []
        for col in columns:
            col_name = col.get("name", "") or col.get("title", "")
            if column_name in col_name or col_name in column_name:
                target_cols.append(col)

        if not target_cols:
            logger.warning("[COLUMN] 未找到栏目: %s", column_name)
            return 0

        total_saved = 0
        for target_col in target_cols:
            # Re-navigate to ensure page is on ZSXQ domain for API calls
            if detail:
                self.navigate()
            # Refresh after each column to catch cross-column duplicates
            existing_ids = self.load_index_ids()
            saved = self._scrape_single_column(
                target_col, existing_ids, max_articles, ocr, column_name, detail, since_year
            )
            total_saved += saved
            if max_articles > 0 and total_saved >= max_articles:
                break

        return total_saved

    def _scrape_single_column(
        self,
        target_col: dict,
        existing_ids: set[str],
        max_articles: int,
        ocr: bool,
        column_name: str = "",
        detail: bool = False,
        since_year: int = 0,
    ) -> int:
        """爬取单个栏目的文章，返回新增数。"""
        col_id = target_col["column_id"]
        col_name = target_col.get("name", str(col_id))
        logger.info(
            "[COLUMN] 找到栏目: %s (id=%s, articles=%s)%s",
            col_name,
            col_id,
            target_col.get("articles_count", "?"),
            " [detail]" if detail else "",
        )

        # Paginate through column topics (40-week window)
        tz = tz_module(timedelta(hours=8))
        now = datetime.now(tz)
        seen_tids: set[str] = set()
        all_topics: list[dict] = []

        for week_back in range(40):
            end_dt = now - timedelta(days=week_back * 7)
            end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.999+0800")
            topics = self._browser.fetch_column_topics(col_id, end_time=end_str, count=30)
            if not topics:
                continue
            for t in topics:
                tid = str(t.get("topic_id", ""))
                if tid and tid not in seen_tids:
                    seen_tids.add(tid)
                    all_topics.append(t)
            if len(all_topics) >= 2000:
                break

        logger.info("[COLUMN] 去重后 %d 个唯一 topics", len(all_topics))

        saved_posts: list[dict] = []
        saved_count = 0
        for i, topic in enumerate(all_topics):
            tid = str(topic.get("topic_id", ""))
            date = (topic.get("create_time") or "")[:16].replace("T", " ")
            # Column API: text is at top level, no separate title
            raw_text = topic.get("text", "") or (topic.get("talk") or {}).get("text", "") or ""
            title = (topic.get("title") or "")[:150]
            if not title and raw_text:
                title = raw_text[:150]
            content = raw_text

            if not date or not title or not title.strip():
                continue

            # Year filter: skip articles before since_year
            if since_year > 0:
                try:
                    article_year = int(date[:4])
                    if article_year < since_year:
                        continue
                except (ValueError, IndexError):
                    pass

            resolved = (
                self._resolve_topic_content(topic, tid, topic.get("type", "talk"))
                if detail and tid
                else {
                    "text": content,
                    "images": [],
                    "article_url": "",
                    "content_source": "list_api",
                    "incomplete": len((content or "").strip()) < 200,
                    "incomplete_reason": "content_too_short"
                    if len((content or "").strip()) < 200
                    else "",
                }
            )
            content = resolved["text"]
            raw_id = self._make_post_id(date, content or title)
            if raw_id in existing_ids:
                continue

            if not content or len(content) < 50:
                content = f"# {title}\n\n{title}"
                if topic.get("likes_count"):
                    content += f" | {topic['likes_count']}赞"
                if topic.get("comments_count"):
                    content += f" | {topic['comments_count']}评论"
                resolved["content_source"] = "synthetic"
                resolved["incomplete"] = True
                resolved["incomplete_reason"] = "synthetic_fallback"

            full_content = content
            companies = [name for name in KNOWN_COMPANIES if name in (full_content + title)]
            tags = [t.strip("#") for t in re.findall(r"#(\S+)", full_content + title)]
            score = None
            sm = re.search(r"能量评分\s*(\d+\.?\d*)\s*分", full_content)
            if sm:
                score = float(sm.group(1))

            post = {
                "id": raw_id,
                "date": date,
                "score": score,
                "topic_id": tid,
                "column": column_name if column_name else "普通",
                "companies": companies,
                "tags": tags,
                "is_qa": "问题" in (title + column_name),
                "title": title[:150],
                "content": full_content,
                "char_count": len(full_content),
                "type": topic.get("type", "q&a" if "问题" in (title + column_name) else "talk"),
                "article_url": resolved.get("article_url", ""),
                "content_source": resolved.get("content_source", ""),
                "incomplete": resolved.get("incomplete", False),
                "incomplete_reason": resolved.get("incomplete_reason", ""),
                "completeness_version": 1,
                "image_count": len(resolved.get("images") or []),
            }

            if post["incomplete"]:
                logger.warning(
                    "[INCOMPLETE] %s 内容可能抓取不全: %s", title[:50], post["incomplete_reason"]
                )

            image_texts = (
                self._process_article_images(resolved.get("images") or [], post["id"])
                if (ocr and resolved.get("images"))
                else []
            )
            self.save_article(post, image_texts)
            saved_posts.append(post)
            saved_count += 1

            if i % 20 == 0:
                logger.info("[COLUMN %d/%d] %s %s", saved_count, len(all_topics), date, title[:50])

            if max_articles > 0 and saved_count >= max_articles:
                break

        if saved_posts:
            n = self.update_index(saved_posts)
            logger.info("[COLUMN] 新增 %d 篇，索引更新 %d 条", saved_count, n)
        else:
            logger.info("[COLUMN] 没有新文章")

        return saved_count

    # ── 主流程 ──────────────────────────────

    def run_full(self, max_articles: int = 0, detail: bool = False, ocr: bool = False):
        logger.info(
            "[SCRAPER] 全量模式, max=%s, detail=%s, ocr=%s", max_articles or "all", detail, ocr
        )

        self.navigate()
        self.scroll_and_load()

        if detail:
            self._scrape_with_detail(max_articles)
        else:
            full_text = self.get_raw_text()
            logger.info("[TEXT] %d chars", len(full_text))

            posts = self.parse_posts(full_text)
            logger.info("[POSTS] 解析出 %d 篇", len(posts))

            if max_articles > 0 and len(posts) > max_articles:
                posts = posts[:max_articles]

            feed_images = self.get_images() if ocr else []
            self._save_posts(posts, feed_images=feed_images)

    def run_incremental(self, detail: bool = True, ocr: bool = True):
        """增量模式 v2：首页 + 精华双源 API 收集 → 3 天窗口 → 详情页全文

        detail 和 ocr 默认开启（必须进详情页获取完整内容）。
        """
        logger.info("[SCRAPER] 增量模式 v2 (窗口=%d天)", INCREMENTAL_WINDOW_DAYS)

        existing_ids = self.load_index_ids()
        existing_by_topic = self.load_index_articles_by_topic_id()
        existing_topic_ids = set(existing_by_topic)
        logger.info(
            "[INDEX] 已有 %d 篇文章, %d 个 topic_id", len(existing_ids), len(existing_topic_ids)
        )

        # ── 步骤 1: 收集首页 topic 元数据 ──
        self._browser.navigate_to_main_feed()
        self._browser.expand_all_articles()
        main_topics = self._collect_topics("all")
        logger.info("[MAIN] 首页收集到 %d 个 topic", len(main_topics))

        # ── 步骤 2: 收集精华页 topic 元数据（补漏）──
        self._browser.navigate_to_digests()
        digest_topics = self._collect_topics("digests")
        logger.info("[DIGEST] 精华页收集到 %d 个 topic", len(digest_topics))

        # ── 步骤 3: 合并 + 去重 + 过滤 ──
        main_tids = {t["topic_id"] for t in main_topics}
        for t in digest_topics:
            if t["topic_id"] not in main_tids:
                main_topics.append(t)

        # 过滤：新增 topic + 已有但疑似残缺的 topic 都要处理
        filtered: list[dict] = []
        for t in main_topics:
            should_process, reason = self._should_process_topic(t, existing_by_topic)
            if not should_process:
                continue
            raw_id = self._make_post_id(t["create_time"], t.get("title", ""))
            if reason == "new" and raw_id in existing_ids:
                continue
            t["_raw_id"] = raw_id
            t["_process_reason"] = reason
            filtered.append(t)

        logger.info("[FILTER] %d 个待处理 topic（新增/修复）", len(filtered))

        if not filtered:
            logger.info("[DONE] 没有新文章")
            return

        # ── 步骤 4: 按类型获取全文 + 图片 ──
        posts: list[dict] = []
        for i, topic in enumerate(filtered):
            tid = topic["topic_id"]
            date = topic["create_time"]
            title = topic.get("title", "")[:150]
            topic_type = topic.get("type", "talk")

            logger.info(
                "[DETAIL] [%d/%d] %s %s [%s]", i + 1, len(filtered), date, title[:50], topic_type
            )

            resolved = self._resolve_topic_content(topic, tid, topic_type)
            text = resolved["text"]
            images = resolved.get("images") or []

            if not text or len(text) < 80:
                logger.warning(
                    "[SKIP] %s 内容太短 (%d chars) type=%s",
                    tid,
                    len(text) if text else 0,
                    topic_type,
                )
                continue

            # 非投资内容过滤
            if not self._is_investment_relevant(title, text):
                logger.info("[SKIP] %s 非投资内容: %s", tid, title[:50])
                continue

            # 解析文章
            parsed = self.parse_posts(text)
            if not parsed:
                tm = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text[:200])
                fallback_date = tm.group(1) if tm else date
                post = self._parse_one(text)
                if post:
                    post["date"] = fallback_date
                    parsed = [post]

            for post in parsed:
                if post["id"] in existing_ids:
                    continue

                # 写入 topic_id + type + 完整性元数据（API 收集阶段已知，parser 不知道）
                post["topic_id"] = tid
                post["type"] = topic_type
                post["article_url"] = resolved.get("article_url", "")
                post["content_source"] = resolved.get("content_source", "")
                post["incomplete"] = resolved.get("incomplete", False)
                post["incomplete_reason"] = resolved.get("incomplete_reason", "")
                post["completeness_version"] = 1
                post["image_count"] = len(images)

                # 图片处理：下载 + LLM 描述 + OCR 兜底
                image_texts = self._process_article_images(images, post["id"]) if images else []

                self.save_article(post, image_texts)
                img_info = f" ({len(image_texts)} images)" if image_texts else ""
                logger.info(
                    "OK %s score=%s %s%s",
                    post["date"],
                    post.get("score", "?"),
                    post["title"][:50],
                    img_info,
                )
                posts.append(post)

        # ── 步骤 5: 更新索引 ──
        if posts:
            n = self.update_index(posts)
            logger.info("[DONE] 新增 %d 篇", n)
        else:
            logger.info("[DONE] 没有新文章")

    def _scrape_with_detail(self, max_articles: int, existing_ids: set[str] | None = None):
        """从详情页抓取全文和图片"""
        urls = self.extract_article_urls()
        if max_articles > 0:
            urls = urls[:max_articles]

        if existing_ids is None:
            existing_ids = set()

        posts = []
        for i, url in enumerate(urls):
            logger.info("[DETAIL] [%d/%d] %s", i + 1, len(urls), url[:80])
            text, images = self.scrape_article_page(url)
            if not text or len(text) < 80:
                logger.warning("[SKIP] 内容太短")
                continue

            parsed = self.parse_posts(text)
            if not parsed:
                # fallback: 把整页当一篇文章
                tm = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text[:200])
                date = tm.group(1) if tm else ""
                if date:
                    post = self._parse_one(text)
                    if post:
                        parsed = [post]

            for post in parsed:
                if post["id"] in existing_ids:
                    logger.info("[SKIP] %s 已存在", post["id"][:12])
                    continue
                image_texts = self.download_and_ocr_images(images, post_id=post["id"])
                self.save_article(post, image_texts)
                logger.info(
                    "OK %s score=%s %s (%d images)",
                    post["date"],
                    post["score"],
                    post["title"][:50],
                    len(image_texts),
                )
                posts.append(post)

        if posts:
            n = self.update_index(posts)
            logger.info("[DONE] 新增 %d 篇", n)
        else:
            logger.info("[DONE] 没有新文章")

    def _save_posts(self, posts: list[dict], feed_images: list[dict] | None = None):
        if not posts:
            logger.warning("[DONE] 没有文章可保存")
            return

        # 构建日期→图片映射，按日期精准关联
        date_to_images: dict[str, list[dict]] = {}
        if feed_images:
            for img in feed_images:
                img_date = img.get("date", "")
                if img_date:
                    date_to_images.setdefault(img_date, []).append(img)

        saved = 0
        total_ocr = 0
        for i, p in enumerate(posts):
            post_date = p.get("date", "")
            # 按日期前缀匹配（文章日期 "2026-06-18 08:55"，图片日期同格式）
            matched = date_to_images.get(post_date, [])
            image_texts = (
                self.download_and_ocr_images(matched, post_id=p["id"], max_images=len(matched))
                if matched
                else []
            )
            self.save_article(p, image_texts)
            saved += 1
            total_ocr += len(image_texts)
            img_info = f" ({len(image_texts)} images)" if image_texts else ""
            logger.info(
                "OK [%d/%d] %s score=%s %s%s",
                i + 1,
                len(posts),
                p["date"],
                p.get("score", "?"),
                p["title"][:50],
                img_info,
            )

        n = self.update_index(posts)
        logger.info("[DONE] 新增 %d 篇, 保存 %d 个文件, OCR图片 %d 张", n, saved, total_ocr)

    # ── 资源清理 ────────────────────────────

    def close(self):
        self.stop_browser()
