"""fin-adjudication stdio MCP server（fin-adjudication-mcp 设计页 · D-052 接口缝）。

Hermes 侧唯一依赖的接口契约（冻结 v1，修订需 FIN/Hermes 双侧会话确认）：
- 模块路径 ``python -m fin_analyse.adjudication.mcp_server``（stdio）；
- 必需 env ``FIN_KNOWLEDGE_BASE_ROOT``（缺失 startup exit 2）；
- 工具闭集 6 个，返回一律 ``{"ok": bool, ...}``，变更动作落 mcp-ops.v1.jsonl；
- 无 auth（stdio 单本机主体；飞书把门在 Hermes 侧 allowlist）。
- 硬边界 3 定向豁免（owner 2026-09-07 拍板，D-052 在案）：score_list 返回的
  评分明细（代码/名称/原因）经 Hermes 进 owner 本人飞书私信，仅此一流；
  其余用户数据仍不出本机。
- 审计窗口注记：变更成功后才写审计行，变更与审计间崩溃 = 该次变更无 mcp
  审计（done 有 inbox.events 兜底；score 两工具接受该窗口，自用取向）。

任何 stray write to stdout would corrupt the JSON-RPC stream, so the stdout
guard below is installed at import time (pattern copied from
read_capabilities/server.py).
"""

from __future__ import annotations

# Stdout guard first — must sit above every import that could print.
import contextlib
import json
import os
import sys
import time
from pathlib import Path


class _StdoutGuard:
    """Redirect stdout writes to stderr; never raise at process exit."""

    def write(self, s: str) -> int:
        try:
            return os.write(2, s.encode() if isinstance(s, str) else s)
        except OSError:
            return 0

    def flush(self) -> None:
        with contextlib.suppress(OSError):
            os.fsync(2)

    def __getattr__(self, name):
        return getattr(sys.__stdout__, name)


_real_stdout = sys.stdout
sys.stdout = _StdoutGuard()

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from fin_analyse.adjudication.inbox import (  # noqa: E402
    AdjudicationInboxError,
    open_default_inbox,
)
from fin_analyse.adjudication.push import PushError, push  # noqa: E402
from fin_analyse.ingestion.instrument_scores import (  # noqa: E402
    instrument_scores_path,
    load_records,
    upsert_records,
)
from fin_analyse.runtime.knowledge_root import (  # noqa: E402
    KNOWLEDGE_BASE_ROOT_ENV,
    KnowledgeRootConfigurationError,
    validate_knowledge_base_root,
)

SCHEMA_VERSION = "fin.adjudication-mcp/v1"
_MAX_SCORE_LIMIT = 50

mcp = FastMCP("fin-adjudication")

AUDIT_FILE_NAME = "mcp-ops.v1.jsonl"


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _kb_root() -> Path:
    configured = os.environ.get(KNOWLEDGE_BASE_ROOT_ENV)
    try:
        return validate_knowledge_base_root(configured)
    except KnowledgeRootConfigurationError as exc:
        _stderr(f"startup failed: {exc}")
        raise SystemExit(2) from exc


def _registry_path() -> Path:
    return instrument_scores_path(_kb_root())


