from __future__ import annotations

import json

from fin_analyse.market.instrument_directory import (
    RuntimeAshareInstrumentDirectory,
    verified_a_share_equity_venue,
)


def _write_directory(path, records, *, count=None) -> None:
    entries = {}
    for code, market, name in records:
        value = {"ticker": code, "market": market, "name": name}
        entries[code] = value
        entries[name] = value
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-23T00:00:00+08:00",
                "count": len(records) if count is None else count,
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_directory_rebuilds_exact_name_index_without_overwriting_ambiguity(tmp_path) -> None:
    path = tmp_path / "a_share_name_map.json"
    _write_directory(
        path,
        (
            ("600000", "上交所", "同名证券"),
            ("000001", "深交所", "同名证券"),
        ),
    )
    directory = RuntimeAshareInstrumentDirectory(path=path)

    assert [entry.symbol for entry in directory.lookup("同名证券")] == [
        "000001.SZ",
        "600000.SH",
    ]
    assert directory.lookup("600000")[0].name == "同名证券"


def test_directory_rejects_wrong_or_unsupported_venue_records(tmp_path) -> None:
    path = tmp_path / "a_share_name_map.json"
    _write_directory(
        path,
        (
            ("002409", "上交所", "错误交易所"),
            ("920000", "上交所", "不得误映射北交所"),
        ),
    )
    directory = RuntimeAshareInstrumentDirectory(path=path)

    assert directory.lookup("002409") == ()
    assert directory.lookup("错误交易所") == ()
    assert directory.lookup("920000") == ()
    assert directory.lookup("不得误映射北交所") == ()
    assert verified_a_share_equity_venue("920000") is None


def test_directory_missing_or_malformed_is_an_empty_read_only_source(tmp_path) -> None:
    missing = RuntimeAshareInstrumentDirectory(path=tmp_path / "missing.json")
    assert missing.lookup("雅克科技") == ()

    broken_path = tmp_path / "broken.json"
    broken_path.write_text('{"count": 1, "entries": {"002409": ', encoding="utf-8")
    broken = RuntimeAshareInstrumentDirectory(path=broken_path)
    assert broken.lookup("002409") == ()


def test_directory_reloads_after_operator_replaces_the_snapshot(tmp_path) -> None:
    path = tmp_path / "a_share_name_map.json"
    _write_directory(path, (("002409", "深交所", "雅克科技"),))
    directory = RuntimeAshareInstrumentDirectory(path=path)
    assert directory.lookup("雅克科技")[0].symbol == "002409.SZ"

    replacement = tmp_path / "replacement.json"
    _write_directory(replacement, (("600000", "上交所", "浦发银行"),))
    replacement.replace(path)

    assert directory.lookup("雅克科技") == ()
    assert directory.lookup("浦发银行")[0].symbol == "600000.SH"
