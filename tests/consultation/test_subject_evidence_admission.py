"""Phase 0.2 RED #1-#3 —— 唯一纯 admission helper 的机械判定测试。

RED #0 fixture 见 subject_evidence_fixtures_20260816；本文件按 design v2
冻结结果逐路径测试（13 选股 admission / 逐路径拒绝 / cap fail-closed）。
"""

from __future__ import annotations

import hashlib

from fin_analyse.consultation.product_binding import admit_consultation_subjects
from tests.consultation.subject_evidence_fixtures_20260816 import (
    ADMIT_ORACLE,
    ALL_SYMBOLS,
    FOREIGN_TURN_KEY,
    FRESH_SYMBOLS,
    REJECT_GAP_ORACLE,
    TURN_KEY,
    sidecar_attestations,
)

PRINCIPAL = {"namespace": "fixture-ns", "principal_id": "finp_fixture"}
GENERATION = TURN_KEY
EVALUATED_AT = "2026-08-15T15:31:00+08:00"


def _call(raw_entries, sidecar_receipts, **overrides):
    kwargs = {
        "raw_entries": raw_entries,
        "sidecar_receipts": sidecar_receipts,
        "principal": PRINCIPAL,
        "generation_identity": GENERATION,
        "evaluated_at": EVALUATED_AT,
    }
    kwargs.update(overrides)
    return admit_consultation_subjects(**kwargs)


def _entries(symbols=ALL_SYMBOLS):
    return [
        {"ticker": symbol, "name": f"fixture-stock-{symbol.split('.')[0]}", "quantity": None}
        for symbol in symbols
    ]


def test_thirteen_stock_fixture_admits_receipt_backed_and_rejects_stale_and_missing() -> None:
    """冻结结果 1+2+4：13 选股同题，6+ receipt-backed subjects 通过 admission。

    - 11 个 fresh receipt → admitted；
    - stale（valid_until 已过）→ 拒绝该 subject + EVIDENCE_SUBJECT_STALE；
    - 无 receipt → 拒绝该 subject + EVIDENCE_SUBJECT_NO_RECEIPT。
    """
    outcome = _call(_entries(), sidecar_attestations())
    admitted_symbols = {item["ticker"] for item in outcome["admitted"]}
    assert admitted_symbols == {s for s, ok in ADMIT_ORACLE.items() if ok}
    assert len(admitted_symbols) >= 6
    omitted_by_ticker = dict(outcome["omitted_gaps"])
    for symbol, expected_gap in REJECT_GAP_ORACLE.items():
        assert omitted_by_ticker.get(symbol) == expected_gap
    assert outcome["fatal_code"] is None


def test_duplicate_ticker_keeps_formal_entry_with_quantity() -> None:
    """冻结结果 2：duplicate → 去重保留最新（formal 有 quantity 优先，顺序稳定）。"""
    entries = _entries(FRESH_SYMBOLS[:2]) + [
        {"ticker": FRESH_SYMBOLS[0], "name": "fixture-formal-name", "quantity": 200},
    ]
    outcome = _call(entries, sidecar_attestations())
    admitted = {item["ticker"]: item for item in outcome["admitted"]}
    assert len(admitted) == 2
    assert admitted[FRESH_SYMBOLS[0]]["quantity"] == 200
    assert admitted[FRESH_SYMBOLS[0]]["name"] == "fixture-formal-name"


def test_foreign_turn_key_rejects_that_batch_subjects() -> None:
    """冻结结果 2：foreign 由 principal-scoped route 派生的 turn_key 指纹承载
    （attestation 层无独立 principal 投影，in-design no-seam 记录）——
    有效但异值的 turn_key → 该批 subjects 逐条拒绝
    （EVIDENCE_SUBJECT_FOREIGN_PRINCIPAL），其余批照常 admitted。"""
    foreign = sidecar_attestations()[0].copy()
    foreign["turn_key"] = FOREIGN_TURN_KEY
    outcome = _call(_entries(), [foreign] + sidecar_attestations()[1:])
    assert outcome["fatal_code"] is None
    omitted = dict(outcome["omitted_gaps"])
    for symbol in FRESH_SYMBOLS[:5]:
        assert omitted[symbol] == "EVIDENCE_SUBJECT_FOREIGN_PRINCIPAL"
    admitted = {item["ticker"] for item in outcome["admitted"]}
    assert admitted == set(FRESH_SYMBOLS[5:])


def test_sidecar_missing_turn_key_is_whole_binding_fatal() -> None:
    """v3：turn_key 必填——sidecar 条目缺失 → 整轮 fatal（fail-closed，不静默通过）。"""
    missing = sidecar_attestations()[0].copy()
    missing.pop("turn_key")
    outcome = _call(_entries(), [missing] + sidecar_attestations()[1:])
    assert outcome["fatal_code"] == "consultation_subject_generation_mixed"
    assert outcome["admitted"] == ()


