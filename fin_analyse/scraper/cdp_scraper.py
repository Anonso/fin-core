"""CDP Bridge 爬虫 — 通过 Windows Chrome 抓取 ZSXQ 内容

正文保持 DOM/detail-first；当无限滚动无法证明三日窗口 coverage 时，使用
同一登录页的固定 native-topic cursor 做有界恢复，拒绝任意 API surface。

用法:
    from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper
    scraper = CdpBridgeScraper()
    n = scraper.run_incremental()
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlencode

from fin_analyse.cognition.llm import CognitionCompletionControl
from fin_analyse.common.execution_control import ExecutionFence
from fin_analyse.guo_teacher_research.source_contract import classify_g_source
from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

from .cdp_diagnostics import CdpBatchResult, classify_cdp_error
from .config import COLUMN_PATTERNS, KNOWN_COMPANIES, score_skip_min

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))
INCREMENTAL_WINDOW_DAYS = 3
# TH CDP Bridge's MV3 extension probes a missing bridge on an approximately
# five-second alarm and can spend another two seconds checking the port. Keep
# the first server generation alive across at least two probe cycles so reload
# cleanup from the previous run can reconnect without operator intervention.
_SCRAPE_EXTENSION_CONNECT_WAIT_SECONDS = 20.0
_TOPIC_CURSOR_PAGE_PACING_SECONDS = 8.0
_TOPIC_CURSOR_RATE_LIMIT_CODE = 1059
#: 图片处理有界预算：图片是辅助内容，其失败/超时/海量不可达只降级图片本身，
#: 绝不能拖垮文字采集与 G 发布（用户硬要求）。预算耗尽 → 跳过剩余图片并告警。
_IMAGE_PROCESS_BUDGET_SECONDS = 180.0
_IMAGE_DEADLINE_RESERVE_SECONDS = 5.0
#: OCR/vision 预算储备：vision 每 endpoint 可等 30s 且可 fallback（最坏 ~90s）。
#: 剩余预算 < 储备时跳过剩余图片的 OCR/vision——图片处理不可能越过 run deadline
#: 拖垮文字保存与 G 发布（用户硬要求；紧 deadline 下图片如实降级）。
_IMAGE_VISION_RESERVE_SECONDS = 90.0
#: 每轮一次的 deep-read LLM config preflight 失败时的 bounded typed run warning。
#: 只暴露类型，不携带配置、异常文本或 secret。
_DEEP_READ_LLM_CONFIG_INVALID = "deep_read_llm_config_invalid"
#: 每轮 ingest 排空的存量非新鲜 strict-G 深化上限（有界，防 LLM 突发）。
_DEEP_READ_BACKLOG_DRAIN_LIMIT = 3
GROUP_URL = "https://wx.zsxq.com/group/15522441811252"


def _score_skip_enabled(score: Any) -> bool:
    """评分门槛判定：score 缺失/非法 → 不跳过；有评分且低于配置阈值 → 跳过。

    阈值来源 config/zsxq_capture.json（D-037 默认 6.0）；无评分/非法评分仍
    不跳过——评分缺失不等于内容无价值（书单/宏观/问答等无表帖照常入库）。
    """
    if score is None:
        return False
    try:
        return float(score) < score_skip_min()
    except (TypeError, ValueError):
        return False


def _cache_bust_url(url: str) -> str:
    """Append a unique cache-busting query parameter to force a fresh server fetch."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_fin_ts={int(time.time() * 1000)}"


def _extract_datetimes_from_text(text: str, now: datetime | None = None) -> list[datetime]:
    """Extract visible ZSXQ timestamps from absolute and relative date text."""
    base = now.astimezone(TZ) if now else datetime.now(TZ)
    dates: list[datetime] = []

    def add_date(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> None:
        with suppress(ValueError):
            dates.append(datetime(year, month, day, hour, minute, tzinfo=TZ))

    # Full absolute forms: 2026-07-09 10:30, 2026/7/9 10:30, 2026年7月9日 10:30.
    for m in re.finditer(
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?:\s+(\d{1,2}):(\d{2}))?",
        text,
    ):
        add_date(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4) or 0),
            int(m.group(5) or 0),
        )

    # ZSXQ often renders recent items as 今天/昨天/前天 HH:MM.
    day_offsets = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2}
    for m in re.finditer(r"(今天|今日|昨天|昨日|前天)\s*(\d{1,2}):(\d{2})", text):
        dt = base - timedelta(days=day_offsets[m.group(1)])
        add_date(dt.year, dt.month, dt.day, int(m.group(2)), int(m.group(3)))

    # Month-day forms without a year: 07-08 18:20 / 7月8日 18:20.
    for m in re.finditer(
        r"(?<![\d/\-])(\d{1,2})[-/月](\d{1,2})(?:日)?\s+(\d{1,2}):(\d{2})",
        text,
    ):
        month = int(m.group(1))
        day = int(m.group(2))
        hour = int(m.group(3))
        minute = int(m.group(4))
        year = base.year
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=TZ)
            if candidate > base + timedelta(days=1):
                candidate = datetime(year - 1, month, day, hour, minute, tzinfo=TZ)
            dates.append(candidate)
        except ValueError:
            pass

    for m in re.finditer(r"(\d{1,3})\s*分钟前", text):
        dates.append(base - timedelta(minutes=int(m.group(1))))
    for m in re.finditer(r"(\d{1,3})\s*小时前", text):
        dates.append(base - timedelta(hours=int(m.group(1))))
    for m in re.finditer(r"(\d{1,2})\s*天前", text):
        dates.append(base - timedelta(days=int(m.group(1))))
    if "刚刚" in text:
        dates.append(base)

    return dates


# 作者名匹配模式（用于切分文章和去重）
AUTHOR_NAME = "三线文案大锅饭"


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting ambiguity at every nesting level."""
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError("duplicate JSON object key")
        decoded[key] = value
    return decoded


_TIMELINE_TIMESTAMP_LINE_RE = re.compile(
    r"(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?"
    r"|\d{4}年\d{1,2}月\d{1,2}日(?:\s+\d{1,2}:\d{2})?"
    r"|(?:今天|今日|昨天|昨日|前天)\s*\d{1,2}:\d{2}"
    r"|\d{1,2}[-/月]\d{1,2}(?:日)?\s+\d{1,2}:\d{2}"
    r"|\d{1,3}\s*(?:分钟|小时|天)前"
    r"|刚刚"
    r")"
)
#: cursor 教师 talk 截断尾（…/...）——feed/detail 双侧都截断的跳转链接文章，
#: 必须诚实标 incomplete，供 _should_recapture 与 derive_quality 识别升级。
_TRUNCATED_TAIL_RE = re.compile(r"(?:\.{3}|…)\s*$")


_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT = r"""
(function finTimelineTimestampEvidence() {
    const TIMELINE_TIME_SELECTOR = 'time, [class*="date"], [class*="time"]';
    const GROUP_TOPIC_PATH = /^\/group\/15522441811252\/topic\/(\d+)\/?$/;

    const isVisible = (node) => {
        if (!(node instanceof Element) || !node.isConnected) return false;
        if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;
        const style = window.getComputedStyle(node);
        if (style.display === 'none'
            || style.visibility === 'hidden'
            || style.visibility === 'collapse'
            || Number(style.opacity) === 0) return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const nativeTopicIds = (candidate) => {
        const ids = new Set();
        for (const node of [candidate, ...candidate.querySelectorAll('[data-topic-id]')]) {
            const value = String(node.getAttribute('data-topic-id') || '');
            if (/^\d+$/.test(value)) ids.add(value);
        }
        for (const link of candidate.querySelectorAll('a[href]')) {
            try {
                const url = new URL(String(link.getAttribute('href') || ''), location.origin);
                if (url.origin !== location.origin) continue;
                const match = url.pathname.match(GROUP_TOPIC_PATH);
                if (match) ids.add(match[1]);
            } catch (_error) {
                // A malformed href is not native topic identity.
            }
        }
        return ids;
    };

    const owningNativeTopicCard = (node) => {
        let candidate = node.parentElement;
        for (let depth = 0; candidate && candidate !== document.body && depth < 12; depth += 1) {
            if (isVisible(candidate)) {
                const ids = nativeTopicIds(candidate);
                if (ids.size === 1) return {card: candidate, topicId: Array.from(ids)[0]};
                if (ids.size > 1) return null;
            }
            candidate = candidate.parentElement;
        }
        return null;
    };

    const evidenceByTopic = new Map();
    for (const node of document.querySelectorAll(TIMELINE_TIME_SELECTOR)) {
        if (!isVisible(node)) continue;
        const owner = owningNativeTopicCard(node);
        if (!owner) continue;
        const timestamp = (node.innerText || node.textContent || '').trim();
        if (!timestamp || timestamp.length > 80) continue;
        const existing = evidenceByTopic.get(owner.topicId) || {
            topic_id: owner.topicId,
            header_lines: String(owner.card.innerText || owner.card.textContent || '')
                .split(/\r?\n/)
                .map((line) => line.trim())
                .filter(Boolean)
                .slice(0, 12),
            timestamps: []
        };
        if (!existing.timestamps.includes(timestamp)) existing.timestamps.push(timestamp);
        evidenceByTopic.set(owner.topicId, existing);
    }

    return JSON.stringify({
        schema_version: 1,
        items: Array.from(evidenceByTopic.values())
    });
})()
"""

_TIMELINE_LOADER_STATE_SCRIPT = r"""
(function finTimelineLoaderState() {
    const isVisibleInViewport = (node) => {
        if (!(node instanceof Element) || !node.isConnected) return false;
        if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;
        const style = window.getComputedStyle(node);
        if (style.display === 'none'
            || style.visibility === 'hidden'
            || style.visibility === 'collapse'
            || Number(style.opacity) === 0) return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0
            && rect.height > 0
            && rect.bottom > 0
            && rect.top < window.innerHeight;
    };
    const candidates = document.querySelectorAll(
        'app-lottie-loading, app-lottie-loading .flow-loading'
    );
    return JSON.stringify({
        visible: Array.from(candidates).some(isVisibleInViewport)
    });
})()
"""

# ── 脚本常量（传输面共享）───────────────────────────────────────────
# 这些脚本由 CdpBridgeScraper 与 Windows 原生 capture（scripts/capture_zsxq_windows.cjs，
# 2026-09-05 自老仓迁入本仓）两侧分别执行；脚本文本必须在两侧逐字节一致
# （一致性测试 tests/scraper/test_capture_script_consistency.py 校验）。
# replay client（capture_replay_client.py）按 sha256 匹配录制输出。

_FULL_TEXT_SCRIPT = "document.body.innerText"
#: 视图滚动指标（_scroll_until_cutoff 的页面底证据）
_SCROLL_METRICS_SCRIPT = """(function() {
    const el = document.scrollingElement || document.documentElement;
    return JSON.stringify({
        scrollTop: el.scrollTop,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight
    });
})()"""
#: 详情页内联文章链接（星大派专栏全文在 articles.zsxq.com）
_EXTRACT_INLINE_ARTICLE_SCRIPT = """(function() {
    var links = document.querySelectorAll('a[href*="articles.zsxq.com/id_"]');
    for (var i = 0; i < links.length; i++) {
        var href = links[i].href || '';
        var m = href.match(/articles\\.zsxq\\.com\\/id_\\w+\\.html/);
        if (m) return href;
    }
    return '';
})()"""
#: 时间线 topic 链接 + 标题（详情页导航的 topic_id 来源）
_EXTRACT_TOPIC_IDS_SCRIPT = """(function() {
    var results = [];
    var links = document.querySelectorAll('a[href*="/topic/"]');
    for (var i = 0; i < links.length; i++) {
        var href = links[i].href || '';
        var text = (links[i].textContent || '').trim();
        var m = href.match(/topic[/](\\d+)/);
        if (m) {
            results.push({topic_id: m[1], title: text.substring(0, 200)});
        }
    }
    return JSON.stringify(results);
})()"""
#: 「查看详情」展开循环（截断短文恢复全文）
_EXPAND_DETAILS_SCRIPT = """(function() {
    const links = document.querySelectorAll('a, span, div');
    for (const el of links) {
        if (el.textContent.trim() === '查看详情') {
            el.click();
            return 'clicked';
        }
    }
    return 'done';
})()"""
#: 有日期锚点的文章卡片图片（本 slice capture 侧暂不采集，输出恒为 []）
_IMAGES_BY_DATE_SCRIPT = """(function() {
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
                result.push({src: img.src, date: date, index: imgIndex++});
            }
        }
    }
    return JSON.stringify(result);
})()"""


def _parse_timeline_timestamp(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse one timestamp collected from a structural timeline time node."""
    normalized = text.strip()
    if not _TIMELINE_TIMESTAMP_LINE_RE.fullmatch(normalized):
        return None
    dates = _extract_datetimes_from_text(normalized, now=now)
    return dates[0] if dates else None


def _decode_timeline_timestamp_evidence(raw: str, *, now: datetime | None = None) -> list[datetime]:
    """Validate DOM timestamp evidence; malformed or ambiguous payloads prove nothing."""

    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "items"}:
        return []
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        return []
    if not isinstance(payload.get("items"), list) or len(payload["items"]) > 500:
        return []

    dates: list[datetime] = []
    seen_topic_ids: set[str] = set()
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != {
            "topic_id",
            "header_lines",
            "timestamps",
        }:
            return []
        topic_id = item.get("topic_id")
        if (
            not isinstance(topic_id, str)
            or not topic_id.isascii()
            or not topic_id.isdecimal()
            or topic_id in seen_topic_ids
        ):
            return []
        seen_topic_ids.add(topic_id)
        header_lines = item.get("header_lines")
        if (
            not isinstance(header_lines, list)
            or not 2 <= len(header_lines) <= 12
            or not all(
                isinstance(line, str) and 0 < len(line.strip()) <= 200 for line in header_lines
            )
        ):
            return []
        timestamps = item.get("timestamps")
        if not isinstance(timestamps, list) or len(timestamps) != 1:
            return []
        timestamp = timestamps[0]
        if not isinstance(timestamp, str):
            return []
        normalized_lines = [line.strip() for line in header_lines]
        try:
            author_index = normalized_lines.index(AUTHOR_NAME)
        except ValueError:
            return []
        if author_index + 1 >= len(normalized_lines):
            return []
        if normalized_lines[author_index + 1] != timestamp.strip():
            return []
        parsed = _parse_timeline_timestamp(timestamp, now=now)
        if parsed is None:
            return []
        dates.append(parsed)
    return dates


