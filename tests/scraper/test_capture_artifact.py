"""capture artifact 校验闭集测试：任何缺陷都必须窄错误码 fail-closed。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from fin_analyse.scraper.capture_artifact import (
    CaptureArtifactError,
    content_sha256,
    load_capture_artifact,
)
from fin_analyse.scraper.cdp_scraper import (
    _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
    TZ,
)
from tests.scraper.capture_fixtures import _sha, build_artifact_payload, write_artifact


def _valid_payload() -> dict:
    return build_artifact_payload(datetime.now(TZ))


def _expect_invalid(tmp_path, payload, subcode: str) -> None:
    artifact_path = write_artifact(tmp_path, payload)
    with pytest.raises(CaptureArtifactError) as excinfo:
        load_capture_artifact(artifact_path)
    assert excinfo.value.code == subcode, excinfo.value.code


def test_valid_artifact_loads(tmp_path):
    artifact = load_capture_artifact(write_artifact(tmp_path, _valid_payload()))
    assert artifact.run_id == "123e4567-e89b-12d3-a456-426614174000"
    assert artifact.final_status == "complete"
    assert artifact.failure_reason is None
    assert artifact.group_full_text()
    assert artifact.group_timeline_evidence()


def test_schema_version_mismatch_rejected(tmp_path):
    payload = _valid_payload()
    payload["schema_version"] = "fin.zsxq-capture-artifact/v0"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "schema_version_mismatch")


def test_content_hash_mismatch_rejected(tmp_path):
    payload = _valid_payload()
    payload["window"]["oldest_seen_date"] = "2099-01-01 00:00"  # 不改 hash
    _expect_invalid(tmp_path, payload, "content_hash_mismatch")


def test_run_id_malformed_rejected(tmp_path):
    payload = _valid_payload()
    payload["run_id"] = "not-a-uuid"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "run_id_invalid")


def test_captured_at_naive_rejected(tmp_path):
    payload = _valid_payload()
    payload["captured_at"] = "2026-08-07T10:00:00"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "captured_at_invalid")


def test_cutoff_deviation_rejected(tmp_path):
    payload = _valid_payload()
    payload["window"]["cutoff"] = "2020-01-01 00:00"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "cutoff_deviation")


def test_target_url_mismatch_rejected(tmp_path):
    payload = _valid_payload()
    payload["target"]["url"] = "https://wx.zsxq.com/group/other"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "target_url_mismatch")


def test_final_status_unknown_rejected(tmp_path):
    payload = _valid_payload()
    payload["final_status"] = "bogus"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "final_status_invalid")


def test_failed_without_failure_rejected(tmp_path):
    payload = _valid_payload()
    payload["final_status"] = "failed"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "failure_missing")


def test_failed_unknown_reason_rejected(tmp_path):
    payload = _valid_payload()
    payload["final_status"] = "failed"
    payload["failure"] = {"reason": "bogus", "detail": "x"}
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "failure_reason_invalid")


def test_complete_with_failure_rejected(tmp_path):
    payload = _valid_payload()
    payload["failure"] = {"reason": "login_required", "detail": "x"}
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "failure_unexpected")


def test_window_coverage_not_proven_rejected(tmp_path):
    payload = _valid_payload()
    payload["window"]["oldest_seen_date"] = payload["window"]["cutoff"]
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "window_coverage_incomplete")


def test_credential_key_rejected(tmp_path):
    """eval 记录精确双键（F-01）：未知 sibling 字段在验证层直接拒绝。"""
    payload = _valid_payload()
    payload["pages"][0]["evals"].append(
        {"script_sha256": "c" * 64, "output": "v", "cookie_value": "y"}
    )
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "eval_invalid")


def test_images_whitelist_src_accepted(tmp_path):
    """图片段：白名单签名 URL（token= 签名参数）接受。"""
    from tests.scraper.capture_fixtures import _sha

    payload = _valid_payload()
    from fin_analyse.scraper.cdp_scraper import _IMAGES_BY_DATE_SCRIPT

    images = [
        {
            "src": "https://images.zsxq.com/FiXtestA?e=1790783999&token=abc123",
            "date": "2026-08-06 09:30",
            "index": 0,
        }
    ]
    payload["images"] = images
    payload["pages"][0]["evals"] = [
        e
        for e in payload["pages"][0]["evals"]
        if e["script_sha256"] != _sha(_IMAGES_BY_DATE_SCRIPT)
    ]
    payload["pages"][0]["evals"].append(
        {
            "script_sha256": _sha(_IMAGES_BY_DATE_SCRIPT),
            "output": json.dumps(images, ensure_ascii=False),
        }
    )
    payload["content_sha256"] = content_sha256(payload)
    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    assert len(artifact.pages) == 1


def test_images_src_not_whitelisted_rejected(tmp_path):
    payload = _valid_payload()
    payload["images"] = [
        {
            "src": "https://evil.example.com/x.jpg?token=abc",
            "date": "2026-08-06 09:30",
            "index": 0,
        }
    ]
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "images_invalid")


def test_images_eval_duplicate_keys_rejected(tmp_path):
    """F-01：images eval 内部重复键（可夹带被豁免的凭证值）→ 拒绝。"""
    from tests.scraper.capture_fixtures import _sha

    payload = _valid_payload()
    from fin_analyse.scraper.cdp_scraper import _IMAGES_BY_DATE_SCRIPT

    payload["pages"][0]["evals"] = [
        e
        for e in payload["pages"][0]["evals"]
        if e["script_sha256"] != _sha(_IMAGES_BY_DATE_SCRIPT)
    ]
    dup_output = (
        '[{"src":"token=synthetic","src":"https://images.zsxq.com/A?e=1&token=allowed",'
        '"date":"2026-08-06 09:30","index":0}]'
    )
    payload["pages"][0]["evals"].append(
        {"script_sha256": _sha(_IMAGES_BY_DATE_SCRIPT), "output": dup_output}
    )
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "images_eval_invalid")


def test_images_eval_mismatch_rejected(tmp_path):
    from tests.scraper.capture_fixtures import _sha

    payload = _valid_payload()
    from fin_analyse.scraper.cdp_scraper import _IMAGES_BY_DATE_SCRIPT

    images = [
        {
            "src": "https://images.zsxq.com/FiXtestA?e=1790783999&token=abc123",
            "date": "2026-08-06 09:30",
            "index": 0,
        }
    ]
    payload["images"] = images
    payload["pages"][0]["evals"] = [
        e
        for e in payload["pages"][0]["evals"]
        if e["script_sha256"] != _sha(_IMAGES_BY_DATE_SCRIPT)
    ]
    payload["pages"][0]["evals"].append(
        {"script_sha256": _sha(_IMAGES_BY_DATE_SCRIPT), "output": "[]"}
    )
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "images_eval_mismatch")


def test_credential_token_outside_images_rejected(tmp_path):
    """token= 只豁免 images 段/该 eval；其它字段仍拒绝。"""
    payload = _valid_payload()
    payload["pages"][0]["evals"].append(
        {"script_sha256": "f" * 64, "output": "debug=1&token=abc123"}
    )
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "credential_field_present")


def test_credential_token_in_nested_images_key_rejected(tmp_path):
    """F-01：豁免只限根层 images 键——嵌套任意层级的 images 键仍扫描。"""
    payload = _valid_payload()
    payload["target"]["images"] = "debug=1&token=abc123"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "credential_field_present")


def test_images_eval_with_mismatched_output_still_scanned(tmp_path):
    """F-01：非 group 页携带图片脚本 SHA 且 output 与 images 段不一致 → 仍扫描。

    同页重复 SHA 由 dup-hash 规则先行拦截（eval_invalid）；本测试覆盖跨页场景，
    验证豁免仅限 output 与已校验 images 段一致的记录。
    """
    from tests.scraper.capture_fixtures import _sha

    payload = _valid_payload()
    from fin_analyse.scraper.cdp_scraper import _IMAGES_BY_DATE_SCRIPT

    payload["pages"].append(
        {
            "url": "https://wx.zsxq.com/group/15522441811252/topic/999",
            "evals": [
                {
                    "script_sha256": _sha(_IMAGES_BY_DATE_SCRIPT),
                    "output": "debug=1&token=abc123",
                }
            ],
        }
    )
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "credential_field_present")


def test_images_src_over_2048_rejected(tmp_path):
    """F-02：src 总长 >2048 拒绝（正则内 2000 仅为 query 部分）。"""
    payload = _valid_payload()
    long_src = "https://images.zsxq.com/" + "A" * 64 + "?" + "q" * 1985  # >2048
    payload["images"] = [
        {"src": long_src, "date": "2026-08-06 09:30", "index": 0}
    ]
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "images_invalid")


def test_images_src_astral_chars_byte_bound_rejected(tmp_path):
    """F-02：按 UTF-8 字节计数（astral 字符 code point 少但字节多，双端一致）。"""
    payload = _valid_payload()
    astral_src = "https://images.zsxq.com/A?" + "\U0001f600" * 700  # 2800 bytes
    payload["images"] = [
        {"src": astral_src, "date": "2026-08-06 09:30", "index": 0}
    ]
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "images_invalid")


def test_images_over_cap_rejected(tmp_path):
    """F-03：图片条目超上限（60）拒绝——病态 artifact 不伪装成功。"""
    payload = _valid_payload()
    images = [
        {
            "src": f"https://images.zsxq.com/FiX{i:03d}?e=1790783999&token=abc",
            "date": "2026-08-06 09:30",
            "index": i,
        }
        for i in range(61)
    ]
    payload["images"] = images
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "images_invalid")


def test_group_page_missing_rejected(tmp_path):
    payload = _valid_payload()
    payload["pages"][0]["url"] = "https://wx.zsxq.com/group/15522441811252/topic/1"
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "group_page_missing")


def test_timeline_evidence_missing_rejected(tmp_path):
    payload = _valid_payload()
    payload["pages"][0]["evals"] = payload["pages"][0]["evals"][1:]  # 丢掉 evidence
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "timeline_evidence_missing")


def test_failed_minimal_shape_loads(tmp_path):
    """failed 终态是宽松契约：capture 在拿到 target/pages 前失败也必须可导入。"""
    from datetime import datetime

    now = datetime.now(TZ)
    payload = {
        "schema_version": "fin.zsxq-capture-artifact/v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "captured_at": now.isoformat(),
        "final_status": "failed",
        "failure": {"reason": "target_invalid", "detail": "no target tab"},
    }
    payload["content_sha256"] = content_sha256(payload)
    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    assert artifact.final_status == "failed"
    assert artifact.failure_reason == "target_invalid"


def test_failed_missing_detail_rejected(tmp_path):
    payload = {
        "schema_version": "fin.zsxq-capture-artifact/v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "captured_at": datetime.now(TZ).isoformat(),
        "final_status": "failed",
        "failure": {"reason": "target_invalid"},
    }
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "failure_detail_invalid")


def test_target_url_with_cache_bust_accepted(tmp_path):
    """F-01：cache-bust 残留 _fin_ts 的 target.url 归一化后接受。"""
    payload = _valid_payload()
    payload["target"]["url"] = "https://wx.zsxq.com/group/15522441811252?_fin_ts=1754000000000"
    payload["content_sha256"] = content_sha256(payload)
    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    assert artifact.target_url == "https://wx.zsxq.com/group/15522441811252?_fin_ts=1754000000000"


def test_page_end_coverage_accepted(tmp_path):
    """F-02：page_end 是合法覆盖证据（短页全部内容在窗口内时 oldest 可 ≥ cutoff）。"""
    payload = _valid_payload()
    payload["window"]["oldest_seen_date"] = payload["window"]["cutoff"]  # ≥ cutoff
    payload["window"]["stopped_by_window_boundary"] = False
    payload["window"]["reached_page_end"] = True
    payload["content_sha256"] = content_sha256(payload)
    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    assert artifact.reached_page_end is True


def test_page_end_without_evidence_rejected(tmp_path):
    """F-02：page_end 标记但无任何证据记录 → 拒绝（fail-closed）。"""
    payload = _valid_payload()
    payload["window"]["oldest_seen_date"] = payload["window"]["cutoff"]
    payload["window"]["stopped_by_window_boundary"] = False
    payload["window"]["reached_page_end"] = True
    payload["pages"][0]["evals"] = [
        e for e in payload["pages"][0]["evals"] if e["script_sha256"] != _sha(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)
    ]
    payload["pages"][0]["evals"].append(
        {"script_sha256": _sha(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT), "output": '{"schema_version": 1, "items": []}'}
    )
    payload.pop("topic_cursor", None)
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "window_coverage_incomplete")


def test_credential_value_pattern_rejected(tmp_path):
    """F-05：值级高信号凭证模式（字段名之外的 cookie=… 值）→ 拒绝。"""
    payload = _valid_payload()
    payload["failure"] = None
    payload["pages"][0]["evals"].append(
        {"script_sha256": "e" * 64, "output": "cookie=abc123; session=x"}
    )
    payload["content_sha256"] = content_sha256(payload)
    _expect_invalid(tmp_path, payload, "credential_field_present")


def test_credential_value_patterns_session_credential_bearer_rejected(tmp_path):
    """F-05：session=/credential:/Bearer token 值模式全部拒绝。"""
    for value in ("session=abc123", "credential: xyz", "Authorization: Bearer abc123"):
        payload = _valid_payload()
        payload["pages"][0]["evals"].append(
            {"script_sha256": "e" * 64, "output": value}
        )
        payload["content_sha256"] = content_sha256(payload)
        _expect_invalid(tmp_path, payload, "credential_field_present")


def test_duplicate_json_key_rejected(tmp_path):
    artifact_path = write_artifact(tmp_path, _valid_payload())
    text = artifact_path.read_text(encoding="utf-8")
    # 注入重复顶层键（合法 JSON 值）→ 必须拒绝
    artifact_path.write_text(text[:-1] + ', "run_id": "dup"}', encoding="utf-8")
    with pytest.raises(CaptureArtifactError) as excinfo:
        load_capture_artifact(artifact_path)
    assert excinfo.value.code == "duplicate_json_key"


def test_json_invalid_rejected(tmp_path):
    artifact_path = write_artifact(tmp_path, _valid_payload())
    artifact_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CaptureArtifactError) as excinfo:
        load_capture_artifact(artifact_path)
    assert excinfo.value.code == "json_invalid"


def test_content_hash_escapes_del_character_like_js_canonicalize(tmp_path):
    """0x7F (DEL) 必须转义为 \\u007f，与 capture JS jsonEscapeString 对齐。

    2026-08-12 20:20 轮失败根因：JS 对 0x7F 保留原始字节、Python 转义为
    \\u007f，两侧 canonicalize 不一致导致 content_hash_mismatch。JS 侧已
    修复；本测试冻结 Python 侧语义防止回归。
    """
    payload = _valid_payload()
    body = "题外话JH：又英维克？"
    # 把 0x7F 放进 FULL_TEXT eval 输出（group_full_text 来源）
    payload["pages"][0]["evals"][1]["output"] = body
    payload["content_sha256"] = content_sha256(payload)

    canonical = json.dumps(
        {k: v for k, v in payload.items() if k != "content_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert "\\u007f\\u007f" in canonical

    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    assert artifact.final_status == "complete"


def test_del_character_in_body_roundtrips_through_hash_and_load(tmp_path):
    """含 DEL 的 artifact：hash 用 \\u007f 转义计算且 load 校验通过。"""
    payload = _valid_payload()
    payload["pages"][0]["evals"][1]["output"] = "前缀后缀"
    payload["content_sha256"] = content_sha256(payload)

    artifact = load_capture_artifact(write_artifact(tmp_path, payload))
    assert "" in artifact.group_full_text()
