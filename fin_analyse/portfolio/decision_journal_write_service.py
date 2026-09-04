"""Local MCP-facing decision journal write service (preview → apply).

The consult-agent thin server exposes the owner-stated decision journal as
one bounded write seam (``record_decision``: list/preview/apply) plus one
read seam (``read_decision_journal``).  Every append goes through
preview → user confirmation → apply: preview structures the owner's stated
decision zero-write and returns a confirmation phrase containing every
substantive field plus a process-local single-use token (TTL 15 minutes; a
server restart invalidates every outstanding token, which fails closed).
apply consumes the token FIRST and then commits; a commit failure leaves
zero rows and the token dead — the only way forward is a fresh preview.
``source`` is always ``owner_stated``, forced server-side, never
client-supplied.  Symbol normalization goes through the shared FIN identity
resolver; an unresolvable reference is rejected at preview (nothing is
stored verbatim).  ``query`` is the zero-write read shared by
``read_decision_journal`` and the ``record_decision`` list verb.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fin_analyse.consultation.instrument_identity import (
    AShareConsultationInstrumentIdentityResolver,
)
from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.portfolio.decision_journal import (
    DecisionJournalError,
    DecisionJournalStore,
    DecisionRecord,
    normalize_decision_fields,
)

_TOKEN_TTL_SECONDS = 15 * 60
_CST = ZoneInfo("Asia/Shanghai")
_SEMANTICS = (
    "owner-stated decision history: factual record for review questions, "
    "user context — never investment evidence; reverted records stay "
    "visible with reverted_by pointing at the correcting record"
)
_TYPE_WORDS = {"buy": "买入", "sell": "卖出", "plan": "计划", "revert": "更正"}


class LocalDecisionPreviewTokenManager:
    """Process-local single-use preview tokens (in-memory; restart invalidates)."""

    def __init__(self, *, ttl_seconds: int = _TOKEN_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, dict[str, object]] = {}

    def issue(
        self,
        *,
        draft: dict[str, object],
        principal_id: str,
        now: datetime,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {
            "draft": draft,
            "principal_id": principal_id,
            "expires_at": now + timedelta(seconds=self._ttl),
        }
        return token

    def consume(
        self,
        token: str,
        *,
        principal_id: str,
        now: datetime,
    ) -> dict[str, object] | None:
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        if (
            entry["principal_id"] != principal_id
            or not isinstance(entry["expires_at"], datetime)
            or entry["expires_at"] <= now
        ):
            return None
        draft = entry["draft"]
        if not isinstance(draft, dict):
            return None
        return draft


class DecisionJournalWriteService:
    """One bounded write service over the append-only decision journal."""

    def __init__(
        self,
        *,
        store: DecisionJournalStore,
        resolver: AShareConsultationInstrumentIdentityResolver,
        principal_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._principal_id = principal_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tokens = LocalDecisionPreviewTokenManager()

    def list(self, *, limit: int = 50) -> dict[str, object]:
        """Zero-write mirror of read_decision_journal (session-side对账)."""
        return self.query(limit=limit)

    def query(
        self,
        *,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        decision_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        read = self._store.query(
            symbol=symbol,
            date_from=date_from,
            date_to=date_to,
            decision_type=decision_type,
            limit=limit,
        )
        return {
            "status": "LISTED",
            "count": len(read.records),
            "revision": read.revision,
            "as_of": read.as_of,
            "records": [_record_dict(record) for record in read.records],
            "semantics": _SEMANTICS,
        }

    def preview(
        self,
        *,
        decision_type: object = None,
        symbol: object = None,
        decision_date: object = None,
        rationale: object = None,
        note: object = None,
        revert_of: object = None,
    ) -> dict[str, object]:
        try:
            draft = normalize_decision_fields(
                decision_type=decision_type,
                symbol=symbol,
                decision_date=(
                    decision_date
                    if decision_date is not None
                    else self._cst_today(self._clock())
                ),
                rationale=rationale,
                note=note,
                revert_of=revert_of,
            )
            if draft["symbol"] is not None:
                draft["symbol"] = self._resolve_symbol(str(draft["symbol"]))
            if draft["revert_of"] is not None:
                target = self._store.get(str(draft["revert_of"]))
                if target is None:
                    return _rejected("decision_journal_revert_target_missing")
                if target.reverted_by is not None:
                    return _rejected(
                        "decision_journal_revert_target_already_reverted"
                    )
        except DecisionJournalError as error:
            return _rejected(str(error))
        token = self._tokens.issue(
            draft=draft,
            principal_id=self._principal_id,
            now=self._clock(),
        )
        return {
            "status": "PREVIEW_READY",
            "draft": dict(draft),
            "confirmation_phrase": _confirmation_phrase(draft),
            "candidate_token": token,
        }

    def apply(self, token: object) -> dict[str, object]:
        if not isinstance(token, str) or not token:
            return _rejected("decision_journal_preview_token_invalid")
        draft = self._tokens.consume(
            token,
            principal_id=self._principal_id,
            now=self._clock(),
        )
        if draft is None:
            return _rejected("decision_journal_preview_token_invalid")
        try:
            mutation = self._store.append(
                decision_type=str(draft["decision_type"]),
                symbol=draft["symbol"],  # type: ignore[arg-type]
                decision_date=str(draft["decision_date"]),
                rationale=str(draft["rationale"]),
                note=draft["note"],  # type: ignore[arg-type]
                revert_of=draft["revert_of"],  # type: ignore[arg-type]
            )
        except Exception:
            # fail-closed：token 已消费不复活、零行落库，只能重新 preview。
            # 任意 append 失败（含非 typed 异常）都不得让 token 或半行状态存活。
            return _rejected("decision_journal_append_failed")
        return {
            "status": "APPLIED",
            "decision_id": mutation.decision_id,
            "revision": mutation.revision,
        }

    def _cst_today(self, now: datetime) -> str:
        return now.astimezone(_CST).date().isoformat()

    def _resolve_symbol(self, ref: str) -> str:
        # 与 watchlist ref 规则同款：纯数字按代码、否则按 canonical 名称。
        identities = self._resolver.resolve_many(
            (InstrumentRef(ticker=ref) if ref.isdigit() else InstrumentRef(name=ref),)
        )
        identity = identities[0] if identities else None
        if identity is None or identity.market_symbol is None:
            raise DecisionJournalError("decision_journal_symbol_unresolved")
        return identity.market_symbol


def _record_dict(record: DecisionRecord) -> dict[str, object]:
    return {
        "decision_id": record.decision_id,
        "decision_type": record.decision_type,
        "symbol": record.symbol,
        "decision_date": record.decision_date,
        "rationale": record.rationale,
        "note": record.note,
        "revert_of": record.revert_of,
        "reverted_by": record.reverted_by,
        "source": record.source,
        "recorded_at": record.recorded_at,
    }


def _confirmation_phrase(draft: dict[str, object]) -> str:
    type_word = _TYPE_WORDS.get(str(draft["decision_type"]), str(draft["decision_type"]))
    target = draft["symbol"] if draft["symbol"] else "组合级"
    parts = [
        f"确认记录决策：{type_word} {target}，决策日 {draft['decision_date']}。",
        f"理由：{draft['rationale']}。",
    ]
    if draft["note"]:
        parts.append(f"备注：{draft['note']}。")
    if draft["revert_of"]:
        parts.append(f"更正对象：{draft['revert_of']}。")
    return "".join(parts)


def _rejected(reason: str) -> dict[str, object]:
    return {"status": "REJECTED", "reason_codes": (reason,)}
