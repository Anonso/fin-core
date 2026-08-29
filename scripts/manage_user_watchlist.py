#!/usr/bin/env python3
"""List, add, or remove entries in the user-maintained typed A-share watchlist.

The watchlist is user context (focus of attention), never investment evidence.
This CLI is the only mutation seam for the user-maintained typed watchlist:
root and principal are derived from production semantics only (absolute
XDG_STATE_HOME plus the owner-only local installation identity); there is no
caller-supplied state root or principal override.

Subcommands:
  list
  add --ref-token <hex> [--apply]
  remove --ref-token <hex> [--apply]

``--ref-token`` carries one instrument ref (six-digit code or the exact
canonical directory name) as UTF-8 hex so the command line never contains
user-visible raw text.  Without ``--apply`` the operation is a zero-write
preview; with ``--apply`` the mutation is committed under revision CAS
(concurrent changes are rejected, never overwritten).

Output schema: user-watchlist-management-result.v2
Exit codes:
  0 success
  1 argument/internal error
  2 state or identity unavailable (fail closed, zero writes)
  3 duplicate/missing no-op (zero writes)
  4 invalid ref token, unresolvable, or non-canonical name (zero writes)
  5 revision CAS conflict (zero writes; batch must stop)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fin_analyse.consultation.instrument_identity import (  # noqa: E402
    AShareConsultationInstrumentIdentityResolver,
    ConsultationInstrumentIdentity,
)
from fin_analyse.guo_teacher_research.principal_binding import (  # noqa: E402
    PrincipalBindingError,
)
from fin_analyse.market.instrument_directory import (  # noqa: E402
    RuntimeAshareInstrumentDirectory,
)
from fin_analyse.portfolio.user_watchlist import (  # noqa: E402
    UserWatchlistStateError,
)
from fin_analyse.portfolio.watchlist_state import (  # noqa: E402
    require_production_watchlist_state,
)
from fin_analyse.portfolio.watchlist_write import (  # noqa: E402
    WatchlistRefError,
    resolve_watchlist_ref,
)

_OUTPUT_SCHEMA = "user-watchlist-management-result.v2"
_HEX_TOKEN = re.compile(r"[0-9a-fA-F]{1,512}\Z")


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("list", help="Show current entries without writing")
    for name in ("add", "remove"):
        sub = commands.add_parser(name, help=f"{name} one resolved instrument")
        sub.add_argument(
            "--ref-token",
            required=True,
            help="UTF-8 hex-encoded ref: six-digit code or exact canonical name",
        )
        sub.add_argument("--apply", action="store_true", help="commit the mutation")
    return parser


def _emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _decode_ref_token(token: str) -> str:
    if _HEX_TOKEN.fullmatch(token) is None:
        raise _ArgumentError("watchlist_ref_token_invalid")
    try:
        ref = bytes.fromhex(token).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise _ArgumentError("watchlist_ref_token_invalid") from None
    if not ref:
        raise _ArgumentError("watchlist_ref_token_invalid")
    return ref


def _resolve_one(
    resolver: AShareConsultationInstrumentIdentityResolver,
    directory: RuntimeAshareInstrumentDirectory,
    ref: str,
) -> ConsultationInstrumentIdentity:
    # 共享 use-case seam（CLI 与 MCP 工具共用同一 canonical 规则，防漂移）。
    return resolve_watchlist_ref(resolver, directory, ref)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except _ArgumentError as error:
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "ok": False,
                "outcome": "invalid_argument",
                "error": str(error),
            },
            1,
        )

    try:
        _root, _principal, store = require_production_watchlist_state()
        read = store.list()
        # 启动时 revision 是 CLI 的 CAS 权威（design v4.1 O4）：read 与 apply 之间
        # 的外部写入必须表现为 conflict/exit 5，而不是被静默折叠进新 revision。
        current = read.revision or "r0"
    except (ValueError, PrincipalBindingError) as error:
        code = (
            "authentication_required"
            if isinstance(error, PrincipalBindingError)
            else "watchlist_state_root_invalid"
        )
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "unavailable",
                "error": code,
            },
            2,
        )
    except UserWatchlistStateError as error:
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "unavailable",
                "error": str(error),
            },
            2,
        )
    except OSError:
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "unavailable",
                "error": "watchlist_state_unavailable",
            },
            2,
        )

    if args.operation == "list":
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": "list",
                "ok": True,
                "outcome": "listed",
                "revision": read.revision,
                "as_of": read.as_of,
                "entries": [
                    {"market_symbol": e.market_symbol, "name": e.name, "added_at": e.added_at}
                    for e in read.entries
                ],
            },
            0,
        )

    try:
        ref = _decode_ref_token(args.ref_token)
    except _ArgumentError as error:
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "unresolved",
                "error": str(error),
            },
            4,
        )

    directory = RuntimeAshareInstrumentDirectory()
    resolver = AShareConsultationInstrumentIdentityResolver()
    try:
        identity = _resolve_one(resolver, directory, ref)
    except WatchlistRefError as error:
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "unresolved",
                "ref": ref,
                "error": str(error),
            },
            4,
        )

    # resolve_watchlist_ref 已保证 market_symbol 非 None（否则抛错）；此处仅类型收窄。
    assert identity.market_symbol is not None
    name = identity.semantic_ref.name or identity.market_symbol
    if not args.apply:
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": True,
                "preview": True,
                "outcome": "preview",
                "ref": ref,
                "name": name,
                "market_symbol": identity.market_symbol,
            },
            0,
        )

    from fin_analyse.portfolio.watchlist_write import (
        WatchlistRefView,
        apply_watchlist_operations,
    )

    view = WatchlistRefView(
        action=args.operation,  # type: ignore[arg-type]
        ref=identity.semantic_ref.name or identity.market_symbol,
        name=name,
        market_symbol=identity.market_symbol,
    )
    # 单 ref，CAS expected = 启动时 revision（O4 冻结语义）；不再二次 list()，
    # 报告用 revision 直接来自 outcome（audit round 2 修正）。
    outcome = apply_watchlist_operations(store, (view,), expected_revision=current)[0]
    if outcome.status == "succeeded":
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": True,
                "outcome": "applied",
                "changed": outcome.changed,
                "revision": outcome.revision,
                "ref": ref,
                "name": name,
                "market_symbol": outcome.market_symbol,
            },
            0,
        )
    if outcome.status == "noop":
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "noop",
                "error": outcome.error,
                "ref": ref,
                "name": name,
                "market_symbol": outcome.market_symbol,
                "changed": False,
                "current_revision": outcome.revision,
            },
            3,
        )
    if outcome.status == "conflict":
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "conflict",
                "error": outcome.error,
                "ref": ref,
                "name": name,
                "market_symbol": outcome.market_symbol,
                "changed": False,
                "current_revision": outcome.revision,
            },
            5,
        )
    if outcome.status == "failed" and outcome.error == "watchlist_state_unavailable":
        return _emit(
            {
                "schema_version": _OUTPUT_SCHEMA,
                "operation": args.operation,
                "ok": False,
                "outcome": "unavailable",
                "error": outcome.error,
                "ref": ref,
                "name": name,
                "market_symbol": outcome.market_symbol,
            },
            2,
        )
    return _emit(
        {
            "schema_version": _OUTPUT_SCHEMA,
            "operation": args.operation,
            "ok": False,
            "outcome": "internal",
            "error": outcome.error or "watchlist_apply_failed",
            "ref": ref,
            "name": name,
            "market_symbol": outcome.market_symbol,
        },
        1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
