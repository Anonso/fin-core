"""Contract tests for the user-confirmed actual advisory portfolio."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator

import fin_analyse.common.owner_only_snapshot as owner_only_snapshot
from fin_analyse.portfolio.actual_advisory import (
    ActualAdvisoryPortfolioPublicationOperator,
    ActualAdvisoryPortfolioPublicationRequest,
    ActualAdvisoryPortfolioReason,
    ActualAdvisoryPortfolioStatus,
    ActualAdvisoryPortfolioStore,
    actual_advisory_snapshot_ref,
)

CN_TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 3, 10, 30, tzinfo=CN_TZ)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "actual-advisory-portfolio.v1",
        "confirmation": "USER_CONFIRMED",
        "source_kind": "USER_CONFIRMED_MANUAL",
        "positions_complete": True,
        "account_alias": "示例账户",
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
                "average_cost": "24.500",
                "snapshot_price": "25.000",
                "market_value": "2500.00",
            },
            {
                "symbol": "000001.SZ",
                "name": "示例银行乙",
                "total_shares": 200,
                "sellable_shares": None,
                "average_cost": "12.000",
                "snapshot_price": "12.500",
                "market_value": "2500.00",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def _target(config_home: Path, payload: dict[str, Any]) -> Path:
    return _write(
        config_home / "fin-analyse" / "actual-advisory-portfolio.v1.json",
        payload,
    )


def test_fresh_snapshot_is_typed_partial_when_margin_or_sellable_fact_is_unknown(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    _target(config_home, _payload())

    result = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    ).read()

    assert result.status is ActualAdvisoryPortfolioStatus.PARTIAL
    assert result.snapshot is not None
    assert result.snapshot.account_alias == "示例账户"
    assert result.snapshot.source_kind == "USER_CONFIRMED_MANUAL"
    assert result.snapshot.margin_debt is None
    assert result.snapshot.positions[0].snapshot_price is not None
    assert result.snapshot.positions[0].market_value is not None
    assert result.snapshot.positions[0].weight is not None
    assert result.snapshot.positions[0].weight == pytest.approx(0.25)
    assert ActualAdvisoryPortfolioReason.MARGIN_DEBT_UNKNOWN in result.reason_codes
    assert result.snapshot.revision.startswith("sha256:")
    assert result.snapshot.valid_until > NOW


def test_complete_fresh_snapshot_is_ready_and_stale_snapshot_remains_partial(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    complete = _payload(
        margin_debt="0.00",
        positions=[
            {
                "symbol": "600000.SH",
                "name": "示例银行甲",
                "total_shares": 100,
                "sellable_shares": 100,
                "average_cost": "49.000",
                "snapshot_price": "50.000",
                "market_value": "5000.00",
            },
        ],
    )
    _target(config_home, complete)
    store = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )
    assert store.read().status is ActualAdvisoryPortfolioStatus.READY

    stale = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)},
        clock=lambda: NOW + timedelta(days=2),
    ).read()
    assert stale.status is ActualAdvisoryPortfolioStatus.PARTIAL
    assert stale.snapshot is not None
    assert stale.reason_codes == (ActualAdvisoryPortfolioReason.STALE,)


def test_complete_fresh_empty_snapshot_is_ready_not_a_data_gap(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    _target(
        config_home,
        _payload(
            net_assets="80000.00",
            available_cash="80000.00",
            margin_debt="0.00",
            positions=[],
        ),
    )

    result = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    ).read()

    assert result.status is ActualAdvisoryPortfolioStatus.READY
    assert result.reason_codes == ()
    assert result.snapshot is not None
    assert result.snapshot.positions == ()


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("average_cost", ActualAdvisoryPortfolioReason.AVERAGE_COST_UNKNOWN),
        ("snapshot_price", ActualAdvisoryPortfolioReason.SNAPSHOT_PRICE_UNKNOWN),
        (
            "market_value",
            ActualAdvisoryPortfolioReason.SUPPLIED_MARKET_VALUE_UNKNOWN,
        ),
    ],
)
def test_unknown_position_facts_remain_partial_even_when_value_can_be_derived(
    tmp_path: Path,
    field: str,
    reason: ActualAdvisoryPortfolioReason,
) -> None:
    config_home = tmp_path / "config"
    position = {
        "symbol": "600000.SH",
        "name": "示例银行甲",
        "total_shares": 100,
        "sellable_shares": 100,
        "average_cost": "49.000",
        "snapshot_price": "50.000",
        "market_value": "5000.00",
    }
    position[field] = None
    _target(
        config_home,
        _payload(margin_debt="0.00", positions=[position]),
    )

    result = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    ).read()

    if field == "market_value":
        # 用户拍板 2026-08-21：份额×快照价可推导市值时不再报"市值缺失"。
        assert result.status is ActualAdvisoryPortfolioStatus.READY
        assert result.snapshot is not None
        assert result.snapshot.positions[0].market_value_derived is True
        assert result.snapshot.positions[0].market_value is not None
        assert (
            ActualAdvisoryPortfolioReason.SUPPLIED_MARKET_VALUE_UNKNOWN
            not in result.reason_codes
        )
        return
    assert result.status is ActualAdvisoryPortfolioStatus.PARTIAL
    assert reason in result.reason_codes


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            _payload(confirmation="SYSTEM_INFERRED"),
            ActualAdvisoryPortfolioReason.INVALID,
        ),
        (
            _payload(as_of="2026-08-03T11:00:00+08:00"),
            ActualAdvisoryPortfolioReason.FUTURE_AS_OF,
        ),
        (
            _payload(
                positions=[
                    {
                        "symbol": "600000.SH",
                        "name": "示例银行甲",
                        "total_shares": 100,
                        "sellable_shares": 101,
                        "average_cost": None,
                        "snapshot_price": None,
                        "market_value": None,
                    }
                ]
            ),
            ActualAdvisoryPortfolioReason.INVALID,
        ),
    ],
)
def test_invalid_or_future_snapshot_is_unknown(
    tmp_path: Path,
    payload: dict[str, Any],
    reason: ActualAdvisoryPortfolioReason,
) -> None:
    config_home = tmp_path / "config"
    _target(config_home, payload)

    result = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    ).read()

    assert result.status is ActualAdvisoryPortfolioStatus.UNKNOWN
    assert result.snapshot is None
    assert result.reason_codes == (reason,)


def test_missing_or_non_owner_only_snapshot_is_unknown_without_creating_state(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    store = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )
    missing = store.read()
    assert missing.status is ActualAdvisoryPortfolioStatus.UNKNOWN
    assert missing.reason_codes == (ActualAdvisoryPortfolioReason.MISSING,)
    assert not config_home.exists()

    target = _target(config_home, _payload())
    target.chmod(0o640)
    invalid = store.read()
    assert invalid.status is ActualAdvisoryPortfolioStatus.UNKNOWN
    assert invalid.reason_codes == (ActualAdvisoryPortfolioReason.INVALID,)


def test_preview_publish_is_cas_bound_owner_only_and_exact_replay_is_zero_write(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    source = _write(tmp_path / "source" / "confirmed.json", _payload())
    operator = ActualAdvisoryPortfolioPublicationOperator(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )

    preview = operator.preview(source)
    assert preview.status == "PREVIEW"
    assert preview.current_revision == "MISSING"
    assert preview.candidate_revision is not None
    assert preview.confirmation_required is True
    assert preview.writes_state is False
    assert preview.candidate_status == "PARTIAL"
    assert "ACTUAL_ADVISORY_MARGIN_DEBT_UNKNOWN" in preview.reason_codes
    assert preview.preview is not None
    assert preview.preview["confirmation"] == "USER_CONFIRMED"
    assert preview.preview["source_kind"] == "USER_CONFIRMED_MANUAL"
    assert preview.preview["positions"][0]["name"] == "示例银行甲"
    assert "path" not in json.dumps(preview.preview).lower()
    assert not config_home.exists()

    rejected = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=preview.candidate_revision,
            expected_current_revision="sha256:" + "0" * 64,
            apply=True,
        )
    )
    assert rejected.status == "REJECTED"
    assert rejected.writes_state is False
    assert not config_home.exists()

    published = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=preview.candidate_revision,
            expected_current_revision="MISSING",
            apply=True,
        )
    )
    target = config_home / "fin-analyse" / "actual-advisory-portfolio.v1.json"
    assert published.status == "PUBLISHED"
    assert published.writes_state is True
    assert published.candidate_status == "PARTIAL"
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    before = target.stat().st_mtime_ns

    replay = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=preview.candidate_revision,
            expected_current_revision=preview.candidate_revision,
            apply=True,
        )
    )
    assert replay.status == "EXACT_REPLAY"
    assert replay.writes_state is False
    assert target.stat().st_mtime_ns == before


def test_publish_failure_after_replace_reports_possible_write_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    target = _target(config_home, _payload(account_alias="旧示例账户"))
    source = _write(
        tmp_path / "source" / "candidate.json",
        _payload(account_alias="新示例账户"),
    )
    operator = ActualAdvisoryPortfolioPublicationOperator(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )
    preview = operator.preview(source)
    assert preview.candidate_revision is not None

    real_fsync = owner_only_snapshot.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory durability failure")
        real_fsync(descriptor)

    monkeypatch.setattr(owner_only_snapshot.os, "fsync", fail_directory_fsync)
    result = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=preview.candidate_revision,
            expected_current_revision=preview.current_revision,
            apply=True,
        )
    )

    assert result.status == "REJECTED"
    assert result.reason_codes == ("ACTUAL_ADVISORY_PUBLICATION_WRITE_FAILED",)
    assert result.writes_state is True
    assert "新示例账户" in target.read_text(encoding="utf-8")


def test_publish_attempt_failure_before_replace_is_not_reported_as_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    target = _target(config_home, _payload(account_alias="旧示例账户"))
    source = _write(
        tmp_path / "source" / "candidate.json",
        _payload(account_alias="新示例账户"),
    )
    operator = ActualAdvisoryPortfolioPublicationOperator(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )
    preview = operator.preview(source)
    assert preview.candidate_revision is not None

    def fail_temp_fsync(_descriptor: int) -> None:
        raise OSError("simulated temp fsync failure")

    monkeypatch.setattr(owner_only_snapshot.os, "fsync", fail_temp_fsync)
    result = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=preview.candidate_revision,
            expected_current_revision=preview.current_revision,
            apply=True,
        )
    )

    assert result.status == "REJECTED"
    assert result.reason_codes == ("ACTUAL_ADVISORY_PUBLICATION_WRITE_FAILED",)
    assert result.writes_state is True
    assert "旧示例账户" in target.read_text(encoding="utf-8")


def test_directory_created_before_validation_failure_is_reported_as_a_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    source = _write(tmp_path / "source" / "candidate.json", _payload())
    operator = ActualAdvisoryPortfolioPublicationOperator(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )
    preview = operator.preview(source)
    assert preview.candidate_revision is not None

    target_parent = config_home / "fin-analyse"
    real_fstat = owner_only_snapshot.os.fstat

    def invalidate_created_target_directory(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        descriptor_path = Path(f"/proc/self/fd/{descriptor}")
        if descriptor_path.resolve() == target_parent:
            values = list(metadata)
            values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(
        owner_only_snapshot.os,
        "fstat",
        invalidate_created_target_directory,
    )
    result = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=preview.candidate_revision,
            expected_current_revision="MISSING",
            apply=True,
        )
    )

    assert result.status == "REJECTED"
    assert result.reason_codes == ("ACTUAL_ADVISORY_PUBLICATION_TARGET_INVALID",)
    assert result.writes_state is True
    assert target_parent.is_dir()


def test_hardlink_and_symlink_sources_are_rejected(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    source = _write(tmp_path / "source" / "confirmed.json", _payload())
    hardlink = source.with_name("hardlink.json")
    os.link(source, hardlink)
    symlink = source.with_name("symlink.json")
    symlink.symlink_to(source)
    operator = ActualAdvisoryPortfolioPublicationOperator(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )

    assert operator.preview(hardlink).status == "REJECTED"
    assert operator.preview(symlink).status == "REJECTED"
    assert not config_home.exists()


def test_invalid_current_snapshot_is_distinct_from_invalid_source_and_never_overwritten(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config"
    target = _target(config_home, _payload(account_alias="旧示例账户"))
    source = _write(
        tmp_path / "source" / "confirmed.json",
        _payload(account_alias="新示例账户"),
    )
    operator = ActualAdvisoryPortfolioPublicationOperator(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    )
    valid_preview = operator.preview(source)
    assert valid_preview.status == "PREVIEW"
    assert valid_preview.candidate_revision is not None

    target.write_text("{", encoding="utf-8")
    target.chmod(0o600)
    preview = operator.preview(source)
    published = operator.publish(
        ActualAdvisoryPortfolioPublicationRequest(
            source=source,
            candidate_revision=valid_preview.candidate_revision,
            expected_current_revision=valid_preview.current_revision,
            apply=True,
        )
    )

    assert preview.reason_codes == ("ACTUAL_ADVISORY_PUBLICATION_CURRENT_INVALID",)
    assert published.reason_codes == ("ACTUAL_ADVISORY_PUBLICATION_CURRENT_INVALID",)
    assert published.writes_state is False
    assert target.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            positions=[
                {
                    "symbol": "600000.SH",
                    "name": "示例银行甲",
                    "total_shares": 100,
                    "sellable_shares": 100,
                    "average_cost": "24.500",
                    "snapshot_price": "25.000",
                    "market_value": "9000.00",
                }
            ]
        ),
        _payload(available_cash="1000.00"),
        _payload(positions_complete=False),
    ],
)
def test_inconsistent_or_incomplete_portfolio_is_unknown(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    config_home = tmp_path / "config"
    _target(config_home, payload)

    result = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    ).read()

    assert result.status is ActualAdvisoryPortfolioStatus.UNKNOWN
    assert result.reason_codes == (ActualAdvisoryPortfolioReason.INVALID,)


def test_published_schema_validates_the_runtime_contract() -> None:
    schema_path = Path(__file__).resolve().parents[2] / (
        "config/actual-advisory-portfolio.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    assert list(validator.iter_errors(_payload())) == []
    assert list(validator.iter_errors(_payload(margin_debt="unknown")))
    assert list(validator.iter_errors(_payload(positions_complete=False)))


def test_public_snapshot_ref_is_stable_and_rejects_noncanonical_revisions() -> None:
    revision = "sha256:" + "a" * 64
    assert actual_advisory_snapshot_ref(revision) == ("actual-advisory-snapshot-aaaaaaaaaaaaaaaa")
    with pytest.raises(ValueError):
        actual_advisory_snapshot_ref("not-a-revision")


def test_position_thesis_round_trips_and_legacy_snapshots_still_parse(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["positions"][0]["thesis"] = "战略金属重估，出口管制下稀缺性溢价"
    config_home = tmp_path / "config"
    _target(config_home, payload)

    result = ActualAdvisoryPortfolioStore(
        environ={"XDG_CONFIG_HOME": str(config_home)}, clock=lambda: NOW
    ).read()

    assert result.snapshot is not None
    assert result.snapshot.positions[0].thesis == "战略金属重估，出口管制下稀缺性溢价"
    # Legacy position without the thesis key parses with thesis=None.
    assert result.snapshot.positions[1].thesis is None
    assert result.snapshot.positions[0].to_safe_dict()["thesis"] == (
        "战略金属重估，出口管制下稀缺性溢价"
    )

    schema_path = Path(__file__).resolve().parents[2] / (
        "config/actual-advisory-portfolio.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(payload)) == []
    assert list(validator.iter_errors(_payload())) == []
