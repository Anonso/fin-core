"""F-07 内联文章回填回归（2026-09-01：回填持久化 + 三退路 + budget 5）。

通过 node -e 注入 fake runOpencli/decodeEvalStdout/sleep 调用导出 helper，
断言回填只改内存 topics、失败保留原文、逐页补抓上限 5。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_CAPTURE = Path(__file__).resolve().parents[2] / "scripts" / "capture_zsxq_windows.cjs"


def _topic(topic_id: str, content: str) -> dict:
    return {
        "topic_id": topic_id,
        "legacy_topic_id": topic_id + "0",
        "create_time": "2026-08-31T21:18:00.000+0800",
        "title": f"星大派特刊：t{topic_id}",
        "topic_type": "talk",
        "content_text": content,
        "source_class": "teacher",
        "answer_state": "not_applicable",
    }


def _run(harness_js: str) -> dict:
    script = f"""
const {{
  backfillTruncatedInlineArticles,
  buildInlineLinkScript,
  buildAnyArticleLinkScript,
  EMBEDDED_SCRIPTS,
}} = require({str(_CAPTURE)!r});
{harness_js}
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_link_scripts_are_self_invoking_for_opencli_eval():
    """opencli eval 拒绝前导 return（生产实证），三个链接脚本必须是自调用表达式。"""
    script = f"""
const m = require({str(_CAPTURE)!r});
console.log(JSON.stringify({{
  inline: m.buildInlineLinkScript('1'),
  any: m.buildAnyArticleLinkScript(),
  title: m.buildTitleArticleLinkScript('星大派：示例'),
}}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n{proc.stdout}"
    for name, text in json.loads(proc.stdout.strip().splitlines()[-1]).items():
        assert text.lstrip().startswith("(async"), name
        assert not text.lstrip().startswith("return"), name


_COMMON_FAKE = r"""
const calls = [];
function makeCli(behavior) {
  return (argv) => {
    calls.push(argv.slice(0, 2).join(':'));
    if (argv[0] === 'open' || argv[0] === 'scroll') return '';
    const script = String(argv[1] || '');
    if (script.includes('finInlineLink')) return behavior.groupLink || '';
    if (script.includes('finTitleArticleLink')) return behavior.titleLink || '';
    if (script.includes('finAnyArticleLink')) {
      if (typeof behavior.detailLink === 'function') return behavior.detailLink();
      return behavior.detailLink || '';
    }
    if (script === EMBEDDED_SCRIPTS.expand) return 'clicked';
    if (script.includes('document.body.innerText')) return behavior.body || '';
    return '';
  };
}
const decode = (raw) => String(raw || '').trim();
const noWait = () => new Promise((r) => setTimeout(r, 0));
"""


def test_group_link_backfills_and_mutates_topics() -> None:
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x', title: 'A',
     topic_type: 'talk', content_text: '开头...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: 'https://articles.zsxq.com/id_x.html',
                         body: '全文正文很长很长很长很长很长很长\n免责声明'}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text, calls}));
})();
"""
    )
    assert out["content"] == "全文正文很长很长很长很长很长很长"
    assert any("open:https://articles.zsxq.com" in c for c in out["calls"])


def test_detail_link_fallback_when_group_link_missing() -> None:
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x', title: 'A',
     topic_type: 'talk', content_text: '开头...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: '',
                         detailLink: 'https://articles.zsxq.com/id_y.html',
                         body: '全文二号，比开头长得多'}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text, calls}));
})();
"""
    )
    assert out["content"] == "全文二号，比开头长得多"
    assert any("open:https://articles.zsxq.com/id_y.html" in c for c in out["calls"])
    assert any(c.startswith("open:https://wx.zsxq.com/group/15522441811252/topic/t1") for c in out["calls"])


def test_title_matched_anchor_backfills_without_detail_navigation() -> None:
    """群页无 card 锚点时按标题精确匹配当前页跳转链接（真实 DOM 主路径）。"""
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x',
     title: '星大派特刊：Trump Zone 现象研究报告', topic_type: 'talk',
     content_text: '目 ...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: '',
                         titleLink: 'https://articles.zsxq.com/id_q2.html',
                         body: '标题匹配全文，不经过详情页'}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text, calls}));
})();
"""
    )
    assert out["content"] == "标题匹配全文，不经过详情页"
    assert not any("open:https://wx.zsxq.com/group/15522441811252/topic" in c for c in out["calls"])


