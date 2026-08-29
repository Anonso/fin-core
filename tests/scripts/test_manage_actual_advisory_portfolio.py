"""Operator CLI contract for the actual advisory portfolio."""

from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NOW = datetime(2026, 8, 3, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(*, alias: str) -> dict[str, object]:
    return {
        "schema_version": "actual-advisory-portfolio.v1",
        "confirmation": "USER_CONFIRMED",
        "source_kind": "USER_CONFIRMED_MANUAL",
        "positions_complete": True,
        "account_alias": alias,
        "as_of": "2026-08-03T09:50:00+08:00",
        "net_assets": "10000.00",
        "available_cash": "5000.00",
        "margin_debt": None,
        "positions": [
            {
                "symbol": "600000.SH",
                "name": "示例银行甲",
                "total_shares": 100,
                "sellable_shares": 100,
                "average_cost": "49.00",
                "snapshot_price": "50.00",
                "market_value": "5000.00",
            }
        ],
    }


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_validate_show_and_preview_are_local_structured_operations(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    from scripts.manage_actual_advisory_portfolio import main

    config_home = tmp_path / "config"
    target = config_home / "fin-analyse" / "actual-advisory-portfolio.v1.json"
    _write(target, _payload(alias="已确认示例账户"))
    source = _write(tmp_path / "source" / "candidate.json", _payload(alias="候选示例账户"))
    environ = {"XDG_CONFIG_HOME": str(config_home)}

    def forbidden_socket(*_args, **_kwargs):
        raise AssertionError("actual advisory portfolio CLI must not use the network")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    assert main(["validate"], environ=environ, clock=lambda: NOW) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["status"] == "partial"
    assert validated["writes"] is False
    assert validated["snapshot_ref"].startswith("actual-advisory-snapshot-")
    assert "portfolio" not in validated

    assert main(["show"], environ=environ, clock=lambda: NOW) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["portfolio"]["account_alias"] == "已确认示例账户"
    assert shown["portfolio"]["positions"][0]["name"] == "示例银行甲"

    assert main(["preview", "--source", str(source)], environ=environ, clock=lambda: NOW) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview"
    assert preview["candidate_status"] == "partial"
    assert "ACTUAL_ADVISORY_MARGIN_DEBT_UNKNOWN" in preview["data_gaps"]
    assert preview["dry_run"] is True
    assert preview["writes"] is False
    assert str(source) not in json.dumps(preview, ensure_ascii=False)


def test_publish_requires_apply_and_exact_preview_revisions(tmp_path: Path, capsys) -> None:
    from scripts.manage_actual_advisory_portfolio import main

    config_home = tmp_path / "config"
    source = _write(tmp_path / "source" / "candidate.json", _payload(alias="候选示例账户"))
    environ = {"XDG_CONFIG_HOME": str(config_home)}

    assert main(["preview", "--source", str(source)], environ=environ, clock=lambda: NOW) == 0
    preview = json.loads(capsys.readouterr().out)
    arguments = [
        "publish",
        "--source",
        str(source),
        "--expected-current-revision",
        "MISSING",
        "--confirm-candidate-revision",
        preview["candidate_revision"],
    ]
    assert main(arguments, environ=environ, clock=lambda: NOW) == 1
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["writes"] is False
    assert dry_run["data_gaps"] == ["ACTUAL_ADVISORY_PUBLICATION_APPLY_REQUIRED"]
    assert not config_home.exists()

    assert main([*arguments, "--apply"], environ=environ, clock=lambda: NOW) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["status"] == "published"
    assert published["dry_run"] is False
    assert published["writes"] is True
    assert (config_home / "fin-analyse" / "actual-advisory-portfolio.v1.json").is_file()


def test_cli_errors_are_stable_and_do_not_expose_internal_details(capsys, monkeypatch) -> None:
    import scripts.manage_actual_advisory_portfolio as command

    class ExplodingOperator:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def publish(self, _request: object) -> object:
            raise RuntimeError("private implementation detail")

    monkeypatch.setattr(
        command,
        "ActualAdvisoryPortfolioPublicationOperator",
        ExplodingOperator,
    )
    result = command.main(
        [
            "publish",
            "--source",
            "/safe/source.json",
            "--expected-current-revision",
            "MISSING",
            "--confirm-candidate-revision",
            "sha256:" + "0" * 64,
            "--apply",
        ],
        clock=lambda: NOW,
    )

    assert result == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "ACTUAL_ADVISORY_PORTFOLIO_INTERNAL_ERROR"
    assert payload["writes"] is None
    assert payload["side_effects"] == "unknown"
    assert "private" not in json.dumps(payload)
