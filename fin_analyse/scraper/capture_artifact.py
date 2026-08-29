"""fin.zsxq-capture-artifact/v1 解析与校验（WSL ingest 侧 fail-closed 入口）。

Windows 原生 capture 产出的 artifact 是本 slice 的唯一运输载体：run_id、时间、
3 天 cutoff、覆盖/终态证据、内容 hash 全部在此校验；凭证类字段在此拒绝。
校验失败的窄错误码由 CLI 原样暴露（capture_artifact_invalid:<subcode>）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .cdp_scraper import (
    _FULL_TEXT_SCRIPT,
    _IMAGES_BY_DATE_SCRIPT,
    _TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT,
)
from .config import GROUP_URL

SCHEMA_VERSION = "fin.zsxq-capture-artifact/v1"
_CAPTURE_WINDOW_DAYS = 3
_CUTOFF_TOLERANCE = timedelta(hours=2)

_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_KEY_RE = re.compile(
    r"cookie|token|secret|password|credential|authorization|set-cookie|session",
    re.IGNORECASE,
)
#: F-05：值级高信号凭证模式（字段名扫描的补充；fail-closed）
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?:cookie|token|secret|password|authorization|bearer|set-cookie|session|credential)"
    r"\s*[=:]\s*\S",
    re.IGNORECASE,
)
_FINAL_STATUSES = frozenset({"complete", "partial", "failed"})
_FAILURE_REASONS = frozenset(
    {
        "target_invalid",
        "login_required",
        "transport_unavailable",
        "window_coverage_incomplete",
        "content_insufficient",
        "unknown",
    }
)
_MAX_PAGES = 32
_MAX_EVALS_PER_PAGE = 64
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_CAPTURE_ARTIFACT_BYTES = 40 * 1024 * 1024
#: 图片条目上限与 src 白名单（images.zsxq.com 签名 URL；token= 是时限性单图访问
#: 能力而非账户凭证——白名单校验通过后豁免通用值级扫描）。上限收紧到 60：真实页面
#: ~19-30 张；病态 artifact（海量不可达图片）会如实 deadline_exceeded/FAILED 且
#: G 不刷新（fail-closed 后盾），不伪装成成功。
_MAX_IMAGES = 60
_IMAGES_SRC_RE = re.compile(
    r"^https://images\.zsxq\.com/[A-Za-z0-9_-]{1,64}(?:\?[^\s\"<>]{0,2000})?$"
)


class CaptureArtifactError(RuntimeError):
    """Fail-closed artifact rejection with a narrow, stable error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"capture_artifact_invalid:{code}")


@dataclass(frozen=True)
class CaptureEval:
    script_sha256: str
    output: str


@dataclass(frozen=True)
class CapturePage:
    url: str
    evals: tuple[CaptureEval, ...]

    def output_for(self, script: str) -> str | None:
        want = sha256_hex(script)
        for eval_record in self.evals:
            if eval_record.script_sha256 == want:
                return eval_record.output
        return None


@dataclass(frozen=True)
class CaptureCursorPage:
    """One native topic-cursor page record（end_time 为 URL 键，空串=首页）。"""

    end_time: str
    script_sha256: str
    output: str