def test_payload_hash_mismatch_rejects_subject() -> None:
    """冻结结果 2：hash 不匹配 → 拒绝该 subject。"""
    tampered = sidecar_attestations()[0].copy()
    instrument = tampered["instruments"][0].copy()
    instrument["payload"] = {"ticker": FRESH_SYMBOLS[0], "close": 12.34}
    tampered["instruments"] = [instrument] + tampered["instruments"][1:]
    for digest_entry in tampered["payload_digests"]:
        if digest_entry["symbol"] == FRESH_SYMBOLS[0]:
            digest_entry["payload_sha256"] = hashlib.sha256(b"different").hexdigest()
    outcome = _call(_entries(), [tampered] + sidecar_attestations()[1:])
    omitted_by_ticker = dict(outcome["omitted_gaps"])
    assert omitted_by_ticker[FRESH_SYMBOLS[0]] == "EVIDENCE_SUBJECT_PAYLOAD_HASH_MISMATCH"


def test_subject_cap_thirty_one_raw_entries_is_fatal() -> None:
    """冻结结果 1：物理 cap 在 dedupe 前按 raw entries 计数，超限 fail-closed。"""
    entries = _entries()
    while len(entries) < 31:
        entries.append({"ticker": f"6002{len(entries):02d}.SH", "name": "x", "quantity": None})
    outcome = _call(entries, sidecar_attestations())
    assert outcome["fatal_code"] == "consultation_subject_cap_exceeded"
    assert outcome["admitted"] == ()


def test_receipt_cap_thirty_one_sidecars_is_fatal() -> None:
    """冻结结果 1：receipts ≤30，超限 fail-closed。"""
    sidecars = [sidecar_attestations()[0]] * 31
    outcome = _call(_entries(), sidecars)
    assert outcome["fatal_code"] == "consultation_subject_cap_exceeded"


def test_portfolio_entry_fresh_as_of_admits_without_market_receipt() -> None:
    """TTL v2：portfolio kind 用 typed snapshot as_of（24h 界），不需 market receipt。"""
    entry = {"ticker": "600100.SH", "name": "pos", "quantity": 100,
             "kind": "portfolio", "as_of": "2026-08-15T14:00:00+08:00"}
    outcome = _call([entry], [])
    assert [item["ticker"] for item in outcome["admitted"]] == ["600100.SH"]
    assert outcome["omitted_gaps"] == ()
    assert outcome["fatal_code"] is None


def test_portfolio_entry_stale_as_of_omitted() -> None:
    """TTL v2：as_of 超过 24h → 从 subjects OMIT + EVIDENCE_SUBJECT_STALE。"""
    entry = {"ticker": "600100.SH", "name": "pos", "quantity": 100,
             "kind": "portfolio", "as_of": "2026-08-13T10:00:00+08:00"}
    outcome = _call([entry], [])
    assert outcome["admitted"] == ()
    assert dict(outcome["omitted_gaps"]) == {"600100.SH": "EVIDENCE_SUBJECT_STALE"}


def test_portfolio_entry_missing_as_of_omitted() -> None:
    """TTL v2：portfolio 无 as_of → 无 freshness 证明，按 stale 拒绝。"""
    entry = {"ticker": "600100.SH", "name": "pos", "quantity": 100, "kind": "portfolio"}
    outcome = _call([entry], [])
    assert outcome["admitted"] == ()
    assert dict(outcome["omitted_gaps"]) == {"600100.SH": "EVIDENCE_SUBJECT_STALE"}


def test_paper_entry_excluded_from_admission() -> None:
    """TTL v2：PAPER 排除出 subject admission（即使有 fresh market receipt）。"""
    entry = {"ticker": FRESH_SYMBOLS[0], "name": "paper-pos", "quantity": 100, "kind": "paper"}
    outcome = _call([entry], sidecar_attestations())
    assert outcome["admitted"] == ()
    assert dict(outcome["omitted_gaps"]) == {
        FRESH_SYMBOLS[0]: "EVIDENCE_SUBJECT_PAPER_EXCLUDED"
    }


def test_focus_entry_requires_exact_market_receipt() -> None:
    """TTL v2：typed/user/watchlist target 必须取得 exact market receipt。"""
    entry = {"ticker": "600300.SH", "name": "target", "quantity": None, "kind": "focus"}
    outcome = _call([entry], sidecar_attestations())
    assert outcome["admitted"] == ()
    assert dict(outcome["omitted_gaps"]) == {"600300.SH": "EVIDENCE_SUBJECT_NO_RECEIPT"}


def test_malformed_valid_until_rejects_subject_as_stale() -> None:
    """TTL v2：缺失/非法 valid_until → 拒绝（无隐式日历）。"""
    malformed = sidecar_attestations()[0].copy()
    malformed["valid_until"] = "not-a-date"
    outcome = _call(_entries(), [malformed] + sidecar_attestations()[1:])
    omitted = dict(outcome["omitted_gaps"])
    assert omitted[FRESH_SYMBOLS[0]] == "EVIDENCE_SUBJECT_STALE"
