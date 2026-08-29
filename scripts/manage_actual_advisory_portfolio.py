#!/usr/bin/env python3
"""Validate, inspect, preview, or publish the actual advisory portfolio."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fin_analyse.portfolio.actual_advisory import (  # noqa: E402
    ActualAdvisoryPortfolioPublicationOperator,
    ActualAdvisoryPortfolioPublicationRequest,
    ActualAdvisoryPortfolioStore,
    actual_advisory_snapshot_ref,
)

_OUTPUT_SCHEMA = "actual-advisory-portfolio-management-result.v1"
_CN_TZ = ZoneInfo("Asia/Shanghai")


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("validate", help="Validate the fixed snapshot without displaying it")
    commands.add_parser("show", help="Display the validated user-confirmed snapshot")
    preview = commands.add_parser("preview", help="Validate a candidate with zero writes")
    preview.add_argument("--source", required=True, type=Path)
    publish = commands.add_parser("publish", help="CAS-publish a previously previewed candidate")
    publish.add_argument("--source", required=True, type=Path)
    publish.add_argument("--expected-current-revision", required=True)
    publish.add_argument("--confirm-candidate-revision", required=True)
    publish.add_argument("--apply", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    operation = "unknown"
    try:
        args = _parser().parse_args(argv)
        operation = str(args.operation)
        active_clock = clock or (lambda: datetime.now(_CN_TZ))
        if operation in {"preview", "publish"}:
            operator = ActualAdvisoryPortfolioPublicationOperator(
                environ=environ,
                clock=active_clock,
            )
            apply = operation == "publish" and bool(args.apply)
            if operation == "preview":
                result = operator.preview(args.source)
            else:
                result = operator.publish(
                    ActualAdvisoryPortfolioPublicationRequest(
                        source=args.source,
                        candidate_revision=str(args.confirm_candidate_revision),
                        expected_current_revision=str(args.expected_current_revision),
                        apply=apply,
                    )
                )
            publication_payload = {
                **_base(operation, dry_run=not apply, writes=result.writes_state),
                "status": result.status.lower(),
                "candidate_status": (
                    result.candidate_status.lower() if result.candidate_status is not None else None
                ),
                "candidate_revision": result.candidate_revision,
                "current_revision": result.current_revision,
                "confirmation_required": result.confirmation_required,
                "preview": result.preview,
                "data_gaps": list(result.reason_codes),
            }
            _print(publication_payload)
            return 0 if result.status in {"PREVIEW", "PUBLISHED", "EXACT_REPLAY"} else 1

        read_result = ActualAdvisoryPortfolioStore(
            environ=environ,
            clock=active_clock,
        ).read()
        if read_result.snapshot is None:
            reason = (
                read_result.reason_codes[0].value
                if read_result.reason_codes
                else "ACTUAL_ADVISORY_PORTFOLIO_INVALID"
            )
            _print(_error(operation, reason))
            return 1
        snapshot = read_result.snapshot
        read_payload: dict[str, Any] = {
            **_base(operation, dry_run=True, writes=False),
            "status": read_result.status.value.lower(),
            "portfolio_schema_version": snapshot.schema_version,
            "snapshot_ref": actual_advisory_snapshot_ref(snapshot.revision),
            "as_of": snapshot.as_of.isoformat(),
            "valid_until": snapshot.valid_until.isoformat(),
            "position_count": len(snapshot.positions),
            "data_gaps": [reason.value for reason in read_result.reason_codes],
        }
        if operation == "show":
            read_payload["portfolio"] = snapshot.to_safe_dict()
        _print(read_payload)
        return 0
    except _ArgumentError:
        _print(_error(operation, "ACTUAL_ADVISORY_PORTFOLIO_INPUT_INVALID"))
        return 2
    except Exception:
        _print(
            {
                **_error(
                    operation,
                    "ACTUAL_ADVISORY_PORTFOLIO_INTERNAL_ERROR",
                    writes=None if operation == "publish" else False,
                ),
                "side_effects": "unknown" if operation == "publish" else "none",
            }
        )
        return 3


def _base(
    operation: str,
    *,
    dry_run: bool,
    writes: bool | None,
) -> dict[str, Any]:
    return {
        "schema_version": _OUTPUT_SCHEMA,
        "operation": operation,
        "dry_run": dry_run,
        "writes": writes,
        "network_used": False,
        "real_broker_connected": False,
        "real_execution_allowed": False,
    }


def _error(
    operation: str,
    code: str,
    *,
    writes: bool | None = False,
) -> dict[str, Any]:
    return {
        **_base(operation, dry_run=operation != "publish", writes=writes),
        "status": "unknown",
        "error_code": code,
    }


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
