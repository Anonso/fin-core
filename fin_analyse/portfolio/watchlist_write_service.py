"""Local MCP-facing watchlist write service (add/tag/remove, preview → apply).

The consult-agent thin server stays read-only except this one bounded write
seam (owner 2026-09-01 decision): the assistant may ADD entries, ADD tags,
and REMOVE entries — but never automatically: every write, including remove,
goes through preview → user confirmation → apply.  Assistant provenance is
forced server-side for adds.  apply accepts only a process-local single-use
token issued by preview (TTL 15 minutes; a server restart invalidates every
outstanding token, which fails closed).  list is a zero-write mirror of
``read_user_watchlist``.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from fin_analyse.consultation.instrument_identity import (
    AShareConsultationInstrumentIdentityResolver,
)
from fin_analyse.market.instrument_directory import AShareInstrumentDirectory
from fin_analyse.portfolio.user_watchlist import (
    UserWatchlistStore,
    UserWatchlistTagError,
)
from fin_analyse.portfolio.watchlist_write import (
    WatchlistBatchCollisionError,
    WatchlistBatchEmptyError,
    WatchlistBatchTooLargeError,
    WatchlistOperationSpec,
    WatchlistRefError,
    WatchlistRefView,
    apply_watchlist_operations,
    preview_watchlist_operations,
)

_TOKEN_TTL_SECONDS = 15 * 60
_ALLOWED_ACTIONS = frozenset({"add", "tag", "remove"})


class LocalWatchlistPreviewTokenManager:
    """Process-local single-use preview tokens (in-memory; restart invalidates)."""

    def __init__(self, *, ttl_seconds: int = _TOKEN_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, dict[str, object]] = {}

    def issue(
        self,
        *,
        views: Sequence[WatchlistRefView],
        principal_id: str,
        now: datetime,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {
            "views": tuple(views),
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
    ) -> tuple[WatchlistRefView, ...] | None:
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        if (
            entry["principal_id"] != principal_id
            or not isinstance(entry["expires_at"], datetime)
            or entry["expires_at"] <= now
        ):
            return None
        views = entry["views"]
        if not isinstance(views, tuple):
            return None
        return tuple(view for view in views if isinstance(view, WatchlistRefView))


class WatchlistWriteService:
    """One bounded write service over the shared resolve/preview/apply seam."""

    def __init__(
        self,
        *,
        store: UserWatchlistStore,
        resolver: AShareConsultationInstrumentIdentityResolver,
        directory: AShareInstrumentDirectory,
        principal_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._directory = directory
        self._principal_id = principal_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tokens = LocalWatchlistPreviewTokenManager()

    def list(self) -> dict[str, object]:
        read = self._store.list()
        return {
            "status": "LISTED",
            "revision": read.revision,
            "as_of": read.as_of,
            "entry_count": len(read.entries),
            "entries": [
                {
                    "market_symbol": entry.market_symbol,
                    "name": entry.name,
                    "added_at": entry.added_at,
                    "provenance": entry.provenance,
                    "tags": list(entry.tags),
                }
                for entry in read.entries
            ],
        }

    def preview(
        self,
        operations: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        try:
            specs = _normalize_operations(operations)
            views = preview_watchlist_operations(
                self._resolver,
                self._directory,
                specs,
            )
            # assistant provenance is server-enforced, never client-supplied.
            views = tuple(
                view if view.action != "add" else _with_add_provenance(view)
                for view in views
            )
        except (
            WatchlistRefError,
            WatchlistBatchCollisionError,
            WatchlistBatchEmptyError,
            WatchlistBatchTooLargeError,
            UserWatchlistTagError,
            ValueError,
        ) as error:
            return _rejected(str(error))
        token = self._tokens.issue(
            views=views,
            principal_id=self._principal_id,
            now=self._clock(),
        )
        return {
            "status": "PREVIEW_READY",
            "operations": [view.to_token_dict() for view in views],
            "confirmation_phrase": _confirmation_phrase(views),
            "candidate_token": token,
        }

    def apply(self, token: str) -> dict[str, object]:
        views = self._tokens.consume(
            token,
            principal_id=self._principal_id,
            now=self._clock(),
        )
        if views is None:
            return _rejected("WATCHLIST_PREVIEW_TOKEN_INVALID")
        outcomes = apply_watchlist_operations(self._store, views)
        return {
            "status": "APPLIED",
            "outcomes": [
                {
                    "action": outcome.action,
                    "market_symbol": outcome.market_symbol,
                    "name": outcome.name,
                    "status": outcome.status,
                    "changed": outcome.changed,
                    "revision": outcome.revision,
                    "error": outcome.error,
                }
                for outcome in outcomes
            ],
        }


def _normalize_operations(
    operations: Sequence[dict[str, object]],
) -> tuple[WatchlistOperationSpec, ...]:
    if not operations:
        raise WatchlistBatchEmptyError("watchlist_batch_empty")
    if len(operations) > 10:
        raise WatchlistBatchTooLargeError("watchlist_batch_too_large")
    specs: list[WatchlistOperationSpec] = []
    for raw in operations:
        if not isinstance(raw, dict) or set(raw) - {"action", "ref", "tags"}:
            raise WatchlistRefError("watchlist_operation_invalid")
        action = raw.get("action")
        ref = raw.get("ref")
        if action not in _ALLOWED_ACTIONS or not isinstance(ref, str) or not ref:
            raise WatchlistRefError("watchlist_invalid_action")
        tags_raw = raw.get("tags", ())
        tags = (
            tuple(tags_raw)
            if isinstance(tags_raw, (list, tuple))
            and all(isinstance(tag, str) for tag in tags_raw)
            else ()
        )
        if action == "tag" and not tags:
            raise WatchlistRefError("watchlist_tags_required")
        if action == "remove" and tags:
            raise WatchlistRefError("watchlist_remove_with_tags_invalid")
        specs.append(
            WatchlistOperationSpec(action=action, ref=ref, tags=tags, provenance="assistant")
        )
    return tuple(specs)


def _with_add_provenance(view: WatchlistRefView) -> WatchlistRefView:
    return WatchlistRefView(
        action=view.action,
        ref=view.ref,
        name=view.name,
        market_symbol=view.market_symbol,
        provenance="assistant",
        tags=view.tags,
    )


def _confirmation_phrase(views: Sequence[WatchlistRefView]) -> str:
    parts: list[str] = []
    for view in views:
        if view.action == "add":
            tag_text = f"，标签：{','.join(view.tags)}" if view.tags else ""
            parts.append(f"新增 {view.name}({view.market_symbol})〔来源：assistant〕{tag_text}")
        elif view.action == "tag":
            parts.append(
                f"为 {view.name}({view.market_symbol}) 添加标签：{','.join(view.tags)}"
            )
        elif view.action == "remove":
            parts.append(f"删除 {view.name}({view.market_symbol})")
    return "确认更新自选股：" + "；".join(parts) + "。"


def _rejected(reason: str) -> dict[str, object]:
    return {"status": "REJECTED", "reason_codes": (reason,)}