@dataclass
class ScrapeResult:
    """增量抓取结果（窗口感知）"""

    new_count: int = 0
    scrape_completed: bool = False
    posts_seen: int = 0
    dom_text_chars: int = 0
    images_days: int = 0

    # 窗口指标
    window_days: int = INCREMENTAL_WINDOW_DAYS
    cutoff: str = ""  # ISO format
    oldest_seen_date: str = ""
    stopped_by_window_boundary: bool = False
    reached_page_end: bool = False

    # Priority event fields
    new_articles: list[str] = field(default_factory=list)
    sources_scanned: list[str] = field(default_factory=list)
    priority_events_created: int = 0
    priority_event_ids: list[str] = field(default_factory=list)

    # Deep read artifacts
    deep_read_artifacts_created: int = 0
    # Deep-read journal counters (observability only; not durable schema).
    deep_read_eligible: int = 0
    deep_read_cache_hit: int = 0
    deep_read_retryable: int = 0
    deep_read_error: int = 0

    # 诊断
    failure_kind: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class GroupTimelineLoadResult:
    """Loaded group timeline text plus batch/fallback diagnostics."""

    full_text: str = ""
    used_batch: bool = False
    stopped_by_window_boundary: bool = False
    reached_page_end: bool = False
    timeline_dates: list[datetime] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _TopicCursorItem:
    """One strictly validated native topic returned by the signed-in ZSXQ page."""

    topic_id: str
    legacy_topic_id: str
    create_time: str
    created_at: datetime
    title: str
    topic_type: str
    content_text: str
    answer_state: str
    is_teacher_source: bool


@dataclass(frozen=True)
class _TopicCursorPage:
    """Typed projection of one fixed ZSXQ group-topic cursor response."""

    http_status: int
    api_succeeded: bool
    api_code: int | None
    topics: tuple[_TopicCursorItem, ...]


@dataclass(frozen=True)
class _TopicCursorCoverage:
    """Bounded cursor result used only when DOM coverage cannot be proven."""

    topics: tuple[_TopicCursorItem, ...] = ()
    covered: bool = False
    boundary_kind: str = ""
    oldest_observed_at: datetime | None = None
    failure_code: str = ""


def _parse_topic_create_time(raw: str) -> datetime | None:
    """Parse one timezone-aware native topic time without guessing a timezone."""
    if not isinstance(raw, str) or not 16 <= len(raw) <= 40 or raw != raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(TZ)


def _decode_topic_cursor_page(raw: str) -> _TopicCursorPage | None:
    """Decode the fixed browser projection; malformed/ambiguous data is no evidence."""
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "http_status",
        "api_succeeded",
        "api_code",
        "topics",
    }:
        return None
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 4:
        return None
    http_status = payload.get("http_status")
    api_succeeded = payload.get("api_succeeded")
    api_code = payload.get("api_code")
    raw_topics = payload.get("topics")
    if (
        type(http_status) is not int
        or not 100 <= http_status <= 599
        or type(api_succeeded) is not bool
        or (api_code is not None and type(api_code) is not int)
        or not isinstance(raw_topics, list)
        or len(raw_topics) > 30
    ):
        return None

    topics: list[_TopicCursorItem] = []
    seen_ids: set[str] = set()
    seen_legacy_ids: set[str] = set()
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict) or set(raw_topic) != {
            "topic_id",
            "legacy_topic_id",
            "create_time",
            "title",
            "topic_type",
            "content_text",
            "source_class",
            "answer_state",
        }:
            return None
        topic_id = raw_topic.get("topic_id")
        legacy_topic_id = raw_topic.get("legacy_topic_id")
        create_time = raw_topic.get("create_time")
        title = raw_topic.get("title")
        topic_type = raw_topic.get("topic_type")
        content_text = raw_topic.get("content_text")
        source_class = raw_topic.get("source_class")
        answer_state = raw_topic.get("answer_state")
        if (
            not isinstance(topic_id, str)
            or not topic_id.isascii()
            or not topic_id.isdecimal()
            or not 1 <= len(topic_id) <= 32
            or topic_id in seen_ids
            or not isinstance(legacy_topic_id, str)
            or not legacy_topic_id.isascii()
            or not legacy_topic_id.isdecimal()
            or not 1 <= len(legacy_topic_id) <= 32
            or legacy_topic_id in seen_legacy_ids
            or not isinstance(create_time, str)
            or not isinstance(title, str)
            or len(title) > 500
            or topic_type not in {"talk", "q&a"}
            or not isinstance(content_text, str)
            or len(content_text) > 200_000
            or source_class not in {"teacher", "coverage_only"}
        ):
            return None
        if topic_type == "talk":
            if answer_state != "not_applicable":
                return None
        elif answer_state not in {"answered", "unanswered"}:
            return None
        if source_class == "coverage_only":
            if content_text or title:
                return None
        elif (
            not content_text.strip()
            or answer_state == "unanswered"
            or (topic_type == "q&a" and title)
        ):
            return None
        is_teacher_source = source_class == "teacher"
        created_at = _parse_topic_create_time(create_time)
        if created_at is None:
            return None
        seen_ids.add(topic_id)
        seen_legacy_ids.add(legacy_topic_id)
        topics.append(
            _TopicCursorItem(
                topic_id=topic_id,
                legacy_topic_id=legacy_topic_id,
                create_time=create_time,
                created_at=created_at,
                title=title,
                topic_type=topic_type,
                content_text=content_text,
                answer_state=answer_state,
                is_teacher_source=is_teacher_source,
            )
        )
    return _TopicCursorPage(
        http_status=http_status,
        api_succeeded=api_succeeded,
        api_code=api_code,
        topics=tuple(topics),
    )


def _is_login_surface_text(text: str) -> bool:
    """Detect the ZSXQ QR login surface from raw page text (same heuristic as
    (keepalive path), so a login expiry is not folded into
    ``content_insufficient``."""
    return "登录" in text and "扫码" in text


_DISCLAIMER_LINE_PREFIX = "免责声明"
_SCORE_LINE_PREFIX = "能量评分"


def _strip_disclaimer_line(content: str) -> str:
    """剥账号免责声明行；声明在尾保留之前、在首保留之后（BUG-027）。

    行首锚定（与 parser.py `line.startswith("免责声明")` 既有语义一致），
    正文中段/行尾内联提及不剥。声明前是否为「实质内容」排除免责声明行与
    能量评分行——评分行前置的头部变体不会把纯评分误判成帖尾声明之前的
    正文。剥离后为空即纯声明帖，诚实丢弃。声明跨多行时只剥锚定行，续行
    会漏入正文（无实证，已知限制）。
    """
    lines = content.splitlines()
    index = next(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip().startswith(_DISCLAIMER_LINE_PREFIX)
        ),
        None,
    )
    if index is None:
        return content.rstrip()
    before_substantive = any(
        line.strip()
        and not line.lstrip().startswith(_DISCLAIMER_LINE_PREFIX)
        and not line.lstrip().startswith(_SCORE_LINE_PREFIX)
        for line in lines[:index]
    )
    if before_substantive:
        return "\n".join(lines[:index]).rstrip()
    return "\n".join(lines[index + 1 :]).strip()


def _is_group_timeline_content_insufficient(result: ScrapeResult) -> bool:
    """Return True when group scan did not capture a usable ZSXQ timeline."""
    # A date-only page is commonly a different ZSXQ surface (digests/archive)
    # reached after tab recovery.  Treat it as incomplete instead of reporting a
    # successful zero-change reconciliation. A strictly validated native cursor
    # may, however, prove a legitimate member-only/empty-teacher window without
    # crossing any member content into ingestion.
    return result.posts_seen == 0 and "group_topic_cursor" not in result.sources_scanned


class _BridgeClient(Protocol):
    """Minimal CDP bridge client surface used by the scraper."""

    _error: str | None

    def start(self) -> bool: ...

    def close(self) -> None: ...

    def navigate(
        self,
        url: str,
        wait: float = 3.0,
        cache_bust: bool = False,
    ) -> None: ...

    def js(self, script: str) -> str: ...

    def scroll_by(self, px: int = 4000, wait: float = 1.0) -> None: ...

    def validate_page_state(
        self,
        page_text: str | None = None,
    ) -> tuple[bool, str]: ...

    def force_navigate_group(self, url: str, wait: float = 5.0) -> bool: ...

    def heal_tab_via_new_window(self, url: str, wait: float = 5.0) -> int | None: ...

    def batch_execute(self, steps: list[dict]) -> CdpBatchResult: ...


