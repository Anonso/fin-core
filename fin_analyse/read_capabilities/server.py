"""Read-capability thin stdio MCP server (design v2 · D1).

Seven read tools over the FIN provider, no gateway, no envelope, no auth
(single local principal — stdio is pulled up by the owner's own CLI).
Any stray write to stdout would corrupt the JSON-RPC stream, so the guard
below is installed at import time, before any other project import.

Module path is a frozen contract: ``python -m fin_analyse.read_capabilities.server``.
"""

from __future__ import annotations

# Stdout guard first — copied from gateway/mcp_server.py:19-58 verbatim in
# behavior. Must sit above every import that could print.
import contextlib
import os
import sys


class _StdoutGuard:
    """Replacement for sys.stdout that redirects writes to stderr.

    Must never raise at process exit — flush/fsync are best-effort.
    """

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

import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from time import monotonic  # noqa: E402
from typing import Any  # noqa: E402

# Ensure logging doesn't add its own stdout handler.
for _handler in logging.root.handlers[:]:
    if isinstance(_handler, logging.StreamHandler) and _handler.stream in (
        sys.__stdout__,
        sys.stdout,
    ):
        logging.root.removeHandler(_handler)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from fin_analyse.read_capabilities.types import (  # noqa: E402
    ProductionReadRequest,
    ProductionReadResult,
)
from fin_analyse.read_capabilities.wiring import (  # noqa: E402
    ReaderWiring,
    build_reader_wiring,
)
from fin_analyse.runtime.knowledge_root import (  # noqa: E402
    KNOWLEDGE_BASE_ROOT_ENV,
    KnowledgeRootConfigurationError,
    validate_knowledge_base_root,
)

TRACE_SCHEMA_VERSION = 1
_TRACE_DIR_MODE = 0o700
_TRACE_FILE_MODE = 0o600
_DEFAULT_TRACE_ROOT = Path.home() / "fin-data" / "trace" / "read-capability"
_TRACE_FILE_NAME = "calls.jsonl"

# Per-tool default deadline budgets (design §3). Clients may tighten a
# budget via ``deadline_seconds`` but never widen it past the default.
_TOOL_DEADLINE_SECONDS: dict[str, float] = {
    "read_g_context": 30.0,
    "read_actual_portfolio": 10.0,
    "read_market_snapshot": 32.0,
    "read_market_overview": 22.0,
    "read_margin_evidence": 30.0,
    "read_ready_evidence": 30.0,
    "read_instrument_scores": 10.0,
    "read_article_search": 15.0,
    "read_article": 20.0,
    "read_user_watchlist": 10.0,
    "update_user_watchlist": 10.0,
}
_MAX_DEADLINE_SECONDS = 300.0
_MAX_SESSION_HINT_CHARS = 128
_MAX_QUESTION_CHARS = 8_192  # mirrors ProductionReadRequest

