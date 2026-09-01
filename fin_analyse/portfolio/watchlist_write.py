"""Shared FIN-owned watchlist write use-case seam (CLI and MCP adapters).

Both mutation adapters (operator CLI ``scripts/manage_user_watchlist.py`` and the
Hermes MCP tool ``update_user_watchlist``) share one resolve + preview + apply
mapping so canonical-name rules, batch symbol-uniqueness, per-ref CAS and
conflict-first no-op semantics cannot drift apart.

Contract notes (design v4, rounds 1-3):
- Ref rules: a bare six-digit code resolves directly; a name-only ref must
  byte-match the directory canonical name (nicknames/aliases/whitespace variants
  are rejected, zero writes).
- Batch preview: after resolving ALL refs, canonical ``market_symbol`` must be
  unique across the batch — same-direction duplicates and opposite actions on
  the same symbol are rejected whole-batch, zero writes (never silently
  deduplicated).
- Apply: per-ref CAS; the MCP adapter re-reads the latest revision immediately
  before each write, while the CLI adapter passes its start-of-run revision
  (``expected_revision``, design v4.1 O4: an external write between read and
  apply stays a conflict / exit 5).  ``UserWatchlistStore`` checks CAS *first*
  and only then duplicate/missing, so a stale expected revision is a conflict
  (zero writes for that ref only) and a fresh revision with the target already
  in the desired state is a typed no-op.  Remaining refs continue after a
  conflict (design v4 frozen contract); the batch is per-ref CAS, not atomic,
  and partial results are reported verbatim.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from fin_analyse.consultation.instrument_identity import (
    AShareConsultationInstrumentIdentityResolver,
    ConsultationInstrumentIdentity,
)
from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.market.instrument_directory import AShareInstrumentDirectory
from fin_analyse.portfolio.user_watchlist import (
    UserWatchlistConflictError,
    UserWatchlistDuplicateError,
    UserWatchlistError,
    UserWatchlistMissingError,
    UserWatchlistStateError,
    UserWatchlistStore,
    UserWatchlistTagError,
    _validate_provenance,
    _validate_tags,
)

WatchlistAction = Literal["add", "remove", "tag", "untag"]
ApplyStatus = Literal["succeeded", "noop", "conflict", "not_attempted", "failed"]


class WatchlistOperationSpec(NamedTuple):
    """One ordered operation input (preview entry; tags/provenance optional)."""

    action: str
    ref: str
    tags: tuple[str, ...] = ()
    provenance: str = "owner"


class WatchlistWriteError(RuntimeError):
    """Base typed error for the shared watchlist write use case."""


class WatchlistRefError(WatchlistWriteError):
    """Ref is unresolvable or a name-only ref is not the exact canonical name."""


class WatchlistBatchCollisionError(WatchlistWriteError):
    """After resolve, two operations target the same canonical market symbol."""


class WatchlistBatchEmptyError(WatchlistWriteError):
    """Batch contains no operations."""


class WatchlistBatchTooLargeError(WatchlistWriteError):
    """Batch exceeds the 10-operation bound."""


@dataclass(frozen=True, slots=True)
class WatchlistRefView:
    """One resolved, ordered operation view (preview or token payload)."""

    action: WatchlistAction
    ref: str
    name: str
    market_symbol: str
    provenance: str = "owner"
    tags: tuple[str, ...] = ()

    def to_token_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "market_symbol": self.market_symbol,
            "name": self.name,
            "provenance": self.provenance,
            "tags": ",".join(self.tags),
        }


@dataclass(frozen=True, slots=True)
class WatchlistApplyOutcome:
    action: WatchlistAction
    ref: str
    name: str
    market_symbol: str
    status: ApplyStatus
    changed: bool
    revision: str | None = None
    error: str | None = None


_MAX_BATCH_OPERATIONS = 10


def resolve_watchlist_ref(
    resolver: AShareConsultationInstrumentIdentityResolver,
    directory: AShareInstrumentDirectory,
    ref: str,
) -> ConsultationInstrumentIdentity:
    """Resolve one ref with the CLI's exact rules (shared by both adapters)."""
    if not ref:
        raise WatchlistRefError("watchlist_instrument_unresolved")
    identities = resolver.resolve_many(
        (InstrumentRef(ticker=ref) if ref.isdigit() else InstrumentRef(name=ref),)
    )
    if not identities:
        raise WatchlistRefError("watchlist_instrument_unresolved")
    identity = identities[0]
    if identity.market_symbol is None:
        raise WatchlistRefError("watchlist_instrument_unresolved")
    if not ref.isdigit():
        # B3 关闭条件：name-only 输入必须与目录 canonical 名称逐字节一致；
        # 归一化可命中但非 canonical 的输入（空格/大小写变体）一律拒绝零写。
        hits = directory.lookup(ref)
        if not any(hit.symbol == identity.market_symbol and hit.name == ref for hit in hits):
            raise WatchlistRefError("watchlist_ref_not_canonical_name")
    return identity


