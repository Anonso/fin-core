"""ZSXQ capture artifact 测试 fixture 构造器（与 capture 侧 canonical hash 一致）。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from fin_analyse.scraper.cdp_scraper import (
    _EXPAND_DETAILS_SCRIPT,
    _FULL_TEXT_SCRIPT,
    _IMAGES_BY_DATE_SCRIPT,
    _SCROLL_METRICS_SCRIPT,
    _TIMELINE_LOADER_STATE_SCRIPT,
    _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
)
from fin_analyse.scraper.config import GROUP_URL

AUTHOR = "三线文案大锅饭"


def _sha(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _post_part(date_str: str, title: str, body: str) -> str:
    return f"{date_str}\n{title}\n{body}\n"


def content_hash(payload: dict) -> str:
    """与 capture_artifact.content_sha256 同形式（JS canonicalize 等价）。"""
    canonical = json.dumps(
        {k: v for k, v in payload.items() if k != "content_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_artifact_payload(now) -> dict:
    """构造一份完整合法的 capture artifact（相对真实时间，窗口恒有效）。"""
    d1 = (now - timedelta(days=1)).replace(hour=9, minute=30)
    d2 = (now - timedelta(days=2)).replace(hour=14, minute=0)
    d3 = (now - timedelta(days=4)).replace(hour=8, minute=0)  # 窗口外，证明覆盖
    cutoff = now - timedelta(days=3)

    p1_title = "提问：新能源车渗透率还能提升多少？"
    p1_lines = [
        "新能源车渗透率的爬坡节奏，核心看电池成本与充电基建的匹配。",
        "明年大概率仍是插混放量、纯电增速回落的格局，这个判断我维持不变。",
        "补贴退坡之后，真实需求的结构变化比总量更重要，市场容易线性外推。",
    ]
    p1_body = "\n".join(p1_lines * 2)  # 每段 >200 字符，保证 _split_by_author 切分
    p2_title = "半导体设备国产替代的节奏观察"
    p2_lines = [
        "能量评分 9.2 分",
        "设备端的国产替代不是匀速推进，而是按环节分批验证。",
        "前道光刻最难，量检测与清洗先兑现，市场给的估值已经包含这一预期差。",
        "跟踪订单与验证进度，比跟踪股价位置更重要。",
    ]
    p2_body = "\n".join(p2_lines * 2)
    p3_title = "旧闻测试标题"
    p3_lines = [
        "这是一条窗口外的旧内容，用于证明三天覆盖边界。",
        "它不应被保存进知识库，因为日期早于 cutoff。",
        "但它的存在证明了滚动已越过窗口边界。",
    ]
    p3_body = "\n".join(p3_lines * 3)

    d1s = d1.strftime("%Y-%m-%d %H:%M")
    d2s = d2.strftime("%Y-%m-%d %H:%M")
    d3s = d3.strftime("%Y-%m-%d %H:%M")

    full_text = "\n".join(
        [
            "大锅饭与小伙伴的进步空间",
            "知识星球",
            AUTHOR,
            _post_part(d1s, p1_title, p1_body),
            AUTHOR,
            _post_part(d2s, p2_title, p2_body),
            AUTHOR,
            _post_part(d3s, p3_title, p3_body),
        ]
    )

    timeline_evidence = json.dumps(
        {
            "schema_version": 1,
            "items": [
                {
                    "topic_id": "600000000000001",
                    "header_lines": [AUTHOR, d1s, p1_title],
                    "timestamps": [d1s],
                },
                {
                    "topic_id": "600000000000002",
                    "header_lines": [AUTHOR, d2s, p2_title],
                    "timestamps": [d2s],
                },
                {
                    "topic_id": "600000000000003",
                    "header_lines": [AUTHOR, d3s, p3_title],
                    "timestamps": [d3s],
                },
            ],
        },
        ensure_ascii=False,
    )

    payload = {
        "schema_version": "fin.zsxq-capture-artifact/v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "captured_at": now.isoformat(),
        "capture_host": "test-host",
        "target": {
            "url": GROUP_URL,
            "title": "大锅饭与小伙伴的进步空间-知识星球",
            "tab_id": "4CC06B3821D91F70A8ADED8DD78E65C1",
        },
        "window": {
            "days": 3,
            "cutoff": cutoff.strftime("%Y-%m-%d %H:%M"),
            "oldest_seen_date": d3s,
            "stopped_by_window_boundary": True,
            "reached_page_end": False,
        },
        "login_state": {
            "login_surface_present": False,
            "challenge_present": False,
            "rate_limit_present": False,
        },
        "pages": [
            {
                "url": f"{GROUP_URL}?_fin_ts=1754000000000",
                "evals": [
                    {
                        "script_sha256": _sha(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT),
                        "output": timeline_evidence,
                    },
                    {"script_sha256": _sha(_FULL_TEXT_SCRIPT), "output": full_text},
                    {"script_sha256": _sha(_IMAGES_BY_DATE_SCRIPT), "output": "[]"},
                    {"script_sha256": _sha(_EXPAND_DETAILS_SCRIPT), "output": "done"},
                    {
                        "script_sha256": _sha(_TIMELINE_LOADER_STATE_SCRIPT),
                        "output": '{"visible": false}',
                    },
                    {
                        "script_sha256": _sha(_SCROLL_METRICS_SCRIPT),
                        "output": '{"scrollTop": 4000, "clientHeight": 900, "scrollHeight": 5000}',
                    },
                ],
            }
        ],
        "images": [],
        "final_status": "complete",
    }
    payload["content_sha256"] = content_hash(payload)
    return payload


def build_cursor_artifact_payload(now) -> dict:
    """构造 DOM 证据为空、依赖 native topic cursor 的 artifact（当前页面真实形态）。"""
    payload = build_artifact_payload(now)
    # DOM 证据空 items（当前页面 data-topic-id 为 0 的真实形态）
    payload["pages"][0]["evals"] = [
        e
        for e in payload["pages"][0]["evals"]
        if e["script_sha256"] != _sha(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)
    ]
    payload["pages"][0]["evals"].append(
        {"script_sha256": _sha(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT), "output": '{"schema_version": 1, "items": []}'}
    )

    def _iso(dt) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0800")

    d1 = (now - timedelta(days=1)).replace(hour=9, minute=30)
    d2 = (now - timedelta(days=2)).replace(hour=14, minute=0)
    d3 = (now - timedelta(days=4)).replace(hour=8, minute=0)

    cursor_page = {
        "schema_version": 4,
        "http_status": 200,
        "api_succeeded": True,
        "api_code": None,
        "topics": [
            {
                "topic_id": "700000000000001",
                "legacy_topic_id": "1",
                "create_time": _iso(d1),
                "title": "半导体设备国产替代的节奏观察",
                "topic_type": "talk",
                "content_text": "\n".join(
                    [
                        "能量评分 9.2 分",
                        "设备端的国产替代不是匀速推进，而是按环节分批验证。",
                        "前道光刻最难，量检测与清洗先兑现，市场给的估值已经包含这一预期差。",
                        "跟踪订单与验证进度，比跟踪股价位置更重要。",
                        "设备端的国产替代不是匀速推进，而是按环节分批验证。",
                        "前道光刻最难，量检测与清洗先兑现，市场给的估值已经包含这一预期差。",
                    ]
                ),
                "source_class": "teacher",
                "answer_state": "not_applicable",
            },
            {
                "topic_id": "700000000000002",
                "legacy_topic_id": "2",
                "create_time": _iso(d2),
                "title": "",
                "topic_type": "q&a",
                "content_text": "提问：新能源车渗透率还能提升多少？\n新能源车渗透率的爬坡节奏，核心看电池成本与充电基建的匹配。\n明年大概率仍是插混放量、纯电增速回落的格局，这个判断我维持不变。\n补贴退坡之后，真实需求的结构变化比总量更重要，市场容易线性外推。\n明年大概率仍是插混放量、纯电增速回落的格局，这个判断我维持不变。",
                "source_class": "teacher",
                "answer_state": "answered",
            },
            {
                "topic_id": "700000000000003",
                "legacy_topic_id": "3",
                "create_time": _iso(d3),
                "title": "",
                "topic_type": "talk",
                "content_text": "",
                "source_class": "coverage_only",
                "answer_state": "not_applicable",
            },
        ],
    }
    payload["topic_cursor"] = [
        {"end_time": "", "script_sha256": "c" * 64, "output": json.dumps(cursor_page, ensure_ascii=False)}
    ]
    payload["window"]["oldest_seen_date"] = d3.strftime("%Y-%m-%d %H:%M")
    payload["content_sha256"] = content_hash(payload)
    return payload


def build_image_artifact_payload(now) -> dict:
    """含 2 个白名单图片条目的 artifact（date 与窗口内 post 日期一致）。"""
    payload = build_artifact_payload(now)
    d1 = (now - timedelta(days=1)).replace(hour=9, minute=30)
    d1s = d1.strftime("%Y-%m-%d %H:%M")
    images = [
        {
            "src": f"https://images.zsxq.com/FiXtestA?e=1790783999&token={'a' * 20}",
            "date": d1s,
            "index": 0,
        },
        {
            "src": f"https://images.zsxq.com/FiXtestB?e=1790783999&token={'b' * 20}",
            "date": d1s,
            "index": 1,
        },
    ]
    images_json = json.dumps(images, ensure_ascii=False)
    payload["images"] = images
    payload["pages"][0]["evals"] = [
        e
        for e in payload["pages"][0]["evals"]
        if e["script_sha256"] != _sha(_IMAGES_BY_DATE_SCRIPT)
    ]
    payload["pages"][0]["evals"].append(
        {"script_sha256": _sha(_IMAGES_BY_DATE_SCRIPT), "output": images_json}
    )
    payload["content_sha256"] = content_hash(payload)
    return payload


def build_page_end_artifact_payload(now) -> dict:
    """短页形态：cursor 页 <30 topics、全部在窗口内（page_end 覆盖，oldest ≥ cutoff）。"""
    payload = build_cursor_artifact_payload(now)
    d1 = (now - timedelta(days=1)).replace(hour=9, minute=30)
    cursor = json.loads(payload["topic_cursor"][0]["output"])
    # 只保留 2 个窗口内 teacher topics，无 cutoff 穿越 topic
    cursor["topics"] = cursor["topics"][:2]
    cursor["topics"][1]["create_time"] = d1.strftime("%Y-%m-%dT%H:%M:%S.000+0800")
    payload["topic_cursor"] = [
        {"end_time": "", "script_sha256": "c" * 64, "output": json.dumps(cursor, ensure_ascii=False)}
    ]
    payload["window"]["oldest_seen_date"] = d1.strftime("%Y-%m-%d %H:%M")  # ≥ cutoff
    payload["window"]["stopped_by_window_boundary"] = False
    payload["window"]["reached_page_end"] = True
    payload["content_sha256"] = content_hash(payload)
    return payload


def write_artifact(tmp_path: Path, payload: dict) -> Path:
    artifact_path = tmp_path / "handoff" / "capture.latest.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return artifact_path
