#!/usr/bin/env node
/**
 * ZSXQ Windows 原生手工 capture — FIN-ZSXQ 已登录 Chrome 专用 profile。
 *
 * 部署：把本文件复制到 Windows（如 C:\Users\<u>\fin-zsxq-capture\capture-zsxq.cjs），
 * 在 FIN-ZSXQ Chrome 打开目标群页后手工运行：
 *     node capture-zsxq.cjs
 *
 * 产出：%USERPROFILE%\fin-zsxq-capture-handoff\capture.latest.json（单临时文件 + 原子替换），
 *       由 WSL 侧 scripts/import_zsxq_capture.py 校验并导入（唯一 ingest 入口）。
 *
 * 边界（与根合同一致）：
 *   - 不经 WSL interop 驱动浏览器；只调用 Windows 原生 node.exe + opencli main.js。
 *   - 不导出 cookie / 浏览器 profile / 登录态 / 凭证；只读页面身份（url/title）与正文证据。
 *   - 不写 FIN ledger/数据库；失败写 failed artifact（精确窄原因），绝不伪造 fresh。
 *   - 图片采集：录制 images eval 真实输出 + 白名单清洗（images.zsxq.com 签名 URL，
 *     上限 60 张、src ≤2048），由 WSL 侧既有下载/OCR/vision 管线处理。
 *
 * 配置（可选环境变量）：
 *   FIN_OPENCLI_PROFILE      opencli Chrome profile/context（默认读 %USERPROFILE%\.opencli\browser-profiles.json 的 defaultContextId）
 *   FIN_ZSXQ_CAPTURE_HANDOFF_DIR   产出目录（默认 %USERPROFILE%\fin-zsxq-capture-handoff）
 */

"use strict";

const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SESSION = "fin-zsxq-scraper-v1";
const GROUP_URL = "https://wx.zsxq.com/group/15522441811252";
const SCHEMA_VERSION = "fin.zsxq-capture-artifact/v1";
const WINDOW_DAYS = 3;
const SCROLL_STEPS = 20;
const SCROLL_PX = 4000;
const EXPAND_MAX = 20;
const COMMAND_TIMEOUT_MS = 90000;

// 与 fin_analyse/scraper/cdp_scraper.py 常量逐字节一致（由
// tests/scraper/test_capture_script_consistency.py 校验；漂移时用生成器重注入）。
const EMBEDDED_SCRIPTS = {
  "timeline_evidence": "\n(function finTimelineTimestampEvidence() {\n    const TIMELINE_TIME_SELECTOR = 'time, [class*=\"date\"], [class*=\"time\"]';\n    const GROUP_TOPIC_PATH = /^\\/group\\/15522441811252\\/topic\\/(\\d+)\\/?$/;\n\n    const isVisible = (node) => {\n        if (!(node instanceof Element) || !node.isConnected) return false;\n        if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;\n        const style = window.getComputedStyle(node);\n        if (style.display === 'none'\n            || style.visibility === 'hidden'\n            || style.visibility === 'collapse'\n            || Number(style.opacity) === 0) return false;\n        const rect = node.getBoundingClientRect();\n        return rect.width > 0 && rect.height > 0;\n    };\n\n    const nativeTopicIds = (candidate) => {\n        const ids = new Set();\n        for (const node of [candidate, ...candidate.querySelectorAll('[data-topic-id]')]) {\n            const value = String(node.getAttribute('data-topic-id') || '');\n            if (/^\\d+$/.test(value)) ids.add(value);\n        }\n        for (const link of candidate.querySelectorAll('a[href]')) {\n            try {\n                const url = new URL(String(link.getAttribute('href') || ''), location.origin);\n                if (url.origin !== location.origin) continue;\n                const match = url.pathname.match(GROUP_TOPIC_PATH);\n                if (match) ids.add(match[1]);\n            } catch (_error) {\n                // A malformed href is not native topic identity.\n            }\n        }\n        return ids;\n    };\n\n    const owningNativeTopicCard = (node) => {\n        let candidate = node.parentElement;\n        for (let depth = 0; candidate && candidate !== document.body && depth < 12; depth += 1) {\n            if (isVisible(candidate)) {\n                const ids = nativeTopicIds(candidate);\n                if (ids.size === 1) return {card: candidate, topicId: Array.from(ids)[0]};\n                if (ids.size > 1) return null;\n            }\n            candidate = candidate.parentElement;\n        }\n        return null;\n    };\n\n    const evidenceByTopic = new Map();\n    for (const node of document.querySelectorAll(TIMELINE_TIME_SELECTOR)) {\n        if (!isVisible(node)) continue;\n        const owner = owningNativeTopicCard(node);\n        if (!owner) continue;\n        const timestamp = (node.innerText || node.textContent || '').trim();\n        if (!timestamp || timestamp.length > 80) continue;\n        const existing = evidenceByTopic.get(owner.topicId) || {\n            topic_id: owner.topicId,\n            header_lines: String(owner.card.innerText || owner.card.textContent || '')\n                .split(/\\r?\\n/)\n                .map((line) => line.trim())\n                .filter(Boolean)\n                .slice(0, 12),\n            timestamps: []\n        };\n        if (!existing.timestamps.includes(timestamp)) existing.timestamps.push(timestamp);\n        evidenceByTopic.set(owner.topicId, existing);\n    }\n\n    return JSON.stringify({\n        schema_version: 1,\n        items: Array.from(evidenceByTopic.values())\n    });\n})()\n",
  "loader_state": "\n(function finTimelineLoaderState() {\n    const isVisibleInViewport = (node) => {\n        if (!(node instanceof Element) || !node.isConnected) return false;\n        if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;\n        const style = window.getComputedStyle(node);\n        if (style.display === 'none'\n            || style.visibility === 'hidden'\n            || style.visibility === 'collapse'\n            || Number(style.opacity) === 0) return false;\n        const rect = node.getBoundingClientRect();\n        return rect.width > 0\n            && rect.height > 0\n            && rect.bottom > 0\n            && rect.top < window.innerHeight;\n    };\n    const candidates = document.querySelectorAll(\n        'app-lottie-loading, app-lottie-loading .flow-loading'\n    );\n    return JSON.stringify({\n        visible: Array.from(candidates).some(isVisibleInViewport)\n    });\n})()\n",
  "scroll_metrics": "(function() {\n    const el = document.scrollingElement || document.documentElement;\n    return JSON.stringify({\n        scrollTop: el.scrollTop,\n        clientHeight: el.clientHeight,\n        scrollHeight: el.scrollHeight\n    });\n})()",
  "full_text": "document.body.innerText",
  "images": "(function() {\n    const datePattern = /\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}/;\n    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);\n    const dateNodes = [];\n    while (walker.nextNode()) {\n        if (datePattern.test(walker.currentNode.textContent)) {\n            dateNodes.push(walker.currentNode);\n        }\n    }\n    const result = [];\n    let imgIndex = 0;\n    const seenSrcs = new Set();\n    for (const node of dateNodes) {\n        const dateMatch = node.textContent.match(datePattern);\n        if (!dateMatch) continue;\n        const date = dateMatch[0];\n        let card = node.parentElement;\n        for (let i = 0; i < 15 && card && card !== document.body; i++) {\n            if (card.textContent.length > 200) break;\n            card = card.parentElement;\n        }\n        if (!card || card === document.body) continue;\n        const imgs = card.querySelectorAll('img[src*=\"images.zsxq.com\"]');\n        for (const img of imgs) {\n            if ((img.width > 100 || img.height > 100) && !seenSrcs.has(img.src)) {\n                seenSrcs.add(img.src);\n                result.push({src: img.src, date: date, index: imgIndex++});\n            }\n        }\n    }\n    return JSON.stringify(result);\n})()",
  "expand": "(function() {\n    const links = document.querySelectorAll('a, span, div');\n    for (const el of links) {\n        if (el.textContent.trim() === '查看详情') {\n            el.click();\n            return 'clicked';\n        }\n    }\n    return 'done';\n})()",
  "body_substring": "document.body.innerText.substring(0, 5000)"
};