def preview_watchlist_operations(
    resolver: AShareConsultationInstrumentIdentityResolver,
    directory: AShareInstrumentDirectory,
    operations: Sequence[WatchlistOperationSpec | tuple[str, str]],
) -> tuple[WatchlistRefView, ...]:
    """Resolve an ordered operation batch, zero writes.

    Raises WatchlistRefError / WatchlistBatchCollisionError /
    WatchlistBatchEmptyError / WatchlistBatchTooLargeError before any write.
    """
    ordered = tuple(operations)
    if not ordered:
        raise WatchlistBatchEmptyError("watchlist_batch_empty")
    if len(ordered) > _MAX_BATCH_OPERATIONS:
        raise WatchlistBatchTooLargeError("watchlist_batch_too_large")
    views: list[WatchlistRefView] = []
    symbols: set[str] = set()
    for raw in ordered:
        if isinstance(raw, WatchlistOperationSpec):
            action, ref, tags, provenance = raw
        elif (
            isinstance(raw, (tuple, list))
            and len(raw) == 2
            and isinstance(raw[0], str)
            and isinstance(raw[1], str)
        ):
            action, ref = raw
            tags, provenance = (), "owner"
        else:
            raise WatchlistRefError("watchlist_operation_invalid")
        if action not in ("add", "remove", "tag", "untag"):
            raise WatchlistRefError("watchlist_invalid_action")
        provenance = _validate_provenance(provenance)
        tags = _validate_tags(tags)
        if action in ("tag", "untag") and not tags:
            raise WatchlistRefError("watchlist_tags_required")
        identity = resolve_watchlist_ref(resolver, directory, ref)
        assert identity.market_symbol is not None
        if identity.market_symbol in symbols:
            raise WatchlistBatchCollisionError("watchlist_batch_symbol_collision")
        symbols.add(identity.market_symbol)
        name = identity.semantic_ref.name or identity.market_symbol
        views.append(
            WatchlistRefView(
                action=action,
                ref=ref,
                name=name,
                market_symbol=identity.market_symbol,
                provenance=provenance,
                tags=tags,
            )
        )
    return tuple(views)


def apply_watchlist_operations(
    store: UserWatchlistStore,
    views: Sequence[WatchlistRefView],
    *,
    expected_revision: str | None = None,
) -> tuple[WatchlistApplyOutcome, ...]:
    """Apply the resolved views per-ref under revision CAS.

    The expected revision is re-read immediately before each write, except the
    first ref when ``expected_revision`` is passed (CLI path, design v4.1 O4:
    the operator's start-of-run revision stays authoritative, so an external
    write between read and apply is a conflict / exit 5, unchanged).  CAS is
    checked by the store before duplicate/missing, so a stale expected revision
    is a conflict (zero writes for that ref only); a fresh revision with the
    target already in the desired state is a typed no-op.  Remaining refs
    continue after a conflict (design v4 frozen contract); the batch is per-ref
    CAS, not atomic.  Every outcome carries the revision the ref was CASed
    against (observed value for MCP; the passed-in expected for the CLI).
    """
    outcomes: list[WatchlistApplyOutcome] = []
    for index, view in enumerate(views):
        try:
            if expected_revision is not None and index == 0:
                current = expected_revision
            else:
                current = store.list().revision or "r0"
            if view.action == "add":
                result = store.add(
                    _identity_from_view(view),
                    expected_revision=current,
                    provenance=view.provenance,
                    tags=view.tags,
                )
            elif view.action == "remove":
                result = store.remove(
                    _identity_from_view(view),
                    expected_revision=current,
                )
            elif view.action == "tag":
                result = store.add_tags(
                    view.market_symbol,
                    view.tags,
                    expected_revision=current,
                )
            else:
                result = store.remove_tags(
                    view.market_symbol,
                    view.tags,
                    expected_revision=current,
                )
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="succeeded",
                    changed=result.changed,
                    revision=result.revision,
                )
            )
        except UserWatchlistDuplicateError:
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="noop",
                    changed=False,
                    revision=current,
                    error="watchlist_duplicate_symbol",
                )
            )
        except UserWatchlistMissingError:
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="noop",
                    changed=False,
                    revision=current,
                    error="watchlist_missing_symbol",
                )
            )
        except UserWatchlistTagError:
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="failed",
                    changed=False,
                    revision=current,
                    error="watchlist_tags_invalid",
                )
            )
        except UserWatchlistConflictError:
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="conflict",
                    changed=False,
                    revision=current,
                    error="watchlist_revision_conflict",
                )
            )
            # 并发漂移只影响该 ref（设计 v4 冻结契约）：其余 ref 继续执行，
            # 已应用的先前 ref 保留并如实报告 partial。不停止整批。
        except UserWatchlistStateError:
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="failed",
                    changed=False,
                    revision=current,
                    error="watchlist_state_unavailable",
                )
            )
            # 状态不可用对所有剩余 ref 同样成立：停止（镜像 CLI exit 2）。
            outcomes.extend(
                WatchlistApplyOutcome(
                    action=remaining.action,
                    ref=remaining.ref,
                    name=remaining.name,
                    market_symbol=remaining.market_symbol,
                    status="not_attempted",
                    changed=False,
                    revision=current,
                )
                for remaining in views[index + 1 :]
            )
            break
        except UserWatchlistError as error:
            outcomes.append(
                WatchlistApplyOutcome(
                    action=view.action,
                    ref=view.ref,
                    name=view.name,
                    market_symbol=view.market_symbol,
                    status="failed",
                    changed=False,
                    revision=current,
                    error=str(error),
                )
            )
    return tuple(outcomes)


def _identity_from_view(view: WatchlistRefView) -> ConsultationInstrumentIdentity:
    from fin_analyse.consultation.instrument_identity import (
        ConsultationInstrumentIdentity,
    )

    return ConsultationInstrumentIdentity(
        status="RESOLVED",
        semantic_ref=InstrumentRef(name=view.name),
        market_symbol=view.market_symbol,
    )


__all__ = [
    "ApplyStatus",
    "WatchlistAction",
    "WatchlistApplyOutcome",
    "WatchlistBatchCollisionError",
    "WatchlistBatchEmptyError",
    "WatchlistBatchTooLargeError",
    "WatchlistRefError",
    "WatchlistRefView",
    "WatchlistWriteError",
    "apply_watchlist_operations",
    "preview_watchlist_operations",
    "resolve_watchlist_ref",
]
