"""Bind one Agent consultation product to FIN-owned context and receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import (
    Mapping,
    Sequence,
)
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

from fin_analyse.guo_teacher_research.semantic_contract import (
    GuidanceContext,
    MultiAssetContext,
    PortfolioContext,
    SingleAssetContext,
)


def has_exact_g_product_binding(product: Mapping[str, Any], *, source_refs: Sequence[object], receipt_references: Sequence[object]) -> bool:
    """Whether one consumed G receipt is exact for this final product.

    The product owns the ordered ``(generation, source_ref)`` pairs. A
    consumed receipt must repeat those pairs exactly, as well as its compact
    ``source_refs`` projection. This supports mixed prefetched and capability
    G generations, where a legacy scalar receipt generation is insufficient.
    """
    product_pairs = _g_reference_pairs(product.get('shared_brain_references'))
    receipt_pairs = _g_reference_pairs(receipt_references)
    normalized_source_refs = _g_source_refs(source_refs)
    return bool(product_pairs is not None and receipt_pairs is not None and (normalized_source_refs is not None) and (product_pairs == receipt_pairs) and (normalized_source_refs == tuple((source_ref for _, source_ref in product_pairs))))

def _expected_binding(context: GuidanceContext) -> dict[str, object]:
    if isinstance(context, PortfolioContext):
        return {'scope_kind': context.kind, 'account_mode': context.account_mode, 'account_snapshot_ref': context.account_snapshot_ref, 'account_status': context.account_status or 'UNKNOWN', 'account_valid_until': context.valid_until.isoformat() if context.valid_until is not None else None}
    return {'scope_kind': context.kind, 'account_mode': 'UNSPECIFIED', 'account_snapshot_ref': None, 'account_status': 'NOT_REQUIRED', 'account_valid_until': None}

def _formal_subjects(context: GuidanceContext) -> list[tuple[object, object, object]]:
    if isinstance(context, PortfolioContext):
        return [(item.instrument.ticker, item.instrument.name, item.quantity) for item in context.positions]
    if isinstance(context, SingleAssetContext):
        return [(context.target.ticker, context.target.name, None)]
    if isinstance(context, MultiAssetContext):
        return [(item.ticker, item.name, None) for item in context.targets]
    return []

def _g_reference_pairs(value: object) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if not 0 < len(value) <= 32:
        return None
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        generation = item.get('generation')
        source_ref = item.get('source_ref')
        if not isinstance(generation, str) or not generation or (not isinstance(source_ref, str)) or (not source_ref):
            return None
        pairs.append((generation, source_ref))
    if len(pairs) != len(set(pairs)) or len({source_ref for _, source_ref in pairs}) != len(pairs):
        return None
    return tuple(pairs)

def _g_source_refs(value: Sequence[object]) -> tuple[str, ...] | None:
    if not 0 < len(value) <= 32:
        return None
    refs: list[str] = []
    for source_ref in value:
        if not isinstance(source_ref, str) or not source_ref:
            return None
        refs.append(source_ref)
    return tuple(refs) if len(refs) == len(set(refs)) else None

_MAX_SUBJECTS_PER_ROUND = 30

_MAX_RECEIPTS_PER_ROUND = 30

_SUBJECT_CAP_EXCEEDED = 'consultation_subject_cap_exceeded'

_SUBJECT_GENERATION_MIXED = 'consultation_subject_generation_mixed'

_SUBJECT_STALE_GAP = 'EVIDENCE_SUBJECT_STALE'

_SUBJECT_NO_RECEIPT_GAP = 'EVIDENCE_SUBJECT_NO_RECEIPT'

_SUBJECT_FOREIGN_GAP = 'EVIDENCE_SUBJECT_FOREIGN_PRINCIPAL'

_SUBJECT_HASH_MISMATCH_GAP = 'EVIDENCE_SUBJECT_PAYLOAD_HASH_MISMATCH'

_SUBJECT_PAPER_EXCLUDED_GAP = 'EVIDENCE_SUBJECT_PAPER_EXCLUDED'

_PORTFOLIO_AS_OF_TTL = timedelta(hours=24)

def _parse_receipt_instant(value: object) -> datetime | None:
    """Parse a receipt timestamp; naive → UTC; malformed → None."""
    if not isinstance(value, str) or not value:
        return None
    raw = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed

def _aware_evaluated_at(evaluated_at: datetime | str) -> datetime:
    """admission 统一按 aware UTC 比较（str 走同源解析；naive 补 UTC）。"""
    if isinstance(evaluated_at, str):
        parsed = _parse_receipt_instant(evaluated_at)
        if parsed is None:
            raise ValueError('evaluated_at is malformed')
        return parsed
    if evaluated_at.tzinfo is None:
        return evaluated_at.replace(tzinfo=UTC)
    return evaluated_at

def admit_consultation_subjects(*, raw_entries: Sequence[Mapping[str, object]], sidecar_receipts: Sequence[Mapping[str, object]], principal: Mapping[str, object], generation_identity: object, evaluated_at: datetime, replay: bool=False) -> dict[str, object]:
    """唯一纯 admission helper（设计 v2）：raw cap → validate → dedupe。

    输出 `{admitted, omitted_gaps, fatal_code}`；公开 subject 只含
    ticker/name/quantity（final schema additionalProperties=False）。
    纯函数：无状态、零写、同输入同输出。
    replay=True：stored immutable receipt 完整性重验（presence/hash/foreign/
    mixed 照查），不重施 freshness/TTL——stored product 已在原时点 admission，
    重放用原 as_of 语义、零新增写入。
    """
    if len(raw_entries) > _MAX_SUBJECTS_PER_ROUND or len(sidecar_receipts) > _MAX_RECEIPTS_PER_ROUND:
        return {'admitted': (), 'admitted_receipts': (), 'omitted_gaps': (), 'fatal_code': _SUBJECT_CAP_EXCEEDED}
    from fin_analyse.guo_teacher_research.failure_diagnostic_sink import is_valid_turn_key
    binding_key_valid = isinstance(generation_identity, str) and is_valid_turn_key(generation_identity)
    receipt_path_needed = bool(sidecar_receipts) or any(isinstance(entry, Mapping) and entry.get('kind') in {'market', 'focus'} for entry in raw_entries)
    if receipt_path_needed and (not binding_key_valid):
        return {'admitted': (), 'admitted_receipts': (), 'omitted_gaps': (), 'fatal_code': _SUBJECT_GENERATION_MIXED}
    for sidecar in sidecar_receipts:
        entry_turn_key = sidecar.get('turn_key')
        if not isinstance(entry_turn_key, str) or not is_valid_turn_key(entry_turn_key):
            return {'admitted': (), 'admitted_receipts': (), 'omitted_gaps': (), 'fatal_code': _SUBJECT_GENERATION_MIXED}
    evaluated = _aware_evaluated_at(evaluated_at)
    receipts = _receipts_by_ticker(sidecar_receipts)
    admitted: list[dict[str, object]] = []
    admitted_receipts: list[dict[str, object]] = []
    omitted_gaps: list[tuple[str, str]] = []
    kept: dict[str, tuple[object, object, object, object, object]] = {}
    order: list[str] = []
    for entry in raw_entries:
        ticker = entry.get('ticker')
        if not isinstance(ticker, str) or not ticker:
            continue
        name = entry.get('name') if isinstance(entry.get('name'), str) else None
        quantity = entry.get('quantity')
        kind = entry.get('kind') if isinstance(entry.get('kind'), str) else 'market'
        as_of = entry.get('as_of')
        evidence_id = entry.get('evidence_id')
        existing = kept.get(ticker)
        if existing is not None:
            if quantity is not None and existing[1] is None:
                kept[ticker] = (name or existing[0], quantity, kind, as_of, evidence_id)
            continue
        kept[ticker] = (name, quantity, kind, as_of, evidence_id)
        order.append(ticker)
    for ticker in order:
        name, quantity, kind, as_of, evidence_id = kept[ticker]
        if kind == 'paper':
            omitted_gaps.append((ticker, _SUBJECT_PAPER_EXCLUDED_GAP))
            continue
        if kind == 'portfolio':
            parsed_as_of = _parse_receipt_instant(as_of)
            if not replay and (parsed_as_of is None or evaluated - parsed_as_of >= _PORTFOLIO_AS_OF_TTL):
                omitted_gaps.append((ticker, _SUBJECT_STALE_GAP))
                continue
            admitted.append({'ticker': ticker, 'name': name, 'quantity': quantity})
            admitted_receipts.append({'ticker': ticker, 'kind': kind, 'as_of': as_of, 'payload_sha256': None, 'turn_key': generation_identity})
            continue
        receipt = _receipt_for_subject(receipts, ticker=ticker, evidence_id=evidence_id)
        if receipt is None:
            omitted_gaps.append((ticker, _SUBJECT_NO_RECEIPT_GAP))
            continue
        if receipt.get('turn_key') != generation_identity:
            omitted_gaps.append((ticker, _SUBJECT_FOREIGN_GAP))
            continue
        payload = receipt.get('payload')
        payload_sha256 = receipt.get('payload_sha256')
        if not _receipt_payload_hash_matches(payload, payload_sha256):
            omitted_gaps.append((ticker, _SUBJECT_HASH_MISMATCH_GAP))
            continue
        valid_until = _parse_receipt_instant(receipt.get('valid_until'))
        if not replay and (valid_until is None or evaluated >= valid_until):
            omitted_gaps.append((ticker, _SUBJECT_STALE_GAP))
            continue
        admitted.append({'ticker': ticker, 'name': name or receipt.get('name'), 'quantity': quantity})
        admitted_receipts.append({'ticker': ticker, 'kind': kind, 'payload_sha256': receipt.get('payload_sha256'), 'turn_key': receipt.get('turn_key'), 'captured_at': receipt.get('captured_at'), 'valid_until': receipt.get('valid_until'), 'as_of': receipt.get('as_of'), 'evidence_id': receipt.get('evidence_id')})
    return {'admitted': tuple(admitted), 'admitted_receipts': tuple(admitted_receipts), 'omitted_gaps': tuple(omitted_gaps), 'fatal_code': None}

def _receipt_payload_hash_matches(payload: object, payload_sha256: object) -> bool:
    """v3：payload_sha256 必填（64 hex）——缺失/非法一律拒绝（fail-closed）；
    payload 在场 → canonical JSON 的 SHA-256 必须等于 payload_sha256。"""
    if not isinstance(payload_sha256, str) or len(payload_sha256) != 64:
        return False
    try:
        int(payload_sha256, 16)
    except ValueError:
        return False
    if payload is None:
        return True
    if isinstance(payload, str):
        canonical = payload.encode('utf-8')
    else:
        try:
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (TypeError, ValueError):
            return False
    return hashlib.sha256(canonical).hexdigest() == payload_sha256

def _receipts_by_ticker(sidecar_receipts: Sequence[Mapping[str, object]]) -> dict[tuple[str, str], Mapping[str, object]]:
    """Flatten split-call attestations into receipts keyed by (symbol, evidence_id)。

    同 identity 多 receipt → 保留 captured_at 最新者（等时/缺失 → 首现稳定）。
    payload_sha256 只从 (symbol, evidence_id) 精确匹配的 digest 取——绝不按
    symbol 混用（r2 B1）。
    """
    receipts: dict[tuple[str, str], Mapping[str, object]] = {}
    for sidecar in sidecar_receipts:
        instruments = sidecar.get('instruments')
        if not isinstance(instruments, list):
            continue
        digests: dict[tuple[str, str], object] = {}
        raw_digests = sidecar.get('payload_digests')
        if isinstance(raw_digests, list):
            for digest_entry in raw_digests:
                if not isinstance(digest_entry, Mapping):
                    continue
                digest_symbol = digest_entry.get('symbol')
                digest_evidence = digest_entry.get('evidence_id')
                if isinstance(digest_symbol, str) and isinstance(digest_evidence, str):
                    digests[digest_symbol, digest_evidence] = digest_entry.get('payload_sha256')
        captured_at = _parse_receipt_instant(sidecar.get('captured_at'))
        for item in instruments:
            if not isinstance(item, Mapping):
                continue
            symbol = item.get('symbol')
            evidence_id = item.get('evidence_id')
            if not isinstance(symbol, str) or not isinstance(evidence_id, str):
                continue
            identity = (symbol, evidence_id)
            existing = receipts.get(identity)
            if existing is not None and (not _newer_captured_at(captured_at, existing.get('_captured_at'))):
                continue
            receipts[identity] = {'valid_until': sidecar.get('valid_until'), 'as_of': sidecar.get('as_of'), 'captured_at': sidecar.get('captured_at'), 'turn_key': sidecar.get('turn_key'), 'name': item.get('name'), 'evidence_id': evidence_id, 'payload': item.get('payload'), 'payload_sha256': digests.get(identity), '_captured_at': captured_at}
    return receipts

def _receipt_for_subject(receipts: Mapping[tuple[str, str], Mapping[str, object]], *, ticker: str, evidence_id: object) -> Mapping[str, object] | None:
    """market 条目按 (ticker, evidence_id) 精确匹配；focus 条目（无 evidence_id）
    只接受该 ticker 的**唯一** receipt——多 evidence 并存 → None（歧义拒绝）。"""
    if isinstance(evidence_id, str) and evidence_id:
        return receipts.get((ticker, evidence_id))
    matches = [r for (sym, _eid), r in receipts.items() if sym == ticker]
    if len(matches) == 1:
        return matches[0]
    return None

def _newer_captured_at(candidate: datetime | None, existing: object) -> bool:
    """True 当 candidate 严格新于 existing；任一缺失 → 保留既有（首现稳定）。"""
    if candidate is None or not isinstance(existing, datetime):
        return False
    return candidate > existing
