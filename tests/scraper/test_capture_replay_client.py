"""CaptureReplayClient 单元测试：录制命中、派生输出、fail-closed 边界。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from fin_analyse.scraper.capture_artifact import load_capture_artifact
from fin_analyse.scraper.capture_replay_client import CaptureReplayClient
from fin_analyse.scraper.cdp_scraper import (
    _EXPAND_DETAILS_SCRIPT,
    _FULL_TEXT_SCRIPT,
    _IMAGES_BY_DATE_SCRIPT,
    _SCROLL_METRICS_SCRIPT,
    _TIMELINE_LOADER_STATE_SCRIPT,
    _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
    TZ,
)
from fin_analyse.scraper.config import GROUP_URL
from tests.scraper.capture_fixtures import build_artifact_payload, content_hash, write_artifact


def _client(tmp_path, payload=None) -> tuple[CaptureReplayClient, object]:

    if payload is None:
        payload = build_artifact_payload(datetime.now(TZ))
    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    client = CaptureReplayClient(artifact)
    assert client.start() is True
    return client, artifact


def test_js_serves_recorded_output_by_script(tmp_path):
    client, _ = _client(tmp_path)
    evidence = client.js(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)
    payload = json.loads(evidence)
    assert len(payload["items"]) == 3


def test_full_text_and_substring_derived(tmp_path):
    client, _ = _client(tmp_path)
    full = client.js(_FULL_TEXT_SCRIPT)
    assert "三线文案大锅饭" in full
    prefix = client.js("document.body.innerText.substring(0, 5000)")
    assert prefix == full[:5000]


def test_cookies_never_served(tmp_path):
    client, _ = _client(tmp_path)
    assert client.js("JSON.stringify(document.cookie)") == ""


def test_unrecorded_script_fails_closed(tmp_path):
    client, _ = _client(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        client.js("console.log('unrecorded')")
    assert "transport_unavailable" in str(excinfo.value)


def test_navigate_normalizes_cache_bust(tmp_path):
    client, _ = _client(tmp_path)
    client.navigate(f"{GROUP_URL}?_fin_ts=9999999")  # 归一化后命中 group 页
    assert client.js(_FULL_TEXT_SCRIPT)


def test_navigate_unrecorded_url_fails_closed(tmp_path):
    client, _ = _client(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        client.navigate("https://wx.zsxq.com/group/15522441811252/topic/1")
    assert "transport_unavailable" in str(excinfo.value)


def test_scroll_by_noop(tmp_path):
    client, _ = _client(tmp_path)
    client.scroll_by(4000, wait=1.0)  # 不 sleep、不抛错
    assert client.js(_SCROLL_METRICS_SCRIPT)  # 仍可继续 serve


def test_batch_execute_ok(tmp_path):
    client, _ = _client(tmp_path)
    batch = client.batch_execute(
        [
            {"action": "navigate", "name": "nav", "url": GROUP_URL, "wait": 1.0},
            {"action": "scroll_by", "name": "scroll", "px": 4000, "repeat": 2, "required": False},
            {"action": "js", "name": "evidence", "script": _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT},
            {"action": "full_text", "name": "body"},
        ]
    )
    assert batch.status == "ok"
    assert batch.result_by_name("evidence")
    assert batch.result_by_name("body")


def test_batch_execute_required_failure_fails_batch(tmp_path):
    client, _ = _client(tmp_path)
    batch = client.batch_execute(
        [
            {"action": "js", "name": "missing", "script": "unknown.script()"},
        ]
    )
    assert batch.status == "failed"
    assert batch.failed_step is not None


def test_batch_execute_optional_failure_partial(tmp_path):
    client, _ = _client(tmp_path)
    batch = client.batch_execute(
        [
            {"action": "js", "name": "missing", "script": "unknown.script()", "required": False},
            {"action": "full_text", "name": "body"},
        ]
    )
    assert batch.status == "partial"
    assert batch.result_by_name("body")


def test_validate_page_state_ok_and_login(tmp_path):
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_artifact_payload(now)
    full_text = payload["pages"][0]["evals"][1]["output"]
    payload["pages"][0]["evals"][1] = {
        "script_sha256": payload["pages"][0]["evals"][1]["script_sha256"],
        "output": "请先登录\n扫码登录\n" + full_text,
    }
    payload["content_sha256"] = content_hash(payload)
    client, _ = _client(tmp_path, payload)
    ok, reason = client.validate_page_state()
    assert ok is False
    assert reason == "login_page"


def test_cursor_pages_served_by_end_time_url_key(tmp_path):
    """topic cursor 页按脚本内 URL 的 end_time 键命中录制输出。"""

    now = datetime.now(TZ)
    payload = build_artifact_payload(now)
    payload["pages"][0]["evals"] = [
        e
        for e in payload["pages"][0]["evals"]
        if e["script_sha256"] != _sha_of(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)
    ]
    payload["pages"][0]["evals"].append(
        {
            "script_sha256": _sha_of(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT),
            "output": '{"schema_version": 1, "items": []}',
        }
    )
    page_one = '{"schema_version":4,"http_status":200,"api_succeeded":true,"api_code":null,"topics":[]}'
    payload["topic_cursor"] = [
        {"end_time": "", "script_sha256": "c" * 64, "output": page_one},
        {
            "end_time": "2026-08-05T14:00:00.000+0800",
            "script_sha256": "d" * 64,
            "output": '{"schema_version":4,"http_status":200,"api_succeeded":true,"api_code":null,"topics":[{"topic_id":"9","legacy_topic_id":"9","create_time":"2026-08-05T14:00:00.000+0800","title":"","topic_type":"talk","content_text":"","source_class":"coverage_only","answer_state":"not_applicable"}]}',
        },
    ]
    payload["content_sha256"] = content_hash(payload)
    client, _ = _client(tmp_path, payload)

    page_one_script = (
        'return await (async function finTopicCursorPage() {\n'
        '  response = await fetch("https://api.zsxq.com/v2/groups/15522441811252/topics?'
        'scope=all&count=30", {credentials: \'include\'});\n})()'
    )
    assert client.js(page_one_script) == page_one

    page_two_script = (
        'return await (async function finTopicCursorPage() {\n'
        '  response = await fetch("https://api.zsxq.com/v2/groups/15522441811252/topics?'
        'scope=all&count=30&end_time=2026-08-05T14%3A00%3A00.000%2B0800", {credentials: \'include\'});\n})()'
    )
    page_two = client.js(page_two_script)
    assert '"topic_id":"9"' in page_two

    # 未录制的 cursor 页 fail-closed
    unrecorded_script = page_two_script.replace("14%3A00", "15%3A00")
    with pytest.raises(RuntimeError) as excinfo:
        client.js(unrecorded_script)
    assert "transport_unavailable" in str(excinfo.value)


def _sha_of(script: str) -> str:
    import hashlib

    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def test_heal_returns_none_fail_closed(tmp_path):
    client, _ = _client(tmp_path)
    assert client.heal_tab_via_new_window(GROUP_URL) is None


def test_loader_and_metrics_scripts_served(tmp_path):
    client, _ = _client(tmp_path)
    assert json.loads(client.js(_TIMELINE_LOADER_STATE_SCRIPT)) == {"visible": False}
    assert json.loads(client.js(_SCROLL_METRICS_SCRIPT))["scrollTop"] == 4000
    assert client.js(_EXPAND_DETAILS_SCRIPT) == "done"
    assert client.js(_IMAGES_BY_DATE_SCRIPT) == "[]"
