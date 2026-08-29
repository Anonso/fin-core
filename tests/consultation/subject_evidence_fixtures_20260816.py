"""Phase 0.2 RED #0 —— production-shaped 脱敏 13 选股 fixture（设计审视 B6 关闭）。

形态对齐真实生产（reviewer 引用的 attestation 形状，agent_runtime.py:454-519）：
- 13 个 canonical symbols、4 批 split-call（5+5+1+1：三批 fresh + 独立 stale 批，
  每批 attestation ≤5 instruments）；
- 每批 attestation 携带 `fin.on-demand-tactical-context/v1` / external_reference / non_g /
  绝对 aware `valid_until` / evidence_id；
- 逐标 admission oracle：11 个 fresh receipt 应 admit，1 个 stale（valid_until 已过）应
  拒（EVIDENCE_SUBJECT_STALE），1 个无 receipt（名字出现在问题但无 attestation）应拒
  （EVIDENCE_SUBJECT_NO_RECEIPT）。

全部标的与名字为合成值，不含任何真实用户数据。
"""

from __future__ import annotations

import hashlib
import json

# 13 个合成 canonical symbols（脱敏；不引用用户自选股）。
FRESH_SYMBOLS: tuple[str, ...] = tuple(f"6001{i:02d}.SH" for i in range(0, 11))
STALE_SYMBOL: str = "600111.SH"
NO_RECEIPT_SYMBOL: str = "600112.SH"
ALL_SYMBOLS: tuple[str, ...] = (*FRESH_SYMBOLS, STALE_SYMBOL, NO_RECEIPT_SYMBOL)

# 真实分批形态：每批 ≤5 instruments（attestation 投影上限），13 输入 = 11 fresh +
# 1 stale + 1 no-receipt；批次 = 5 fresh + 5 fresh + 1 fresh + 1 stale（5+5+1+1）。
AS_OF: str = "2026-08-15T15:30:00+08:00"
SESSION_PHASE: str = "CONTINUOUS_TRADING"
FRESH_VALID_UNTIL: str = "2026-08-15T15:35:00+08:00"
STALE_VALID_UNTIL: str = "2026-08-15T15:25:00+08:00"
TURN_KEY: str = "fin.turn-idempotency/v1:" + "a" * 64
FOREIGN_TURN_KEY: str = "fin.turn-idempotency/v1:" + "b" * 64


def canonical_payload(symbol: str) -> str:
    """canonical JSON（sort_keys/separators/allow_nan=False）口径的 payload 摘要。"""
    return json.dumps(
        {"symbol": symbol, "kind": "fixture-payload"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def payload_sha256_for(symbol: str) -> str:
    return hashlib.sha256(canonical_payload(symbol).encode("utf-8")).hexdigest()


def _instrument(symbol: str, evidence_id: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"fixture-stock-{symbol.split('.')[0]}",
        "evidence_id": evidence_id,
        "status": "READY",
        "quote_observed_at": AS_OF,
        "reference_only": False,
        "manual_review_eligible": True,
        "latest_completed_bar_date": "2026-08-15",
        "completed_bar_count": 120,
        "payload": canonical_payload(symbol),
    }


def sidecar_attestations() -> list[dict[str, object]]:
    """4 批可信 attestation（5+5+1+1：三批 fresh + 独立 stale 批）。

    每批 ≤5 instruments；stale 批携带已过期 valid_until；NO_RECEIPT_SYMBOL
    不出现在任何 attestation 中。v3：全部批次必携带 turn_key/captured_at/
    per-instrument payload_sha256。
    """
    remaining = list(FRESH_SYMBOLS)
    fresh_batches = (5, 5)
    batches: list[dict[str, object]] = []
    for index, size in enumerate(fresh_batches):
        instruments = [
            _instrument(symbol, f"ev-fixture-{index * 5 + position:02d}")
            for position, symbol in enumerate(remaining[:size])
        ]
        del remaining[:size]
        batches.append(
            {
                "schema_version": "fin.on-demand-tactical-context/v1",
                "source_boundary": "a_share_on_demand_tactical_context",
                "source_kind": "external_reference",
                "source_trust": "non_g",
                "status": "READY",
                "as_of": AS_OF,
                "captured_at": AS_OF,
                "turn_key": TURN_KEY,
                "valid_until": FRESH_VALID_UNTIL,
                "session_phase": SESSION_PHASE,
                "payload_digests": [
                    {
                        "symbol": ins["symbol"],
                        "evidence_id": ins["evidence_id"],
                        "payload_sha256": payload_sha256_for(str(ins["symbol"])),
                    }
                    for ins in instruments
                ],
                "instruments": instruments,
            }
        )
    # 第三批：fresh 尾（1）；stale 独立一批（valid_until 已过：as_of 之后、
    # evaluated 之前）。
    batches.append(
        {
            "schema_version": "fin.on-demand-tactical-context/v1",
            "source_boundary": "a_share_on_demand_tactical_context",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "as_of": AS_OF,
            "captured_at": AS_OF,
            "turn_key": TURN_KEY,
            "valid_until": FRESH_VALID_UNTIL,
            "session_phase": SESSION_PHASE,
            "payload_digests": [
                {
                    "symbol": remaining[0],
                    "evidence_id": "ev-fixture-10",
                    "payload_sha256": payload_sha256_for(remaining[0]),
                }
            ],
            "instruments": [_instrument(remaining[0], "ev-fixture-10")],
        }
    )
    batches.append(
        {
            "schema_version": "fin.on-demand-tactical-context/v1",
            "source_boundary": "a_share_on_demand_tactical_context",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "as_of": AS_OF,
            "captured_at": AS_OF,
            "turn_key": TURN_KEY,
            "valid_until": STALE_VALID_UNTIL,
            "session_phase": SESSION_PHASE,
            "payload_digests": [
                {
                    "symbol": STALE_SYMBOL,
                    "evidence_id": "ev-fixture-stale",
                    "payload_sha256": payload_sha256_for(STALE_SYMBOL),
                }
            ],
            "instruments": [_instrument(STALE_SYMBOL, "ev-fixture-stale")],
        }
    )
    return batches


# 逐标 admission oracle（RED 测试与实现共用的机械判定基准）。
ADMIT_ORACLE: dict[str, bool] = dict.fromkeys(FRESH_SYMBOLS, True) | {
    STALE_SYMBOL: False,
    NO_RECEIPT_SYMBOL: False,
}

REJECT_GAP_ORACLE: dict[str, str] = {
    STALE_SYMBOL: "EVIDENCE_SUBJECT_STALE",
    NO_RECEIPT_SYMBOL: "EVIDENCE_SUBJECT_NO_RECEIPT",
}