# Tools whose reader requires an explicit as_of (ready_evidence returns
# unavailable without one); the server fills the current moment when the
# client omits it.
_TOOLS_REQUIRING_AS_OF = frozenset({"read_ready_evidence", "read_instrument_scores"})

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_g_context": (
        "Read the layered G (Guo teacher) cognition mainline: pinned sources, "
        "fresh commentary, framework, facts, associations. Supports point-in-time "
        "audit via as_of. Advisory reference only, never a trade instruction. "
        "HARD RULE: any analysis/opinion question (怎么样/怎么看/该不该/要不要) "
        "calls this FIRST — analysis always consults the G mainline. Only a pure "
        "factual lookup (what holdings, what cost, how many) or an explicit "
        "user opt-out ('不用 G 认知' / don't use the teacher's framework) skips "
        "it; when in doubt, call it. 星大派每日热点 (daily hot list) items are "
        "AI-summarized reference information, not teacher opinions — never "
        "present them as 'the teacher thinks'."
    ),
    "read_actual_portfolio": (
        "Read the latest user-confirmed actual portfolio snapshot: holdings, "
        "quantities, costs, cash, exposure, per-holding owner thesis. Read-only; "
        "never changes the portfolio. HARD RULE for any instrument/holdings "
        "question: call this plus read_market_snapshot FIRST. For any "
        "analysis/opinion question about holdings, call read_g_context FIRST "
        "as well — only a pure factual lookup (what holdings, what cost) or an "
        "explicit user opt-out of G cognition skips G. Never invent holdings, "
        "names, or portfolio rules from memory."
    ),
    "read_market_snapshot": (
        "Read on-demand tactical market context for up to 5 A-share instruments "
        "(quotes, daily bars, technicals). May fetch from public market sources "
        "when cached artifacts are missing. A data gap or empty return means "
        "DATA UNAVAILABLE — never state it as 'no such data exists in the "
        "world'; say the tool returned nothing and give conditional guidance."
    ),
    "read_market_overview": (
        "Read the current A-share market overview: indices, breadth, boards, "
        "session phase. Takes no instruments."
    ),
    "read_margin_evidence": (
        "Read market-wide margin financing (两融) evidence: aggregate A-share "
        "margin balance, margin buy volume and leverage-crowding indicators "
        "from cached artifacts. Use it to gauge market-wide leverage risk "
        "appetite; this is NOT account-level margin data."
    ),
    "read_ready_evidence": (
        "Read recent reference-ready evidence (ZSXQ tiered articles) selected "
        "for the research question as of a moment in time."
    ),
    "read_instrument_scores": (
        "Read structured instrument scores (利好度/共识度, normalized 1-10) "
        "extracted from ZSXQ ordinary-column research/AI-analysis rating tables. "
        "Query by six-digit code or company name via instruments, or by "
        "sector/keyword in the question. Returns recent records by default "
        "(window-limited); a question mentioning 历史/演变/全部 returns more "
        "history. needs_review rows are excluded from the list but counted. "
        "SOURCE: ordinary-column research reports (reference layer, "
        "advisory_only, dated) — these are AI analysis of broker reports, NOT "
        "the teacher's G opinions; pair with read_g_context for G direction."
    ),
    "read_article_search": (
        "Search local ZSXQ articles by keyword (industry commentary, 板块, "
        "theme, company) across all columns and dates. Returns dated article "
        "references with a short excerpt — use it to find articles to read, "
        "not as facts. SOURCE: local ZSXQ article library (reference layer, "
        "advisory_only); never present an article's AI summary as the "
        "teacher's own G opinion."
    ),
    "read_article": (
        "Read one local ZSXQ article's bounded full text by article_id "
        "(returned by read_article_search). Includes column/date/score and a "
        "layer hint (g = 星大派特刊/锐评/好问题/每日热点/凤仙郡/人脉/版本强势英雄; "
        "reference = 普通栏研报/问答). Preserve the article's column and "
        "date when citing; AI summaries inside are reference material, not "
        "the teacher's own G opinion."
    ),
    "read_user_watchlist": (
        "Read the user-maintained A-share watchlist (自选股/观察票): codes, "
        "names, added dates, add provenance (owner/assistant), tags, list "
        "revision. USER CONTEXT ONLY — a focus-of-attention list the owner "
        "curates; never investment evidence and never a trade instruction. "
        "Call it when the question mentions the watchlist (自选/自选股/观察票) "
        "or asks what to watch next; pair with read_market_snapshot for "
        "prices. An empty list means the watchlist is empty — say that "
        "plainly, it is not an error. Read-only; watchlist updates go "
        "through update_user_watchlist (add/tag only)."
    ),
    "update_user_watchlist": (
        "Maintain the user watchlist with bounded write semantics: list, "
        "preview, or apply. Actions: add-new-entry (action=add), add-tags "
        "(action=tag), and remove-entry (action=remove). NEVER delete or "
        "rename automatically: remove requires the user's explicit "
        "instruction, and any write (including remove) only happens through "
        "preview → user confirmation → apply. Without an explicit delete "
        "instruction, propose deletion by adding the reserved tag "
        "suggest_delete instead. preview resolves operations zero-write and "
        "returns a confirmation phrase plus a candidate token; apply takes "
        "ONLY that token (TTL 15 min, single use). Assistant provenance is "
        "forced server-side for adds. Never apply without the user's "
        "explicit confirmation of the exact preview phrase; in "
        "headless/one-shot sessions only preview, never apply. One operation "
        "= {action: add|tag|remove, ref: six-digit code or exact canonical "
        "name, tags: [..]} (remove takes no tags)."
    ),
}


def _stderr(message: str) -> None:
    print(f"[read-capability] {message}", file=sys.stderr)