class CaptureError extends Error {
  constructor(reason, detail) {
    super(detail);
    this.reason = reason;
  }
}

function fail(reason, detail) {
  throw new CaptureError(reason, detail);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ── 时间（+08 wall）───────────────────────────────────────────

function nowCST() {
  return new Date(Date.now() + 8 * 3600 * 1000); // 移位后的 UTC 分量 = +08 wall
}

function isoCST(d) {
  return d.toISOString().replace("Z", "+08:00");
}

function fmtCST(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(
    d.getUTCHours()
  )}:${p(d.getUTCMinutes())}`;
}

function parseTimestamp(text, now) {
  const t = String(text || "").trim();
  let m = t.match(/^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?:(?:\s+|T)(\d{1,2}):(\d{2}))?/);
  if (m) {
    return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0)));
  }
  const rel = { 今天: 0, 今日: 0, 昨天: 1, 昨日: 1, 前天: 2 };
  m = t.match(/^(今天|今日|昨天|昨日|前天)\s*(\d{1,2}):(\d{2})$/);
  if (m) {
    const d = new Date(now.getTime() - rel[m[1]] * 86400000);
    d.setUTCHours(+m[2], +m[3], 0, 0);
    return d;
  }
  m = t.match(/^(\d{1,3})\s*分钟前$/);
  if (m) return new Date(now.getTime() - +m[1] * 60000);
  m = t.match(/^(\d{1,3})\s*小时前$/);
  if (m) return new Date(now.getTime() - +m[1] * 3600000);
  m = t.match(/^(\d{1,2})\s*天前$/);
  if (m) return new Date(now.getTime() - +m[1] * 86400000);
  m = t.match(/^(\d{1,2})[-/月](\d{1,2})(?:日)?\s+(\d{1,2}):(\d{2})$/);
  if (m) {
    let year = now.getUTCFullYear();
    let d = new Date(Date.UTC(year, +m[1] - 1, +m[2], +m[3], +m[4]));
    if (d.getTime() > now.getTime() + 86400000) {
      d = new Date(Date.UTC(year - 1, +m[1] - 1, +m[2], +m[3], +m[4]));
    }
    return d;
  }
  if (t === "刚刚") return now;
  return null;
}

function parseEvidenceTimestamps(raw, now) {
  let payload = null;
  try {
    payload = JSON.parse(raw);
  } catch (_e) {
    return [];
  }
  if (!payload || payload.schema_version !== 1 || !Array.isArray(payload.items)) return [];
  const dates = [];
  for (const item of payload.items) {
    if (!item || !Array.isArray(item.timestamps) || item.timestamps.length !== 1) continue;
    const parsed = parseTimestamp(item.timestamps[0], now);
    if (parsed !== null) dates.push(parsed);
  }
  return dates;
}

// ── native topic cursor（DOM 证据不可用时的主路径；投影与 WSL 严格解码兼容）──

// F-07（2026-08-17；2026-09-01 扩）：内联文章链接提取脚本——在群页 DOM 里按
// topic card 找 articles.zsxq.com 锚点（预览只渲染链接、API 文本不带，必须走
// DOM）。按 a.href 绝对化判定，兼容相对 href / JS 跳转后的真实链接。
function buildInlineLinkScript(topicId) {
  return `(async function finInlineLink() {
    const cards = [...document.querySelectorAll('[data-topic-id]')];
    const card = cards.find((c) => c.getAttribute('data-topic-id') === ${JSON.stringify(topicId)});
    const anchors = card ? [...card.querySelectorAll('a[href]')] : [];
    for (const anchor of anchors) {
      const href = String(anchor.href || '');
      if (href.startsWith('https://articles.zsxq.com/')) return href;
    }
    return '';
  })()`;
}

// F-07 第二退路：当前页任意 articles.zsxq.com 锚点（topic 详情页跳转链接）。
function buildAnyArticleLinkScript() {
  return `(async function finAnyArticleLink() {
    const anchors = [...document.querySelectorAll('a[href]')];
    for (const anchor of anchors) {
      const href = String(anchor.href || '');
      if (href.startsWith('https://articles.zsxq.com/')) return href;
    }
    return '';
  })()`;
}

// F-07 第一退路（真实 DOM 实证）：群页/详情页的跳转锚点文案=文章标题。
// [data-topic-id] card 在现行 DOM 里不存在，故按标题文本精确匹配锚点。
function buildTitleArticleLinkScript(title) {
  const titleJs = JSON.stringify(title || '');
  return `(async function finTitleArticleLink() {
    const anchors = [...document.querySelectorAll('a[href]')];
    const candidates = anchors.filter((a) => String(a.href || '').startsWith('https://articles.zsxq.com/'));
    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
    const want = norm(${titleJs});
    if (!want) return '';
    for (const anchor of candidates) {
      if (norm(anchor.textContent) === want) return anchor.href;
    }
    for (const anchor of candidates) {
      const text = norm(anchor.textContent);
      if (text && (want.startsWith(text) || text.startsWith(want))) return anchor.href;
    }
    return '';
  })()`;
}

// F-07：articles.zsxq.com 文章正文清洗——剥导航壳/免责声明/风险提示/扫码。
// 免责声明与 WSL `_strip_disclaimer_line`（BUG-027）同语义：行首锚定，
// 帖尾（声明行前有实质内容）保留之前，帖首保留之后；行中内联提及不剥，
// 声明跨多行只剥锚定行（已知限制，与 WSL 一致）。其余页脚标记（页面壳，
// cursor 面不存在）维持首次出现截断。
function cleanInlineArticleText(raw) {
  let lines = (raw || '').split(/\r?\n/).map((ln) => ln.trim()).filter(Boolean);
  // 剥头部导航：标题后的“来自：”“老师名”“时间”行
  while (lines.length && /^(来自[：:]|三线文案大锅饭|\d{4}年\d{2}月\d{2}日)/.test(lines[0])) {
    lines.shift();
  }
  const idx = lines.findIndex((ln) => ln.startsWith('免责声明'));
  if (idx !== -1) {
    const beforeSubstantive = lines
      .slice(0, idx)
      .some((ln) => !ln.startsWith('免责声明') && !ln.startsWith('能量评分'));
    lines = beforeSubstantive ? lines.slice(0, idx) : lines.slice(idx + 1);
  }
  let text = lines.join('\n');
  for (const marker of ['风险提示', '扫码加入星球', '查看更多优质内容']) {
    const markerIdx = text.indexOf(marker);
    if (markerIdx !== -1) text = text.slice(0, markerIdx);
  }
  return text.replace(/\s+$/, '');
}

// F-07 截断判据：feed/detail 双侧都截断的 teacher talk 以 …/... 结尾。
function isTruncatedTail(text) {
  return /(?:\.{3}|…)\s*$/.test(text || '');
}

// F-07（2026-09-01 重构）：把截断 teacher talk 的全文回填从
// collectCursorCoverage 抽出，可注入 deps 供测试。回填只改内存 topics，
// 持久化责任在上层（output = JSON.stringify(parsed)）。
async function backfillTruncatedInlineArticles(parsed, tabId, deps = {}) {
  const runCli = deps.runOpencli || runOpencli;
  const decode = deps.decodeEvalStdout || decodeEvalStdout;
  const wait = deps.sleep || sleep;
  if (!parsed || !Array.isArray(parsed.topics)) return;
  const candidates = parsed.topics
    .filter(
      (t) =>
        t.source_class === 'teacher' &&
        t.topic_type === 'talk' &&
        isTruncatedTail(t.content_text)
    )
    .slice(0, 5);
  for (const topic of candidates) {
    let navigatedAway = false;
    try {
      let link = decode(
        runCli(["eval", buildInlineLinkScript(topic.topic_id), "--tab", tabId])
      ).trim();
      if (!link.startsWith("https://articles.zsxq.com/")) {
        link = decode(
          runCli(["eval", buildTitleArticleLinkScript(topic.title), "--tab", tabId])
        ).trim();
      }
      if (!link.startsWith("https://articles.zsxq.com/")) {
        // 第二退路：topic 详情页展开「查看详情」后轮询跳转链接，再兜底详情页正文。
        runCli([
          "open",
          `${GROUP_URL}/topic/${encodeURIComponent(topic.topic_id)}`,
          "--tab",
          tabId,
        ]);
        navigatedAway = true;
        await wait(2500);
        try {
          decode(runCli(["eval", EMBEDDED_SCRIPTS.expand, "--tab", tabId]));
          await wait(800);
        } catch (_expandError) {
          // 展开失败不影响后续链接提取。
        }
        for (let attempt = 0; attempt < 5 && !link.startsWith("https://articles.zsxq.com/"); attempt += 1) {
          if (attempt) await wait(1000);
          link = decode(
            runCli(["eval", buildAnyArticleLinkScript(), "--tab", tabId])
          ).trim();
        }
        if (!link.startsWith("https://articles.zsxq.com/")) {
          const detailRaw = decode(
            runCli([
              "eval",
              "(async () => (document.body.innerText || '').slice(0, 120000))()",
              "--tab",
              tabId,
            ])
          );
          const detailCleaned = cleanInlineArticleText(detailRaw);
          if (detailCleaned.length > (topic.content_text || '').length) {
            topic.content_text = detailCleaned.slice(0, 200000);
          }
        }
      }
      if (link.startsWith("https://articles.zsxq.com/")) {
        runCli(["open", link, "--tab", tabId]);
        navigatedAway = true;
        await wait(2500);
        for (let scroll = 0; scroll < 6; scroll += 1) {
          runCli(["scroll", "down", "--amount", String(SCROLL_PX), "--tab", tabId]);
        }
        const articleRaw = decode(
          runCli([
            "eval",
            "(async () => (document.body.innerText || '').slice(0, 120000))()",
            "--tab",
            tabId,
          ])
        );
        const cleaned = cleanInlineArticleText(articleRaw);
        if (cleaned.length > (topic.content_text || '').length) {
          topic.content_text = cleaned.slice(0, 200000);
        }
      }
    } catch (_error) {
      // 内联补抓失败：保留已捕获内容，不使整页作废。
    } finally {
      if (navigatedAway) {
        try {
          runCli(["open", `${GROUP_URL}?_fin_ts=${Date.now()}`, "--tab", tabId]);
        } catch (_restoreError) {
          // 群页恢复失败不影响已回填内容。
        }
        await wait(2000);
      }
    }
  }
}

function buildTopicCursorScript(endTime) {
  const params = { scope: "all", count: "30" };
  if (endTime) params.end_time = endTime;
  const url = "https://api.zsxq.com/v2/groups/15522441811252/topics?" + new URLSearchParams(params).toString();
  return `return await (async function finTopicCursorPage() {
    let response;
    let body = null;
    try {
      response = await fetch(${JSON.stringify(url)}, {credentials: 'include'});
      body = await response.json();
    } catch (_error) {
      return JSON.stringify({
        schema_version: 4,
        http_status: response && Number.isInteger(response.status) ? response.status : 599,
        api_succeeded: false,
        api_code: null,
        topics: []
      });
    }
    const nativeTopics = body && body.resp_data && Array.isArray(body.resp_data.topics)
      ? body.resp_data.topics.slice(0, 30)
      : [];
    // F-03：投影必须通过 WSL _decode_topic_cursor_page 的接受谓词，否则整页作废。
    const decoderAccepts = (t) => {
      if (!t) return false;
      if (typeof t.topic_id !== 'string' || !/^[0-9]{1,32}$/.test(t.topic_id)) return false;
      if (typeof t.legacy_topic_id !== 'string' || !/^[0-9]{1,32}$/.test(t.legacy_topic_id)) return false;
      // F-03：create_time 必须为可解析的 ISO 时间（WSL _parse_topic_create_time 16..40 字符）
      if (typeof t.create_time !== 'string' || t.create_time.length < 16 || t.create_time.length > 40
        || Number.isNaN(Date.parse(t.create_time))) return false;
      if (t.source_class !== 'teacher' && t.source_class !== 'coverage_only') return false;
      if (typeof t.content_text !== 'string' || t.content_text.length > 200000) return false;
      if (typeof t.title !== 'string' || t.title.length > 500) return false;
      if (t.topic_type === 'talk') {
        if (t.answer_state !== 'not_applicable') return false;
      } else if (t.topic_type === 'q&a') {
        if (t.answer_state !== 'answered' && t.answer_state !== 'unanswered') return false;
        if (t.title) return false;
      } else {
        return false;
      }
      if (t.source_class === 'coverage_only') {
        if (t.content_text || t.title) return false;
      } else if (!t.content_text.trim() || t.answer_state === 'unanswered') {
        return false;
      }
      return true;
    };
    const topics = nativeTopics.map((topic) => {
      const type = String(topic && topic.type || 'talk');
      let owner = null;
      let answerState = 'invalid';
      let rawContent = '';
      if (type === 'q&a') {
        const hasMultipleAnswers = Boolean(
          topic && Array.isArray(topic.answers) && topic.answers.length
        );
        const answer = topic && topic.answer;
        if (hasMultipleAnswers || Array.isArray(answer)) {
          answerState = 'invalid';
        } else if (answer == null) {
          answerState = 'unanswered';
        } else if (
          typeof answer === 'object'
          && answer
          && typeof answer.owner === 'object'
          && answer.owner
          && typeof answer.text === 'string'
        ) {
          answerState = 'answered';
          owner = answer.owner;
          rawContent = answer.text;
        }
      } else if (type === 'talk') {
        const talk = topic && topic.talk;
        if (
          typeof talk === 'object'
          && talk
          && typeof talk.owner === 'object'
          && talk.owner
          && typeof talk.text === 'string'
        ) {
          answerState = 'not_applicable';
          owner = talk.owner;
          rawContent = talk.text;
        }
      }
      const ownerUserId = owner && (
        typeof owner.user_id === 'string'
        || Number.isSafeInteger(owner.user_id)
      ) ? String(owner.user_id) : '';
      const ownerIdentityValid = /^[0-9]{1,32}$/.test(ownerUserId)
        && /[1-9]/.test(ownerUserId)
        && typeof owner.name === 'string'
        && owner.name === owner.name.trim()
        && owner.name.length >= 1
        && owner.name.length <= 200;
      const isTeacher = ownerIdentityValid
        && owner.name === ${JSON.stringify("三线文案大锅饭")}
        && answerState !== 'unanswered'
        && answerState !== 'invalid';
      const sourceClass = answerState === 'unanswered'
        ? 'coverage_only'
        : !ownerIdentityValid || answerState === 'invalid'
        ? 'invalid'
        : isTeacher
        ? 'teacher'
        : 'coverage_only';
      return {
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
        content_text: isTeacher ? rawContent.slice(0, 200000) : ''
      };
    }).filter(decoderAccepts);
    // F-03：页内 topic_id / legacy_topic_id 唯一（WSL decoder 拒绝重复）
    const seenTopicIds = new Set();
    const seenLegacyIds = new Set();
    const uniqueTopics = topics.filter((t) => {
      if (seenTopicIds.has(t.topic_id) || seenLegacyIds.has(t.legacy_topic_id)) return false;
      seenTopicIds.add(t.topic_id);
      seenLegacyIds.add(t.legacy_topic_id);
      return true;
    });
    // F-06（2026-08-17 修复）：feed 长文截断补抓——teacher talk 正文以 …/...
    // 结尾视为截断，用同会话 topic detail 接口取全文回填 content_text；
    // 详情失败保留 feed 原文，绝不使整页作废。每页最多补 5 条。
    const TRUNCATION_MARK = /(?:\.{3}|…)\s*$/;
    const truncated = uniqueTopics.filter(
      (t) => t.source_class === 'teacher' && t.topic_type === 'talk'
        && TRUNCATION_MARK.test(t.content_text)
    );
    for (const topic of truncated.slice(0, 5)) {
      try {
        const detailResponse = await fetch(
          'https://api.zsxq.com/v2/topics/' + encodeURIComponent(topic.topic_id),
          {credentials: 'include'}
        );
        const detailBody = await detailResponse.json();
        const fullText = detailBody && detailBody.resp_data && detailBody.resp_data.topic
          && detailBody.resp_data.topic.talk
          && typeof detailBody.resp_data.topic.talk.text === 'string'
          ? detailBody.resp_data.topic.talk.text
          : '';
        if (fullText && !TRUNCATION_MARK.test(fullText)
            && fullText.length > topic.content_text.length) {
          topic.content_text = fullText.slice(0, 200000);
        }
      } catch (_error) {
        // 详情抓取失败：保留 feed 原文。
      }
    }
    return JSON.stringify({
      schema_version: 4,
      http_status: Number.isInteger(response.status) ? response.status : 599,
      api_succeeded: Boolean(body && body.succeeded === true),
      api_code: body && Number.isInteger(body.code) ? body.code : null,
      topics: uniqueTopics
    });
  })()`;
}

function parseCursorPage(raw) {
  let payload = null;
  try {
    payload = JSON.parse(raw);
  } catch (_e) {
    return null;
  }
  if (
    !payload
    || payload.schema_version !== 4
    || !Number.isInteger(payload.http_status)
    || typeof payload.api_succeeded !== "boolean"
    || !Array.isArray(payload.topics)
  ) {
    return null;
  }
  return payload;
}

async function collectCursorCoverage(startedAt, cutoff, tabId) {
  const pages = [];
  let cursorEndTime = "";
  let covered = false;
  let pageEnd = false;
  let oldest = null;
  let reason = "";
  for (let page = 0; page < 8; page += 1) {
    // 顶层 return 需包成 async IIFE（与 WSL _wrap_eval_script 语义一致）
    const script = "(async () => {\n" + buildTopicCursorScript(cursorEndTime) + "\n})()";
    let output = decodeEvalStdout(runOpencli(["eval", script, "--tab", tabId]));
    let parsed = parseCursorPage(output);
    // 限流（1059）重试一次；只录制最终结果
    if (parsed && parsed.http_status === 200 && parsed.api_code === 1059) {
      await sleep(8000);
      output = decodeEvalStdout(runOpencli(["eval", script, "--tab", tabId]));
      parsed = parseCursorPage(output);
    }
    if (parsed === null) {
      reason = "cursor_page_invalid";
      break;
    }
    if (parsed.http_status === 401 || parsed.http_status === 403) {
      fail("login_required", `cursor http ${parsed.http_status}`);
    }
    if (parsed.http_status !== 200 || !parsed.api_succeeded) {
      reason = `cursor_api_rejected(http=${parsed.http_status},code=${parsed.api_code})`;
      break;
    }
    // F-07：截断特刊内联文章补抓（feed/detail 双侧都截断的 teacher talk）。
    // 回填完成后必须重序列化——旧实现 push 原始 output，回填全文被静默丢弃。
    await backfillTruncatedInlineArticles(parsed, tabId);
    output = JSON.stringify(parsed);
    pages.push({ end_time: cursorEndTime, script_sha256: sha256Hex(script), output });
    const createTimes = parsed.topics
      .map((t) => parseTimestamp(t && t.create_time, startedAt))
      .filter(Boolean);
    const pageOldest = createTimes.length
      ? new Date(Math.min(...createTimes.map((d) => d.getTime())))
      : null;
    if (pageOldest !== null && pageOldest.getTime() < cutoff.getTime()) {
      oldest = pageOldest;
      covered = true;
      break;
    }
    if (parsed.topics.length === 0) {
      reason = "cursor_empty_page";
      break;
    }
    if (parsed.topics.length < 30) {
      // F-02：pageEnd 也必须写入 oldest，否则上层 fmtCST(null)。
      // page_end 判定与 WSL 语义一致（同一过滤后数据 → 同一结论；raw 计数不可跨
      // 投影契约传递——decoder 要求 5 键精确集合，改动属既有 seam 语义，非本 slice 引入）。
      oldest = pageOldest;
      if (oldest === null) {
        reason = "cursor_timestamps_invalid";
        break;
      }
      pageEnd = true;
      break;
    }
    const last = parsed.topics[parsed.topics.length - 1];
    if (!last || !last.create_time) {
      reason = "cursor_not_advancing";
      break;
    }
    cursorEndTime = last.create_time;
    await sleep(8000);
  }
  if (!covered && !pageEnd && !reason) reason = "page_budget_exhausted";
  return { pages, covered, pageEnd, oldest, reason };
}

// ── URL / 脚本 / hash ──────────────────────────────────────────

function stripFinTs(url) {
  const qIndex = url.indexOf("?");
  if (qIndex < 0) return url;
  const params = new URLSearchParams(url.slice(qIndex + 1));
  params.delete("_fin_ts");
  const qs = params.toString();
  return qs ? url.slice(0, qIndex) + "?" + qs : url.slice(0, qIndex);
}

// 用户授权（2026-08-18）：capture 在严格合法空 inventory 时自动 tab new 群页自愈（至多 3 轮）。
// 非空但无 target、畸形输出、transport 失败一律不 open（不触碰用户无关 tab）。
// 返回 {ok:true,target} 或 {ok:false,reason}；wire failure reason 保持 target_invalid（闭集不变）。
async function findOrCreateGroupTab(listTabsFn, runOpencliFn, sleepMs) {
  const matchGroup = (tabs) =>
    tabs.filter((t) => stripFinTs(String((t && t.url) || "")) === GROUP_URL);
  const initial = listTabsFn();
  let targets = matchGroup(initial);
  if (targets.length > 1) return { ok: false, reason: "ambiguous" };
  if (targets.length === 1) return { ok: true, target: targets[0] };
  if (initial.length > 0) return { ok: false, reason: "missing_nonempty" };
  // 严格合法空数组：自动开群页 tab 并轮询注册（opencli open 返回 page id 前不重复 open）
  for (let attempt = 0; attempt < 3; attempt += 1) {
    runOpencliFn(["open", GROUP_URL]);
    await sleepMs(3000);
    targets = matchGroup(listTabsFn());
    if (targets.length > 1) return { ok: false, reason: "ambiguous" };
    if (targets.length === 1) return { ok: true, target: targets[0] };
  }
  return { ok: false, reason: "missing" };
}

function sha256Hex(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

function jsonEscapeString(text) {
  // 与 Python json.dumps(ensure_ascii=True) 一致：非 ASCII 全部转 \uXXXX（代理对拆分），
  // 控制字符用 \b\t\n\f\r 快捷转义；JSON.stringify 默认输出原始 UTF-8，不能直接用。
  let out = '"';
  for (const ch of text) {
    const code = ch.codePointAt(0);
    if (code < 0x20 || code === 0x22 || code === 0x5c) {
      out += JSON.stringify(ch).slice(1, -1);
    } else if (code < 0x7f) {
      out += ch;
    } else if (code > 0xffff) {
      const hi = ch.charCodeAt(0);
      const lo = ch.charCodeAt(1);
      out += "\\u" + hi.toString(16).padStart(4, "0") + "\\u" + lo.toString(16).padStart(4, "0");
    } else {
      out += "\\u" + code.toString(16).padStart(4, "0");
    }
  }
  return out + '"';
}

function canonicalize(value) {
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalize).join(",") + "]";
  }
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return (
      "{" +
      keys.map((k) => jsonEscapeString(k) + ":" + canonicalize(value[k])).join(",") +
      "}"
    );
  }
  if (typeof value === "string") {
    return jsonEscapeString(value);
  }
  return JSON.stringify(value);
}

function decodeEvalStdout(raw) {
  const stripped = String(raw || "").replace(/\r?\n$/, "");
  try {
    const value = JSON.parse(stripped);
    if (typeof value === "string") return value;
    if (Array.isArray(value) || (value !== null && typeof value === "object")) {
      return JSON.stringify(value);
    }
    return stripped;
  } catch (_e) {
    return stripped;
  }
}

// ── opencli 原生调用（不经 WSL interop）────────────────────────

let _resolved = null;

function resolvePaths() {
  if (_resolved) return _resolved;
  const appData = process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming");
  const npmDir = path.join(appData, "npm");
  const mainJs = path.join(npmDir, "node_modules", "@jackwener", "opencli", "dist", "src", "main.js");
  let nodeExe = path.join(npmDir, "node.exe");
  if (!fs.existsSync(nodeExe)) nodeExe = "C:\\Program Files\\nodejs\\node.exe";
  if (!fs.existsSync(nodeExe) || !fs.existsSync(mainJs)) {
    fail("transport_unavailable", "opencli node/main.js 不可解析（npm dir 或 Program Files nodejs）");
  }
  _resolved = { nodeExe, mainJs };
  return _resolved;
}

function resolveProfile() {
  const envProfile = process.env.FIN_OPENCLI_PROFILE;
  if (envProfile) {
    if (!/^[^\s\x00]+$/.test(envProfile)) fail("transport_unavailable", "FIN_OPENCLI_PROFILE 非法");
    return envProfile;
  }
  try {
    const cfg = JSON.parse(
      fs.readFileSync(path.join(os.homedir(), ".opencli", "browser-profiles.json"), "utf8")
    );
    if (typeof cfg.defaultContextId === "string" && cfg.defaultContextId) {
      return cfg.defaultContextId;
    }
  } catch (_e) {
    /* 无配置 → 单 profile 环境，省略 --profile */
  }
  return null;
}

function resolveHandoffDir() {
  if (process.env.FIN_ZSXQ_CAPTURE_HANDOFF_DIR) return process.env.FIN_ZSXQ_CAPTURE_HANDOFF_DIR;
  return path.join(os.homedir(), "fin-zsxq-capture-handoff");
}

function runOpencli(cmd) {
  const { nodeExe, mainJs } = resolvePaths();
  const argv = [mainJs];
  const profile = resolveProfile();
  if (profile) argv.push("--profile", profile);
  argv.push("browser", SESSION, ...cmd);
  const result = spawnSync(nodeExe, argv, {
    encoding: "utf8",
    timeout: COMMAND_TIMEOUT_MS,
    windowsHide: true,
  });
  if (result.error) {
    fail("transport_unavailable", `opencli ${cmd[0]} spawn: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const stderr = String(result.stderr || "").slice(0, 300);
    fail("transport_unavailable", `opencli ${cmd[0]} exit ${result.status}: ${stderr}`);
  }
  return String(result.stdout || "");
}