@dataclass(frozen=True)
class CaptureArtifact:
    run_id: str
    captured_at: datetime
    capture_host: str
    target_url: str
    target_title: str
    target_tab_id: str
    window_days: int
    cutoff: datetime
    oldest_seen_date: datetime | None
    stopped_by_window_boundary: bool
    reached_page_end: bool
    login_state: dict[str, bool]
    pages: tuple[CapturePage, ...]
    cursor_pages: tuple[CaptureCursorPage, ...]
    final_status: str
    failure_reason: str | None
    content_sha256: str

    def group_page(self) -> CapturePage | None:
        """Return the group timeline page record (URL 归一化匹配，忽略 _fin_ts）。"""
        for page in self.pages:
            if _normalize_url(page.url) == _normalize_url(GROUP_URL):
                return page
        return None

    def group_full_text(self) -> str | None:
        page = self.group_page()
        if page is None:
            return None
        return page.output_for(_FULL_TEXT_SCRIPT)

    def group_timeline_evidence(self) -> str | None:
        page = self.group_page()
        if page is None:
            return None
        return page.output_for(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_IMAGES_EVAL_SHA = sha256_hex(_IMAGES_BY_DATE_SCRIPT)


def content_sha256(payload: dict[str, Any]) -> str:
    """Canonical content hash，与 Windows capture 侧 canonicalize 逐字节一致。

    形式固定为：递归 key 排序 + 无空格分隔符 + 非 ASCII 转义为 \\uXXXX
    （等价于 JS ``JSON.stringify`` 语义），排除自身字段。
    """
    canonical = json.dumps(
        {k: v for k, v in payload.items() if k != "content_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_url(url: str) -> str:
    """归一化导航 URL：剥离 _fin_ts cache-bust 参数（仅本参数，其它保留）。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    scheme, netloc, path, query, fragment = urlsplit(url)
    if not query:
        return url
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "_fin_ts"]
    normalized = urlunsplit((scheme, netloc, path, urlencode(kept), fragment))
    return normalized


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CaptureArtifactError(code)


def _as_str(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise CaptureArtifactError(code)
    return value


def _as_dict(value: object, code: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise CaptureArtifactError(code)
    return value


def _parse_wall_time(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise CaptureArtifactError(code)
    for candidate in (value, value.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        return parsed
    raise CaptureArtifactError(code)


def _parse_window_time(value: object, tz: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise CaptureArtifactError(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        raise CaptureArtifactError(code) from None
    return parsed.replace(tzinfo=tz)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise CaptureArtifactError("duplicate_json_key")
        decoded[key] = value
    return decoded


def _output_matches_images(value: dict[Any, Any], images: list[Any]) -> bool:
    """images eval 记录的 output 是否与已校验 images 段逐项一致。

    F-01：以重复键拒绝钩子解析——内部重复键可夹带被豁免的凭证值（JSON 保留末值
    与根 images 相等，但原始字符串含 token=）。重复键 → 视为不一致 → 该 output
    不豁免，继续扫描并拒绝。
    """
    try:
        decoded = json.loads(
            str(value.get("output") or ""),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, TypeError, ValueError, CaptureArtifactError):
        return False
    return isinstance(decoded, list) and decoded == images


def _evidence_has_items(raw: str | None) -> bool:
    """时间线证据是否含非空 items（page_end 覆盖的伴随证据判定）。"""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    items = payload.get("items")
    return isinstance(items, list) and bool(items)


def _scan_credentials(value: Any, *, images: list[Any] | None = None, root: bool = False) -> None:
    """递归扫描：凭证类键名或值级高信号凭证模式直接拒绝（fail-closed）。

    豁免面严格受限（F-01）：仅根层 ``images`` 键（其内容已逐项白名单校验）与
    output 与已校验 images 段逐项一致的 images eval 记录；嵌套任意层级的 ``images``
    键、或携带图片脚本 SHA 但 output 不一致的 dict，一律继续扫描。
    """
    if isinstance(value, dict):
        # F-01：豁免只跳过 output 值（其内容 == 已校验 images 段）；dict 的其它
        # sibling 字段（验证层已强制精确双键，此处为纵深防御）仍继续扫描。
        exempt_output = (
            images is not None
            and value.get("script_sha256") == _IMAGES_EVAL_SHA
            and _output_matches_images(value, images)
        )
        for key, child in value.items():
            if isinstance(key, str) and _CREDENTIAL_KEY_RE.search(key):
                raise CaptureArtifactError("credential_field_present")
            if exempt_output and key == "output":
                continue
            if root and key == "images":
                continue
            _scan_credentials(child, images=images, root=False)
    elif isinstance(value, list):
        for child in value:
            _scan_credentials(child, images=images, root=False)
    elif isinstance(value, str) and _CREDENTIAL_VALUE_RE.search(value):
        raise CaptureArtifactError("credential_field_present")


def load_capture_artifact(path: str | Path) -> CaptureArtifact:
    """Parse and validate one artifact file; raise CaptureArtifactError on any defect."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CaptureArtifactError("file_unreadable") from error
    return parse_capture_artifact(raw)


def parse_capture_artifact(raw: bytes) -> CaptureArtifact:
    """Parse one immutable raw payload without reopening a replaceable path."""
    if len(raw) > MAX_CAPTURE_ARTIFACT_BYTES:
        raise CaptureArtifactError("file_oversized")
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except CaptureArtifactError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise CaptureArtifactError("json_invalid") from error
    if not isinstance(payload, dict):
        raise CaptureArtifactError("root_not_object")

    _require(payload.get("schema_version") == SCHEMA_VERSION, "schema_version_mismatch")
    _require(isinstance(payload.get("content_sha256"), str), "content_hash_missing")
    _require(
        payload["content_sha256"] == content_sha256(payload),
        "content_hash_mismatch",
    )

    run_id = _as_str(payload.get("run_id"), "run_id_invalid")
    _require(_RUN_ID_RE.fullmatch(run_id) is not None, "run_id_invalid")

    captured_at = _parse_wall_time(payload.get("captured_at"), "captured_at_invalid")

    # failed 终态是宽松契约：capture 可能在拿到 target/window/pages 前失败，
    # 只要求 schema/run_id/时间/hash/failure 齐备即可精确 FAILED（不刷新 G）。
    final_status = _as_str(payload.get("final_status"), "final_status_invalid")
    _require(final_status in _FINAL_STATUSES, "final_status_invalid")
    failure = payload.get("failure")
    if final_status == "failed":
        failure_dict = _as_dict(failure, "failure_missing")
        reason = _as_str(failure_dict.get("reason"), "failure_reason_invalid")
        detail = _as_str(failure_dict.get("detail"), "failure_detail_invalid")
        _require(reason in _FAILURE_REASONS, "failure_reason_invalid")
        _require(len(detail) <= 500, "failure_detail_invalid")
        _scan_credentials(payload)
        return CaptureArtifact(
            run_id=run_id,
            captured_at=captured_at,
            capture_host=str(payload.get("capture_host") or ""),
            target_url="",
            target_title="",
            target_tab_id="",
            window_days=0,
            cutoff=captured_at,
            oldest_seen_date=None,
            stopped_by_window_boundary=False,
            reached_page_end=False,
            login_state={},
            pages=(),
            cursor_pages=(),
            final_status="failed",
            failure_reason=reason,
            content_sha256=payload["content_sha256"],
        )
    _require(failure is None, "failure_unexpected")

    target = _as_dict(payload.get("target"), "target_invalid")
    # F-01：cache-bust 后 tab URL 可能残留 _fin_ts —— 归一化后比较（capture 侧也写归一化值）
    _require(
        _normalize_url(_as_str(target.get("url"), "target_url_mismatch")) == GROUP_URL,
        "target_url_mismatch",
    )
    target_title = _as_str(target.get("title"), "target_title_invalid")
    _require(1 <= len(target_title) <= 200, "target_title_invalid")
    target_tab_id = _as_str(target.get("tab_id"), "target_tab_id_invalid")
    _require(1 <= len(target_tab_id) <= 64, "target_tab_id_invalid")

    window = _as_dict(payload.get("window"), "window_invalid")
    _require(window.get("days") == _CAPTURE_WINDOW_DAYS, "window_days_invalid")
    cutoff = _parse_window_time(window.get("cutoff"), captured_at.tzinfo, "cutoff_invalid")
    _require(
        abs((captured_at - cutoff) - timedelta(days=_CAPTURE_WINDOW_DAYS)) <= _CUTOFF_TOLERANCE,
        "cutoff_deviation",
    )
    oldest_value = window.get("oldest_seen_date")
    oldest_seen: datetime | None = None
    if oldest_value is not None and oldest_value != "":
        oldest_seen = _parse_window_time(
            _as_str(oldest_value, "oldest_seen_date_invalid"),
            captured_at.tzinfo,
            "oldest_seen_date_invalid",
        )
    stopped_by_boundary = window.get("stopped_by_window_boundary")
    reached_page_end = window.get("reached_page_end")
    if type(stopped_by_boundary) is not bool or type(reached_page_end) is not bool:
        raise CaptureArtifactError("window_flags_invalid")

    login_state = _as_dict(payload.get("login_state"), "login_state_invalid")
    _require(
        set(login_state) <= {"login_surface_present", "challenge_present", "rate_limit_present"},
        "login_state_fields_invalid",
    )
    _require(
        all(type(value) is bool for value in login_state.values()), "login_state_values_invalid"
    )

    pages_value = payload.get("pages")
    if not isinstance(pages_value, list) or not 1 <= len(pages_value) <= _MAX_PAGES:
        raise CaptureArtifactError("pages_invalid")
    pages: list[CapturePage] = []
    for page_value in pages_value:
        if not isinstance(page_value, dict):
            raise CaptureArtifactError("page_invalid")
        url = page_value.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            raise CaptureArtifactError("page_url_invalid")
        evals_value = page_value.get("evals")
        if not isinstance(evals_value, list) or not 1 <= len(evals_value) <= _MAX_EVALS_PER_PAGE:
            raise CaptureArtifactError("page_evals_invalid")
        eval_records: list[CaptureEval] = []
        seen_hashes: set[str] = set()
        for eval_value in evals_value:
            if not isinstance(eval_value, dict):
                raise CaptureArtifactError("eval_invalid")
            # F-01：eval 记录精确双键——未知 sibling 字段（可夹带凭证值）直接拒绝
            if set(eval_value) != {"script_sha256", "output"}:
                raise CaptureArtifactError("eval_invalid")
            script_hash = eval_value.get("script_sha256")
            output = eval_value.get("output")
            if (
                not isinstance(script_hash, str)
                or _SHA256_RE.fullmatch(script_hash) is None
                or not isinstance(output, str)
                or script_hash in seen_hashes
            ):
                raise CaptureArtifactError("eval_invalid")
            seen_hashes.add(script_hash)
            eval_records.append(CaptureEval(script_sha256=script_hash, output=output))
        pages.append(CapturePage(url=url, evals=tuple(eval_records)))
    if sum(len(page.evals) for page in pages) > _MAX_EVALS_PER_PAGE * _MAX_PAGES:
        raise CaptureArtifactError("pages_too_large")
    total_output = sum(
        len(eval_record.output.encode("utf-8")) for page in pages for eval_record in page.evals
    )
    if total_output > _MAX_OUTPUT_BYTES:
        raise CaptureArtifactError("output_too_large")

    # 覆盖/终态证据：group 页必须含时间线证据与全文记录
    group_page = next(
        (p for p in pages if _normalize_url(p.url) == _normalize_url(GROUP_URL)), None
    )
    if group_page is None:
        raise CaptureArtifactError("group_page_missing")
    _require(
        group_page.output_for(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT) is not None,
        "timeline_evidence_missing",
    )
    _require(group_page.output_for(_FULL_TEXT_SCRIPT) is not None, "full_text_missing")

    images = payload.get("images")
    if not isinstance(images, list) or not 0 <= len(images) <= _MAX_IMAGES:
        raise CaptureArtifactError("images_invalid")
    for image in images:
        if (
            not isinstance(image, dict)
            or set(image) != {"src", "date", "index"}
            or not isinstance(image["src"], str)
            # F-02：UTF-8 字节数上限（与 JS Buffer.byteLength 一致；code point 与
            # UTF-16 unit 计数在 astral 字符上不一致，统一按字节）
            or len(image["src"].encode("utf-8")) > 2048
            or _IMAGES_SRC_RE.fullmatch(image["src"]) is None
            or not isinstance(image["date"], str)
            or not 16 <= len(image["date"]) <= 40
            or type(image["index"]) is not int
            or not 0 <= image["index"] <= 10_000
        ):
            raise CaptureArtifactError("images_invalid")
    images_eval_raw = group_page.output_for(_IMAGES_BY_DATE_SCRIPT)
    if images_eval_raw is None:
        raise CaptureArtifactError("images_eval_missing")
    try:
        # F-01：重复键拒绝钩子（内部重复键可夹带被豁免的凭证值）
        images_eval = json.loads(images_eval_raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, TypeError, ValueError, CaptureArtifactError):
        raise CaptureArtifactError("images_eval_invalid") from None
    _require(images_eval == images, "images_eval_mismatch")

    cursor_value = payload.get("topic_cursor")
    cursor_pages: list[CaptureCursorPage] = []
    if cursor_value is not None:
        if not isinstance(cursor_value, list) or not 1 <= len(cursor_value) <= 16:
            raise CaptureArtifactError("topic_cursor_invalid")
        seen_end_times: set[str] = set()
        for cursor_entry in cursor_value:
            if not isinstance(cursor_entry, dict):
                raise CaptureArtifactError("topic_cursor_invalid")
            end_time = cursor_entry.get("end_time")
            script_hash = cursor_entry.get("script_sha256")
            output = cursor_entry.get("output")
            if (
                not isinstance(end_time, str)
                or not isinstance(script_hash, str)
                or _SHA256_RE.fullmatch(script_hash) is None
                or not isinstance(output, str)
                or len(end_time) > 80
                or end_time in seen_end_times
            ):
                raise CaptureArtifactError("topic_cursor_invalid")
            seen_end_times.add(end_time)
            cursor_pages.append(
                CaptureCursorPage(end_time=end_time, script_sha256=script_hash, output=output)
            )

    if final_status in {"complete", "partial"}:
        if oldest_seen is None:
            raise CaptureArtifactError("oldest_seen_date_missing")
        # F-02：page_end 是合法覆盖证据（短页全部内容在窗口内时 oldest 可能 ≥ cutoff），
        # 但需配合非空时间线证据或 cursor 记录（与 WSL page_end_covered 语义一致）
        evidence_raw = group_page.output_for(_TIMELINE_TIMESTAMP_EVIDENCE_SCRIPT)
        coverage_proven = oldest_seen < cutoff or (
            reached_page_end and (_evidence_has_items(evidence_raw) or bool(cursor_pages))
        )
        _require(coverage_proven, "window_coverage_incomplete")

    _scan_credentials(payload, images=images, root=True)

    return CaptureArtifact(
        run_id=run_id,
        captured_at=captured_at,
        capture_host=str(payload.get("capture_host") or ""),
        target_url=_as_str(target.get("url"), "target_url_mismatch"),
        target_title=target_title,
        target_tab_id=target_tab_id,
        window_days=window["days"],
        cutoff=cutoff,
        oldest_seen_date=oldest_seen,
        stopped_by_window_boundary=stopped_by_boundary,
        reached_page_end=reached_page_end,
        login_state=dict(login_state),
        pages=tuple(pages),
        cursor_pages=tuple(cursor_pages),
        final_status=final_status,
        failure_reason=None,
        content_sha256=payload["content_sha256"],
    )
