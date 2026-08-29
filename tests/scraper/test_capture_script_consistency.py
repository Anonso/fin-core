"""Windows capture 脚本与 WSL 侧的一致性门：脚本逐字节一致 + canonical hash 跨语言等价。

漂移修复：从 fin_analyse.scraper.cdp_scraper 常量重新注入 EMBEDDED_SCRIPTS：
    python -c "import json,pathlib; from fin_analyse.scraper.cdp_scraper import _EXPAND_DETAILS_SCRIPT as e, _FULL_TEXT_SCRIPT as f, _IMAGES_BY_DATE_SCRIPT as i, _SCROLL_METRICS_SCRIPT as s, _TIMELINE_LOADER_STATE_SCRIPT as l, _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT as t; p=pathlib.Path('scripts/capture_zsxq_windows.cjs'); x=p.read_text(encoding='utf-8'); import re; x=re.sub(r'const EMBEDDED_SCRIPTS = \\{.*?\\};\\n', 'const EMBEDDED_SCRIPTS = '+json.dumps({'timeline_evidence':t,'loader_state':l,'scroll_metrics':s,'full_text':f,'images':i,'expand':e,'body_substring':'document.body.innerText.substring(0, 5000)'},ensure_ascii=False,indent=2)+';\\n', x, count=1, flags=re.S); p.write_text(x, encoding='utf-8')"
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from fin_analyse.scraper.capture_artifact import (
    CaptureArtifactError,
    content_sha256,
    load_capture_artifact,
)
from fin_analyse.scraper.cdp_scraper import (
    _EXPAND_DETAILS_SCRIPT,
    _FULL_TEXT_SCRIPT,
    _IMAGES_BY_DATE_SCRIPT,
    _SCROLL_METRICS_SCRIPT,
    _TIMELINE_LOADER_STATE_SCRIPT,
    _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
)

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "capture_zsxq_windows.cjs"

_EXPECTED = {
    "timeline_evidence": _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
    "loader_state": _TIMELINE_LOADER_STATE_SCRIPT,
    "scroll_metrics": _SCROLL_METRICS_SCRIPT,
    "full_text": _FULL_TEXT_SCRIPT,
    "images": _IMAGES_BY_DATE_SCRIPT,
    "expand": _EXPAND_DETAILS_SCRIPT,
    "body_substring": "document.body.innerText.substring(0, 5000)",
}


def _extract_embedded_scripts() -> dict[str, str]:
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    marker = "const EMBEDDED_SCRIPTS = "
    start = text.index(marker) + len(marker)
    decoded, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(decoded, dict):
        raise AssertionError("EMBEDDED_SCRIPTS is not an object")
    return {str(k): str(v) for k, v in decoded.items()}


def test_embedded_scripts_byte_identical_to_repo_constants():
    embedded = _extract_embedded_scripts()
    assert set(embedded) == set(_EXPECTED)
    for identity, expected in _EXPECTED.items():
        assert embedded[identity] == expected, (
            f"script {identity!r} drifted from repo constant — 重新注入 EMBEDDED_SCRIPTS"
        )
        assert hashlib.sha256(embedded[identity].encode("utf-8")).hexdigest() == hashlib.sha256(
            expected.encode("utf-8")
        ).hexdigest()


@pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="node 不可用（仅开发机）",
)
def test_canonical_hash_matches_python_across_languages():
    """JS canonicalize 与 Python content_sha256 对同一 payload 产出相同 hash。"""
    sample = {
        "schema_version": "fin.zsxq-capture-artifact/v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "captured_at": "2026-08-07T10:00:00.000+08:00",
        "target": {"url": "https://wx.zsxq.com/group/15522441811252", "title": "大锅饭与小伙伴的进步空间-知识星球", "tab_id": "4CC06B3821D91F70A8ADED8DD78E65C1"},
        "window": {"days": 3, "cutoff": "2026-08-04T10:00:00", "oldest_seen_date": "2026-08-03T08:00:00", "stopped_by_window_boundary": True, "reached_page_end": False},
        "login_state": {"login_surface_present": False, "challenge_present": False, "rate_limit_present": False},
        "pages": [
            {
                "url": "https://wx.zsxq.com/group/15522441811252?_fin_ts=1754000000000",
                "evals": [
                    {"script_sha256": "a" * 64, "output": "{\"schema_version\": 1, \"items\": [{\"topic_id\": \"600000000000001\", \"header_lines\": [\"三线文案大锅饭\", \"2026-08-06 09:30\"], \"timestamps\": [\"2026-08-06 09:30\"]}]}"},
                    {
                        "script_sha256": "b" * 64,
                        "output": "正文中文内容\n第二行 with \\n escaped\u007f\u007f",
                    },
                ],
            }
        ],
        "images": [],
        "final_status": "complete",
    }
    expected = content_sha256(sample)
    node_script = (
        "const m=require(process.argv[1]);"
        "process.stdout.write(m.canonicalize(JSON.parse(process.argv[2])))"
    )
    completed = subprocess.run(
        [
            "node",
            "-e",
            node_script,
            str(_SCRIPT_PATH),
            json.dumps(sample, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    canonical = completed.stdout.decode("utf-8")
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == expected


_IMAGES_SHA = hashlib.sha256(_IMAGES_BY_DATE_SCRIPT.encode("utf-8")).hexdigest()
_NODE_AVAILABLE = subprocess.run(["node", "--version"], capture_output=True).returncode == 0

_IMAGES_EVAL_OUTPUT = json.dumps(
    [
        {
            "src": "https://images.zsxq.com/FiXtestA?e=1790783999&token=abc123",
            "date": "2026-08-06 09:30",
            "index": 0,
        }
    ],
    ensure_ascii=False,
)


def _node_stdout(expr: str, payload: dict) -> str:
    """node -e 调用 cjs 模块导出函数，返回 stdout。"""
    node_script = f"const m=require(process.argv[1]);{expr}"
    completed = subprocess.run(
        ["node", "-e", node_script, str(_SCRIPT_PATH), json.dumps(payload, ensure_ascii=False)],
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout.decode("utf-8")


def _scrub_via_node(payload: dict) -> dict:
    return json.loads(
        _node_stdout(
            "process.stdout.write(JSON.stringify(m.scrubFailedArtifact(JSON.parse(process.argv[2]))))",
            payload,
        )
    )


@pytest.mark.skipif(not _NODE_AVAILABLE, reason="node 不可用（仅开发机）")
def test_scrub_failed_artifact_drops_only_images_eval():
    """失败路径 scrub：剔除 images eval（token= 签名 URL 载体），保留其余 eval 诊断。"""
    payload = {
        "pages": [
            {
                "url": "https://wx.zsxq.com/group/15522441811252",
                "evals": [
                    {"script_sha256": "e" * 64, "output": "诊断全文"},
                    {"script_sha256": _IMAGES_SHA, "output": _IMAGES_EVAL_OUTPUT},
                ],
            }
        ]
    }
    result = _scrub_via_node(payload)
    evals = result["pages"][0]["evals"]
    assert [e["script_sha256"] for e in evals] == ["e" * 64]


@pytest.mark.skipif(not _NODE_AVAILABLE, reason="node 不可用（仅开发机）")
def test_scrubbed_failed_artifact_loads_as_precise_failure(tmp_path):
    """JS 失败路径产物（scrub 后）必须被 WSL failed 契约接受为精确 failed。"""
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = {
        "schema_version": "fin.zsxq-capture-artifact/v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "captured_at": now.isoformat(),
        "capture_host": "WIN-PC",
        "final_status": "failed",
        "failure": {
            "reason": "window_coverage_incomplete",
            "detail": "cursor coverage unproven（cursor_api_rejected(http=200,code=1059)）",
        },
        # JS 失败路径真实产物：步骤 4 在门禁前记录 pages（含 images eval）
        "pages": [
            {
                "url": "https://wx.zsxq.com/group/15522441811252?_fin_ts=1754000000000",
                "evals": [{"script_sha256": _IMAGES_SHA, "output": _IMAGES_EVAL_OUTPUT}],
            }
        ],
    }
    scrubbed = _scrub_via_node(payload)
    scrubbed["content_sha256"] = content_sha256(scrubbed)
    artifact_path = tmp_path / "failed.json"
    artifact_path.write_text(json.dumps(scrubbed, ensure_ascii=False), encoding="utf-8")
    artifact = load_capture_artifact(artifact_path)
    assert artifact.final_status == "failed"
    assert artifact.failure_reason == "window_coverage_incomplete"


def test_unscrubbed_failed_artifact_rejected_as_credential(tmp_path):
    """未 scrub 的 failed artifact（带 images eval）被 WSL 误拒 credential_field_present。

    这是本修复的动因（WSL failed 分支无 images 豁免，属冻结契约，不改 WSL 侧）；
    该断言同时作为 tripwire：若 WSL 侧将来改变豁免语义，此测试显式失败需重审。
    """
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = {
        "schema_version": "fin.zsxq-capture-artifact/v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "captured_at": now.isoformat(),
        "capture_host": "WIN-PC",
        "final_status": "failed",
        "failure": {
            "reason": "window_coverage_incomplete",
            "detail": "cursor coverage unproven",
        },
        "pages": [
            {
                "url": "https://wx.zsxq.com/group/15522441811252?_fin_ts=1754000000000",
                "evals": [{"script_sha256": _IMAGES_SHA, "output": _IMAGES_EVAL_OUTPUT}],
            }
        ],
    }
    payload["content_sha256"] = content_sha256(payload)
    artifact_path = tmp_path / "unscrubbed.json"
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CaptureArtifactError) as excinfo:
        load_capture_artifact(artifact_path)
    assert excinfo.value.code == "credential_field_present"


def test_scrub_called_from_failure_handler():
    """catch 唯一调用点：保证脚本失败路径真实调用 scrub（不是仅导出未接线）。"""
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert text.count("scrubFailedArtifact(payload);") == 1