function listTabs() {
  const stdout = runOpencli(["tab", "list"]);
  let raw = null;
  try {
    raw = JSON.parse(stdout);
  } catch (_e) {
    fail("transport_unavailable", "tab list 输出非 JSON");
  }
  if (!Array.isArray(raw)) fail("transport_unavailable", "tab list 输出非数组");
  return raw;
}

// ── failed artifact scrub ──────────────────────────────────────
// WSL failed 契约只消费 schema/run_id/captured_at/hash/failure，pages 被忽略
// （capture_artifact.py failed 分支直接返回）。但 images eval 的 output 是
// images.zsxq.com 签名 URL（必含 token=），而 failed 分支的凭证扫描无 images 豁免
// （豁免只在 complete 分支）——保留该记录会把 failed artifact 误拒为
// credential_field_present（invalid_request）而非精确 failed。失败路径剔除它，
// 其余 eval（全文/时间线证据）保留作诊断。调用点只有 catch 一处（一致性测试断言）。
function scrubFailedArtifact(payload) {
  if (!payload || !Array.isArray(payload.pages)) return payload;
  const imagesHash = sha256Hex(EMBEDDED_SCRIPTS.images);
  for (const page of payload.pages) {
    if (page && Array.isArray(page.evals)) {
      page.evals = page.evals.filter((e) => e && e.script_sha256 !== imagesHash);
    }
  }
  return payload;
}