def _append_audit(*, action: str, target: str, note: str | None) -> None:
    """One owner-commanded mutation = one local audit line (0600, best-effort)."""

    from fin_analyse.runtime.state_roots import (
        adjudication_inbox_state_root,
        ensure_private_state_directory,
    )

    root = ensure_private_state_directory(adjudication_inbox_state_root())
    line = json.dumps(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "action": action,
            "target": target,
            "note": note,
            "via": "mcp",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    descriptor = os.open(
        root / AUDIT_FILE_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (line + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def _err(error: str, **extra: object) -> dict[str, object]:
    return {"ok": False, "error": error, **extra}


# -- inbox handlers --------------------------------------------------------


def _make_adjudication_list_handler():
    def handler() -> dict[str, object]:
        "List open adjudication items (oldest first) with backlog days and hints."
        try:
            rows = open_default_inbox().list_open()
        except AdjudicationInboxError as exc:
            return _err("inbox_unavailable", detail=str(exc))
        now = time.time()
        items = [
            {
                "item_id": row.item_id,
                "kind": row.kind,
                "title": row.title,
                "backlog_days": max(0, int((now - row.opened_at) // 86400)),
                "resolution_hint": row.resolution_hint,
                "payload_ref": row.payload_ref,
            }
            for row in rows
        ]
        return {"ok": True, "items": items}

    handler.__name__ = "adjudication_list"
    return handler


def _make_adjudication_done_handler():
    def handler(item_id: str, note: str | None = None) -> dict[str, object]:
        "Close one open adjudication item (owner decision); audited. item_id MUST come from adjudication_list."
        if not item_id or not item_id.strip():
            return _err("adjudication_item_id_required")
        try:
            open_default_inbox().resolve(item_id.strip(), source="owner", note=note)
        except AdjudicationInboxError as exc:
            return _err("adjudication_item_not_open", item_id=item_id, detail=str(exc))
        _append_audit(action="done", target=item_id, note=note)
        return {"ok": True, "status": "done", "item_id": item_id}

    handler.__name__ = "adjudication_done"
    return handler


# -- score needs_review handlers -------------------------------------------


def _make_score_list_handler():
    def handler(limit: int = 20, offset: int = 0) -> dict[str, object]:
        "Page through score records pending review (newest article first)."
        try:
            records = load_records(_registry_path())
        except OSError as exc:
            return _err("registry_unreadable", detail=str(exc))
        pending = [
            row
            for row in records.values()
            if row.get("status") == "needs_review"
        ]
        pending.sort(key=lambda row: str(row.get("article_date", "")), reverse=True)
        clamped_limit = max(0, min(int(limit), _MAX_SCORE_LIMIT))
        clamped_offset = max(0, int(offset))
        window = pending[clamped_offset : clamped_offset + clamped_limit]
        rows = [
            {
                "record_id": row.get("record_id"),
                "code": row.get("code"),
                "name": row.get("name"),
                "article_date": row.get("article_date"),
                "review_reason": row.get("review_reason"),
            }
            for row in window
        ]
        return {
            "ok": True,
            "pending_total": len(pending),
            "offset": clamped_offset,
            "rows": rows,
        }

    handler.__name__ = "score_list"
    return handler


def _make_score_confirm_handler():
    def handler(record_id: str, note: str | None = None) -> dict[str, object]:
        "Confirm one needs_review score record (status -> ok); audited. id = exact or unique-prefix."
        path = _registry_path()
        records = load_records(path)
        resolved, resolve_error = _resolve_record_id(records, record_id)
        if resolved is None:
            return _err(resolve_error, record_id=record_id)
        record = records[resolved]
        if record.get("status") == "ok":
            return {"ok": True, "status": "already_ok", "record_id": resolved}
        record["status"] = "ok"
        record["review_reason"] = None
        upsert_records(path, [_DictRecord(record)])
        _append_audit(action="score_confirm", target=resolved, note=note)
        return {"ok": True, "status": "confirmed", "record_id": resolved}

    handler.__name__ = "score_confirm"
    return handler


def _make_score_drop_handler():
    def handler(record_id: str, note: str | None = None) -> dict[str, object]:
        "Irreversible deletion. NEVER call unless the owner explicitly named THIS record (exact or unique-prefix id) in the current conversation; audited."
        path = _registry_path()
        records = load_records(path)
        resolved, resolve_error = _resolve_record_id(records, record_id)
        if resolved is None:
            return _err(resolve_error, record_id=record_id)
        upsert_records(path, [], remove_record_ids=[resolved])
        if resolved in load_records(path):  # 写后读：被并发写者抢先回写才算失败
            return _err("record_not_found", record_id=record_id)
        # 永久剔除：落墓碑，重析/采集水位不再复活（D-053 闭环补全）
        from fin_analyse.ingestion.instrument_scores import record_tombstones

        record_tombstones(
            _registry_path().parent, [resolved],
            reason=(note or "owner score_drop via Feishu/MCP"),
        )
        _append_audit(action="score_drop", target=resolved, note=note)
        return {"ok": True, "status": "dropped", "record_id": resolved}

    handler.__name__ = "score_drop"
    return handler


def _resolve_record_id(
    records: dict[str, dict], ref: str
) -> tuple[str | None, str | None]:
    """Exact or unique-prefix id resolution (飞书手打 64 位 hex 的现实容错）。"""

    if not ref or not ref.strip():
        return None, "record_id_required"
    ref = ref.strip()
    if ref in records:
        return ref, None
    matches = [rid for rid in records if rid.startswith(ref)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "record_id_ambiguous"
    return None, "record_not_found"


class _DictRecord:
    """Adapter so upsert_records accepts a plain registry dict."""

    def __init__(self, value: dict) -> None:
        self.record_id = value["record_id"]
        self._value = value

    def to_dict(self) -> dict:
        return self._value


def _make_score_get_handler():
    def handler(record_id: str) -> dict[str, object]:
        "Fetch one score record with ALL fields (incl. conflict_detail for cross-source rows). id = exact or unique-prefix."
        try:
            records = load_records(_registry_path())
        except OSError as exc:
            return _err("registry_unreadable", detail=str(exc))
        resolved, resolve_error = _resolve_record_id(records, record_id)
        if resolved is None:
            return _err(resolve_error, record_id=record_id)
        return {"ok": True, "record": records[resolved]}

    handler.__name__ = "score_get"
    return handler


def _make_g_batch_list_handler():
    def handler() -> dict[str, object]:
        "List the pending G annotation batch: every post-as_of teacher article with judging info (date/column/score/nominated/title/topic_id)."
        from fin_analyse.guo_teacher_research.mainline_candidates import (
            scan_annotation_batch_view,
        )

        kb = _kb_root()
        view = scan_annotation_batch_view(
            annotation_path=kb / "manual-annotations" / "g-cognition-mainline.md",
            index_path=kb / "index.json",
        )
        if view.disposition != "SCANNED":
            return _err("g_batch_view_unavailable", detail=str(view.reason))
        return {
            "ok": True,
            "as_of": view.as_of,
            "lag_days": view.lag_days,
            "nominated": sum(1 for a in view.articles if a.nominated),
            "articles": [
                {
                    "date": a.date,
                    "column": a.column,
                    "score": a.score,
                    "nominated": a.nominated,
                    "title": a.title,
                    "topic_id": a.topic_id,
                }
                for a in view.articles
            ],
        }

    handler.__name__ = "g_batch_list"
    return handler


def _make_g_batch_select_handler():
    def handler(entries: list[str], verdict: str) -> dict[str, object]:
        "Record owner's batch selection (verdict: keep|drop) for article topic_ids into the selection sidecar consumed by the local drafting session; audited."
        from fin_analyse.runtime.state_roots import (
            adjudication_inbox_state_root,
            ensure_private_state_directory,
        )

        if verdict not in ("keep", "drop"):
            return _err("verdict_invalid", detail="verdict must be keep|drop")
        if not entries or not all(isinstance(e, str) and e.strip() for e in entries):
            return _err("entries_required")
        root = ensure_private_state_directory(adjudication_inbox_state_root())
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        lines = [
            json.dumps(
                {"at": stamp, "topic_id": e.strip(), "verdict": verdict, "via": "mcp"},
                ensure_ascii=False,
                sort_keys=True,
            )
            for e in entries
        ]
        target = root / "g-batch-selections.v1.jsonl"
        descriptor = os.open(
            target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        _append_audit(action="g_batch_select", target=",".join(e.strip() for e in entries), note=verdict)
        return {"ok": True, "recorded": len(entries), "verdict": verdict}

    handler.__name__ = "g_batch_select"
    return handler


def _make_g_draft_list_handler():
    def handler() -> dict[str, object]:
        "List drafted G annotation units awaiting owner final review (empty until the drafting session publishes its manifest)."
        from fin_analyse.runtime.state_roots import adjudication_inbox_state_root

        manifest = adjudication_inbox_state_root() / "g-batch-draft.v1.jsonl"
        if not manifest.exists():
            return {"ok": True, "units": [], "hint": "起草会话尚未发布 g-batch-draft.v1.jsonl"}
        units = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                units.append(json.loads(line))
        return {"ok": True, "units": units}

    handler.__name__ = "g_draft_list"
    return handler


def _make_g_draft_verdict_handler():
    def handler(unit_ids: list[str], verdict: str, note: str | None = None) -> dict[str, object]:
        "Record owner's final review verdict (approve|reject) per drafted unit_id; the local drafting session applies verdicts (archive or revise). Audited."
        from fin_analyse.runtime.state_roots import (
            adjudication_inbox_state_root,
            ensure_private_state_directory,
        )

        if verdict not in ("approve", "reject"):
            return _err("verdict_invalid", detail="verdict must be approve|reject")
        if not unit_ids or not all(isinstance(u, str) and u.strip() for u in unit_ids):
            return _err("unit_ids_required")
        root = ensure_private_state_directory(adjudication_inbox_state_root())
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        sidecar = root / "g-batch-verdicts.v1.jsonl"
        lines = [
            json.dumps(
                {"at": stamp, "unit_id": u.strip(), "verdict": verdict,
                 "note": note, "via": "mcp"},
                ensure_ascii=False, sort_keys=True,
            )
            for u in unit_ids
        ]
        descriptor = os.open(sidecar, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        _append_audit(action="g_draft_verdict", target=",".join(u.strip() for u in unit_ids), note=verdict)
        return {"ok": True, "recorded": len(unit_ids), "verdict": verdict}

    handler.__name__ = "g_draft_verdict"
    return handler


# -- digest handler ---------------------------------------------------------


def _make_digest_send_handler():
    def handler() -> dict[str, object]:
        "Send the pending-adjudications digest now (same fingerprint dedupe)."
        from fin_analyse.operations.daily_workspace_delivery import (
            HermesCliMessageSender,
        )

        target = os.environ.get("FIN_DAILY_WORKSPACE_DELIVERY_TARGET")
        if not target:
            return _err("digest_target_missing")
        try:
            outcome = push(
                open_default_inbox(),
                sender=HermesCliMessageSender(target=target),
                target_environ=dict(os.environ),
            )
        except PushError as exc:
            return _err(str(exc).split(":")[0], detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - sender 失败面（hermes 缺失/超时/非零
            # 退出/非法 target）必须 typed，不得漏成 JSON-RPC error（评审 P1）
            return _err("digest_send_failed", detail=f"{type(exc).__name__}: {exc}")
        return {"ok": True, "disposition": outcome.disposition}

    handler.__name__ = "digest_send"
    return handler


# -- registration -----------------------------------------------------------

_LIST = _make_adjudication_list_handler()
_DONE = _make_adjudication_done_handler()
_SCORE_LIST = _make_score_list_handler()
_SCORE_CONFIRM = _make_score_confirm_handler()
_SCORE_DROP = _make_score_drop_handler()
_SCORE_GET = _make_score_get_handler()
_G_BATCH_LIST = _make_g_batch_list_handler()
_G_BATCH_SELECT = _make_g_batch_select_handler()
_G_DRAFT_LIST = _make_g_draft_list_handler()
_G_DRAFT_VERDICT = _make_g_draft_verdict_handler()
_DIGEST_SEND = _make_digest_send_handler()

mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
)(_LIST)
mcp.tool()(_DONE)
mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
)(_SCORE_LIST)
mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
)(_SCORE_GET)
mcp.tool()(_SCORE_CONFIRM)
mcp.tool(
    annotations=ToolAnnotations(destructiveHint=True),
)(_SCORE_DROP)
mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
)(_G_BATCH_LIST)
mcp.tool()(_G_BATCH_SELECT)
mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True),
)(_G_DRAFT_LIST)
mcp.tool()(_G_DRAFT_VERDICT)
mcp.tool()(_DIGEST_SEND)


def run(runner=None) -> None:
    """Preflight the kb root (fail-closed, exit non-zero) then serve stdio."""

    _kb_root()
    _stderr(f"serving fin-adjudication mcp; schema={SCHEMA_VERSION}")
    _run = mcp.run if runner is None else runner
    _run()


if __name__ == "__main__":
    # Restore real stdout — FastMCP.run() takes over stdout for JSON-RPC.
    sys.stdout = _real_stdout
    run()