# ── Trace (design §5: 调用率仪表, not consumption proof) ───────────────────


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class CallTrace:
    """Single-process O_APPEND single-line JSONL trace writer."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._initialized = False

    def _ensure_file(self) -> None:
        if self._initialized:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=_TRACE_DIR_MODE)
        os.chmod(self._path.parent, _TRACE_DIR_MODE)
        if not self._path.exists():
            self._path.touch(mode=_TRACE_FILE_MODE)
        os.chmod(self._path, _TRACE_FILE_MODE)
        self._initialized = True

    def record(
        self,
        *,
        tool: str,
        question: str,
        args: Mapping[str, Any],
        status: str,
        data_gaps: tuple[str, ...],
        latency_ms: int,
        session_hint: str,
        as_of: datetime | None,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        try:
            self._ensure_file()
            record: dict[str, object] = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "ts": datetime.now(UTC).isoformat(),
                "tool": tool,
                "question_digest": _digest(question),
                "args_digest": _digest(json.dumps(args, sort_keys=True, default=str)),
                "status": status,
                "data_gaps": list(data_gaps),
                "latency_ms": latency_ms,
                "session_hint": session_hint[:_MAX_SESSION_HINT_CHARS],
                "as_of": as_of.isoformat() if as_of is not None else None,
            }
            if summary is not None:
                record["summary"] = summary
            line = json.dumps(record, ensure_ascii=False)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # Trace is instrumentation, never a functional dependency.
            _stderr(f"trace write failed: {type(exc).__name__}")


# ── Failure taxonomy (design §3: fixed per class) ──────────────────────────


class InvalidParamsError(ValueError):
    """JSON-RPC invalid_params: empty question, unknown field, over-long."""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    status: str
    value: dict[str, object]
    data_gaps: tuple[str, ...]


def _deadline_gap(tool: str) -> str:
    return f"{tool}_deadline_exceeded"


def _unavailable_gap(tool: str) -> str:
    return f"{tool}_unavailable"


def _invoke_tool(
    wiring: ReaderWiring,
    trace: CallTrace,
    *,
    tool: str,
    payload: Mapping[str, Any],
) -> dict[str, object]:
    """Validate input, apply the deadline, invoke, trace; never raise for
    reader-level failures (only invalid input propagates as JSON-RPC error).
    """

    unknown = set(payload) - {
        "question",
        "instruments",
        "article_id",
        "as_of",
        "deadline_seconds",
        "session_hint",
    }
    if unknown:
        raise InvalidParamsError(f"invalid_params: unknown fields: {sorted(unknown)}")

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise InvalidParamsError("invalid_params: question must be a non-empty string")
    if len(question) > _MAX_QUESTION_CHARS:
        raise InvalidParamsError("invalid_params: question too long")

    instruments = payload.get("instruments", ())
    if not isinstance(instruments, (list, tuple)):
        raise InvalidParamsError("invalid_params: instruments must be a list")
    if len(instruments) > 64:
        raise InvalidParamsError("invalid_params: too many instruments")

    article_id = payload.get("article_id")
    if article_id is not None and (
        not isinstance(article_id, str) or not article_id.strip() or len(article_id) > 160
    ):
        raise InvalidParamsError("invalid_params: article_id invalid")

    as_of: datetime | None = None
    raw_as_of = payload.get("as_of")
    if raw_as_of is not None:
        if not isinstance(raw_as_of, str) or not raw_as_of.strip():
            raise InvalidParamsError("invalid_params: as_of must be an ISO string")
        try:
            as_of = datetime.fromisoformat(raw_as_of)
        except ValueError as exc:
            raise InvalidParamsError("invalid_params: as_of not ISO format") from exc
        if as_of.tzinfo is None:
            raise InvalidParamsError("invalid_params: as_of must be timezone-aware")
    if tool in _TOOLS_REQUIRING_AS_OF and as_of is None:
        as_of = datetime.now(UTC)

    default_budget = _TOOL_DEADLINE_SECONDS[tool]
    budget = default_budget
    raw_deadline = payload.get("deadline_seconds")
    if raw_deadline is not None:
        if not isinstance(raw_deadline, (int, float)) or raw_deadline <= 0:
            raise InvalidParamsError("invalid_params: deadline_seconds must be positive")
        # Clients may tighten, never widen.
        budget = min(float(raw_deadline), default_budget, _MAX_DEADLINE_SECONDS)

    session_hint = payload.get("session_hint", "")
    if not isinstance(session_hint, str):
        raise InvalidParamsError("invalid_params: session_hint must be a string")

    runner = wiring.runners.get(tool)
    if runner is None:
        gaps = (
            _unavailable_gap(tool),
            *(reason for name, reason in wiring.unavailable_tools if name == tool),
        )
        trace.record(
            tool=tool,
            question=question,
            args=payload,
            status="unavailable",
            data_gaps=gaps,
            latency_ms=0,
            session_hint=session_hint,
            as_of=as_of,
        )
        return {"value": {}, "data_gaps": list(gaps)}

    deadline_at = datetime.now(UTC) + timedelta(seconds=budget)
    request = ProductionReadRequest(
        question=question,
        instruments=tuple(str(item) for item in instruments),
        article_id=article_id,
        as_of=as_of,
        deadline_at=deadline_at,
    )

    started = monotonic()
    result: ProductionReadResult | None = None
    failure_gap: str | None = None
    try:
        result = runner(request)
        if not isinstance(result, ProductionReadResult):
            failure_gap = _unavailable_gap(tool)
    except TimeoutError:
        failure_gap = _deadline_gap(tool)
    except Exception:
        failure_gap = _unavailable_gap(tool)

    latency_ms = int((monotonic() - started) * 1000)

    if failure_gap is not None:
        gaps = (failure_gap,)
        trace.record(
            tool=tool,
            question=question,
            args=payload,
            status="failed",
            data_gaps=gaps,
            latency_ms=latency_ms,
            session_hint=session_hint,
            as_of=as_of,
        )
        return {"value": {}, "data_gaps": list(gaps)}

    status = "ok" if not result.data_gaps else "gaps"
    summary = _trace_summary(tool, result.value)
    trace.record(
        tool=tool,
        question=question,
        args=payload,
        status=status,
        data_gaps=result.data_gaps,
        latency_ms=latency_ms,
        session_hint=session_hint,
        as_of=as_of,
        summary=summary,
    )
    return {"value": result.value, "data_gaps": list(result.data_gaps)}


def _trace_summary(tool: str, value: object) -> dict[str, object] | None:
    """Optional per-tool trace enrichment (currently only G pinned summary)."""
    if tool != "read_g_context" or not isinstance(value, dict):
        return None
    attestation = value.get("attestation")
    if not isinstance(attestation, dict):
        return None
    quality = attestation.get("quality")
    if not isinstance(quality, dict):
        return None
    keys = ("pinned_injected", "pinned_candidate_seen", "pinned_layer_count")
    picked: dict[str, object] = {
        key: quality[key] for key in keys if key in quality
    }
    pinned_gaps = quality.get("pinned_data_gaps")
    if isinstance(pinned_gaps, list):
        picked["pinned_data_gaps"] = pinned_gaps
    return {"g_pinned": picked} if picked else None


# ── Server assembly ─────────────────────────────────────────────────────────

mcp = FastMCP("fin-read-capabilities")

_wiring: ReaderWiring | None = None
_trace: CallTrace | None = None


def _tool_session() -> tuple[ReaderWiring, CallTrace]:
    if _wiring is None or _trace is None:
        raise RuntimeError("read_capability_server_not_initialized")
    return _wiring, _trace


def initialize(
    knowledge_base_root: str | Path,
    *,
    trace_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Build wiring + trace once; fail-closed on an invalid kb root."""

    global _wiring, _trace
    root = validate_knowledge_base_root(knowledge_base_root)
    _wiring = build_reader_wiring(root, environ=dict(environ) if environ else None)
    _trace = CallTrace(trace_path or _DEFAULT_TRACE_ROOT / _TRACE_FILE_NAME)