// ── capture 流程 ───────────────────────────────────────────────

async function main() {
  const startedAt = nowCST();
  const runId = crypto.randomUUID();
  const handoffDir = resolveHandoffDir();
  fs.mkdirSync(handoffDir, { recursive: true });
  const artifactPath = path.join(handoffDir, "capture.latest.json");

  const payload = {
    schema_version: SCHEMA_VERSION,
    run_id: runId,
    captured_at: isoCST(startedAt),
    capture_host: os.hostname(),
    final_status: "failed",
    failure: null,
  };
  let tabId = null;

  try {
    // 1. 目标 tab：恰一个已登录群页 tab；严格合法空 inventory 时自动 tab new 自愈
    const found = await findOrCreateGroupTab(listTabs, runOpencli, (ms) => sleep(ms));
    if (!found.ok) fail("target_invalid", "无目标群页 tab（FIN-ZSXQ 未打开群页）");
    const target = found.target;
    tabId = String(target.page || target.id || "");
    if (!tabId) fail("target_invalid", "目标 tab 无 page id");
    payload.target = {
      url: String(target.url),
      title: String(target.title || ""),
      tab_id: tabId,
    };

    // 2. 导航（cache-bust）+ 等待水合
    runOpencli(["open", `${GROUP_URL}?_fin_ts=${Date.now()}`, "--tab", tabId]);
    await sleep(5000);
    for (let i = 0; i < 10; i += 1) {
      const probe = decodeEvalStdout(
        runOpencli(["eval", EMBEDDED_SCRIPTS.body_substring, "--tab", tabId])
      );
      if (probe.length > 500) break;
      await sleep(1000);
    }

    // 3. 固定滚动计划 → 展开详情（scroll 显式 --tab，不依赖 session 默认 tab）
    for (let i = 0; i < SCROLL_STEPS; i += 1) {
      runOpencli(["scroll", "down", "--amount", String(SCROLL_PX), "--tab", tabId]);
      await sleep(1500);
    }
    for (let i = 0; i < EXPAND_MAX; i += 1) {
      const out = decodeEvalStdout(
        runOpencli(["eval", EMBEDDED_SCRIPTS.expand, "--tab", tabId])
      );
      if (String(out).includes("done")) break;
      await sleep(500);
    }

    // 4. 录制 group 页证据（脚本与 WSL 常量逐字节一致，按 sha256 匹配）
    const evidenceRaw = decodeEvalStdout(
      runOpencli(["eval", EMBEDDED_SCRIPTS.timeline_evidence, "--tab", tabId])
    );
    const loaderRaw = decodeEvalStdout(
      runOpencli(["eval", EMBEDDED_SCRIPTS.loader_state, "--tab", tabId])
    );
    const metricsRaw = decodeEvalStdout(
      runOpencli(["eval", EMBEDDED_SCRIPTS.scroll_metrics, "--tab", tabId])
    );
    const fullText = decodeEvalStdout(
      runOpencli(["eval", EMBEDDED_SCRIPTS.full_text, "--tab", tabId])
    );
    // F-03：images eval 的两次调用都包在降级块内——runOpencli 失败/解析失败一律
    // 降级 images=[] 继续（用户硬要求：图片失败绝不影响文字爬取与已采集的全文）。
    let imagesList = null;
    let imagesRaw = "";
    for (let attempt = 0; attempt < 2 && imagesList === null; attempt += 1) {
      try {
        imagesRaw = decodeEvalStdout(
          runOpencli(["eval", EMBEDDED_SCRIPTS.images, "--tab", tabId])
        );
        const parsed = JSON.parse(imagesRaw);
        if (Array.isArray(parsed)) {
          // 图片白名单清洗：只保留 images.zsxq.com 签名 URL（token= 为时限性单图
          // 访问能力，与 WSL 白名单校验一致）；src UTF-8 字节数 ≤2048（F-02，
          // 与 Python len(encode) 一致）。
          imagesList = parsed
            .filter(
              (img) =>
                img
                && typeof img.src === "string"
                && Buffer.byteLength(img.src, "utf8") <= 2048
                && /^https:\/\/images\.zsxq\.com\/[A-Za-z0-9_-]{1,64}(\?[^\s"<>]{0,2000})?$/.test(img.src)
                && typeof img.date === "string"
                && img.date.length >= 16
                && img.date.length <= 40
                && Number.isInteger(img.index)
                && img.index >= 0
                && img.index <= 10000
            )
            .slice(0, 60);
        } else {
          imagesList = [];
        }
      } catch (_e) {
        if (attempt === 0) {
          await sleep(2000);
        }
      }
    }
    if (imagesList === null) {
      // 用户硬要求：图片失败不影响文字爬取——images eval 不可用 → 降级为空，
      // 采集继续（artifact images=[] 如实标注未采集图片）。
      imagesList = [];
      console.warn("images eval 不可用（重试后仍失败），降级 images=[]");
    }
    const imagesNormalized = JSON.stringify(imagesList);
    const evals = [
      { script_sha256: sha256Hex(EMBEDDED_SCRIPTS.timeline_evidence), output: evidenceRaw },
      { script_sha256: sha256Hex(EMBEDDED_SCRIPTS.loader_state), output: loaderRaw },
      { script_sha256: sha256Hex(EMBEDDED_SCRIPTS.scroll_metrics), output: metricsRaw },
      { script_sha256: sha256Hex(EMBEDDED_SCRIPTS.full_text), output: fullText },
      { script_sha256: sha256Hex(EMBEDDED_SCRIPTS.images), output: imagesNormalized },
      { script_sha256: sha256Hex(EMBEDDED_SCRIPTS.expand), output: "done" },
    ];
    payload.pages = [
      {
        url: `${GROUP_URL}?_fin_ts=${Date.now()}`,
        evals,
      },
    ];

    // 5. 终态门：登录 / 限流 / 内容 —— 任一不过 → failed，绝不伪造 fresh
    if (/登录/.test(fullText) && /扫码/.test(fullText)) {
      fail("login_required", "页面呈现登录/扫码表面");
    }
    if (/请求过于频繁|访问过于频繁|稍后重试|too many requests|rate limit/i.test(fullText)) {
      fail("transport_unavailable", "页面呈现限流表面");
    }
    if (fullText.length <= 500) {
      fail("content_insufficient", `页面文本过短（${fullText.length} chars）`);
    }

    // 6. 覆盖证据：DOM 时间线证据优先；不可证明时录制 native topic cursor
    //    （当前页面 data-topic-id 为 0，DOM 证据恒空 → cursor 是实际主路径）。
    const cutoff = new Date(startedAt.getTime() - WINDOW_DAYS * 86400000);
    const evidenceDates = parseEvidenceTimestamps(evidenceRaw, startedAt);
    let oldest = evidenceDates.length
      ? new Date(Math.min(...evidenceDates.map((d) => d.getTime())))
      : null;
    let cursorCovered = false;
    let cursorPageEnd = false;
    if (oldest === null || oldest.getTime() >= cutoff.getTime()) {
      const cursorResult = await collectCursorCoverage(startedAt, cutoff, tabId);
      if (cursorResult.pages.length > 0) {
        payload.topic_cursor = cursorResult.pages;
      }
      cursorCovered = cursorResult.covered;
      cursorPageEnd = cursorResult.pageEnd;
      if (!cursorCovered && !cursorPageEnd) {
        fail(
          "window_coverage_incomplete",
          `cursor coverage unproven（${cursorResult.reason || "unknown"}）`
        );
      }
      if (
        cursorResult.oldest !== null
        && (oldest === null || cursorResult.oldest.getTime() < oldest.getTime())
      ) {
        oldest = cursorResult.oldest;
      }
    }
    // F-02：page_end 是合法覆盖证据（短页全部内容在窗口内时 oldest 可 ≥ cutoff）
    const coveredByCutoff = oldest !== null && oldest.getTime() < cutoff.getTime();
    if (!coveredByCutoff && !cursorCovered && !cursorPageEnd) {
      fail(
        "window_coverage_incomplete",
        `oldest ${oldest ? fmtCST(oldest) : "unknown"} >= cutoff ${fmtCST(cutoff)}`
      );
    }

    payload.target = {
      // F-01：写归一化 URL（cache-bust 残留的 _fin_ts 剥离），WSL 侧同归一化校验
      url: stripFinTs(String(target.url)),
      title: String(target.title || ""),
      tab_id: tabId,
    };
    payload.window = {
      days: WINDOW_DAYS,
      cutoff: fmtCST(cutoff),
      oldest_seen_date: fmtCST(oldest),
      stopped_by_window_boundary: coveredByCutoff || cursorCovered,
      reached_page_end: cursorPageEnd,
    };
    payload.login_state = {
      login_surface_present: false,
      challenge_present: false,
      rate_limit_present: false,
    };
    payload.images = imagesList;
    payload.final_status = "complete";
  } catch (error) {
    payload.final_status = "failed";
    // F-05：detail 写 artifact 前做凭证模式脱敏（与 WSL 值级扫描同模式）
    const rawDetail = String((error && error.message) || error).slice(0, 500);
    payload.failure = {
      reason: error instanceof CaptureError ? error.reason : "unknown",
      // F-05：行级脱敏（\S+ 停在空格会保留 "Bearer <token>" 的 token）
      detail: rawDetail.replace(
        /(cookie|token|secret|password|authorization|bearer|set-cookie|session|credential)\s*[=:][^\n\r]*/gi,
        "[redacted]"
      ),
    };
    // 失败路径剔除 images eval（token= 签名 URL 与 WSL failed 分支无豁免扫描冲突，
    // 否则失败被误判 credential_field_present 而非精确 failed）。
    scrubFailedArtifact(payload);
  }

  const hashPayload = { ...payload };
  delete hashPayload.content_sha256;
  payload.content_sha256 = sha256Hex(canonicalize(hashPayload));

  // 单临时文件 + 原子替换（同卷 rename；Windows MoveFileEx REPLACE_EXISTING）
  const tmpPath = `${artifactPath}.${process.pid}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), "utf8");
  fs.renameSync(tmpPath, artifactPath);

  console.log(
    JSON.stringify(
      {
        schema_version: "fin.zsxq-capture-run/v1",
        run_id: runId,
        final_status: payload.final_status,
        failure_reason: payload.failure ? payload.failure.reason : null,
        detail: payload.failure ? payload.failure.detail : null,
        artifact: artifactPath,
      },
      null,
      2
    )
  );
  return payload.final_status === "complete" ? 0 : 1;
}

if (require.main === module) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      process.stderr.write(String((error && error.stack) || error) + "\n");
      process.exitCode = 1;
    });
}

module.exports = {
  canonicalize,
  parseTimestamp,
  stripFinTs,
  decodeEvalStdout,
  scrubFailedArtifact,
  findOrCreateGroupTab,
  buildInlineLinkScript,
  buildAnyArticleLinkScript,
  buildTitleArticleLinkScript,
  cleanInlineArticleText,
  isTruncatedTail,
  backfillTruncatedInlineArticles,
  EMBEDDED_SCRIPTS,
  SCHEMA_VERSION,
  GROUP_URL,
  WINDOW_DAYS,
};
