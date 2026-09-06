"""cognition_replay_lib 单元测试：闭集校验、原子写、口径计算。"""

from __future__ import annotations

import json

import pytest

from fin_analyse.guo_teacher_research import cognition_replay_lib as lib


def _mapping(jargon_sha: str) -> dict:
    return {
        "schema_version": "fin.cognition-replay-mapping/v1",
        "updated_at": "2026-09-06",
        "jargon_source_sha256": jargon_sha,
        "entries": {"老登": {"group_id": "legacy_value", "meaning": "x",
                             "confidence": "owner_confirmed", "kind": "指代",
                             "evidence": [], "note": ""}},
        "groups": {"legacy_value": {"codes": ["sz399986"], "weights": [1.0],
                                    "role": "benchmark"}},
    }


def test_mapping_ok_and_sha_pin(tmp_path):
    ok = _mapping("a" * 64)
    assert lib.validate_mapping(ok, "a" * 64)["entries"]["老登"]["group_id"] == "legacy_value"
    with pytest.raises(lib.ReplaySchemaError, match="sha256 mismatch"):
        lib.validate_mapping(ok, "b" * 64)


def test_mapping_weights_must_sum_to_one():
    bad = _mapping("a" * 64)
    bad["groups"]["legacy_value"]["weights"] = [0.5]
    with pytest.raises(lib.ReplaySchemaError, match="sum to 1"):
        lib.validate_mapping(bad, "a" * 64)


def _spec(**over):
    s = {
        "check_id": "CHK-X", "unit_id": "CU-X", "claim_type": "forecast",
        "caliber": "relative_spread",
        "window": {"start": "2026-08-27", "end": None,
                   "maturity_rule": "owner_pinned_pending"},
        "start_anchor": "锚", "params": {},
        "mainline_group": "m", "benchmark_group": "b", "status": "active",
    }
    s.update(over)
    return s


def test_specs_guardrails():
    ok = {"schema_version": "fin.cognition-replay-specs/v1", "specs": [_spec()]}
    lib.validate_specs(ok)
    for over, msg in [
        ({"start_anchor": ""}, "start_anchor"),
        ({"window": {"start": "2026-08-27", "end": None, "maturity_rule": "tbd"}},
         "owner_pinned_pending"),
        ({"mainline_group": None}, "mainline"),
        ({"caliber": "same_day"}, "not derivable"),
        ({"status": "maybe"}, "closed set"),
    ]:
        with pytest.raises(lib.ReplaySchemaError, match=msg):
            lib.validate_specs(
                {"schema_version": "fin.cognition-replay-specs/v1", "specs": [_spec(**over)]}
            )


def test_snapshot_line_closed_keys():
    line = {
        "schema_version": "fin.cognition-replay-snapshot/v1",
        "batch": "20260904", "as_of": "2026-09-04", "group_id": "g", "role": "reference",
        "adjustment_basis": "index_raw", "retrieved_at": "t", "source": "s", "note": None,
        "by_code": {"sh000001": {"2026-09-04": {"open": 1.0, "high": 1.0, "low": 1.0,
                                                "close": 1.0, "volume": 0}}},
    }
    lib.validate_snapshot_line(line)
    bad = dict(line, surprise=1)
    with pytest.raises(lib.ReplaySchemaError, match="unknown keys"):
        lib.validate_snapshot_line(bad)
    bad_role = dict(line, role="oracle")
    with pytest.raises(lib.ReplaySchemaError, match="closed set"):
        lib.validate_snapshot_line(bad_role)


def test_nomination_open_must_not_carry_relation():
    n = {"schema_version": "fin.cognition-replay-nomination/v1", "batch": "b",
         "as_of": "2026-09-04", "nominations": [
             {"check_id": "c", "unit_id": "u", "maturity": "open",
              "proposed_relation": None, "computed": {}, "members_snapshot": {}}]}
    lib.validate_nomination(n)
    n["nominations"][0]["proposed_relation"] = "supports"
    with pytest.raises(lib.ReplaySchemaError):
        lib.validate_nomination(n)
    n["nominations"][0].update(maturity="matured", proposed_relation="maybe")
    with pytest.raises(lib.ReplaySchemaError, match="closed set"):
        lib.validate_nomination(n)


def test_basket_return_and_gaps():
    by_code = {
        "a": {"2026-08-01": {"close": 100.0}, "2026-08-02": {"close": 110.0}},
        "b": {"2026-08-01": {"close": 50.0}, "2026-08-02": {"close": 45.0}},
    }
    ret, err = lib.basket_return(by_code, ["a", "b"], [0.5, 0.5], "2026-08-01", "2026-08-02")
    assert err is None
    assert abs(ret - (0.5 * 0.10 + 0.5 * -0.10)) < 1e-9
    ret, err = lib.basket_return({"a": {}}, ["a", "b"], [0.5, 0.5], "2026-08-01", "2026-08-02")
    assert ret is None and "no series" in err