def _make_tool_handler(tool: str):
    def _handler(
        question: str,
        instruments: list[str] | None = None,
        as_of: str | None = None,
        deadline_seconds: float | None = None,
        session_hint: str = "",
    ) -> dict[str, object]:
        """Bounded FIN read; see the tool description."""
        wiring, trace = _tool_session()
        payload: dict[str, object] = {"question": question, "session_hint": session_hint}
        if instruments is not None:
            payload["instruments"] = instruments
        if as_of is not None:
            payload["as_of"] = as_of
        if deadline_seconds is not None:
            payload["deadline_seconds"] = deadline_seconds
        return _invoke_tool(wiring, trace, tool=tool, payload=payload)

    _handler.__name__ = tool
    _handler.__doc__ = _TOOL_DESCRIPTIONS.get(tool, tool)
    return _handler


def _make_watchlist_handler():
    """Bounded write tool: list/preview/apply, add+tag only (design §2)."""

    def _handler(
        action: str,
        operations: list[dict[str, object]] | None = None,
        token: str | None = None,
        question: str = "",
        session_hint: str = "",
    ) -> dict[str, object]:
        wiring, trace = _tool_session()
        payload: dict[str, object] = {"action": action, "session_hint": session_hint}
        if operations is not None:
            payload["operations"] = operations
        if token is not None:
            payload["token"] = token
        service = wiring.watchlist_write
        started = monotonic()
        if service is None:
            trace.record(
                tool="update_user_watchlist",
                question=question,
                args=payload,
                status="unavailable",
                data_gaps=("watchlist_write_unavailable",),
                latency_ms=0,
                session_hint=session_hint,
                as_of=None,
            )
            return {
                "value": {
                    "status": "UNAVAILABLE",
                    "reason_codes": ["watchlist_write_unavailable"],
                },
                "data_gaps": ["watchlist_write_unavailable"],
            }
        if action not in ("list", "preview", "apply"):
            raise InvalidParamsError("invalid_params: action must be list|preview|apply")
        try:
            if action == "list":
                if operations is not None or token is not None:
                    raise InvalidParamsError(
                        "invalid_params: list takes no operations/token"
                    )
                result = service.list()
            elif action == "preview":
                if token is not None or not isinstance(operations, list) or not operations:
                    raise InvalidParamsError(
                        "invalid_params: preview requires non-empty operations and no token"
                    )
                result = service.preview(operations)
            else:
                if operations is not None or not isinstance(token, str) or not token:
                    raise InvalidParamsError(
                        "invalid_params: apply requires token and no operations"
                    )
                result = service.apply(token)
        except InvalidParamsError:
            raise
        except Exception:
            result = {"status": "REJECTED", "reason_codes": ["watchlist_write_failed"]}
        latency_ms = int((monotonic() - started) * 1000)
        rejected = result.get("status") == "REJECTED"
        gaps = (
            tuple(str(code) for code in result.get("reason_codes", ()))
            if rejected
            else ()
        )
        trace.record(
            tool="update_user_watchlist",
            question=question,
            args=payload,
            status="gaps" if rejected else "ok",
            data_gaps=gaps,
            latency_ms=latency_ms,
            session_hint=session_hint,
            as_of=None,
        )
        return {"value": result, "data_gaps": list(gaps)}

    _handler.__name__ = "update_user_watchlist"
    _handler.__doc__ = _TOOL_DESCRIPTIONS["update_user_watchlist"]
    return _handler