class CdpBridgeScraper:
    """基于 CDP Bridge 的 ZSXQ 爬虫（DOM-first，不调 ZSXQ API）"""

    def __init__(
        self,
        priority_only: bool = False,
        *,
        knowledge_base_root: Path | str | None = None,
        deadline_at: datetime | None = None,
        checkpoint: Callable[[], None] | None = None,
        client_factory: Callable[..., _BridgeClient] | None = None,
        incremental_cutoff: datetime | None = None,
    ):
        self._client: _BridgeClient | None = None
        self._client_factory = client_factory
        self._index: dict[str, dict] = {}
        self._new_count = 0
        self._author_name = AUTHOR_NAME
        self._priority_only = priority_only
        self._sources_scanned: list[str] = []
        self._kb_root = (
            Path(knowledge_base_root)
            if knowledge_base_root is not None
            else default_knowledge_base_root()
        )
        self._articles_dir = self._kb_root / "articles"
        self._index_file = self._kb_root / "index.json"
        #: 每轮一次的 deep-read LLM config preflight；None=未执行，True/False=结果。
        #: 失败时只记一次 typed warning，零逐文章 LLM 调用。
        self._deep_read_llm_preflight_ok: bool | None = None
        self._deep_read_llm_config_warning_recorded = False
        #: Optional total-deadline + cooperative checkpoint threaded from the
        #: module. Both default off so existing direct callers are unaffected.
        self._deadline_at = deadline_at
        self._checkpoint = checkpoint
        # A validated capture artifact freezes its collection window before the
        # WSL handoff. Live scraping leaves this unset and uses the local clock.
        self._incremental_cutoff = incremental_cutoff

    # ── 启动/关闭 ──────────────────────────────────────────

    def _surface_checkpoint(self) -> None:
        """Cooperative heartbeat/deadline boundary between bounded scraping steps.

        No-op for direct callers (no checkpoint injected). When the module injects
        one, it renews the lease and rejects an expired total deadline before more
        work — the rejection propagates out of the scraper to fail visibly.
        """
        if self._checkpoint is not None:
            self._checkpoint()

    def _wait_between_topic_cursor_requests(self) -> None:
        """Pace native cursor requests without escaping the total deadline."""
        if self._client is None:
            return
        self._surface_checkpoint()
        wait_seconds = _TOPIC_CURSOR_PAGE_PACING_SECONDS
        if self._deadline_at is not None:
            remaining = (self._deadline_at - datetime.now(self._deadline_at.tzinfo)).total_seconds()
            if remaining <= 0:
                raise RuntimeError("topic cursor deadline exhausted before pacing")
            wait_seconds = min(wait_seconds, remaining)
        time.sleep(wait_seconds)
        self._surface_checkpoint()
        if (
            self._deadline_at is not None
            and datetime.now(self._deadline_at.tzinfo) >= self._deadline_at
        ):
            raise RuntimeError("topic cursor deadline exhausted during pacing")

    def start(self) -> bool:
        if self._client_factory is None:
            raise RuntimeError("CdpBridgeScraper.start requires an injected client_factory")
        client_kwargs: dict[str, Any] = {
            "startup_wait": _SCRAPE_EXTENSION_CONNECT_WAIT_SECONDS,
            "purpose": "scrape",
            "deadline_at": self._deadline_at,
        }
        self._client = self._client_factory(**client_kwargs)
        return self._client.start()

    def close(self):
        if self._client:
            self._client.close()
        self._client = None

    def __enter__(self):
        if not self.start():
            error_msg = (self._client._error if self._client else None) or "CDP Bridge 连接失败"
            kind = classify_cdp_error(error_msg)
            raise RuntimeError(f"CDP Bridge 连接失败 [{kind}]: {error_msg}")
        return self

    def __exit__(self, *args):
        self.close()

    # ── 页面操作 ────────────────────────────────────────────

    def _nav(self, url: str, wait: float = 4.0):
        assert self._client is not None  # set by start()
        self._client.navigate(url, wait=wait)

    def _js(self, script: str) -> str:
        assert self._client is not None  # set by start()
        return self._client.js(script)

    def _validate_and_heal_tab(self) -> tuple[bool, str]:
        """验证当前 Tab 页面状态，异常时自愈（重新导航 → 开新 Tab）。

        两层策略：
        1. 强制重新导航当前 Tab（force_navigate_group）
        2. 开全新 Tab 导航到首页（heal_tab_via_new_window）

        Returns:
            (healed, reason) — healed=True 表示 Tab 状态已恢复。
        """
        assert self._client is not None

        # 先验证
        is_valid, reason = self._client.validate_page_state()
        if is_valid:
            return True, "tab_ok"

        logger.warning("[HEAL] Tab 状态异常 (%s)，开始自愈...", reason)

        # 第一层：强制重新导航当前 Tab
        if self._client.force_navigate_group(GROUP_URL, wait=5.0):
            logger.info("[HEAL] 第一层自愈成功: force_navigate_group")
            return True, "force_navigate_ok"

        # 第二层：开新 Tab
        logger.warning("[HEAL] force_navigate_group 无效，尝试开新 Tab...")
        new_id = self._client.heal_tab_via_new_window(GROUP_URL, wait=5.0)
        if new_id is not None:
            logger.info("[HEAL] 第二层自愈成功: new_tab=%s", new_id)
            return True, f"new_tab_{new_id}"

        return False, reason

    def _scan_digests(self, cutoff: datetime, existing_ids: set[str]) -> list[str]:
        """Navigate to digests page and extract star column articles.

        Lightweight: navigates once, does limited scrolling (not full window scan),
        and only saves articles in star columns.

        Returns list of newly saved article IDs.
        """
        saved: list[str] = []

        self._nav("https://wx.zsxq.com/digests/15522441811252", wait=4.0)
        assert self._client is not None  # set by start()
        for _ in range(3):
            self._client.scroll_by(1000, wait=1.0)

        full_text = self._full_text()
        images_by_date = self._images_by_date_from_page()

        posts = self._split_by_author(full_text)
        star_posts: list[dict] = []
        for part in posts:
            post = self._parse_post(part)
            if not post:
                continue
            post_date = self._parse_date(post.get("date", ""))
            if post_date and post_date < cutoff:
                continue
            post_id = self._make_id(post)
            if post_id in existing_ids:
                continue
            if self._is_platform_chrome(post.get("title", ""), post.get("content", "")):
                continue
            if post.get("column", "普通") not in self._STAR_COLUMNS_FOR_PRIORITY:
                continue
            star_posts.append(post)

        for post in star_posts:
            post_id = self._make_id(post)
            # _capture_full_article will attempt _extract_topic_id_for_title
            # from the current page. On React-based ZSXQ pages, topic URLs
            # are often not in static HTML — capture falls back to card text.
            post = self._capture_full_article(post, post_id)
            post_date_str = post.get("date", "")
            matched_images = images_by_date.get(post_date_str, [])
            image_texts = self._process_images(matched_images, post_id) if matched_images else []
            self._save_article(post, image_texts=image_texts)
            existing_ids.add(post_id)
            saved.append(post_id)
            logger.info("[DIGESTS] 星大派: %s | %s", post_id, post.get("title", "")[:60])

        return saved

    def _scan_star_section(self, cutoff: datetime, existing_ids: set[str]) -> list[str]:
        """Navigate to group page star columns section and extract star articles.

        Scans the group timeline but only saves star column articles (特刊>锐评>好问题),
        capturing full detail content for each.
        """
        saved: list[str] = []

        self._nav(GROUP_URL, wait=5.0)
        assert self._client is not None  # set by start()
        # Light scroll — star articles are near the top / recent
        for _ in range(5):
            self._client.scroll_by(1500, wait=1.0)

        self._expand_all_details()
        full_text = self._full_text()
        images_by_date = self._images_by_date_from_page()

        posts = self._split_by_author(full_text)
        # Sort star column posts first: 特刊 > 锐评 > 好问题
        star_posts: list[dict] = []
        for part in posts:
            post = self._parse_post(part)
            if not post:
                continue
            post_date = self._parse_date(post.get("date", ""))
            if post_date and post_date < cutoff:
                continue
            post_id = self._make_id(post)
            if post_id in existing_ids:
                continue
            if self._is_platform_chrome(post.get("title", ""), post.get("content", "")):
                continue
            if post.get("column", "普通") in self._STAR_COLUMNS_FOR_PRIORITY:
                star_posts.append(post)

        # Priority sort: 特刊 > 锐评 > 好问题
        _col_rank = {
            "星大派特刊": 0,
            "星大派每日热点": 0,
            "星大派锐评": 1,
            "星大派人脉": 1,
            "凤仙郡小故事": 1,
            "星大派好问题": 2,
            "好问题": 3,
            "合格好问题": 3,
        }
        star_posts.sort(key=lambda p: _col_rank.get(p.get("column", ""), 99))

        for post in star_posts:
            post_id = self._make_id(post)
            # Capture full detail
            post = self._capture_full_article(post, post_id)
            post_date_str = post.get("date", "")
            matched_images = images_by_date.get(post_date_str, [])
            image_texts = self._process_images(matched_images, post_id) if matched_images else []
            self._save_article(post, image_texts=image_texts)
            existing_ids.add(post_id)
            saved.append(post_id)
            logger.info("[STAR] %s: %s", post.get("column", ""), post.get("title", "")[:60])

        return saved

    def _capture_full_article(self, post: dict, post_id: str) -> dict:
        """Navigate to article detail page and capture complete content.

        Strategy by article type:
        - 星大派特刊/锐评: two-step (topic detail → inline article → full content)
        - 星大派好问题/Q&A: topic detail page (question + answer)
        - Talk (普通): topic detail page (full text is on the detail page)

        CRITICAL: _parse_post() does NOT produce topic_id/article_url, so we MUST
        extract them from the current page DOM BEFORE checking has_id.
        """
        from .cdp_article_capture import build_article_detail_url, is_valid_article_url

        article_url = post.get("article_url", "")
        topic_id = post.get("topic_id", "")
        column = post.get("column", "普通")
        is_star = column in self._STAR_COLUMNS_FOR_PRIORITY
        is_qa = post.get("is_qa", False)

        # Step 0: extract topic_id from current page DOM if missing.
        # This MUST happen before the has_id check because _parse_post()
        # never produces topic_id/article_url — all articles come in empty.
        if not topic_id and not article_url:
            extracted = self._extract_topic_id_for_title(post.get("title", ""))
            if extracted:
                topic_id = extracted
                post["topic_id"] = topic_id

        has_id = bool(topic_id or article_url)
        if not has_id:
            # Explicitly mark as degraded: no topic_id means we can't navigate
            # to the detail page for full content. This lets Hermes/repair passes
            # distinguish DOM card captures from full detail captures.
            if not post.get("content_source"):
                post["content_source"] = "dom_card_fallback"
            if not post.get("incomplete"):
                post["incomplete"] = True
            if not post.get("incomplete_reason"):
                post["incomplete_reason"] = "topic_id_not_found"
            return post

        try:
            # Step 1: navigate to topic detail page
            detail_url = build_article_detail_url(
                topic_id=topic_id or "",
                article_url=article_url,
            )
            if not is_valid_article_url(detail_url, topic_id or ""):
                return post

            self._nav(detail_url, wait=3.0)

            # Step 2: for star columns, check for inline article URL
            # (专栏文章 in ZSXQ have full content on articles.zsxq.com, not the topic page)
            inline_url: str | None = None
            if is_star:
                inline_url = self._extract_inline_article_url()
                if inline_url:
                    logger.info("[CAPTURE] 专栏 → 内联文章: %s", inline_url)
                    self._nav(inline_url, wait=3.0)
                    post["article_url"] = inline_url

            # Step 3: scroll and extract full content
            # Star columns with inline articles are long-form → 8 scrolls
            # Star/Q&A on topic detail → 5 scrolls
            # Talk on topic detail → 4 scrolls
            if is_star and inline_url:
                scrolls = 8
            elif is_star or is_qa:
                scrolls = 5
            else:
                scrolls = 4
            assert self._client is not None  # set by start()
            for _ in range(scrolls):
                self._client.scroll_by(4000, wait=1.0)

            full_text = self._full_text()
            clean = self._clean_article_text(full_text, is_qa=is_qa)

            if clean and len(clean) > len(post.get("content", "")):
                post["_orig_content"] = post.get("content", "")
                post["content"] = clean
                post["char_count"] = len(clean)
                post["content_source"] = "inline_article" if inline_url else "topic_detail"
                post["article_url"] = inline_url or detail_url
                post["incomplete"] = len(clean) < 300
                post["incomplete_reason"] = (
                    "" if len(clean) >= 300 else f"content_too_short({len(clean)})"
                )
                post["completeness_version"] = post.get("completeness_version", 1) + 1
                logger.info(
                    "[CAPTURE] %s → %d chars (was %d, via %s)",
                    post_id,
                    len(clean),
                    len(post.get("_orig_content", "")) or len(post.get("content", "")),
                    "inline_article" if inline_url else "topic_detail",
                )
            else:
                logger.info("[CAPTURE] 无更完整内容: %s", post_id)

        except Exception as e:
            logger.warning("[CAPTURE] 详情页抓取失败 %s: %s", post_id, e)

        return post

    def _extract_inline_article_url(self) -> str | None:
        """Extract the inline article URL from the current topic detail page.

        ZSXQ topic detail pages often contain a link to the real article at
        articles.zsxq.com/id_<short_code>.html. That's where the full content lives.
        """
        try:
            raw = self._js(_EXTRACT_INLINE_ARTICLE_SCRIPT)
            if raw and raw.strip() and "articles.zsxq.com" in raw:
                return raw.strip().strip('"').strip("'")
        except Exception:
            pass
        return None

    def _clean_article_text(self, raw: str, is_qa: bool = False) -> str:
        """Clean ZSXQ page text: remove navigation chrome, footer, comments.

        For Q&A articles, preserves question/answer structure.
        """
        lines = raw.split("\n")
        chrome = {
            "笔记",
            "星球管理后台",
            "榜单",
            "详情",
            "知识星球",
            "扫码加入星球",
            "查看更多优质内容",
            "返回 大锅饭与小伙伴的进步空间",
        }
        stop_markers = ("ljq_driver:",)
        result: list[str] = []
        in_body = False
        for line in lines:
            s = line.strip()
            if not s:
                if result:
                    result.append("")  # preserve paragraph breaks
                continue
            if s in chrome:
                continue
            # Author/date line signals body start
            if s.startswith("三线文案大锅饭") or s.startswith("2026年") or s.startswith("2026-"):
                in_body = True
                continue
            if not in_body:
                continue
            # Stop at footer/comments
            if any(m in s for m in stop_markers):
                break
            if "人觉得很赞" in s:
                break
            # For Q&A, preserve 提问/问题 markers
            if is_qa and ("提问" in s or "问题" in s):
                result.append(s)
                continue
            # Skip comment author lines
            if s.startswith("锅儿：") or s.startswith("评论"):
                break
            result.append(s)

        # Remove trailing empty lines
        while result and not result[-1]:
            result.pop()
        return "\n".join(result)

    def _extract_topic_id_for_title(self, title_hint: str) -> str | None:
        """Extract topic_id from the current page DOM for a given title.

        Uses JS to find topic links in the page and match by title text.
        Returns the topic_id (numeric string) or None.
        """
        try:
            raw = self._js(_EXTRACT_TOPIC_IDS_SCRIPT)
            if not raw:
                return None
            topics = json.loads(raw)
            for t in topics:
                tid = str(t.get("topic_id", ""))
                title = str(t.get("title", ""))
                if not tid:
                    continue
                # Match by title substring (fuzzy)
                if title_hint and (title_hint[:20] in title or title[:20] in title_hint):
                    return tid
                # Fallback: any star-like title match
                if ("星大派" in title or "特刊" in title or "锐评" in title) and (
                    title_hint[:10] in title or title[:10] in title_hint
                ):
                    return tid
            # If no title match, return the first topic_id found (best effort)
            if topics:
                return str(topics[0].get("topic_id", ""))
        except Exception:
            pass
        return None

    def _scroll_until_cutoff(
        self, cutoff: datetime, max_scrolls: int = 40
    ) -> tuple[int, bool, bool]:
        """固定次数滚动加载 ZSXQ 懒加载内容，遇到早于 cutoff 的日期后停止。

        Returns:
            (scroll_count, stopped_by_boundary, reached_page_end)
        """
        stopped_by_boundary = False
        reached_page_end = False
        last_scroll_height: float | None = None
        stable_bottom_observations = 0

        assert self._client is not None  # set by start()
        for i in range(max_scrolls):
            self._client.scroll_by(4000, wait=1.5)

            # 每 3 次滚动检查一次可见日期
            if i > 0 and i % 3 == 0:
                visible_dates = self._extract_visible_dates()
                if visible_dates:
                    oldest = min(visible_dates)
                    if oldest < cutoff:
                        stopped_by_boundary = True
                        logger.info(
                            "[SCROLL] 停止: 可见日期 %s 早于 cutoff %s (%d scrolls)",
                            oldest.strftime("%Y-%m-%d %H:%M"),
                            cutoff.strftime("%Y-%m-%d"),
                            i + 1,
                        )
                        return (i + 1, stopped_by_boundary, reached_page_end)

            metrics = self._scroll_metrics()
            if metrics is None:
                stable_bottom_observations = 0
                last_scroll_height = None
            else:
                scroll_top, client_height, scroll_height = metrics
                at_bottom = scroll_top + client_height >= scroll_height - 2
                if at_bottom:
                    stable_bottom_observations = (
                        stable_bottom_observations + 1 if scroll_height == last_scroll_height else 1
                    )
                else:
                    stable_bottom_observations = 0
                last_scroll_height = scroll_height

                if stable_bottom_observations >= 3:
                    loader_visible = self._timeline_loader_visible()
                    if loader_visible is False and self._extract_visible_dates():
                        reached_page_end = True
                        logger.info(
                            "[SCROLL] 停止: 时间线页面底部连续稳定 3 次且无加载器 "
                            "(%d scrolls, height=%s)",
                            i + 1,
                            scroll_height,
                        )
                        return (i + 1, stopped_by_boundary, reached_page_end)
                    if loader_visible is True:
                        logger.info(
                            "[SCROLL] 底部加载器仍可见，不能作为页面结束证据 "
                            "(%d scrolls, height=%s)",
                            i + 1,
                            scroll_height,
                        )
                    else:
                        logger.info(
                            "[SCROLL] 加载器状态不可证明，不能作为页面结束证据 "
                            "(%d scrolls, height=%s)",
                            i + 1,
                            scroll_height,
                        )
                    stable_bottom_observations = 0

            if i % 5 == 0 or i < 3:
                logger.info("[SCROLL] %d/%d", i + 1, max_scrolls)

        return (max_scrolls, stopped_by_boundary, reached_page_end)

    def _timeline_loader_visible(self) -> bool | None:
        """Return exact visible-loader state; malformed bridge data proves nothing."""
        try:
            raw = self._js(_TIMELINE_LOADER_STATE_SCRIPT)
            payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError):
            return None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"visible"}
            or type(payload.get("visible")) is not bool
        ):
            return None
        return bool(payload["visible"])

    def _scroll_metrics(self) -> tuple[float, float, float] | None:
        """Return validated viewport metrics; malformed bridge data is no evidence."""
        try:
            raw = self._js(_SCROLL_METRICS_SCRIPT)
            payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "scrollTop",
            "clientHeight",
            "scrollHeight",
        }:
            return None
        scroll_top_raw = payload.get("scrollTop")
        client_height_raw = payload.get("clientHeight")
        scroll_height_raw = payload.get("scrollHeight")
        if (
            not isinstance(scroll_top_raw, (int, float))
            or isinstance(scroll_top_raw, bool)
            or not isinstance(client_height_raw, (int, float))
            or isinstance(client_height_raw, bool)
            or not isinstance(scroll_height_raw, (int, float))
            or isinstance(scroll_height_raw, bool)
        ):
            return None
        scroll_top = float(scroll_top_raw)
        client_height = float(client_height_raw)
        scroll_height = float(scroll_height_raw)
        if not all(math.isfinite(value) for value in (scroll_top, client_height, scroll_height)):
            return None
        max_scroll_top = scroll_height - client_height
        if (
            scroll_top < 0
            or client_height <= 0
            or scroll_height < client_height
            or scroll_top > max_scroll_top + 2
        ):
            return None
        return scroll_top, client_height, scroll_height

    def _extract_visible_dates(self) -> list[datetime]:
        """Collect timestamps from visible DOM timeline items, never from body text."""
        try:
            raw = self._js(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)
        except Exception:
            return []
        return _decode_timeline_timestamp_evidence(raw)

    def _expand_all_details(self):
        """点击页面上所有「查看详情」按钮，展开被截断的短文内容"""
        n = 0
        for _ in range(20):
            result = self._js(_EXPAND_DETAILS_SCRIPT)
            if "done" in str(result):
                break
            n += 1
            time.sleep(0.5)
        if n > 0:
            logger.info("[EXPAND] 点击了 %d 个「查看详情」", n)

    def _click_tab(self, tab_text: str) -> bool:
        """点击指定文本的 tab，返回是否成功"""
        # 优先尝试按文本精确匹配 + 宽松 children 限制
        r = self._js(f"""
        (function() {{
            for (const el of document.querySelectorAll('span, a, div, li')) {{
                const txt = (el.textContent || '').trim();
                if (txt === '{tab_text}' && el.children.length <= 3) {{
                    el.click(); return 'ok';
                }}
            }}
            // fallback: 包含匹配（tab_text 可能被包裹在子元素中）
            for (const el of document.querySelectorAll('span, a, div, li')) {{
                const txt = (el.textContent || '').trim();
                if (txt.includes('{tab_text}') && el.children.length <= 2) {{
                    el.click(); return 'ok_fuzzy';
                }}
            }}
            return 'not found';
        }})()
        """)
        return "'ok'" in r or '"ok"' in r

    def _full_text(self) -> str:
        return self._js(_FULL_TEXT_SCRIPT)

    def _images_by_date_from_page(self) -> dict[str, list[dict]]:
        """提取有日期锚点的文章卡片图片，按日期分组。"""
        raw = self._js(_IMAGES_BY_DATE_SCRIPT)
        try:
            images = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            images = []
        grouped: dict[str, list[dict]] = {}
        for img in images:
            date = img.get("date", "")
            if date:
                grouped.setdefault(date, []).append(img)
        return grouped

    def _process_images(self, images: list[dict], post_id: str) -> list[dict]:
        """下载图片 → LLM vision (mimo → GLM-4.6V-Flash → SiliconFlow → OCR fallback)，返回结构化 provenance。

        返回 [{filename, path, ocr_text, llm_desc, vision_provider, vision_model,
                fallback_chain, error}]。
        """
        if not images:
            return []
        try:
            import io

            import requests
            from PIL import Image
            from pytesseract import image_to_string

            from .downloader import describe_image_with_provenance
        except ImportError:
            logger.warning("[IMG] 缺少依赖 (Pillow/pytesseract)，跳过图片处理")
            return []

        cookies_raw = self._get_cookies_from_browser()
        session = requests.Session()
        for k, v in cookies_raw.items():
            session.cookies.set(k, v)

        # 有界图片预算：图片失败/超时不得拖垮文字采集与 G 发布——预算耗尽即跳过
        # 剩余图片（降级该文章图片，不改 run 终态）。
        budget_deadline = None
        if self._deadline_at is not None:
            budget_deadline = min(
                datetime.now(self._deadline_at.tzinfo)
                + timedelta(seconds=_IMAGE_PROCESS_BUDGET_SECONDS),
                self._deadline_at - timedelta(seconds=_IMAGE_DEADLINE_RESERVE_SECONDS),
            )

        results: list[dict] = []
        for i, img in enumerate(images):
            if (
                budget_deadline is not None
                and datetime.now(budget_deadline.tzinfo) >= budget_deadline
            ):
                logger.warning("[IMG] 图片处理预算耗尽，跳过剩余 %d 张", len(images) - i)
                break
            # F-03：单次下载超时按剩余预算封顶——图片请求不可能越过 run deadline
            # 拖垮文字保存与 G 发布。
            request_timeout = 30.0
            if budget_deadline is not None:
                remaining = (budget_deadline - datetime.now(budget_deadline.tzinfo)).total_seconds()
                request_timeout = max(1.0, min(30.0, remaining))
            src = img.get("src", "")
            if not src:
                continue
            try:
                resp = session.get(
                    src,
                    timeout=request_timeout,
                    headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                        "Referer": "https://wx.zsxq.com/",
                    },
                )
                if resp.status_code != 200:
                    continue
                ext = "jpg" if "jpeg" in resp.headers.get("Content-Type", "") else "png"
                filename = f"{i:03d}.{ext}"
                local_path = f"images/{post_id}/{filename}"
                abs_path = self._kb_root / local_path
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_bytes(resp.content)

                # F-03：OCR/vision 前检查预算储备——剩余 < 储备（vision 最坏 ~90s）
                # 则跳过剩余图片的 OCR/vision，图片处理不可能越过 run deadline。
                if budget_deadline is not None:
                    remaining_for_vision = (
                        budget_deadline - datetime.now(budget_deadline.tzinfo)
                    ).total_seconds()
                    if remaining_for_vision < _IMAGE_VISION_RESERVE_SECONDS:
                        logger.warning(
                            "[IMG] OCR/vision 预算储备不足（剩余 %.0fs < %.0fs），"
                            "跳过剩余 %d 张图片的 OCR/vision",
                            remaining_for_vision,
                            _IMAGE_VISION_RESERVE_SECONDS,
                            len(images) - i,
                        )
                        break

                # OCR fallback (always run for text extraction)
                ocr_text = ""
                try:
                    img_obj = Image.open(io.BytesIO(resp.content))
                    raw = image_to_string(img_obj, lang="chi_sim+eng", config="--psm 6").strip()
                    chinese_chars = sum(1 for c in raw if "一" <= c <= "鿿")
                    if chinese_chars >= 10 and len(raw) >= 50:
                        ocr_text = raw
                except Exception:
                    pass

                # Vision analysis with structured provenance (mimo → GLM-4.6V-Flash → SiliconFlow → OCR)
                provenance = describe_image_with_provenance(str(abs_path))
                llm_desc = provenance.llm_desc

                results.append(
                    {
                        "filename": filename,
                        "path": local_path,
                        "ocr_text": ocr_text,
                        "llm_desc": llm_desc,
                        "vision_provider": provenance.vision_provider,
                        "vision_model": provenance.vision_model,
                        "fallback_chain": provenance.fallback_chain,
                        "error": provenance.error,
                    }
                )
                time.sleep(0.5)
            except Exception as e:
                logger.warning("[IMG] %s: %s", src, e)

        return results

    def _get_cookies_from_browser(self) -> dict[str, str]:
        """从 CDP 浏览器提取 cookies 字典"""
        raw = self._js("JSON.stringify(document.cookie)")
        cookies: dict[str, str] = {}
        if raw and raw.strip():
            try:
                # document.cookie returns a string like "key1=val1; key2=val2"
                for pair in raw.split(";"):
                    pair = pair.strip()
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        cookies[k.strip()] = v.strip()
            except Exception:
                pass
        return cookies

    # ── 批量优先加载 ────────────────────────────────────────

    def _load_group_timeline_batch_first(self, cutoff: datetime) -> GroupTimelineLoadResult:
        """Load the group timeline with batch-first execution and single-step fallback."""
        batch_result = None
        warnings: list[str] = []

        if self._client is not None and hasattr(self._client, "batch_execute"):
            batch_result = self._client.batch_execute(
                [
                    {
                        "action": "navigate",
                        "name": "navigate_group",
                        "url": _cache_bust_url(GROUP_URL),
                        "wait": 5.0,
                    },
                    {"action": "wait", "name": "settle_group", "wait": 2.0, "required": False},
                    {
                        "action": "scroll_by",
                        "name": "initial_scroll",
                        "px": 4000,
                        "wait": 1.5,
                        "repeat": 2,
                        "required": False,
                    },
                    {
                        "action": "js",
                        "name": "timeline_dates",
                        "script": _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
                    },
                    {
                        "action": "expand_details",
                        "name": "expand_details",
                        "required": False,
                    },
                    {"action": "full_text", "name": "body"},
                ]
            )
            if batch_result.status in ("ok", "partial"):
                full_text = batch_result.result_by_name("body") or ""
                timeline_dates = _decode_timeline_timestamp_evidence(
                    batch_result.result_by_name("timeline_dates") or ""
                )
                if full_text:
                    if batch_result.status == "partial" and batch_result.failed_step:
                        warnings.append(self._format_batch_warning(batch_result))
                    trace = getattr(batch_result, "cdp_trace", {}) or {}
                    initial_tab = trace.get("initial_tab_id")
                    final_tab = trace.get("final_tab_id")
                    tab_changed = (
                        initial_tab is not None
                        and final_tab is not None
                        and str(initial_tab) != str(final_tab)
                    )
                    if tab_changed:
                        warnings.append(
                            f"batch_group_timeline_tab_changed:{initial_tab}->{final_tab}:reload_required"
                        )
                    else:
                        sufficient, insufficiency_reason = self._is_batch_text_sufficient(
                            str(full_text), cutoff, timeline_dates=timeline_dates
                        )
                        if sufficient:
                            return GroupTimelineLoadResult(
                                full_text=str(full_text),
                                used_batch=True,
                                stopped_by_window_boundary=True,
                                timeline_dates=timeline_dates,
                                warnings=warnings,
                            )
                        warnings.append(
                            f"batch_group_timeline_insufficient:{insufficiency_reason}:step=body"
                        )
            if batch_result is not None and not warnings:
                warnings.append(self._format_batch_warning(batch_result))

        self._nav(_cache_bust_url(GROUP_URL), wait=5.0)
        time.sleep(2.0)
        scrolls, stopped_by_boundary, reached_end = self._scroll_until_cutoff(cutoff)
        self._expand_all_details()
        full_text = self._full_text()
        return GroupTimelineLoadResult(
            full_text=full_text,
            used_batch=False,
            stopped_by_window_boundary=stopped_by_boundary,
            reached_page_end=reached_end,
            timeline_dates=self._extract_visible_dates(),
            warnings=warnings,
        )

    @staticmethod
    def _is_batch_text_sufficient(
        text: str,
        cutoff: datetime,
        *,
        timeline_dates: list[datetime] | None = None,
    ) -> tuple[bool, str]:
        """Check that batch text has enough content to skip single-step fallback.

        Returns (is_sufficient, reason). A sufficient text must:
        1. Contain the group timeline author marker.
        2. Carry at least one structured DOM timeline date.
        3. Have its oldest date earlier than cutoff (proves we scrolled far enough).
        4. NOT contain "查看详情" (means details were never expanded).
        """
        # 1. No "查看详情" — else we missed the expand step
        if "查看详情" in text:
            return False, "unexpanded_details"

        # A date-rich digests/archive page can otherwise pass the window check.
        if AUTHOR_NAME not in text:
            return False, "missing_group_author"

        dates = timeline_dates or []
        if not dates:
            return False, "no_structured_timeline_dates"

        # 3. Oldest date must be before cutoff
        oldest = min(dates)
        if oldest >= cutoff:
            return False, f"oldest_date_{oldest.strftime('%Y%m%d')}_not_before_cutoff"

        return True, ""

    @staticmethod
    def _format_batch_warning(batch_result) -> str:
        failed = getattr(batch_result, "failed_step", None) or {}
        failure_kind = failed.get("failure_kind") or "unknown"
        step_name = failed.get("name") or failed.get("action") or "unknown"
        return f"batch_group_timeline_failed:{failure_kind}:step={step_name}"

    def _fetch_topic_cursor_page(self, end_time: str) -> _TopicCursorPage | None:
        """Fetch one fixed native-topic page through the user's signed-in ZSXQ tab.

        The browser script projects only the fields owned by this ingestion seam.
        Raw API errors and unrelated account data never cross into FIN logs/state.
        """
        query: dict[str, str] = {"scope": "all", "count": "30"}
        if end_time:
            query["end_time"] = end_time
        url = "https://api.zsxq.com/v2/groups/15522441811252/topics?" + urlencode(query)
        encoded_url = json.dumps(url)
        encoded_author_name = json.dumps(AUTHOR_NAME, ensure_ascii=False)
        script = f"""
        return await (async function finTopicCursorPage() {{
            let response;
            let body = null;
            try {{
                response = await fetch({encoded_url}, {{credentials: 'include'}});
                body = await response.json();
            }} catch (_error) {{
                return JSON.stringify({{
                    schema_version: 4,
                    http_status: response && Number.isInteger(response.status)
                        ? response.status : 599,
                    api_succeeded: false,
                    api_code: null,
                    topics: []
                }});
            }}
            const nativeTopics = body
                && body.resp_data
                && Array.isArray(body.resp_data.topics)
                ? body.resp_data.topics.slice(0, 31)
                : [];
            const topics = nativeTopics.map((topic) => {{
                const type = String(topic && topic.type || 'talk');
                let owner = null;
                let answerState = 'invalid';
                let rawContent = '';
                if (type === 'q&a') {{
                    const hasMultipleAnswers = Boolean(
                        topic
                        && Array.isArray(topic.answers)
                        && topic.answers.length
                    );
                    const answer = topic && topic.answer;
                    if (hasMultipleAnswers || Array.isArray(answer)) {{
                        answerState = 'invalid';
                    }} else if (answer == null) {{
                        answerState = 'unanswered';
                    }} else if (
                        typeof answer === 'object'
                        && answer
                        && typeof answer.owner === 'object'
                        && answer.owner
                        && typeof answer.text === 'string'
                    ) {{
                        answerState = 'answered';
                        owner = answer.owner;
                        // Member questions are context, not teacher cognition.
                        // Only the singular official answer crosses this seam.
                        rawContent = answer.text;
                    }}
                }} else if (type === 'talk') {{
                    const talk = topic && topic.talk;
                    if (
                        typeof talk === 'object'
                        && talk
                        && typeof talk.owner === 'object'
                        && talk.owner
                        && typeof talk.text === 'string'
                    ) {{
                        answerState = 'not_applicable';
                        owner = talk.owner;
                        rawContent = talk.text;
                    }}
                }}
                const ownerUserId = owner && (
                    typeof owner.user_id === 'string'
                    || Number.isSafeInteger(owner.user_id)
                ) ? String(owner.user_id) : '';
                const ownerIdentityValid = /^[0-9]{{1,32}}$/.test(ownerUserId)
                    && /[1-9]/.test(ownerUserId)
                    && typeof owner.name === 'string'
                    && owner.name === owner.name.trim()
                    && owner.name.length >= 1
                    && owner.name.length <= 200;
                const isTeacher = ownerIdentityValid
                    && owner.name === {encoded_author_name}
                    && answerState !== 'unanswered'
                    && answerState !== 'invalid';
                const sourceClass = answerState === 'unanswered'
                    ? 'coverage_only'
                    : !ownerIdentityValid || answerState === 'invalid'
                    ? 'invalid'
                    : isTeacher
                    ? 'teacher'
                    : 'coverage_only';
                return {{
                    // topic_id is an unsafe JSON number; only a string topic_uid is exact.
                    topic_id: topic && typeof topic.topic_uid === 'string'
                        ? topic.topic_uid
                        : '',
                    legacy_topic_id: topic
                        && typeof topic.topic_id === 'number'
                        && Number.isFinite(topic.topic_id)
                        && Number.isInteger(topic.topic_id)
                        ? String(topic.topic_id)
                        : '',
                    create_time: String(topic && topic.create_time || ''),
                    title: isTeacher && type === 'talk'
                        ? String(topic && topic.title || '').slice(0, 500)
                        : '',
                    topic_type: type,
                    source_class: sourceClass,
                    answer_state: answerState,
                    content_text: isTeacher ? rawContent.slice(0, 200001) : ''
                }};
            }});
            return JSON.stringify({{
                schema_version: 4,
                http_status: Number.isInteger(response.status) ? response.status : 599,
                api_succeeded: Boolean(body && body.succeeded === true),
                api_code: body && Number.isInteger(body.code) ? body.code : null,
                topics
            }});
        }})()
        """
        return _decode_topic_cursor_page(self._js(script))

    def _collect_topic_cursor_coverage(
        self,
        cutoff: datetime,
        *,
        max_pages: int = 8,
    ) -> _TopicCursorCoverage:
        """Collect a bounded three-day native topic window with a progressing cursor."""
        topics: list[_TopicCursorItem] = []
        seen_ids: set[str] = set()
        seen_legacy_ids: set[str] = set()
        cursor = ""
        previous_oldest: datetime | None = None
        previous_boundary: _TopicCursorItem | None = None
        oldest_observed: datetime | None = None
        observed_topic_count = 0

        for _page_number in range(max_pages):
            self._surface_checkpoint()
            if cursor:
                self._wait_between_topic_cursor_requests()
            page = self._fetch_topic_cursor_page(cursor)
            if (
                page is not None
                and page.http_status == 200
                and page.api_succeeded is False
                and page.api_code == _TOPIC_CURSOR_RATE_LIMIT_CODE
            ):
                self._wait_between_topic_cursor_requests()
                page = self._fetch_topic_cursor_page(cursor)
            if page is None:
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="page_invalid",
                )
            if page.http_status != 200 or not page.api_succeeded:
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="api_rejected",
                )
            if not page.topics:
                if observed_topic_count == 0:
                    return _TopicCursorCoverage(failure_code="empty_first_page")
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    covered=True,
                    boundary_kind="page_end",
                    oldest_observed_at=oldest_observed,
                )

            raw_page_topics = page.topics
            page_dates = [topic.created_at for topic in raw_page_topics]
            if any(newer < older for newer, older in zip(page_dates, page_dates[1:], strict=False)):
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="page_not_descending",
                )

            page_topics = raw_page_topics
            if previous_boundary is not None and raw_page_topics[0].topic_id == (
                previous_boundary.topic_id
            ):
                if raw_page_topics[0] != previous_boundary:
                    return _TopicCursorCoverage(
                        topics=tuple(topics),
                        oldest_observed_at=oldest_observed,
                        failure_code="duplicate_topic_id",
                    )
                page_topics = raw_page_topics[1:]

            if not page_topics:
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="cursor_not_advancing",
                )

            page_oldest = page_topics[-1].created_at
            if previous_oldest is not None and page_oldest >= previous_oldest:
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="cursor_not_advancing",
                )

            if any(topic.topic_id in seen_ids for topic in page_topics):
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="duplicate_topic_id",
                )
            if any(topic.legacy_topic_id in seen_legacy_ids for topic in page_topics):
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    oldest_observed_at=oldest_observed,
                    failure_code="duplicate_legacy_topic_id",
                )

            oldest_observed = (
                page_oldest if oldest_observed is None else min(oldest_observed, page_oldest)
            )
            observed_topic_count += len(page_topics)
            seen_ids.update(topic.topic_id for topic in page_topics)
            seen_legacy_ids.update(topic.legacy_topic_id for topic in page_topics)
            for topic in page_topics:
                if topic.created_at < cutoff:
                    return _TopicCursorCoverage(
                        topics=tuple(topics),
                        covered=True,
                        boundary_kind="cutoff",
                        oldest_observed_at=topic.created_at,
                    )
                if topic.is_teacher_source:
                    topics.append(topic)

            if len(raw_page_topics) < 30:
                return _TopicCursorCoverage(
                    topics=tuple(topics),
                    covered=True,
                    boundary_kind="page_end",
                    oldest_observed_at=oldest_observed,
                )

            previous_oldest = page_oldest
            previous_boundary = raw_page_topics[-1]
            cursor = previous_boundary.create_time

        return _TopicCursorCoverage(
            topics=tuple(topics),
            oldest_observed_at=oldest_observed,
            failure_code="page_budget_exhausted",
        )

    def _post_from_topic_cursor_item(self, topic: _TopicCursorItem) -> dict | None:
        """Project one native teacher-source topic into the existing article parser."""
        if not topic.is_teacher_source:
            return None
        content = re.sub(
            r'<e\s+[^>]*?title="([^"]*)"[^>]*?/>',
            lambda match: unquote(match.group(1)),
            topic.content_text,
        )
        content = re.sub(r"<[^>]+>", "", content).strip()
        content = _strip_disclaimer_line(content)
        title = topic.title.strip()
        if not title:
            title = next(
                (
                    line.strip()[:150]
                    for line in content.splitlines()
                    if line.strip() and not line.strip().startswith("能量评分")
                ),
                "",
            )
        if not title:
            return None
        raw = f"{topic.created_at.strftime('%Y-%m-%d %H:%M')}\n{title}\n{content}"
        post = self._parse_post(raw)
        if post is None:
            return None
        post["topic_id"] = topic.topic_id
        post["legacy_topic_id"] = topic.legacy_topic_id
        post["type"] = topic.topic_type
        post["is_qa"] = topic.topic_type == "q&a"
        post["content_source"] = "zsxq_topic_cursor"
        post["source_classification"] = "teacher_original"
        post["answer_state"] = topic.answer_state
        # 跳转链接文章：feed/detail 双侧都截断时正文以 …/... 结尾。诚实标
        # incomplete，等更完整 capture 升级（_should_recapture 文件尾判据）。
        if _TRUNCATED_TAIL_RE.search(content):
            post["incomplete"] = True
            post["incomplete_reason"] = "cursor_content_truncated"
        else:
            post["incomplete"] = False
            post["incomplete_reason"] = ""
        return post

    # ── 增量抓取 ────────────────────────────────────────────

    def run_priority_scan(self) -> ScrapeResult:
        """Lightweight priority scan: star_columns + digests only, no full group scan.

        For 14:00-23:55 every-5-minute polling. Discovers star articles from the
        star columns section and digests page, captures full detail, saves, and
        writes T0 priority events + analysis jobs.

        Does NOT run the full group timeline scan — that's for the main
        canonical scheduled-run sync path.
        """
        self._surface_checkpoint()
        result = ScrapeResult()
        now = datetime.now(TZ)
        cutoff = now - timedelta(days=INCREMENTAL_WINDOW_DAYS)
        result.cutoff = cutoff.strftime("%Y-%m-%d %H:%M")
        self._sources_scanned = []

        logger.info(
            "[PRIORITY-SCAN] 轻量星大派巡检 (窗口=%d天, cutoff=%s)",
            INCREMENTAL_WINDOW_DAYS,
            result.cutoff,
        )

        self._load_index()
        existing_ids = set(self._index.keys())

        all_saved: list[str] = []

        # Star columns section
        try:
            star_ids = self._scan_star_section(cutoff, existing_ids)
            all_saved.extend(star_ids)
            self._sources_scanned.append("star_columns")
            logger.info("[PRIORITY-SCAN] star_columns: %d", len(star_ids))
        except Exception as e:
            logger.warning("[PRIORITY-SCAN] star_columns failed: %s", e)
            result.warnings.append("priority_surface_failed:star_columns")

        # Digests page — the cooperative checkpoint sits OUTSIDE the best-effort
        # handler so an expired-deadline/checkpoint exception propagates instead of
        # being swallowed as a "digests failed" warning and a falsely-successful scan.
        self._surface_checkpoint()
        try:
            digest_ids = self._scan_digests(cutoff, existing_ids)
            all_saved.extend(digest_ids)
            self._sources_scanned.append("digests")
            logger.info("[PRIORITY-SCAN] digests: %d", len(digest_ids))
        except Exception as e:
            logger.warning("[PRIORITY-SCAN] digests failed: %s", e)
            result.warnings.append("priority_surface_failed:digests")

        # Uncaught boundary AFTER the last priority surface: a deadline/checkpoint
        # error swallowed inside digests' best-effort handler must re-propagate here
        # instead of falling through to scrape_completed=True (a false success).
        self._surface_checkpoint()

        result.new_count = len(all_saved)
        result.sources_scanned = list(self._sources_scanned)
        result.scrape_completed = True

        # Also replay existing event→job pairs when this scan discovers nothing.
        try:
            pe_count = self._write_priority_events_for_new_articles(result, all_saved)
            result.priority_events_created = pe_count
        except Exception as e:
            logger.warning("[PRIORITY-SCAN] events failed: %s", e)
            result.warnings.append(f"priority_events_failed: {e}")

        # Deep read artifacts for new star articles
        if all_saved:
            self._surface_checkpoint()
            try:
                da_count = self._ensure_deep_read_artifacts_for_new(result, all_saved)
                result.deep_read_artifacts_created = da_count
            except Exception as e:
                logger.warning("[PRIORITY-SCAN] deep-read artifacts failed: %s", e)
                result.warnings.append(f"deep_read_artifacts_failed: {e}")
            self._surface_checkpoint()

        # Macro index（sidecar，best-effort）：每日热点在 watch 面即打标。
        try:
            from fin_analyse.cognition.macro_index import update_macro_index

            macro_report = update_macro_index(self._kb_root, saved_ids=all_saved)
            if macro_report.incomplete:
                logger.warning(
                    "[MACRO-INDEX] watch 面部分完成: tagged=%d removed=%d warnings=%s",
                    macro_report.tagged,
                    macro_report.removed,
                    ",".join(macro_report.warnings[:3]),
                )
                result.warnings.append(
                    "macro_index_incomplete:"
                    f"lock_busy={macro_report.lock_busy},warnings={len(macro_report.warnings)}"
                )
        except Exception as e:  # noqa: BLE001 — 钩子绝不阻塞 ingest
            logger.warning("[MACRO-INDEX] watch 面打标失败(不阻塞): %s", e)
            result.warnings.append(f"macro_index_failed: {e}")

        logger.info(
            "[PRIORITY-SCAN] 完成: new=%d priority=%d deep_read=%d sources=%s",
            result.new_count,
            result.priority_events_created,
            result.deep_read_artifacts_created,
            result.sources_scanned,
        )
        return result

    def run_incremental(self) -> int:
        """增量抓取：fresh navigate → 窗口感知滚动 → DOM提取 → 去重保存。

        返回新增文章数。新代码建议使用 run_incremental_with_result()。
        """
        result = self.run_incremental_with_result()
        return result.new_count

    def run_incremental_with_result(self) -> ScrapeResult:
        """增量抓取并返回结构化 ScrapeResult（含窗口指标和诊断）。

        流程:
        1. fresh navigate 到 GROUP_URL（不复用旧 DOM）
        2. 窗口感知滚动：_scroll_until_cutoff(cutoff)
        3. 展开详情 → DOM 提取 → 切分解析 → 去重保存
        4. 保存阶段 post_date < cutoff 作为双保险
        """
        self._surface_checkpoint()
        result = ScrapeResult()
        cutoff = self._incremental_cutoff or (
            datetime.now(TZ) - timedelta(days=INCREMENTAL_WINDOW_DAYS)
        )
        result.cutoff = cutoff.strftime("%Y-%m-%d %H:%M")

        logger.info(
            "[CDP-SCRAPER] 增量抓取 (窗口=%d天, cutoff=%s, priority_only=%s)",
            INCREMENTAL_WINDOW_DAYS,
            result.cutoff,
            self._priority_only,
        )

        self._load_index()
        existing_ids = set(self._index.keys())
        logger.info("[INDEX] 已有 %d 篇文章", len(existing_ids))
        self._sources_scanned = []

        # ── 1-4. group timeline load: batch-first with single-step fallback ──
        self._surface_checkpoint()
        group_load = self._load_group_timeline_batch_first(cutoff)
        result.warnings.extend(group_load.warnings)
        result.stopped_by_window_boundary = group_load.stopped_by_window_boundary
        result.reached_page_end = group_load.reached_page_end
        full_text = group_load.full_text
        result.dom_text_chars = len(full_text)
        logger.info("[TEXT] 页面文本: %d 字符", result.dom_text_chars)

        # ── 5. Parse and prove timeline coverage before any mutable KB write ──
        posts = self._split_by_author(full_text)
        result.posts_seen = len(posts)
        logger.info("[SPLIT] 按作者名切分得到 %d 篇", len(posts))
        parsed_posts = [post for part in posts if (post := self._parse_post(part)) is not None]
        timeline_dates = group_load.timeline_dates
        if timeline_dates:
            result.oldest_seen_date = min(timeline_dates).strftime("%Y-%m-%d %H:%M")

        self._sources_scanned.append("group")

        oldest_timeline_date = min(timeline_dates) if timeline_dates else None
        cutoff_covered = oldest_timeline_date is not None and oldest_timeline_date < cutoff
        page_end_covered = result.reached_page_end and bool(timeline_dates)
        if not cutoff_covered and not page_end_covered:
            cursor_coverage = (
                self._collect_topic_cursor_coverage(cutoff)
                if self._client is not None
                else _TopicCursorCoverage(failure_code="client_unavailable")
            )
            if cursor_coverage.covered and cursor_coverage.oldest_observed_at is not None:
                cursor_posts: list[dict] = []
                for topic in cursor_coverage.topics:
                    cursor_post = self._post_from_topic_cursor_item(topic)
                    if cursor_post is not None:
                        cursor_posts.append(cursor_post)
                # A proven native cursor is authoritative for this recovery
                # window. Do not mix anonymous DOM cards back in: their edited
                # titles cannot be reliably rebound to topic ids and would
                # recreate duplicates on later runs.
                parsed_posts = cursor_posts
                result.posts_seen = len(parsed_posts)
                oldest_timeline_date = cursor_coverage.oldest_observed_at
                result.oldest_seen_date = oldest_timeline_date.strftime("%Y-%m-%d %H:%M")
                result.stopped_by_window_boundary = cursor_coverage.boundary_kind == "cutoff"
                result.reached_page_end = cursor_coverage.boundary_kind == "page_end"
                cutoff_covered = oldest_timeline_date < cutoff
                page_end_covered = result.reached_page_end
                self._sources_scanned.append("group_topic_cursor")
                result.warnings.append(
                    f"group_timeline_cursor_recovered:{cursor_coverage.boundary_kind}"
                )
            else:
                result.warnings.append(
                    "group_timeline_cursor_failed:"
                    f"{cursor_coverage.failure_code or 'coverage_unproven'}"
                )
        result.sources_scanned = list(self._sources_scanned)

        if _is_group_timeline_content_insufficient(result):
            result.oldest_seen_date = ""
            result.warnings.append("boundary_status=unknown: 无法解析文章发布时间")
            if _is_login_surface_text(full_text):
                result.failure_kind = "login_required"
            else:
                result.failure_kind = "content_insufficient"
            result.warnings.append(
                "group_timeline_content_insufficient:"
                f"chars={result.dom_text_chars}:posts={result.posts_seen}"
            )
            return result

        if not cutoff_covered and not page_end_covered:
            result.failure_kind = "window_coverage_incomplete"
            result.warnings.append(
                "group_timeline_window_coverage_incomplete:"
                f"oldest_post={result.oldest_seen_date or 'unknown'}:"
                f"cutoff={result.cutoff}:page_end={result.reached_page_end}"
            )
            return result

        # ── 6. Images are read and written only after coverage is proven ──
        images_by_date = self._images_by_date_from_page()
        result.images_days = len(images_by_date)
        logger.info("[IMAGES] 提取图片: %d 天", result.images_days)

        # ── 7. Filter → deduplicate → save ──
        new_count = 0
        saved_ids: list[str] = []

        for post in parsed_posts:
            # 时间窗口过滤
            post_date = self._parse_date(post.get("date", ""))
            if post_date and post_date < cutoff:
                continue

            topic_id = str(post.get("topic_id", ""))
            legacy_topic_id = str(post.get("legacy_topic_id", ""))
            post_id = self._make_id(post)
            existing_topic_entry = None
            if topic_id.isascii() and topic_id.isdecimal():
                existing_topic_entry = self._find_by_topic_id(topic_id)
            if (
                existing_topic_entry is None
                and legacy_topic_id.isascii()
                and legacy_topic_id.isdecimal()
            ):
                existing_topic_entry = self._find_by_topic_id(legacy_topic_id)
            if existing_topic_entry is not None:
                # A native topic identity survives title/content edits.  The
                # legacy date+title hash remains only for anonymous DOM cards;
                # it must never create a second article for an indexed topic.
                # 例外：截断/不完整存稿拿到严格更长的正文时原位升级（跳转链接
                # 文章补抓场景），否则跳过。
                bound_topic_id = str(
                    existing_topic_entry.get("topic_id") or topic_id or legacy_topic_id
                )
                if self._should_recapture(
                    bound_topic_id,
                    new_content_len=len(post.get("content", "")),
                    new_completeness_version=post.get("completeness_version", 1),
                ):
                    existing_ids.discard(post_id)
                else:
                    continue

            if post_id in existing_ids:
                continue

            if self._is_platform_chrome(post.get("title", ""), post.get("content", "")):
                continue

            is_column_article = post.get("column", "普通") != "普通"
            post_score = post.get("score")
            is_authenticated_teacher_cursor = (
                post.get("content_source") == "zsxq_topic_cursor"
                and post.get("source_classification") == "teacher_original"
            )

            # priority_only mode: skip non-star, non-9.0+ articles
            if (
                self._priority_only
                and not is_column_article
                and (post_score is None or post_score < 9.0)
            ):
                continue

            # 已认证 cursor 教师原帖和专栏文章不受 DOM 内容启发式过滤。
            if (
                not is_authenticated_teacher_cursor
                and post.get("column", "普通") == "普通"
                and not self._is_investment_relevant(post.get("title", ""), post.get("content", ""))
            ):
                continue

            # 评分过滤（D-037，2026-09-03）：普通栏/Q&A 有评分且 < 配置阈值
            # 跳过；无评分不拦（评分缺失 ≠ 内容无价值）；专栏不受影响。
            if not is_authenticated_teacher_cursor and not is_column_article:
                if _score_skip_enabled(post_score):
                    logger.info(
                        "[FILTER] 评分 %.1f 不足 %.1f，跳过: %s",
                        post_score or 0,
                        score_skip_min(),
                        post.get("title", "")[:40],
                    )
                    continue

            post_date_str = post.get("date", "")
            matched_images = images_by_date.get(post_date_str, [])
            image_texts = self._process_images(matched_images, post_id) if matched_images else []
            self._save_article(post, image_texts=image_texts)
            existing_ids.add(post_id)
            saved_ids.append(post_id)
            new_count += 1
            img_info = f" ({len(image_texts)} images)" if image_texts else ""
            logger.info(
                "[SAVE] %s | %s | %.1f分%s",
                post_id,
                post.get("title", "")[:60],
                post.get("score") or 0,
                img_info,
            )

        # ── 7b. Priority surfaces only after the timeline coverage proof ──
        # These helpers persist articles as they scan. Running them before the
        # group timeline proof made an incomplete reconciliation mutate the KB.
        if self._priority_only:
            try:
                star_ids = self._scan_star_section(cutoff, existing_ids)
                saved_ids.extend(star_ids)
                new_count += len(star_ids)
                self._sources_scanned.append("star_columns")
                logger.info("[STAR] 星大派专栏扫描: %d 篇新文章", len(star_ids))
            except Exception as e:
                logger.warning("[STAR] 星大派专栏扫描失败: %s", e)
                result.warnings.append("priority_surface_failed:star_columns")

            try:
                digest_ids = self._scan_digests(cutoff, existing_ids)
                saved_ids.extend(digest_ids)
                new_count += len(digest_ids)
                self._sources_scanned.append("digests")
                logger.info("[DIGESTS] 精华页星大派扫描: %d 篇新文章", len(digest_ids))
            except Exception as e:
                logger.warning("[DIGESTS] 精华页扫描失败: %s", e)
                result.warnings.append("priority_surface_failed:digests")
            result.sources_scanned = list(self._sources_scanned)

        result.new_count = new_count
        result.scrape_completed = True
        self._new_count = new_count

        # ── 8. Priority events / analysis jobs for star articles ──
        try:
            pe_count = self._write_priority_events_for_new_articles(result, saved_ids)
            result.priority_events_created = pe_count
        except Exception as e:
            logger.warning("[PRIORITY] 写 priority events 失败: %s", e)
            result.warnings.append(f"priority_events_failed: {e}")

        # ── 9. Deep read artifacts for requires_deep_read articles ──
        if saved_ids:
            self._surface_checkpoint()
            try:
                da_count = self._ensure_deep_read_artifacts_for_new(result, saved_ids)
                result.deep_read_artifacts_created = da_count
            except Exception as e:
                logger.warning("[DEEP-READ] 生成 artifacts 失败: %s", e)
                result.warnings.append(f"deep_read_artifacts_failed: {e}")
            self._surface_checkpoint()

        # ── 9b. 有界排空存量非新鲜 strict-G 深化（retryable / hash 漂移）──
        try:
            backlog_ids = self._collect_deep_read_backlog_ids(
                limit=_DEEP_READ_BACKLOG_DRAIN_LIMIT, exclude=set(saved_ids)
            )
        except Exception as e:
            backlog_ids = []
            logger.warning("[DEEP-READ] backlog 排空扫描失败: %s", e)
            result.warnings.append(f"deep_read_backlog_scan_failed: {e}")
        if backlog_ids:
            logger.info("[DEEP-READ] backlog drain: %d 篇待补做", len(backlog_ids))
            self._surface_checkpoint()
            try:
                drained = self._ensure_deep_read_artifacts_for_new(result, backlog_ids)
                result.deep_read_artifacts_created += drained
            except Exception as e:
                logger.warning("[DEEP-READ] backlog 排空失败: %s", e)
                result.warnings.append(f"deep_read_backlog_drain_failed: {e}")
            self._surface_checkpoint()

        # ── 10. Boundary healing: 页面异常时自愈重提取 ──
        if not result.oldest_seen_date and result.new_count == 0 and result.dom_text_chars > 100:
            logger.warning(
                "[HEAL] boundary_status=unknown + new=0 (chars=%d)，触发 Tab 自愈...",
                result.dom_text_chars,
            )
            try:
                healed, heal_reason = self._validate_and_heal_tab()
                if healed:
                    # 轻量重提取：滚动 → 展开 → 提取日期 → 解析
                    for _ in range(8):
                        assert self._client is not None
                        self._client.scroll_by(4000, wait=1.5)
                    self._expand_all_details()
                    full_text_healed = self._full_text()
                    visible_dates_healed = self._extract_visible_dates()
                    if visible_dates_healed:
                        result.oldest_seen_date = min(visible_dates_healed).strftime(
                            "%Y-%m-%d %H:%M"
                        )

                    # 重新解析并保存帖子
                    posts_healed = self._split_by_author(full_text_healed)
                    healed_count = 0
                    for part in posts_healed:
                        post = self._parse_post(part)
                        if not post:
                            continue
                        post_date = self._parse_date(post.get("date", ""))
                        if post_date and post_date < cutoff:
                            continue
                        post_id = self._make_id(post)
                        if post_id in existing_ids:
                            continue
                        if self._is_platform_chrome(post.get("title", ""), post.get("content", "")):
                            continue
                        if not self._priority_only:
                            if not post.get("column", "").startswith(
                                ("特刊", "锐评", "好问题")
                            ) and not self._is_investment_relevant(
                                post.get("title", ""), post.get("content", "")
                            ):
                                continue
                            if (
                                not post.get("column", "").startswith(
                                    ("特刊", "锐评", "好问题")
                                )
                                and _score_skip_enabled(post.get("score"))
                            ):
                                continue
                        self._save_article(post)
                        existing_ids.add(post_id)
                        saved_ids.append(post_id)
                        healed_count += 1

                    result.new_count = new_count + healed_count
                    result.dom_text_chars = max(result.dom_text_chars, len(full_text_healed))
                    result.posts_seen = result.posts_seen + len(posts_healed)
                    logger.info(
                        "[HEAL] Tab 自愈成功，重提取 %d 篇新文章 (total new=%d)",
                        healed_count,
                        result.new_count,
                    )
                    # 为新保存的文章写 priority events + deep read artifacts
                    if healed_count:
                        healed_saved = saved_ids[-healed_count:]
                        try:
                            pe_count = self._write_priority_events_for_new_articles(
                                result, healed_saved
                            )
                            result.priority_events_created += pe_count
                        except Exception as e:
                            logger.warning("[HEAL] priority events 失败: %s", e)
                            result.warnings.append("priority_events_failed:healed_articles")
                        try:
                            da_count = self._ensure_deep_read_artifacts_for_new(
                                result, healed_saved
                            )
                            result.deep_read_artifacts_created += da_count
                        except Exception as e:
                            logger.warning("[HEAL] deep read artifacts 失败: %s", e)
                            result.warnings.append("deep_read_artifacts_failed:healed_articles")
                else:
                    result.warnings.append(f"boundary_heal_failed: {heal_reason}")
                    logger.warning("[HEAL] Tab 自愈失败: %s", heal_reason)
            except Exception as e:
                result.warnings.append(f"boundary_heal_error: {e}")
                logger.warning("[HEAL] Tab 自愈异常: %s", e)
            self._surface_checkpoint()

        # 诊断：如果没有解析到日期，标记 boundary 未知
        if not result.oldest_seen_date:
            result.warnings.append("boundary_status=unknown: 无法解析页面日期")

        # Heal support artifacts for every article in the active G window, not
        # only articles first observed by this run. This closes legacy gaps
        # without expanding the crawl window; both outboxes and deep-read
        # generation are idempotent.
        self._repair_active_g_support_artifacts(result)
        self._surface_checkpoint()

        # ── 11. Article tags（sidecar，best-effort）: 对 saved_ids 打 content 维标。
        # 失败 warning 不阻塞 ingest（设计稿 v2 §3）；缺口由 reconciler 闭合。
        if saved_ids:
            try:
                from fin_analyse.cognition.article_tags import tag_saved_articles

                tag_report = tag_saved_articles(saved_ids, kb_root=self._kb_root)
                if tag_report.incomplete:
                    logger.warning(
                        "[ARTICLE-TAGS] 部分打标未完成: tagged=%d already=%d "
                        "lock_busy=%d errors=%d gaps=%s",
                        tag_report.tagged,
                        tag_report.already_tagged,
                        tag_report.lock_busy,
                        tag_report.errors,
                        ",".join(tag_report.warnings[:3]),
                    )
                    result.warnings.append(
                        "article_tags_incomplete:"
                        f"lock_busy={tag_report.lock_busy},errors={tag_report.errors}"
                    )
            except Exception as e:  # noqa: BLE001 — 钩子绝不阻塞 ingest
                logger.warning("[ARTICLE-TAGS] 打标失败(不阻塞): %s", e)
                result.warnings.append(f"article_tags_failed: {e}")

        # ── 11b. Macro index（sidecar，best-effort）: 基线 + 本次新文宏观打标。
        # 失败 warning 不阻塞 ingest；宏观缺料由下一次运行/手动 CLI 闭合。
        try:
            from fin_analyse.cognition.macro_index import update_macro_index

            macro_report = update_macro_index(self._kb_root, saved_ids=saved_ids)
            if macro_report.incomplete:
                logger.warning(
                    "[MACRO-INDEX] 部分完成: tagged=%d removed=%d warnings=%s",
                    macro_report.tagged,
                    macro_report.removed,
                    ",".join(macro_report.warnings[:3]),
                )
                result.warnings.append(
                    "macro_index_incomplete:"
                    f"lock_busy={macro_report.lock_busy},warnings={len(macro_report.warnings)}"
                )
        except Exception as e:  # noqa: BLE001 — 钩子绝不阻塞 ingest
            logger.warning("[MACRO-INDEX] 打标失败(不阻塞): %s", e)
            result.warnings.append(f"macro_index_failed: {e}")

        coverage_reason = (
            "dom_header_cutoff"
            if oldest_timeline_date and oldest_timeline_date < cutoff
            else "page_end"
        )
        logger.info(
            "[DONE] 新增 %d 篇 (scanned=%d, chars=%d, coverage=%s, priority=%d, "
            "deep_read eligible=%d generated=%d cache_hit=%d retryable=%d error=%d)",
            new_count,
            result.posts_seen,
            result.dom_text_chars,
            coverage_reason,
            result.priority_events_created,
            result.deep_read_eligible,
            result.deep_read_artifacts_created,
            result.deep_read_cache_hit,
            result.deep_read_retryable,
            result.deep_read_error,
        )
        return result

    def _repair_active_g_support_artifacts(self, result: ScrapeResult) -> None:
        """Idempotently heal priority/deep-read support for the active strict-G set."""
        try:
            from fin_analyse.guo_teacher_research.g_working_set import (
                GWorkingSetService,
            )

            assessment = GWorkingSetService(kb_root=self._kb_root).reconcile()
            if "g_working_set_priority_event_contract_mismatch" in assessment.data_gaps:
                raise ValueError("active G priority event contract mismatch")
            active_g_ids: list[str] = []
            missing_event_ids: list[str] = []
            for item in assessment.manifest.get("articles", ()):
                if not isinstance(item, dict) or not item.get("article_id"):
                    continue
                article_id = str(item["article_id"])
                active_g_ids.append(article_id)
                if item.get("priority_event_id") is None:
                    missing_event_ids.append(article_id)
            if not active_g_ids:
                return
            recorded_new_articles = list(result.new_articles)
            try:
                result.priority_events_created += self._write_priority_events_for_new_articles(
                    result, missing_event_ids
                )
            finally:
                result.new_articles = recorded_new_articles
            result.deep_read_artifacts_created += self._ensure_deep_read_artifacts_for_new(
                result, active_g_ids
            )
        except Exception as e:
            logger.warning("[G-WORKING-SET] support artifact repair failed: %s", e)
            result.warnings.append("g_working_set_support_repair_failed")

    # ── 文章解析 ────────────────────────────────────────────

    def _split_by_author(self, text: str) -> list[str]:
        """按作者名切分全文"""
        parts = re.split(rf"{re.escape(self._author_name)}\s*\n", text)
        # 第一段是页面头部（作者名之前的内容），跳过
        return [p.strip() for p in parts[1:] if len(p.strip()) > 200]

    def _parse_date(self, date_str: str) -> datetime | None:
        """解析日期字符串"""
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"]:
            try:
                return datetime.strptime(date_str[:16], fmt).replace(tzinfo=TZ)
            except ValueError:
                continue
        dates = _extract_datetimes_from_text(date_str)
        if dates:
            return dates[0]
        return None

    def _parse_post(self, text: str) -> dict | None:
        """解析单篇文章"""
        # ── 日期 ──
        tm = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2})", text[:200])
        if not tm:
            tm = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2})", text[:200])
        date_str = tm.group(1) if tm else ""
        if not date_str:
            parsed_dates = _extract_datetimes_from_text(text[:200])
            if parsed_dates:
                date_str = parsed_dates[0].strftime("%Y-%m-%d %H:%M")

        # ── 评分 ──
        sm = re.search(r"能量评分\s*(\d+\.?\d*)\s*分", text)
        score = float(sm.group(1)) if sm else None

        # ── 标签 ──
        tags = []
        for t in re.findall(r"#(\S+)", text):
            t = t.rstrip(",，。.!！?？")
            if t and len(t) < 30:
                tags.append(t)
        priority_label = (
            "重中之重"
            if "重中之重" in tags or re.search(r"(?m)^\s*(?:#\s*)?重中之重(?:\s|[:：]|$)", text)
            else None
        )

        # ── 栏目 ──
        column = "普通"
        for pat, col in COLUMN_PATTERNS:
            if re.search(pat, text):
                column = col
                break

        # ── QA 检测 ──
        is_qa = bool(re.search(r"(提问|问题)[：:]", text))

        # ── 公司 ──
        companies = [name for name in KNOWN_COMPANIES if name in text]

        # ── 标题提取 ──
        title_skip = [
            re.compile(p)
            for p in [
                r"^\d{4}[-/]\d{2}[-/]\d{2}",  # 日期行
                r"^\d{4}年\d{1,2}月\d{1,2}日",  # 中文日期行
                r"^(今天|今日|昨天|昨日|前天)\s*\d{1,2}:\d{2}",  # 相对日期行
                r"^\d{1,2}[-/月]\d{1,2}(?:日)?\s+\d{1,2}:\d{2}",  # 月日日期行
                r"^\d{1,3}\s*(分钟|小时|天)前",  # 相对时间行
                r"^刚刚$",
                r"^能量评分",  # 评分行
                r"^（中东战争",  # 免责声明
                r"^\(中东战争",
                r"^免责声明",
                r"^展开全部",
                r"^收起",
                r"^\d+人觉得很赞",
                r"^\d+条评论",
                r"^为我总结",
                r"^查看详情",
            ]
        ]

        lines = text.split("\n")
        title = ""
        content_start = 0
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            if any(r.match(line) for r in title_skip):
                continue
            if len(line) < 5:
                continue
            title = line[:150]
            content_start = i + 1
            break

        if not title:
            return None

        # ── 正文 ──
        content_lines = []
        footer_stop = [
            re.compile(p)
            for p in [
                r"等?\d+人觉得很赞",
                r"^\d+条评论$",
                r"^评论\s*\d",
                r"^查看详情",
            ]
        ]
        content_skip = title_skip + [
            re.compile(p)
            for p in [
                r"^赞\s*\d",
                r"^评论\s*\d",
                r"^[^\s]+觉得很赞$",
                r"^[^\s]+等\d+人觉得很赞$",
                r"^[^\s]+\s*回复\s*",
                r"^首页$",
                r"^ljq_driver",
            ]
        ]

        for line in lines[content_start:]:
            line = line.strip()
            if not line:
                continue
            if any(r.search(line) for r in footer_stop):
                break
            if any(r.match(line) for r in content_skip):
                continue
            content_lines.append(line)

        content = "\n".join(content_lines)
        return {
            "date": date_str,
            "score": score,
            "title": title,
            "tags": tags,
            "content": content,
            "char_count": len(content),
            "column": column,
            "priority_label": priority_label,
            "is_qa": is_qa,
            "companies": companies,
        }

    # ── 过滤 ────────────────────────────────────────────────

    def _is_platform_chrome(self, title: str, content: str) -> bool:
        """检测 ZSXQ 平台 chrome 页面（群介绍、付费引导、归档导航等），避免误存为文章。"""
        text = title + "\n" + content[:800]
        chrome_patterns = [
            # 付费/加入引导
            "微信小程序知识星球加入",
            "支付不成功请",
            "三天内退款",
            "星球每增加",
            "苹果用户请通过",
            "如果不习惯请务必",
            # 管理员列表
            "星主&合伙人",
            "共 24 个专栏",
            # 日期归档导航（连续月份是强信号）
            "快速预览星球全部精华",
            # 评分缺失 + 无实质内容标题
        ]
        if any(p in text for p in chrome_patterns):
            return True

        # 日期归档导航：连续出现 ≥6 个月份/年份行
        month_year = re.findall(r"^\d{4}$|^\d{1,2}月$", content, re.MULTILINE)
        return len(month_year) >= 6

    def _is_investment_relevant(self, title: str, content: str) -> bool:
        text = title + " " + content[:500]
        investment_kw = [
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
            "消费",
            "汽车",
            "机器人",
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
            "PCB",
            "光通信",
            "CPO",
            "DRAM",
            "HBM",
            "稀土",
            "锂矿",
            "铜",
            "钨",
            "英伟达",
            "台积电",
            "中芯",
            "华为",
            "宁德",
            "比亚迪",
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
            "经济",
            "GDP",
            "PMI",
        ]
        return any(kw in text for kw in investment_kw)

    def _make_legacy_id(self, post: dict) -> str:
        raw = post.get("date", "") + post.get("title", "")
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _make_id(self, post: dict) -> str:
        topic_id = str(post.get("topic_id", ""))
        if topic_id.isascii() and topic_id.isdecimal():
            return f"zsxq-{topic_id}"
        return self._make_legacy_id(post)

    # ── 完整性修复 ────────────────────────────────────────────

    def _find_by_topic_id(self, topic_id: str) -> dict | None:
        """Find an article in the index by its ZSXQ topic_id."""
        if not topic_id:
            return None
        for article in self._index.values():
            if str(article.get("topic_id", "")) == str(topic_id):
                return article
        return None

    def _existing_article_body(self, existing: dict) -> str:
        """Read the stored article body (frontmatter stripped); empty on failure.

        The index ``char_count`` can drift from the file (legacy captures), so
        truncation/upgrade decisions must read the file body, not the index.
        """
        raw_path = existing.get("path")
        path = Path(str(raw_path)) if raw_path else None
        if path is None or not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if text.startswith("---"):
            parts = text.split("\n---", 1)
            if len(parts) == 2:
                return parts[1]
        return text

    def _existing_body_truncated(self, existing: dict) -> bool:
        """True when the stored body ends with the cursor truncation tail."""
        body = self._existing_article_body(existing).rstrip()
        return bool(body) and _TRUNCATED_TAIL_RE.search(body) is not None

    def _should_recapture(
        self,
        topic_id: str,
        new_content_len: int = 0,
        new_completeness_version: int = 1,
    ) -> bool:
        """Decide whether to recapture an article.

        Returns True if:
        - Article doesn't exist in index (new)
        - Existing article is marked incomplete (repair)
        - New capture has higher completeness_version (upgrade)
        """
        if not topic_id:
            return True  # No topic_id = always capture

        existing = self._find_by_topic_id(topic_id)
        if existing is None:
            return True  # New article

        # Repair: existing is incomplete or truncated → recapture only when the
        # new capture is strictly longer (avoids rewriting on every run when the
        # jump-link article keeps failing to backfill).
        if existing.get("incomplete", False) or self._existing_body_truncated(existing):
            existing_body_len = len(self._existing_article_body(existing).strip())
            return new_content_len > existing_body_len

        # Upgrade: new version is more complete
        existing_version: int = existing.get("completeness_version", 1)
        return new_completeness_version > existing_version

    # ── 索引/持久化 ─────────────────────────────────────────

    def _load_index(self):
        if self._index_file.exists():
            try:
                data = json.loads(self._index_file.read_text())
            except (OSError, ValueError) as error:
                # fail-closed：索引在但读不了时宁可本 run 失败。带空索引继续跑，
                # 下一次 _save_index 会用本轮新文覆盖全量索引（BUG-027 同款爆炸半径）。
                raise RuntimeError(
                    f"knowledge index 存在但不可读（{self._index_file}）：{error!r}；"
                    "拒绝以空索引继续，先人工修复或移除该文件"
                ) from error
        else:
            self._index = {}
            return

        # Normalize: support both articles-list and flat-dict formats
        if isinstance(data, dict) and "articles" in data:
            articles = data.get("articles", [])
            self._index = {}
            for a in articles:
                aid = a.get("id")
                if aid:
                    self._index[aid] = a
        elif isinstance(data, dict):
            # Legacy flat CDP format: {"post_id": {...}, ...}
            self._index = data
        else:
            self._index = {}

    def _save_index(self) -> None:
        self._index_file.parent.mkdir(parents=True, exist_ok=True)
        articles = list(self._index.values())
        data = {
            "articles": articles,
            "updated": datetime.now(TZ).isoformat(),
            "total": len(articles),
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)

        try:
            existing_mode = stat.S_IMODE(self._index_file.stat().st_mode)
        except FileNotFoundError:
            existing_mode = None

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        temporary_path: Path | None = None
        temporary_fd: int | None = None
        for _ in range(100):
            candidate = self._index_file.with_name(
                f".{self._index_file.name}.{secrets.token_hex(16)}.tmp"
            )
            try:
                temporary_fd = os.open(candidate, flags, 0o666)
            except FileExistsError:
                continue
            temporary_path = candidate
            break
        else:
            raise FileExistsError(
                f"unable to create a unique temporary file for {self._index_file.name}"
            )

        try:
            temporary_file = os.fdopen(temporary_fd, "w", encoding="utf-8")
            temporary_fd = None
            with temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                if existing_mode is not None:
                    os.fchmod(temporary_file.fileno(), existing_mode)
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, self._index_file)
            temporary_path = None

            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self._index_file.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                if temporary_fd is not None:
                    os.close(temporary_fd)
            finally:
                if temporary_path is not None:
                    with suppress(FileNotFoundError):
                        temporary_path.unlink()

    def _save_article(self, post: dict, image_texts: list[dict] | None = None):
        post_id = self._make_id(post)
        date_str = (post.get("date", "") or "unknown")[:10].replace("-", "").replace("/", "")
        filename = f"{date_str}_{post_id}.md"
        filepath = self._articles_dir / filename
        self._articles_dir.mkdir(parents=True, exist_ok=True)

        tags_yml = "\n".join(f"  - {t}" for t in post.get("tags", []))
        image_texts = image_texts or []
        image_paths = [item["path"] for item in image_texts if item.get("path")]
        image_paths_str = ", ".join(image_paths)
        companies_str = ", ".join(post.get("companies", []))
        is_qa_val = post.get("is_qa", False)
        article_type = post.get("type", "q&a" if is_qa_val else "talk")

        # Build image provenance sections
        llm_parts = []
        prov_parts = []
        for item in image_texts:
            fn = item.get("filename", "")
            if fn and item.get("llm_desc"):
                provider = item.get("vision_provider", "unknown")
                model = item.get("vision_model", "")
                chain = item.get("fallback_chain", [])
                llm_parts.append(
                    f"### {fn} (LLM · {provider}/{model})\n\n"
                    f"fallback_chain: {chain}\n\n{item['llm_desc']}\n"
                )
            if fn and item.get("vision_provider"):
                prov_parts.append(
                    f"- {fn}: provider={item.get('vision_provider')}, "
                    f"model={item.get('vision_model')}, "
                    f"chain={item.get('fallback_chain')}"
                    f"{', error=' + item['error'] if item.get('error') else ''}"
                )

        ocr_parts = [
            f"### {item['filename']} (OCR)\n\n{item['ocr_text']}\n"
            for item in image_texts
            if item.get("filename") and item.get("ocr_text")
        ]
        llm_section = "\n## 图片描述\n\n" + "\n".join(llm_parts) if llm_parts else ""
        ocr_section = "\n## 图片OCR文字\n\n" + "\n".join(ocr_parts) if ocr_parts else ""
        prov_section = "\n".join(prov_parts)

        # Collect provenance summary for frontmatter
        vision_providers = [
            item.get("vision_provider", "") for item in image_texts if item.get("vision_provider")
        ]
        native_source_lines: list[str] = []
        topic_id = str(post.get("topic_id", ""))
        content_source = str(post.get("content_source", ""))
        column = str(post.get("column", "普通"))
        source_classification = str(post.get("source_classification", "teacher_original"))
        source_decision = classify_g_source(
            column,
            teacher_original=source_classification == "teacher_original",
            is_qa=is_qa_val is True,
            priority_label=post.get("priority_label"),
        )
        source = source_decision.classification if source_decision.eligible else None
        if topic_id.isascii() and topic_id.isdecimal():
            native_source_lines.append(f"topic_id: {topic_id}")
        if content_source:
            native_source_lines.append(f"content_source: {content_source}")
        if source is not None or content_source == "zsxq_topic_cursor":
            native_source_lines.append(f"source_classification: {source_classification}")
        if source is not None:
            native_source_lines.extend(
                (
                    f"source_family: {source.source_family}",
                    f"content_type: {source.content_type}",
                    f"source_usage: {source.usage}",
                    f"priority_label: {source.priority_label or ''}",
                )
            )
        native_source_frontmatter = (
            "\n".join(native_source_lines) + "\n" if native_source_lines else ""
        )
        incomplete_flag = bool(post.get("incomplete", False))
        completeness_yml = (
            f"incomplete: {incomplete_flag}\n"
            f"incomplete_reason: {post.get('incomplete_reason', '')}\n"
            f"completeness_version: {post.get('completeness_version', 1)}\n"
        )

        md = f"""---
id: {post_id}
date: {post.get("date", "")}
score: {post.get("score", "")}
column: {column}
companies: [{companies_str}]
is_qa: {is_qa_val}
type: {article_type}
{native_source_frontmatter}tags:
{tags_yml}
image_count: {len(image_paths)}
images: [{image_paths_str}]
image_provenance: [{", ".join(vision_providers)}]
{completeness_yml}---

# {post.get("title", "")}

{post.get("content", "")}
{llm_section}
{ocr_section}
"""
        if prov_section:
            md += f"\n## 图片溯源\n\n{prov_section}\n"
        filepath.write_text(md, encoding="utf-8")

        index_entry = {
            "id": post_id,
            "date": post.get("date", ""),
            "score": post.get("score"),
            "title": post.get("title", ""),
            "tags": post.get("tags", []),
            "char_count": post.get("char_count", 0),
            "column": column,
            "companies": post.get("companies", []),
            "is_qa": post.get("is_qa", False),
            "type": article_type,
            "image_count": len(image_paths),
            "path": str(filepath),
            "file": filename,
            "incomplete": incomplete_flag,
            "incomplete_reason": post.get("incomplete_reason", ""),
            "completeness_version": post.get("completeness_version", 1),
        }
        if topic_id.isascii() and topic_id.isdecimal():
            index_entry["topic_id"] = topic_id
        if content_source:
            index_entry["content_source"] = content_source
        if source is not None or content_source == "zsxq_topic_cursor":
            index_entry["source_classification"] = source_classification
        if source is not None:
            index_entry.update(
                {
                    "source_family": source.source_family,
                    "content_type": source.content_type,
                    "source_usage": source.usage,
                    "priority_label": source.priority_label,
                }
            )
        self._index[post_id] = index_entry
        self._save_index()

    # ── Priority events / analysis jobs ────────────────────────

    # 星大派专栏 — trigger T0 events
    _STAR_COLUMNS_FOR_PRIORITY = frozenset(
        {
            "星大派特刊",
            "星大派锐评",
            "星大派好问题",
            "星大派每日热点",
            "星大派人脉",
            "凤仙郡小故事",
        }
    )

    def _write_priority_events_for_new_articles(
        self, result: ScrapeResult, saved_ids: list[str]
    ) -> int:
        """Write priority events + analysis jobs for *newly saved* star articles only.

        Args:
            result: ScrapeResult to populate with event metadata.
            saved_ids: Article IDs that were just saved this run (not the entire index).

        Returns the number of priority events created.
        """
        from fin_analyse.cognition.priority_articles import (
            PriorityAnalysisJob,
            PriorityAnalysisJobOutbox,
            PriorityArticleEvent,
            PriorityEventOutbox,
        )
        from fin_analyse.utils.ids import stable_id

        runtime_dir = self._kb_root / "runtime" / "cognition"
        event_outbox = PriorityEventOutbox(runtime_dir / "priority_events.jsonl")
        job_outbox = PriorityAnalysisJobOutbox(runtime_dir / "priority_analysis_jobs.jsonl")

        # Repair the independent outboxes before considering new articles. A
        # replay may have no ``saved_ids`` because the article/index write was
        # durable before the event→job process loss.
        for existing_event in event_outbox.list_events():
            repair = PriorityAnalysisJob.from_event(existing_event, user_id="ypk")
            if job_outbox.append(repair):
                logger.info("[PRIORITY] Repaired job: %s", repair.job_id)

        if not saved_ids:
            return 0

        created = 0
        now_str = datetime.now(TZ).isoformat()

        for article_id in saved_ids:
            entry = self._index.get(article_id)
            if entry is None:
                continue

            column = str(entry.get("column", ""))
            source_decision = classify_g_source(
                column,
                teacher_original=(
                    str(entry.get("source_classification", "teacher_original"))
                    == "teacher_original"
                ),
                is_qa=entry.get("is_qa") is True,
                priority_label=entry.get("priority_label"),
            )
            if not source_decision.eligible:
                if source_decision.data_gap:
                    result.warnings.append(f"{source_decision.data_gap}:{article_id}")
                continue
            source = source_decision.classification
            assert source is not None

            title = str(entry.get("title", ""))
            score = entry.get("score")
            resolved_article_path = self._safe_index_article_path(entry)
            article_path = str(resolved_article_path) if resolved_article_path else ""

            event = PriorityArticleEvent(
                event_id=stable_id("priority_article", article_id, prefix="pa:"),
                article_id=article_id,
                title=title,
                priority_tier="T0",
                push_policy="always_push",
                push_reason=f"G source: {source.source_family}/{source.content_type}",
                source_classification="teacher_original",
                persona_eligible=True,
                requires_deep_read=True,
                half_life_class="medium_logic",
                created_at=now_str,
                metadata={
                    "column": column,
                    "score": score,
                    "path": article_path,
                    "source_family": source.source_family,
                    "content_type": source.content_type,
                    "source_usage": source.usage,
                    "priority_label": source.priority_label,
                    "is_qa": entry.get("is_qa") is True,
                    "recheck_priority": (
                        "elevated" if source.priority_label == "重中之重" else "normal"
                    ),
                },
            )

            if event_outbox.append(event):
                result.priority_event_ids.append(event.event_id)
                created += 1
                logger.info("[PRIORITY] T0 event: %s — %s", event.event_id, title[:60])

            # Event and job use independent idempotency keys. Always replay the
            # job append so a process loss after the event line can be repaired.
            job = PriorityAnalysisJob.from_event(event, user_id="ypk")
            if job_outbox.append(job):
                logger.info("[PRIORITY] Job: %s", job.job_id)

            result.new_articles.append(article_id)

        return created

    def _strict_g_entry_pending_pair(
        self, article_id: str, entry: dict[str, Any]
    ) -> tuple[str, Path] | None:
        """Return ``(article_id, path)`` for an indexed strict-G source with a
        resolvable article file; ``None`` otherwise.

        单一判定定义：当轮新生成与存量排空两条路径共用，保证资格语义一致。
        """
        column = str(entry.get("column", ""))
        source_decision = classify_g_source(
            column,
            teacher_original=(
                str(entry.get("source_classification", "teacher_original"))
                == "teacher_original"
            ),
            is_qa=entry.get("is_qa") is True,
            priority_label=entry.get("priority_label"),
        )
        if not source_decision.eligible:
            return None
        article_path = self._safe_index_article_path(entry)
        if not article_path:
            return None
        return article_id, article_path

    def _collect_deep_read_backlog_ids(self, limit: int, exclude: set[str]) -> list[str]:
        """Bounded drain candidates: strict-G entries whose deep-read pair is stale.

        覆盖两类积压：LLM 失败留下的 retryable 残留、文章内容 hash 漂移。
        每轮最多 ``limit`` 篇、确定性顺序；单条检查失败只跳过，不阻塞 ingest。
        """
        if limit <= 0:
            return []
        from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

        service = DeepReadArtifactService(self._kb_root)
        found: list[str] = []
        for article_id, entry in sorted(self._index.items()):
            if article_id in exclude or len(found) >= limit:
                continue
            pair = self._strict_g_entry_pending_pair(article_id, entry)
            if pair is None:
                continue
            try:
                fresh = service.is_fresh(pair[0], pair[1])
            except Exception as e:
                logger.warning(
                    "[DEEP-READ] backlog freshness check failed for %s: %s",
                    article_id,
                    e,
                )
                continue
            if not fresh:
                found.append(article_id)
        return found

    def _ensure_deep_read_artifacts_for_new(
        self, result: ScrapeResult, saved_ids: list[str]
    ) -> int:
        """Generate deep-read artifacts for new articles that require them.

        Only processes star-column articles (same scope as
        _STAR_COLUMNS_FOR_PRIORITY) — these are the ones marked
        requires_deep_read=True in priority events.

        Fresh pairs are identified first; the pinned LLM config is compiled
        once per run only when at least one eligible article still needs
        generation.  An invalid/empty LLM plan records one bounded typed run
        warning and skips all per-article LLM calls — articles stay materialized
        and the G completion remains partial until a later run fixes config.
        """
        if not saved_ids:
            return 0

        from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

        service = DeepReadArtifactService(self._kb_root)
        control = (
            CognitionCompletionControl(
                fence=ExecutionFence(self._deadline_at),
                checkpoint=self._surface_checkpoint,
            )
            if self._deadline_at is not None
            else None
        )
        pending: list[tuple[str, Path]] = []

        for article_id in saved_ids:
            entry = self._index.get(article_id)
            if entry is None:
                continue
            pair = self._strict_g_entry_pending_pair(article_id, entry)
            if pair is not None:
                pending.append(pair)

        if not pending:
            return 0
        result.deep_read_eligible += len(pending)

        # 只有确需生成的 strict-G 文章才值得 compile config；全 cache hit 时
        # 不触发任何 LLM 配置读取。
        needs_generation = [
            (article_id, article_path)
            for article_id, article_path in pending
            if not service.is_fresh(article_id, article_path)
        ]
        result.deep_read_cache_hit += len(pending) - len(needs_generation)
        if not needs_generation:
            return 0

        # 每轮一次的 preflight：在第一次 LLM 调用前用现有 closed compiler
        # 校验 pinned runtime config；失败时零逐文章 LLM 调用，只记一个
        # bounded typed run warning，不删除文章。
        if self._deep_read_llm_preflight_ok is None:
            self._deep_read_llm_preflight_ok = self._deep_read_llm_preflight()
        if not self._deep_read_llm_preflight_ok:
            if not self._deep_read_llm_config_warning_recorded:
                self._deep_read_llm_config_warning_recorded = True
                result.warnings.append(_DEEP_READ_LLM_CONFIG_INVALID)
                logger.warning(
                    "[DEEP-READ] %s: skipping LLM generation this run",
                    _DEEP_READ_LLM_CONFIG_INVALID,
                )
            return 0

        created = 0
        for article_id, article_path in needs_generation:
            self._surface_checkpoint()
            try:
                status = (
                    service.ensure_artifacts(article_id, article_path)
                    if control is None
                    else service.ensure_artifacts(
                        article_id,
                        article_path,
                        control=control,
                    )
                )
                if status.get("status") == "generated":
                    created += 1
                    logger.info(
                        "[DEEP-READ] generated: %s (%s)",
                        article_id,
                        status.get("generated_at", ""),
                    )
                elif status.get("status") == "cache_hit":
                    logger.info("[DEEP-READ] cache hit: %s", article_id)
                elif status.get("status") == "retryable":
                    result.deep_read_retryable += 1
                    msg = f"[DEEP-READ] {article_id}: retryable"
                    logger.warning(msg)
                    result.warnings.append(msg)
                else:
                    result.deep_read_error += 1
                    svc_warnings = status.get("warnings", [])
                    msg = (
                        f"[DEEP-READ] {article_id}: {status.get('status', '?')}"
                        f" — {'; '.join(svc_warnings[:2])}"
                    )
                    logger.warning(msg)
                    result.warnings.append(msg)
            except Exception as e:
                result.deep_read_error += 1
                msg = f"[DEEP-READ] failed for {article_id}: {e}"
                logger.warning(msg)
                result.warnings.append(msg)
            self._surface_checkpoint()

        return created

    def _deep_read_llm_preflight(self) -> bool:
        """Compile the pinned LLM config once before any deep-read LLM call.

        Reuses the existing ``load_llm_config()`` + ``compile_backend_plan()``;
        no new config parser or fallback path.  An empty compiled plan is
        treated as unusable so a missing/empty config cannot silently produce
        retryable artifacts.
        """
        from fin_analyse.claims.config_loader import (
            compile_backend_plan,
            load_llm_config,
        )

        try:
            plan = compile_backend_plan(load_llm_config())
        except Exception:
            logger.warning(
                "[DEEP-READ] LLM config preflight rejected the pinned runtime config"
            )
            return False
        return bool(plan)

    def _safe_index_article_path(self, entry: dict[str, Any]) -> Path | None:
        """Resolve one index article strictly below this scraper's articles root."""
        candidate: Path | None = None
        article_file = entry.get("file")
        if isinstance(article_file, str):
            normalized_file = article_file.strip()
            file_path = Path(normalized_file)
            if (
                normalized_file
                and normalized_file not in {".", ".."}
                and not file_path.is_absolute()
                and file_path.name == normalized_file
            ):
                candidate = self._articles_dir / normalized_file

        if candidate is None:
            raw_path = entry.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                return None
            candidate = Path(raw_path.strip())
            if not candidate.is_absolute():
                candidate = self._kb_root / candidate
        try:
            candidate.resolve(strict=False).relative_to(self._articles_dir.resolve(strict=False))
        except (OSError, ValueError):
            return None
        return candidate
