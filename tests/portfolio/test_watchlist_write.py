"""Focused tests for the shared watchlist write use-case seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from fin_analyse.consultation.instrument_identity import ConsultationInstrumentIdentity
from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.portfolio.user_watchlist import (
    UserWatchlistConflictError,
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
    resolve_watchlist_ref,
)


class _FakeDirectory:
    def __init__(self, entries: dict[str, tuple[str, str]]) -> None:
        # name -> (symbol, name)
        self._entries = entries

    def lookup(self, value: str) -> tuple[object, ...]:
        hit = self._entries.get(value)
        if hit is None:
            return ()
        symbol, name = hit
        return (type("Entry", (), {"symbol": symbol, "name": name})(),)


class _FakeResolver:
    def __init__(self, identities: dict[str, ConsultationInstrumentIdentity]) -> None:
        # canonical ref string -> identity
        self._identities = identities

    def resolve_many(self, targets: tuple[InstrumentRef, ...]) -> tuple[ConsultationInstrumentIdentity, ...]:
        out = []
        for target in targets:
            ref = target.ticker if target.ticker else target.name
            identity = self._identities.get(ref)
            if identity is None:
                out.append(
                    ConsultationInstrumentIdentity(
                        status="UNRESOLVED",
                        semantic_ref=target,
                        market_symbol=None,
                    )
                )
            else:
                out.append(identity)
        return tuple(out)


_DIRECTORY = _FakeDirectory(
    {
        "贵州茅台": ("600519.SH", "贵州茅台"),
        "新希望": ("000876.SZ", "新希望"),
    }
)

_RESOLVER = _FakeResolver(
    {
        "600519": ConsultationInstrumentIdentity(
            status="RESOLVED",
            semantic_ref=InstrumentRef(ticker="600519"),
            market_symbol="600519.SH",
        ),
        "000876": ConsultationInstrumentIdentity(
            status="RESOLVED",
            semantic_ref=InstrumentRef(ticker="000876"),
            market_symbol="000876.SZ",
        ),
        "贵州茅台": ConsultationInstrumentIdentity(
            status="RESOLVED",
            semantic_ref=InstrumentRef(name="贵州茅台"),
            market_symbol="600519.SH",
        ),
        "新希望": ConsultationInstrumentIdentity(
            status="RESOLVED",
            semantic_ref=InstrumentRef(name="新希望"),
            market_symbol="000876.SZ",
        ),
    }
)


class TestResolve:
    def test_bare_code_resolves(self) -> None:
        identity = resolve_watchlist_ref(_RESOLVER, _DIRECTORY, "600519")
        assert identity.market_symbol == "600519.SH"

    def test_canonical_name_resolves(self) -> None:
        identity = resolve_watchlist_ref(_RESOLVER, _DIRECTORY, "贵州茅台")
        assert identity.market_symbol == "600519.SH"

    def test_non_canonical_name_rejected(self) -> None:
        with pytest.raises(WatchlistRefError):
            resolve_watchlist_ref(_RESOLVER, _DIRECTORY, " 贵州茅台 ")

    def test_unresolved_ref_rejected(self) -> None:
        with pytest.raises(WatchlistRefError):
            resolve_watchlist_ref(_RESOLVER, _DIRECTORY, "不存在的公司")

    def test_empty_ref_rejected(self) -> None:
        with pytest.raises(WatchlistRefError):
            resolve_watchlist_ref(_RESOLVER, _DIRECTORY, "")


class TestPreview:
    def test_preview_returns_ordered_views(self) -> None:
        views = preview_watchlist_operations(
            _RESOLVER,
            _DIRECTORY,
            (("add", "600519"), ("remove", "000876")),
        )
        assert views == (
            WatchlistRefView(action="add", ref="600519", name="600519.SH", market_symbol="600519.SH"),
            WatchlistRefView(action="remove", ref="000876", name="000876.SZ", market_symbol="000876.SZ"),
        )

    def test_preview_empty_batch_rejected(self) -> None:
        with pytest.raises(WatchlistBatchEmptyError):
            preview_watchlist_operations(_RESOLVER, _DIRECTORY, ())

    def test_preview_eleven_ops_rejected(self) -> None:
        operations = tuple(("add", f"6005{i:02d}") for i in range(11))
        with pytest.raises(WatchlistBatchTooLargeError):
            preview_watchlist_operations(_RESOLVER, _DIRECTORY, operations)

    def test_preview_same_symbol_same_direction_rejected(self) -> None:
        with pytest.raises(WatchlistBatchCollisionError):
            preview_watchlist_operations(
                _RESOLVER,
                _DIRECTORY,
                (("add", "600519"), ("add", "贵州茅台")),
            )

    def test_preview_same_symbol_opposite_actions_rejected(self) -> None:
        with pytest.raises(WatchlistBatchCollisionError):
            preview_watchlist_operations(
                _RESOLVER,
                _DIRECTORY,
                (("add", "600519"), ("remove", "贵州茅台")),
            )

    def test_preview_invalid_action_rejected(self) -> None:
        with pytest.raises(WatchlistRefError):
            preview_watchlist_operations(_RESOLVER, _DIRECTORY, (("rename", "600519"),))

    def test_preview_is_zero_write(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        assert store.list().revision == ""
        preview_watchlist_operations(_RESOLVER, _DIRECTORY, (("add", "600519"),))
        assert store.list().revision == ""
        assert store.audit_events() == ()


class TestApply:
    def test_apply_add_succeeds(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        views = (
            WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
        )
        outcomes = apply_watchlist_operations(store, views)
        assert len(outcomes) == 1
        assert outcomes[0].status == "succeeded"
        assert outcomes[0].changed is True
        assert store.list().revision != ""

    def test_apply_duplicate_is_noop(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        views = (
            WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
        )
        apply_watchlist_operations(store, views)
        revision = store.list().revision
        outcomes = apply_watchlist_operations(store, views)
        assert outcomes[0].status == "noop"
        assert outcomes[0].changed is False
        assert store.list().revision == revision  # zero write

    def test_apply_remove_missing_is_noop(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        views = (
            WatchlistRefView(action="remove", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
        )
        outcomes = apply_watchlist_operations(store, views)
        assert outcomes[0].status == "noop"
        assert store.list().revision == ""

    def test_apply_conflict_continues_remaining_refs(self, tmp_path: Path) -> None:
        """Design v4 frozen contract: a CAS conflict zero-writes only that ref;
        remaining refs continue (batch is per-ref CAS, not atomic)."""
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")

        def _conflict(*args, **kwargs):
            raise UserWatchlistConflictError("watchlist_revision_conflict")

        original_add = store.add
        store.add = _conflict  # type: ignore[method-assign]
        try:
            views = (
                WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
                WatchlistRefView(action="remove", ref="000876", name="新希望", market_symbol="000876.SZ"),
            )
            outcomes = apply_watchlist_operations(store, views)
        finally:
            store.add = original_add  # type: ignore[method-assign]
        assert outcomes[0].status == "conflict"
        assert outcomes[0].changed is False
        # 后续 ref 继续执行（missing remove → typed no-op），不标 not_attempted
        assert outcomes[1].status == "noop"
        assert outcomes[1].error == "watchlist_missing_symbol"
        assert store.list().revision == ""

    def test_apply_conflict_then_success_reports_both(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")

        calls = {"n": 0}
        original_add = store.add

        def _conflict_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise UserWatchlistConflictError("watchlist_revision_conflict")
            return original_add(*args, **kwargs)

        store.add = _conflict_once  # type: ignore[method-assign]
        try:
            views = (
                WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
                WatchlistRefView(action="add", ref="000876", name="新希望", market_symbol="000876.SZ"),
            )
            outcomes = apply_watchlist_operations(store, views)
        finally:
            store.add = original_add  # type: ignore[method-assign]
        assert [o.status for o in outcomes] == ["conflict", "succeeded"]
        assert outcomes[1].changed is True
        assert {e.market_symbol for e in store.list().entries} == {"000876.SZ"}

    def test_apply_passed_expected_revision_conflicts_on_drift(
        self, tmp_path: Path
    ) -> None:
        """CLI path (design v4.1 O4): the start-of-run revision stays the CAS
        authority — an external write after the read is a conflict (zero write
        for that ref), never folded into the new revision (audit round 2)."""
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        # CLI 启动时读到 r0（空态）；随后外部写入 r0 -> r1
        store.add(
            _RESOLVER.resolve_many((InstrumentRef(ticker="600519"),))[0],
            expected_revision="r0",
        )
        views = (
            WatchlistRefView(
                action="add", ref="000876", name="新希望", market_symbol="000876.SZ"
            ),
        )
        outcomes = apply_watchlist_operations(store, views, expected_revision="r0")
        assert outcomes[0].status == "conflict"
        assert outcomes[0].changed is False
        assert outcomes[0].revision == "r0"
        assert {e.market_symbol for e in store.list().entries} == {"600519.SH"}

    def test_apply_passed_expected_revision_fresh_succeeds(
        self, tmp_path: Path
    ) -> None:
        """CLI path: a passed expected revision that is still current applies
        normally (no re-read needed); outcome carries the write revision."""
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        views = (
            WatchlistRefView(
                action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"
            ),
        )
        outcomes = apply_watchlist_operations(store, views, expected_revision="r0")
        assert outcomes[0].status == "succeeded"
        assert outcomes[0].changed is True
        assert outcomes[0].revision != "r0"
        assert {e.market_symbol for e in store.list().entries} == {"600519.SH"}

    def test_apply_mixed_success_then_noop_continues(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        views = (
            WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
            WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
            WatchlistRefView(action="remove", ref="000876", name="新希望", market_symbol="000876.SZ"),
        )
        outcomes = apply_watchlist_operations(store, views)
        assert [o.status for o in outcomes] == ["succeeded", "noop", "noop"]

    def test_apply_per_ref_revision_reresolution(self, tmp_path: Path) -> None:
        """Each ref re-reads the latest revision immediately before writing."""
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        views = (
            WatchlistRefView(action="add", ref="600519", name="贵州茅台", market_symbol="600519.SH"),
            WatchlistRefView(action="add", ref="000876", name="新希望", market_symbol="000876.SZ"),
        )
        outcomes = apply_watchlist_operations(store, views)
        assert [o.status for o in outcomes] == ["succeeded", "succeeded"]
        assert outcomes[0].revision != outcomes[1].revision
        entries = store.list().entries
        assert {e.market_symbol for e in entries} == {"600519.SH", "000876.SZ"}

    def test_apply_tag_and_untag_roundtrip(self, tmp_path: Path) -> None:
        store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
        apply_watchlist_operations(
            store,
            (
                WatchlistRefView(
                    action="add",
                    ref="600519",
                    name="贵州茅台",
                    market_symbol="600519.SH",
                ),
            ),
        )
        tagged = apply_watchlist_operations(
            store,
            (
                WatchlistRefView(
                    action="tag",
                    ref="600519",
                    name="贵州茅台",
                    market_symbol="600519.SH",
                    tags=("suggest_delete", "mainline_ai"),
                ),
            ),
        )
        assert tagged[0].status == "succeeded"
        assert store.list().entries[0].tags == ("suggest_delete", "mainline_ai")

        untagged = apply_watchlist_operations(
            store,
            (
                WatchlistRefView(
                    action="untag",
                    ref="600519",
                    name="贵州茅台",
                    market_symbol="600519.SH",
                    tags=("mainline_ai",),
                ),
            ),
        )
        assert untagged[0].changed is True
        assert store.list().entries[0].tags == ("suggest_delete",)


class TestTagsInPreview:
    def test_preview_add_carries_provenance_and_tags(self) -> None:
        views = preview_watchlist_operations(
            _RESOLVER,
            _DIRECTORY,
            (
                WatchlistOperationSpec(
                    action="add",
                    ref="600519",
                    tags=("mainline_ai",),
                    provenance="assistant",
                ),
            ),
        )
        assert views[0].provenance == "assistant"
        assert views[0].tags == ("mainline_ai",)

    def test_preview_tag_requires_tags(self) -> None:
        with pytest.raises(WatchlistRefError):
            preview_watchlist_operations(
                _RESOLVER,
                _DIRECTORY,
                (WatchlistOperationSpec(action="tag", ref="600519"),),
            )

    def test_preview_untag_requires_tags(self) -> None:
        with pytest.raises(WatchlistRefError):
            preview_watchlist_operations(
                _RESOLVER,
                _DIRECTORY,
                (WatchlistOperationSpec(action="untag", ref="600519"),),
            )

    def test_preview_rejects_bad_tags(self) -> None:
        with pytest.raises(UserWatchlistTagError):
            preview_watchlist_operations(
                _RESOLVER,
                _DIRECTORY,
                (WatchlistOperationSpec(action="add", ref="600519", tags=("bad tag",)),),
            )
