"""Playwright 浏览器管理（启动、关闭、页面导航）"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from typing import Any, cast

from playwright.sync_api import BrowserContext, Playwright, sync_playwright
from playwright.sync_api import Page as PwPage
from playwright.sync_api import TimeoutError as PwTimeout

from . import config
from .config import (
    EXECUTABLE_PATH,
    GROUP_URL,
    USER_DATA_DIR,
)

logger = logging.getLogger(__name__)


class ZsxqApiAuthError(RuntimeError):
    """ZSXQ API authentication is not valid for browser fetch calls."""


class BrowserManager:
    """管理 Playwright 浏览器生命周期和基础导航"""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright: Playwright | None = None
        self.browser: Any = None  # Browser | BrowserContext | None
        self.context: BrowserContext | None = None
        self._page: PwPage | None = None

    @property
    def page(self) -> PwPage | None:
        return self._page

    @page.setter
    def page(self, value: PwPage | None) -> None:
        self._page = value

    def _require_page(self) -> PwPage:
        if self._page is None:
            raise RuntimeError("start_browser() must be called first")
        return self._page

    def _require_context(self) -> BrowserContext:
        if self.context is None:
            raise RuntimeError("start_browser() must be called first")
        return self.context

    def start_browser(self):
        self.playwright = sync_playwright().start()
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 修复 DISPLAY: WSLg 使用 unix socket (:0) 而非 TCP (127.0.0.1:0.0)
        browser_env: dict[str, str | float | bool] = {}
        if not self.headless:
            display = self._fix_display()
            if display:
                browser_env["DISPLAY"] = display

        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            executable_path=EXECUTABLE_PATH,
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
            env=browser_env if browser_env else None,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _fix_display(self) -> str:
        """WSLg: 将 DISPLAY 修正为 unix socket，返回正确的 DISPLAY 值"""
        if os.path.exists("/tmp/.X11-unix"):
            sockets = os.listdir("/tmp/.X11-unix")
            if sockets:
                display = sockets[0].lstrip("X")
                val = f":{display}"
                os.environ["DISPLAY"] = val
                return val
        return os.environ.get("DISPLAY", "")

    def stop_browser(self):
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()

    def login(self):
        """手动登录流程：打开浏览器等用户登完"""
        self.start_browser()
        page = self._require_page()
        page.goto(GROUP_URL, wait_until="domcontentloaded")
        print("[LOGIN] 请在浏览器中登录知识星球，完成后按 Enter 继续...", flush=True)
        input()
        page.goto(GROUP_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        logger.info("[LOGIN] 登录完成")

    def navigate(self, author_name: str | None = None):
        """打开圈子页，切到精华 tab，返回检测到的作者名"""
        page = self._require_page()
        page.goto(GROUP_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 检测作者名
        detected_author = self._detect_author()

        # 点击"精华"
        try:
            el = page.locator("text=精华").first
            el.click(timeout=5000)
            page.wait_for_timeout(3000)
            logger.info("[NAV] 已切换到精华 tab")
        except PwTimeout:
            logger.warning("[NAV] 未找到精华 tab，继续")

        return detected_author

    def navigate_to_main_feed(self):
        """打开圈子首页时间线（不切精华 tab），返回检测到的作者名"""
        page = self._require_page()
        page.goto(GROUP_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        author = self._detect_author()
        logger.info("[NAV] 首页时间线加载完成, author=%s", author)
        return author

    def navigate_to_digests(self):
        """直接打开精华主题页"""
        from .config import DIGESTS_URL

        page = self._require_page()
        page.goto(DIGESTS_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        logger.info("[NAV] 精华主题页加载完成")

    def expand_all_articles(self):
        """点击页面上所有「展开全部」按钮，展开截断的帖子正文"""
        page = self._require_page()
        try:
            buttons = page.locator("text=展开全部").all()
            count = 0
            for btn in buttons:
                try:
                    btn.click(timeout=2000)
                    page.wait_for_timeout(500)
                    count += 1
                except Exception:
                    continue
            logger.info("[EXPAND] 点击了 %d 个展开按钮", count)
        except Exception as e:
            logger.warning("[EXPAND] 展开失败: %s", e)

    def _detect_author(self) -> str | None:
        """从页面中检测作者/博主名"""
        import re

        page = self._require_page()
        text = page.inner_text("body")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if re.search(r"2026[/-]\d{2}", line) and i > 0 and len(lines[i - 1].strip()) > 2:
                author_name = lines[i - 1].strip()
                logger.info("[AUTHOR] 检测到: %s", author_name)
                return author_name
        logger.warning("[AUTHOR] 未能自动检测作者名")
        return None

    def scroll_and_load(
        self, max_scrolls: int, scroll_px: int, scroll_wait: float, stall_limit: int
    ):
        """滚动加载精华列表"""
        page = self._require_page()
        last_h = 0
        stall = 0
        for i in range(max_scrolls):
            page.evaluate(f"window.scrollBy(0, {scroll_px})")
            page.wait_for_timeout(int(scroll_wait * 1000))
            h = page.evaluate("document.body.scrollHeight")
            if h == last_h:
                stall += 1
            else:
                stall = 0
            if stall > stall_limit:
                logger.info("[SCROLL] 停止于第%d次, height=%s", i + 1, h)
                break
            last_h = h
            if i % 5 == 0:
                logger.debug("[SCROLL] %d/%d h=%s", i + 1, max_scrolls, h)

    def get_raw_text(self) -> str:
        return cast(str, self._require_page().inner_text("body"))

    def get_images(self) -> list[dict[str, Any]]:
        """提取有日期锚点的文章卡片中图片，返回 [{src, date, index}]"""
        page = self._require_page()
        raw: Any = page.evaluate("""() => {
            const datePattern = /\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}/;
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            const dateNodes = [];
            while (walker.nextNode()) {
                if (datePattern.test(walker.currentNode.textContent)) {
                    dateNodes.push(walker.currentNode);
                }
            }

            const result = [];
            let imgIndex = 0;
            const seenSrcs = new Set();

            for (const node of dateNodes) {
                const dateMatch = node.textContent.match(datePattern);
                if (!dateMatch) continue;
                const date = dateMatch[0];

                // Walk up to find containing article card
                let card = node.parentElement;
                for (let i = 0; i < 15 && card && card !== document.body; i++) {
                    if (card.textContent.length > 200) break;
                    card = card.parentElement;
                }
                if (!card || card === document.body) continue;

                const imgs = card.querySelectorAll('img[src*="images.zsxq.com"]');
                for (const img of imgs) {
                    if ((img.width > 100 || img.height > 100) && !seenSrcs.has(img.src)) {
                        seenSrcs.add(img.src);
                        result.push({ src: img.src, date: date, index: imgIndex++ });
                    }
                }
            }
            return result;
        }""")
        return cast(list[dict[str, Any]], raw)

    @staticmethod
    def extract_article_urls_from_text(text: str) -> list[str]:
        """Extract real ZSXQ short-code article URLs from text/JSON payloads."""
        urls = re.findall(r"https://articles\.zsxq\.com/id_[A-Za-z0-9]+\.html", text or "")
        return list(dict.fromkeys(urls))

    @classmethod
    def extract_article_url_from_topic(cls, topic: dict) -> str | None:
        """Return a real embedded article URL from a topic payload, if present."""
        candidates: list[str] = []
        talk = topic.get("talk") or {}
        article = talk.get("article") or {}
        for key in ("inline_article_url", "url", "article_url"):
            value = article.get(key) or talk.get(key) or topic.get(key)
            if isinstance(value, str):
                candidates.extend(cls.extract_article_urls_from_text(value))
        if candidates:
            return candidates[0]

        import json

        candidates.extend(cls.extract_article_urls_from_text(json.dumps(topic, ensure_ascii=False)))
        return candidates[0] if candidates else None

    @staticmethod
    def _raw_images_from_topic_payload(topic: dict, topic_type: str) -> list[dict]:
        if topic_type == "q&a":
            question_images = (topic.get("question") or {}).get("images") or []
            answer_images = (topic.get("answer") or {}).get("images") or []
            return [*question_images, *answer_images]
        return (topic.get("talk") or {}).get("images") or []

    @staticmethod
    def _normalize_topic_images(raw_images: list[dict]) -> list[dict]:
        images = []
        for idx, img in enumerate(raw_images):
            src = (img.get("original") or img.get("large") or {}).get("url", "")
            if src:
                images.append({"src": src, "date": "", "index": idx})
        return images

    @classmethod
    def extract_images_from_topic_payload(cls, topic: dict, topic_type: str = "talk") -> list[dict]:
        """Extract image URLs from a topic payload without making another API call."""
        raw_images = cls._raw_images_from_topic_payload(topic, topic_type)
        return cls._normalize_topic_images(raw_images)

    def extract_article_urls(self) -> list[str]:
        """从精华列表页提取所有文章详情页 URL"""
        page = self._require_page()
        urls = page.evaluate("""() => {
            const hrefs = [];
            // 方式1: 直接 a 标签
            document.querySelectorAll('a[href*="articles.zsxq.com/id_"]').forEach(a => hrefs.push(a.href));
            // 方式2: 所有 a 标签里找 articles.zsxq.com
            document.querySelectorAll('a[href*="articles.zsxq.com"]').forEach(a => hrefs.push(a.href));
            // 方式3: onclick 或 data-url 属性里的链接
            document.querySelectorAll('[data-url*="articles.zsxq.com"], [data-href*="articles.zsxq.com"]').forEach(el => {
                hrefs.push(el.dataset.url || el.dataset.href);
            });
            // 方式4: 从 data-topic-id 属性构造 URL
            document.querySelectorAll('[data-topic-id]').forEach(el => {
                const id = el.getAttribute('data-topic-id');
                if (id) hrefs.push('https://articles.zsxq.com/id_' + id + '.html');
            });
            // 方式5: 从 window 或 React state 获取（最后一次尝试）
            return [...new Set(hrefs)];
        }""")
        seen = set()
        unique = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        logger.info("[URLS] 提取到 %d 个文章链接", len(unique))
        return unique

    def scrape_article_page(self, url: str, allowed_hosts: set[str]) -> tuple[str, list[dict]]:
        """进入文章详情页，返回 (正文, 图片列表)"""
        # Convert articles.zsxq.com URL to wx.zsxq.com mweb URL
        import re

        tid_match = re.search(r"/id_(\d+)", url)
        if tid_match:
            tid = tid_match.group(1)
            url = f"https://wx.zsxq.com/mweb/views/topicdetail/topicdetail.html?topic_id={tid}"

        page = self._require_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(5000)
            with contextlib.suppress(Exception):
                page.wait_for_function(
                    "document.body.innerText.length > 300",
                    timeout=10000,
                )
        except Exception:
            logger.warning("[WARN] 无法加载 %s", url[:80])
            return "", []

        text = page.inner_text("body")
        # Filter out ZSXQ platform chrome and user comments
        text = self._clean_detail_text(text)
        images = self.get_images()
        return text, images

    @staticmethod
    def _clean_detail_text(text: str) -> str:
        """清理文章详情页文本：去广告、去评论、留正文"""
        lines = text.split("\n")
        result = []
        in_footer = False
        footer_markers = [
            "收费公示",
            "下载知识星球",
            "企业认证",
            "星球榜单",
            "运营高品质社群",
            "发表主题",
            "创建付费星球",
            "一分钟轻松创建",
            "内容创作、知识付费更方便",
            "支持的系统版本",
            "优质星球推荐",
        ]
        nav_markers = ["笔记", "管理后台", "榜单", "详情"]
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Stop at footer
            if any(marker in stripped for marker in footer_markers):
                in_footer = True
                continue
            if in_footer:
                continue
            # Skip navigation chrome
            if stripped in nav_markers:
                continue
            # Skip comment lines (username + colon + text)
            if re.match(r"^[^\s]{2,12}[：:]\s*\S", stripped):
                continue
            # Skip like/comment counts
            if re.match(r"^\d+人觉得很赞$|^\d+条评论$", stripped):
                continue
            result.append(stripped)
        return "\n".join(result)

    def get_cookies(self) -> dict[str, str]:
        ctx = self._require_context()
        cookies = ctx.cookies()
        return {c["name"]: c["value"] for c in cookies}

    def _save_page_diagnostics(self, context: str) -> str | None:
        """保存当前页面截图 + 状态摘要到 DEBUG_DIR，返回保存目录路径。失败返回 None。"""
        try:
            config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
            dir_name = f"{ts}_{context[:40].replace('/', '_').replace(' ', '_')}"
            diag_dir = config.DEBUG_DIR / dir_name
            diag_dir.mkdir(parents=True, exist_ok=True)

            page = self._require_page()
            # 截图
            screenshot_path = diag_dir / "page.png"
            screenshot_path.write_bytes(page.screenshot())

            # 页面状态摘要
            page_url = page.url if hasattr(page, "url") else "unknown"
            page_title = ""
            body_excerpt = ""
            with contextlib.suppress(Exception):
                page_title = page.title()
            with contextlib.suppress(Exception):
                body_excerpt = (page.evaluate("document.body?.innerText || ''") or "")[:500]

            state_text = (
                f"context: {context}\n"
                f"url: {page_url}\n"
                f"title: {page_title}\n"
                f"body_excerpt:\n{body_excerpt}\n"
            )
            (diag_dir / "state.txt").write_text(state_text, encoding="utf-8")

            logger.info("[DIAG] 页面诊断已保存: %s", diag_dir)
            return str(diag_dir)
        except Exception as e:
            logger.warning("[DIAG] 保存页面诊断失败: %s", e)
            return None

    @staticmethod
    def _api_delay():
        """随机延迟 0.5-2s，降低 API 反爬触发概率。"""
        import random as _random
        import time as _time

        _time.sleep(_random.uniform(0.5, 2.0))

    def fetch_api(self, url: str) -> dict[str, Any]:
        """用浏览器 Cookie 发起 API 请求并返回 JSON。"""
        self._api_delay()
        page = self._require_page()
        raw: Any = page.evaluate(f"""
        (async () => {{
            const resp = await fetch('{url}', {{credentials: 'include'}});
            let body = {{}};
            try {{ body = await resp.json(); }} catch (e) {{ body = {{error: String(e)}}; }}
            return {{status: resp.status, body}};
        }})()
        """)
        result: dict[str, Any] = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        status = result.get("status") if isinstance(result, dict) else None
        body = result.get("body", {}) if isinstance(result, dict) else result
        if status == 401:
            self._save_page_diagnostics(f"api_401_{url.split('/')[-2]}")
            raise ZsxqApiAuthError(f"ZSXQ API auth failed: HTTP {status}")
        if isinstance(body, dict) and body.get("succeeded") is False:
            code = body.get("code")
            if code in (401, 1059):
                self._save_page_diagnostics(f"api_code_{code}")
                raise ZsxqApiAuthError(
                    f"ZSXQ API auth failed: code={code} {body.get('info') or body.get('error') or ''}"
                )
            logger.warning(
                "[API] %s failed: code=%s %s",
                url[:80],
                code,
                body.get("info") or body.get("error") or "",
            )
        if status is not None and status != 200:
            ctx = f"api_status_{status}_code_{body.get('code', '?')}"
            self._save_page_diagnostics(ctx)
            return {}
        return body if isinstance(body, dict) else {}

    def fetch_topic_detail_payload(self, topic_id: str) -> dict[str, Any]:
        """通过 API 获取 topic 原始详情 payload。code=1059 时回退 DOM 提取。"""
        try:
            data = self.fetch_api(f"https://api.zsxq.com/v2/topics/{topic_id}")
            payload: Any = data.get("resp_data", {}).get("topic", {})
            if payload:
                return cast(dict[str, Any], payload)
        except ZsxqApiAuthError as e:
            logger.warning("[TOPIC] API 1059, falling back to DOM: %s", e)
        except Exception as e:
            logger.warning("[TOPIC] API 错误, falling back to DOM: %s", e)

        # Layer 2: DOM fallback
        return self.fetch_topic_detail_from_dom(topic_id) or {}

    def fetch_topic_detail_from_dom(self, topic_id: str) -> dict[str, Any] | None:
        """Layer 2: 浏览器导航到 topic 页面，从 DOM 提取 Q&A/talk 内容。

        返回与 API payload 兼容的 dict，失败返回 None。
        """
        import re

        group_id = "15522441811252"
        url = f"https://wx.zsxq.com/group/{group_id}/topic/{topic_id}"

        page = self._require_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            with contextlib.suppress(Exception):
                page.wait_for_function("document.body.innerText.length > 300", timeout=10000)
            text = page.inner_text("body")

            if not text or "没有查看该主题的权限" in text or len(text) < 100:
                logger.warning("[TOPIC-DOM] 无法访问 %s", topic_id)
                return None

            # Detect Q&A on RAW text: "提问：..." → [answer] → "等N人觉得很赞"
            qa_marker = re.search(r"(?:.+\s*)?提问[：:]", text)
            if qa_marker:
                after_q = text[qa_marker.end() :]
                question_text = ""
                answer_text = ""

                # Split Q from A: try disclaimer marker first, then blank line
                disclaimer_pos = after_q.find("免责声明")
                if disclaimer_pos > 0:
                    question_text = after_q[:disclaimer_pos].strip()
                    answer_text = after_q[disclaimer_pos + 4 :].strip()
                else:
                    # No disclaimer: split on first blank line
                    parts = after_q.split("\n\n", 1)
                    question_text = parts[0].strip()
                    answer_text = parts[1].strip() if len(parts) > 1 else ""

                # Stop answer at footer markers
                for end_marker in [r"等?\d+人觉得很赞", r"\d+条评论", "知识星球\nCDP Bridge"]:
                    m = re.search(end_marker, answer_text)
                    if m:
                        answer_text = answer_text[: m.start()]
                        break
                answer_text = answer_text.strip()[:5000]

                if question_text:
                    logger.info(
                        "[TOPIC-DOM] Q&A extracted: Q=%d A=%d chars",
                        len(question_text),
                        len(answer_text),
                    )
                    return {
                        "type": "q&a",
                        "question": {"text": question_text},
                        "answer": {"text": answer_text},
                    }

            # Clean and treat as talk
            cleaned = self._clean_detail_text(text)
            logger.info("[TOPIC-DOM] talk extracted: %d chars", len(cleaned))
            return {"type": "talk", "talk": {"text": cleaned}}

        except Exception as e:
            logger.warning("[TOPIC-DOM] 提取失败 %s: %s", topic_id, e)
            return None

    def fetch_topic_detail(self, topic_id: str) -> str:
        """通过 API 获取文章全文（比页面抓取更可靠，无广告文本）"""
        topic = self.fetch_topic_detail_payload(topic_id)
        if not topic:
            return ""
        talk: dict[str, Any] | None = topic.get("talk")  # type: ignore[assignment]
        if talk and talk.get("text"):
            return self._clean_api_text(cast(str, talk["text"]))
        return cast(str, topic.get("text", ""))

    @staticmethod
    def _clean_api_text(text: str) -> str:
        """清理 API 文本中的 HTML 标签"""
        import re

        # Remove <e ... /> tags but keep text content
        text = re.sub(r"<e\s+[^>]*?title=\"([^\"]*)\"[^>]*?/>", r"\1", text)
        # URL decode the title attribute values
        from urllib.parse import unquote

        text = unquote(text)
        # Remove remaining <e> tags
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()

    def fetch_columns(self) -> list[dict[str, Any]]:
        """获取圈子的所有栏目列表"""
        data = self.fetch_api("https://api.zsxq.com/v2/groups/15522441811252/columns?count=30")
        return cast(list[dict[str, Any]], data.get("resp_data", {}).get("columns", []))

    def fetch_column_topics(
        self, column_id: str, end_time: str = "", count: int = 30
    ) -> list[dict[str, Any]]:
        """获取指定栏目的文章列表，支持时间分页"""
        params = f"count={count}&sort=default&direction=desc"
        if end_time:
            from urllib.parse import quote

            params += f"&end_time={quote(end_time, safe='')}"
        url = f"https://api.zsxq.com/v2/groups/15522441811252/columns/{column_id}/topics?{params}"
        data = self.fetch_api(url)
        return cast(list[dict[str, Any]], data.get("resp_data", {}).get("topics", []))

    def fetch_topics_by_scope(self, scope: str, end_time: str = "") -> list[dict[str, Any]]:
        """通过 API 获取指定 scope 的 topic 列表（含 create_time + 类型 + 文本）。

        scope: "all"（全部）或 "digests"（精华）
        返回 [{topic_id, create_time, title, type, content_text}]
        content_text 从 talk.text（普通帖）或 question.text+answer.text（问答帖）提取
        """
        from urllib.parse import quote

        params = f"scope={scope}&count=30"
        if end_time:
            params += f"&end_time={quote(end_time, safe='')}"
        url = f"https://api.zsxq.com/v2/groups/15522441811252/topics?{params}"

        data = self.fetch_api(url)
        topics = data.get("resp_data", {}).get("topics", [])

        result = []
        for t in topics:
            topic_type = t.get("type", "talk")
            # 按类型提取文本
            if topic_type == "q&a":
                question_text = (t.get("question") or {}).get("text", "") or ""
                answer_text = (t.get("answer") or {}).get("text", "") or ""
                content_text = (
                    f"问：{question_text}\n\n答：{answer_text}" if question_text else answer_text
                )
            else:
                content_text = (t.get("talk") or {}).get("text", "") or ""

            result.append(
                {
                    "topic_id": str(t.get("topic_id", "")),
                    "create_time": (t.get("create_time") or "")[:16].replace("T", " "),
                    "title": (t.get("title") or "")[:150],
                    "type": topic_type,
                    "content_text": content_text,
                    "article_url": (t.get("talk") or {})
                    .get("article", {})
                    .get("inline_article_url", "")
                    or "",
                }
            )

        logger.info("[API] scope=%s end_time=%s → %d topics", scope, end_time[:20], len(result))
        return result

    def fetch_article_content(self, article_url: str) -> tuple[str, list[dict]]:
        """从 ZSXQ 文章链接获取全文 + 图片，返回 (纯文本, [{src, date, index}])。

        用于 talk.article 型帖子——talk.text 只是摘要，正文+图片在文章链接里。
        优先通过当前登录浏览器打开文章页，失败时再退回 requests。
        """
        import re
        from urllib.parse import urlparse

        # SSRF 防护：只允许 zsxq.com 域名
        parsed = urlparse(article_url)
        hostname = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not hostname.endswith(".zsxq.com")
            and hostname != "zsxq.com"
        ):
            logger.warning("[ARTICLE] 拒绝非 ZSXQ 域名的文章链接: %s", article_url)
            return "", []

        page = self.page
        if page:
            try:
                page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
                with contextlib.suppress(Exception):
                    page.wait_for_function(
                        "document.body.innerText.length > 300", timeout=10000
                    )
                page_data = page.evaluate("""() => {
                    const urls = [];
                    const seen = new Set();
                    document.querySelectorAll('img[src*="article-images.zsxq.com"], img[src*="images.zsxq.com"]').forEach((img) => {
                        if (img.src && !seen.has(img.src)) {
                            seen.add(img.src);
                            urls.push(img.src);
                        }
                    });
                    return {
                        text: document.body.innerText,
                        images: urls.map((src, index) => ({src, date: "", index})),
                    };
                }""")
                text = (
                    self._clean_detail_text(page_data.get("text", ""))
                    if isinstance(page_data, dict)
                    else ""
                )
                images = page_data.get("images", []) if isinstance(page_data, dict) else []
                if text and len(text) > 80:
                    if images:
                        logger.info("[ARTICLE] 从浏览器页面提取 %d 张图片", len(images))
                    return text, images
                logger.warning("[ARTICLE] 浏览器页面正文过短，回退 requests: %s", article_url)
                self._save_page_diagnostics(f"article_short_text_{len(text)}")
            except Exception as e:
                logger.warning(
                    "[ARTICLE] 浏览器打开文章失败，回退 requests: %s — %s", article_url, e
                )
                self._save_page_diagnostics(f"article_goto_failed_{type(e).__name__}")

        cookies = self.get_cookies()
        try:
            import requests

            s = requests.Session()
            for k, v in cookies.items():
                s.cookies.set(k, v)
            resp = s.get(
                article_url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://wx.zsxq.com/"},
                timeout=30,
            )
            if resp.status_code != 200:
                return "", []
            html = resp.text
        except Exception:
            return "", []

        # 提取图片 URL（去重保持顺序）
        img_urls: list[str] = []
        seen: set[str] = set()
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', html):
            url = m.group(1)
            if url not in seen and "article-images.zsxq.com" in url:
                seen.add(url)
                img_urls.append(url)
        images = [{"src": u, "date": "", "index": i} for i, u in enumerate(img_urls)]
        if images:
            logger.info("[ARTICLE] 从文章 HTML 提取 %d 张图片", len(images))

        # 清理 HTML → 纯文本
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "", html)
        import html as html_mod

        text = html_mod.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, images

    def extract_images_from_topic(self, topic_id: str, topic_type: str = "talk") -> list[dict]:
        """从列表 API 提取指定 topic 的图片 URL（详情页无法获取时使用）。

        返回格式与 get_images() 兼容：[{src, date, index}]
        """
        from urllib.parse import quote

        # 用列表 API 查询（最近一周覆盖该 topic）
        end_time = quote("2026-06-25T00:00:00.000+0800", safe="")
        url = f"https://api.zsxq.com/v2/groups/15522441811252/topics?scope=all&count=30&end_time={end_time}"
        try:
            data = self.fetch_api(url)
            topics = data.get("resp_data", {}).get("topics", [])
        except Exception:
            return []

        for topic in topics:
            if str(topic.get("topic_id", "")) == str(topic_id):
                images = self.extract_images_from_topic_payload(topic, topic_type)
                if images:
                    logger.info(
                        "[IMAGES] 从列表 API 提取 %d 张图片 (type=%s)", len(images), topic_type
                    )
                return images
        return []

    def close(self):
        self.stop_browser()