def test_range_check_counts():
    rows = {"c": {f"2026-08-0{i}": {"close": 3950.0 + i, "low": 3850.0 + i}
                  for i in range(1, 4)}}
    res = lib.range_check(rows, "c", "2026-08-01", "2026-08-31", 3900.0, 4000.0, "close")
    assert res["covered_days"] == 3 and res["inside_days"] == 3 and not res["outside_days"]
    res = lib.range_check(rows, "c", "2026-08-01", "2026-08-31", None, None, "low")
    assert res["min_value"] == 3851.0 and res["apply_to"] == "low"


def test_monthly_anchor_and_pit():
    rows = {
        "2026-07-31": {"close": 3832.26},
        "2026-08-03": {"open": 3812.61, "close": 3809.66},
        "2026-08-31": {"open": 3926.53, "close": 3986.30},
    }
    a = lib.monthly_anchor(rows, "2026-08")
    assert abs(a["close"] - 3986.30) < 1e-9
    assert abs(a["open"] - 3812.61) < 1e-9
    assert abs(a["monthly_ret"] - (3986.30 / 3832.26 - 1.0)) < 1e-9
    # 发文日 8/19 无 8/18 行时取最近前收（8/03）；有 8/18 行则取 8/18
    assert lib.last_closed_trading_day(rows, "2026-08-19") == "2026-08-03"
    rows2 = dict(rows, **{"2026-08-18": {"close": 3990.30}})
    assert lib.last_closed_trading_day(rows2, "2026-08-19") == "2026-08-18"
    assert lib.same_day_close_available("2026-08-11T15:30:00+08:00", "2026-08-11")
    assert not lib.same_day_close_available("2026-08-11T12:47:00+08:00", "2026-08-11")


def test_resolve_state_root_respects_xdg(tmp_path, monkeypatch):
    root = lib.resolve_state_root(environ={"XDG_STATE_HOME": str(tmp_path)})
    assert root == tmp_path / "fin-analyse" / "cognition-replay-evidence"
    assert (root.stat().st_mode & 0o777) == 0o700


# ── snapshot CLI：硬错误守卫与 require-today 门 ───────────────

def _bootstrap_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    root = lib.resolve_state_root(environ={"XDG_STATE_HOME": str(tmp_path)})
    jargon = json.loads((tmp_path / "jargon.json").read_text()) if False else None
    import hashlib
    jargon_path = lib.Path(__file__).resolve().parents[2] / "config" / "zsxq_jargon.json"
    jargon = json.loads(jargon_path.read_text())
    sha = lib.canonical_sha256(jargon)
    lib.atomic_write_json(root / "mapping.v1.json", {
        "schema_version": "fin.cognition-replay-mapping/v1",
        "updated_at": "2026-09-06",
        "jargon_source_sha256": sha,
        "entries": {"老登": {"group_id": "g1", "meaning": "x", "confidence": "owner_confirmed",
                             "kind": "指代", "evidence": [], "note": ""}},
        "groups": {"g1": {"codes": ["sh000001"], "weights": [1.0], "role": "reference"}},
    })
    lib.atomic_write_json(root / "specs.v1.json", {
        "schema_version": "fin.cognition-replay-specs/v1",
        "specs": [_spec()],
    })
    return root


def _load_snapshot_cli():
    import importlib.util
    path = lib.Path(__file__).resolve().parents[2] / "scripts" / "cognition_replay_snapshot.py"
    spec = importlib.util.spec_from_file_location("crs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_snapshot_cli_all_fetch_fail_is_hard_error(tmp_path, monkeypatch, capsys):
    from fin_analyse.market.providers.akshare import AKShareProvider
    root = _bootstrap_state(tmp_path, monkeypatch)
    mod = _load_snapshot_cli()
    monkeypatch.setattr(AKShareProvider, "get_index_history", lambda self, t, s, e: [])
    monkeypatch.setattr("sys.argv", ["crs", "--as-of", "20260904", "--require-today", "--apply"])
    rc = mod.main()
    assert rc == 1
    assert '"mode": "error"' in capsys.readouterr().out
    assert not (root / "snapshot-20260904.jsonl").exists()


def test_snapshot_cli_require_today_skips_without_close_row(tmp_path, monkeypatch, capsys):
    from fin_analyse.market.providers.base import OHLCV
    from fin_analyse.market.providers.akshare import AKShareProvider
    root = _bootstrap_state(tmp_path, monkeypatch)
    mod = _load_snapshot_cli()
    rows = [OHLCV(date="2026-09-03", open=1, high=1, low=1, close=3900.0, volume=0)]

    def fake(self, t, s, e):
        return [r for r in rows if s <= r.date <= e]
    monkeypatch.setattr(AKShareProvider, "get_index_history", fake)
    monkeypatch.setattr("sys.argv", ["crs", "--as-of", "20260904", "--require-today", "--apply"])
    rc = mod.main()
    assert rc == 0
    assert '"mode": "skip"' in capsys.readouterr().out
    assert not (root / "snapshot-20260904.jsonl").exists()