for _tool_name in _TOOL_DEADLINE_SECONDS:
    if _tool_name == "update_user_watchlist":
        continue
    mcp.tool(
        name=_tool_name,
        description=_TOOL_DESCRIPTIONS.get(_tool_name, _tool_name),
        annotations=_READ_ANNOTATIONS,
    )(_make_tool_handler(_tool_name))

mcp.tool(
    name="update_user_watchlist",
    description=_TOOL_DESCRIPTIONS["update_user_watchlist"],
    annotations=_WRITE_ANNOTATIONS,
)(_make_watchlist_handler())


def run(runner=None) -> None:
    """Preflight the kb root (fail-closed, exit non-zero) then serve stdio."""

    configured = os.environ.get(KNOWLEDGE_BASE_ROOT_ENV)
    try:
        root = validate_knowledge_base_root(configured)
    except KnowledgeRootConfigurationError as exc:
        _stderr(f"startup failed: {exc}")
        raise SystemExit(2) from exc
    initialize(root)
    _stderr(f"serving {len(_TOOL_DEADLINE_SECONDS)} tools; kb_root={root}")
    _run = mcp.run if runner is None else runner
    _run()


if __name__ == "__main__":
    # Restore real stdout — FastMCP.run() takes over stdout for JSON-RPC.
    sys.stdout = _real_stdout
    run()