def test_detail_anchor_polling_waits_for_spa_render() -> None:
    """详情页锚点 SPA 晚渲染：轮询最多 5 次，取到即用。"""
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x', title: 'A',
     topic_type: 'talk', content_text: '开头...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  let polls = 0;
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: '', titleLink: '',
                         detailLink: () => { polls += 1; return polls >= 3
                           ? 'https://articles.zsxq.com/id_late.html' : ''; },
                         body: '轮询后全文，内容更长一些'}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text, polls}));
})();
"""
    )
    assert out["content"] == "轮询后全文，内容更长一些"
    assert out["polls"] >= 3


def test_detail_body_fallback_when_no_link_anywhere() -> None:
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x', title: 'A',
     topic_type: 'talk', content_text: '开头...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: '', detailLink: '', body: '详情页兜底正文'}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text, calls}));
})();
"""
    )
    assert out["content"] == "详情页兜底正文"


def test_keeps_original_when_candidates_shorter_or_missing() -> None:
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x', title: 'A',
     topic_type: 'talk', content_text: '开头...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: '', detailLink: '', body: ''}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text, calls}));
})();
"""
    )
    assert out["content"] == "开头..."


def test_budget_caps_backfill_attempts_at_five() -> None:
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const topics = [];
  for (let i = 1; i <= 7; i += 1) {
    topics.push({topic_id: 't' + i, legacy_topic_id: 't' + i + '0',
      create_time: 'x', title: 'A', topic_type: 'talk',
      content_text: '开头...', source_class: 'teacher',
      answer_state: 'not_applicable'});
  }
  const parsed = {topics};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: '', detailLink: '', body: ''}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  const linkAttempts = calls.filter((c) => c.startsWith('eval:') && c.includes('finInlineLink')).length;
  console.log(JSON.stringify({linkAttempts}));
})();
"""
    )
    assert out["linkAttempts"] == 5


def test_clean_inline_article_text_disclaimer_semantics() -> None:
    """声明语义与 WSL `_strip_disclaimer_line` 对齐（BUG-027/NOW #19）。

    帖尾保留之前、帖首保留之后、行中内联提及不剥、能量评分行不算实质内容、
    导航壳先剥后头式声明保留之后；其余页脚标记维持首次出现截断。
    """
    out = _run(
        r"""
const m = require('CAPTURE');
const run = (raw) => m.cleanInlineArticleText(raw);
console.log(JSON.stringify({
  tail: run('正文A\n正文B\n免责声明：锅师和助理们不是财务顾问\n页脚残留'),
  head: run('免责声明：锅师和助理们不是财务顾问\n正文A\n正文B'),
  midline: run('开头\n这一句行中提到免责声明字样\n结尾'),
  scoreHead: run('能量评分：7.8\n免责声明：xxx\n正文A'),
  navThenHead: run('来自：星大派\n2026年09月01日\n免责声明：xxx\n正文A\n正文B'),
  otherFooter: run('正文A\n扫码加入星球\n正文B'),
  noDisclaimer: run('正文A\n正文B'),
}));
""".replace('CAPTURE', str(_CAPTURE))
    )
    assert out["tail"] == "正文A\n正文B"
    assert out["head"] == "正文A\n正文B"
    assert out["midline"] == "开头\n这一句行中提到免责声明字样\n结尾"
    assert out["scoreHead"] == "正文A"
    assert out["navThenHead"] == "正文A\n正文B"
    assert out["otherFooter"] == "正文A"
    assert out["noDisclaimer"] == "正文A\n正文B"


def test_head_form_disclaimer_backfills_body() -> None:
    """帖首声明截断帖：回填保留声明后正文（旧行为 indexOf 截空，回填失效）。"""
    out = _run(
        _COMMON_FAKE
        + r"""
(async () => {
  const parsed = {topics: [
    {topic_id: 't1', legacy_topic_id: 't10', create_time: 'x', title: 'A',
     topic_type: 'talk', content_text: '开 ...', source_class: 'teacher',
     answer_state: 'not_applicable'},
  ]};
  await backfillTruncatedInlineArticles(parsed, 'tab1', {
    runOpencli: makeCli({groupLink: 'https://articles.zsxq.com/id_head.html',
                         body: '免责声明：锅师和助理们不是财务顾问\n全文正文很长很长很长很长很长远超截断稿'}),
    decodeEvalStdout: decode,
    sleep: noWait,
  });
  console.log(JSON.stringify({content: parsed.topics[0].content_text}));
})();
"""
    )
    assert out["content"] == "全文正文很长很长很长很长很长远超截断稿"
