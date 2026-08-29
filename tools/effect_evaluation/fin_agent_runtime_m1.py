"""FIN Agent Runtime M1 Evaluation Harness — lightweight effect evaluation.

Historical M1 effect evidence only. Legacy treatment identifiers are retained for
reproducibility and must not be copied into current production routing.

Design: docs/architecture/fin-domain-kernel-agent-runtime.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os as _os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from fin_analyse.guo_teacher_research.agent_runtime import AgentRunRequest

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

_PREREGISTERED_PAIR_IDS: tuple[str, ...] = (
    "pair-helium-01",
    "pair-helium-02",
    "pair-helium-03",
    "pair-helium-04",
    "pair-helium-05",
)

_FIXED_MAPPING: dict[str, dict[str, str]] = {
    "pair-helium-01": {"a": "baseline", "b": "treatment"},
    "pair-helium-02": {"a": "treatment", "b": "baseline"},
    "pair-helium-03": {"a": "baseline", "b": "treatment"},
    "pair-helium-04": {"a": "treatment", "b": "baseline"},
    "pair-helium-05": {"a": "baseline", "b": "treatment"},
}

_VALID_JUDGMENT_CHOICES: frozenset[str] = frozenset({"A", "B", "tie", "review_required"})
_VALID_CONFIDENCE_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

_SOURCE_FORBIDDEN_LEVELS: frozenset[str] = frozenset({"g_direct", "teacher_direct"})

# Exact Golden helium question
_GOLDEN_QUESTION = "氦气涨价和供给紧张现在怎么看，国产替代到哪一步？"

_HELIUM_ARTICLE_ID = "01a99e429a3d"
_HELIUM_ARTICLE_REL_PATH = "knowledge-base/articles/20260708_01a99e429a3d.md"

_RESEARCH_PRODUCT_CONTRACT_ID = "research_product"
_DISPLAY_PRODUCT_CONTRACT_ID = "display_product"

# Allowlisted keys for blind packet payloads — everything else must be stripped
_BLIND_ALLOWLISTED_TOP_KEYS: frozenset[str] = frozenset(
    {
        "research_product",
        "display_product",
    }
)
_BLIND_FORBIDDEN_SUBSTR: tuple[str, ...] = (
    "provider",
    "backend",
    "model",
    "usage",
    "gate",
    "runtime",
    "mapping",
    "FIN",
    "baseline",
    "treatment",
    "planner_source",
    "source_label",
    "article_id",
    "article_excerpt",
    "article_path",
    "evidence_context",
    "codex",
    "sonnet",
    "opus",
    "haiku",
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. assemble_batch
# ═══════════════════════════════════════════════════════════════════════════════


def assemble_batch(
    *,
    model: str,
    question: str = _GOLDEN_QUESTION,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Build exactly 5 preregistered baseline/treatment pairs with fixed mapping."""
    if not model or not model.strip() or model.strip().lower() in ("default", "unknown"):
        raise ValueError(f"An explicit model ID is required, got {model!r}")

    pairs: list[dict[str, Any]] = []

    for pair_id in _PREREGISTERED_PAIR_IDS:
        mapping = _FIXED_MAPPING[pair_id]
        pair: dict[str, Any] = {"pair_id": pair_id, "_mapping": dict(mapping)}

        for arm_key in ("a", "b"):
            arm_type = mapping[arm_key]
            arm = _build_arm_request(
                arm_type=arm_type,
                model=model,
                question=question,
                timeout_seconds=timeout_seconds,
                label=arm_key.upper(),
            )
            pair[arm_key] = arm

        pairs.append(pair)

    return {"pairs": pairs}


def _build_arm_request(
    *,
    arm_type: str,
    model: str,
    question: str,
    timeout_seconds: float,
    label: str,
) -> dict[str, Any]:
    common_envelope = {
        "model": model,
        "backend": "codex",
        "timeout_seconds": timeout_seconds,
        "call_cap": 1,
        "read_only": True,
        "ephemeral": True,
        "token_envelope": "provider_default",
        "token_cap_enforced": False,
        "workspace": {"type": "tmp_empty", "path": ""},
        "label": label,
        "question": question,
        "budget": {
            "reasoning_effort": "low",
            "max_tool_calls": 0,
        },
        "opaque_continuation": {},
        # Strict top-level boundary booleans required for risk gate
        "advisory_only": True,
        "execution_allowed": False,
        "human_confirmation_required": True,
    }

    boundaries_dict = {
        "advisory_only": True,
        "execution_allowed": False,
        "human_confirmation_required": True,
    }
    if arm_type == "baseline":
        return {
            **common_envelope,
            "use_case_ref": "generic_research_answer",
            "context_pack": {},
            "capability_scope": {},
            "product_contracts": [],
            "boundaries": dict(boundaries_dict),
        }

    # Treatment arm
    evidence = _load_helium_evidence()
    treatment_context = {
        "context_pack_id": "ctx-helium-m1",
        "source_scope": {"recent_reference": {"available": True}},
        "evidence_context": evidence,
    }
    treatment_capability = {
        "use_case_id": "agent_analyze_general",
        "capability_name": "agent_analyze",
        "task_type": "auto",
        "product_contract_ids": [_RESEARCH_PRODUCT_CONTRACT_ID, _DISPLAY_PRODUCT_CONTRACT_ID],
    }
    treatment_contracts = _build_treatment_contracts()

    return {
        **common_envelope,
        "use_case_ref": "agent_analyze_general",
        "context_pack": treatment_context,
        "capability_scope": treatment_capability,
        "product_contracts": treatment_contracts,
        "boundaries": dict(boundaries_dict),
        "_treatment_note": (
            "M1 treatment tests FIN context/evidence plus contract/guard "
            "scaffolding, not proven live FIN tool-calling gain."
        ),
    }


def _load_helium_evidence() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]
    article_path = project_root / _HELIUM_ARTICLE_REL_PATH
    excerpt = ""
    if article_path.exists():
        raw = article_path.read_text(encoding="utf-8")
        excerpt = raw[:3000]
    else:
        excerpt = (
            "氦气供给紧张，卡塔尔/俄罗斯扰动导致30%-38%供给中断，"
            "全球氦气市场面临结构性短缺。5N涨约4倍，6N有价无市。"
            "BOG提氦和新疆四川试产是国产替代关键路径。"
        )
    return {
        "article_id": _HELIUM_ARTICLE_ID,
        "article_excerpt": excerpt,
        "source_bucket": "recent_reference",
        "source_level": "recent_reference",
        "source_label": (
            "source classification preserved: bucket=recent_reference, "
            "level=recent_reference; must not be promoted"
        ),
    }


def _build_treatment_contracts() -> list[dict[str, Any]]:
    from fin_analyse.guo_teacher_research.product_contracts import AgentProductContractRegistry

    registry = AgentProductContractRegistry()
    contracts: list[dict[str, Any]] = []
    for cid in (_RESEARCH_PRODUCT_CONTRACT_ID, _DISPLAY_PRODUCT_CONTRACT_ID):
        contract = registry.get(cid)
        if contract is None:
            continue
        contracts.append(
            {
                "contract_id": contract.contract_id,
                "version": contract.version,
                "required_fields": list(contract.required_fields),
                "forbidden_fields": list(contract.forbidden_fields),
            }
        )
    return contracts


# ═══════════════════════════════════════════════════════════════════════════════
# 2. validate_hard_gates — arm-type-aware, fail-closed vetoes
# ═══════════════════════════════════════════════════════════════════════════════


def validate_hard_gates(
    result: dict[str, Any],
    *,
    arm_type: str = "treatment",
) -> dict[str, Any]:
    """Run independent hard-gate vetoes.  *arm_type* must be 'baseline' or 'treatment'."""
    gates: dict[str, bool] = {
        "schema": _gate_schema(result),
        "source": _gate_source(result, arm_type=arm_type),
        "cognition": _gate_cognition(result),
        "risk": _gate_risk(result),
        "write_side_effect": _gate_write_side_effect(result),
    }
    gates["all_passed"] = all(gates.values())
    return gates


def _gate_schema(result: dict[str, Any]) -> bool:
    from fin_analyse.guo_teacher_research.product_contracts import AgentProductContractValidator

    payload = result.get("payload", {})
    if not isinstance(payload, dict) or not payload:
        return False
    validator = AgentProductContractValidator()
    research = payload.get("research_product")
    if not isinstance(research, dict):
        return False
    if not validator.validate(_RESEARCH_PRODUCT_CONTRACT_ID, research).valid:
        return False
    display = payload.get("display_product")
    if not isinstance(display, dict):
        return False
    if not validator.validate(_DISPLAY_PRODUCT_CONTRACT_ID, display).valid:
        return False
    cb = display.get("confidence_boundary")
    if not isinstance(cb, dict):
        return False
    return {"advisory_only", "execution_allowed", "human_confirmation_required"}.issubset(cb.keys())


def _gate_source(result: dict[str, Any], *, arm_type: str) -> bool:
    payload = result.get("payload", {})
    if not isinstance(payload, dict):
        return False
    research = payload.get("research_product")
    if not isinstance(research, dict):
        return False  # must have valid research_product

    source_level = research.get("source_level", "")

    if arm_type == "treatment":
        # Treatment MUST have source_level EXACTLY recent_reference
        if source_level != "recent_reference":
            return False
        # Must have explicit source_attribution with BOTH recent_reference
        # classification AND not-teacher/G-direct disclaimer semantics
        attribution = research.get("source_attribution", "")
        if not isinstance(attribution, str) or not attribution.strip():
            return False
        attr_lower = attribution.lower()
        # Must explicitly classify as recent_reference
        if "recent_reference" not in attr_lower:
            return False
        # Must explicitly disclaim teacher/G-direct cognition
        return not (
            "not teacher" not in attr_lower
            and "not g_direct" not in attr_lower
            and "must not be promoted" not in attr_lower
        )

    # Baseline: must NOT contain helium article id, path, excerpt, or treatment-only metadata
    rp_str = json.dumps(research)
    if _HELIUM_ARTICLE_ID in rp_str:
        return False
    # Generic baseline answers may describe source quality as a JSON object.
    # Scan the serialized value so structured output cannot crash the gate or
    # hide a forbidden cognition label in a nested field.
    source_level_text = json.dumps(source_level, ensure_ascii=False).lower()
    return not any(level in source_level_text for level in _SOURCE_FORBIDDEN_LEVELS)


def _gate_cognition(result: dict[str, Any]) -> bool:
    if result.get("status") != "ok":
        return False
    opaque = result.get("opaque_continuation", {})
    if opaque:
        return False
    # capability_trace must be explicitly present and be a list
    if "capability_trace" not in result:
        return False
    trace = result.get("capability_trace")
    if not isinstance(trace, list):
        return False
    # No cognition_write / memory_write entries
    for entry in trace:
        if isinstance(entry, dict) and entry.get("type") in ("cognition_write", "memory_write"):
            return False
    return True


def _gate_risk(result: dict[str, Any]) -> bool:
    from fin_analyse.guo_teacher_research.product_contracts import (
        AgentProductContractValidator,
    )

    payload = result.get("payload", {})
    if not isinstance(payload, dict):
        return False

    # REQUIRED strict top-level boundary booleans
    if payload.get("advisory_only") is not True:
        return False
    if payload.get("execution_allowed") is not False:
        return False
    if payload.get("human_confirmation_required") is not True:
        return False

    # REQUIRED: research_product must exist
    research = payload.get("research_product")
    if not isinstance(research, dict):
        return False

    # REQUIRED: display_product must exist
    display = payload.get("display_product")
    if not isinstance(display, dict):
        return False

    # REQUIRED: display_product must have confidence_boundary with boundary booleans
    cb = display.get("confidence_boundary")
    if not isinstance(cb, dict):
        return False
    if cb.get("advisory_only") is not True:
        return False
    if cb.get("execution_allowed") is not False:
        return False
    if cb.get("human_confirmation_required") is not True:
        return False

    # The canonical contracts recursively reject every forbidden execution field.
    validator = AgentProductContractValidator()
    return (
        validator.validate(_RESEARCH_PRODUCT_CONTRACT_ID, research).valid
        and validator.validate(_DISPLAY_PRODUCT_CONTRACT_ID, display).valid
    )


def _gate_write_side_effect(result: dict[str, Any]) -> bool:
    """Require explicit provenance: read_only=True, isolated_workspace=True,
    workspace_persisted=False, write_detected=False."""
    provenance = result.get("provenance", {})
    if not isinstance(provenance, dict):
        return False
    if provenance.get("read_only") is not True:
        return False
    if provenance.get("isolated_workspace") is not True:
        return False
    if provenance.get("workspace_persisted") is not False:
        return False
    return provenance.get("write_detected") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. evaluate_comparability — envelope-aware, measured facts
# ═══════════════════════════════════════════════════════════════════════════════


def evaluate_comparability(
    *,
    arm_a_result: dict[str, Any],
    arm_b_result: dict[str, Any],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    """Compare two arms against each other and the preregistered envelope.

    Requires EVERY preregistered envelope fact to be present and matching
    in both arms' run_facts.  Compares both arms to the envelope AND to
    each other.  actual_model must come from sanitized runtime provenance.
    """
    reasons: list[str] = []

    # Both status ok
    for label, res in (("arm_a", arm_a_result), ("arm_b", arm_b_result)):
        if res.get("status") != "ok":
            reasons.append(f"{label} status is not ok")

    # ── Provenance must be a dict with nonblank backend and model ──
    env_model = envelope.get("model", "")
    env_backend = envelope.get("backend", "codex")
    for label, res in (("arm_a", arm_a_result), ("arm_b", arm_b_result)):
        prov = res.get("provenance", {})
        if not isinstance(prov, dict):
            reasons.append(f"{label} provenance is missing or not a dict")
            continue
        prov_backend = prov.get("backend", "")
        prov_model = prov.get("model", "")
        if not prov_backend or not isinstance(prov_backend, str) or not prov_backend.strip():
            reasons.append(f"{label} provenance.backend is blank or missing")
        elif prov_backend != env_backend:
            reasons.append(
                f"{label} backend mismatch: expected={env_backend}, actual={prov_backend}"
            )
        if not prov_model or not isinstance(prov_model, str) or not prov_model.strip():
            reasons.append(f"{label} provenance.model is blank or missing")
        elif prov_model != env_model:
            reasons.append(f"{label} model mismatch: declared={env_model}, actual={prov_model}")

    # ── Preregistered envelope facts every arm must satisfy ──
    _required_facts: dict[str, Any] = {
        "question": envelope.get("question", ""),
        "timeout_seconds": envelope.get("timeout_seconds", 0),
        "call_count": 1,
        "call_cap": 1,
        "read_only": True,
        "ephemeral": True,
        "token_envelope": "provider_default",
        "token_cap_enforced": False,
        "generic_tools": "none",
        "workspace_type": "tmp_empty",
        "workspace_initially_empty": True,
        "isolated_workspace": True,
        "workspace_persisted": False,
        "write_detected": False,
    }
    _required_facts_conditional: dict[str, Any] = {}
    _env_reasoning_effort = envelope.get("reasoning_effort")
    if _env_reasoning_effort is not None and _env_reasoning_effort != "":
        _required_facts_conditional["reasoning_effort"] = _env_reasoning_effort
    _env_max_tool_calls = envelope.get("max_tool_calls")
    if _env_max_tool_calls is not None:
        _required_facts_conditional["max_tool_calls"] = _env_max_tool_calls
    _required_facts = {**_required_facts, **_required_facts_conditional}

    arm_facts: dict[str, dict[str, Any]] = {}
    for label, res in (("arm_a", arm_a_result), ("arm_b", arm_b_result)):
        facts = res.get("run_facts", {})
        if not isinstance(facts, dict) or not facts:
            reasons.append(f"{label} missing run_facts")
            arm_facts[label] = {}
            continue
        arm_facts[label] = facts

        # Check every required fact
        for fact_key, expected_value in _required_facts.items():
            if fact_key not in facts:
                reasons.append(f"{label} missing run_fact: {fact_key}")
            else:
                actual_val = facts[fact_key]
                expected = expected_value
                # question: compare to envelope question
                if fact_key == "question":
                    expected = envelope.get("question", "")
                elif fact_key == "timeout_seconds":
                    expected = envelope.get("timeout_seconds", 0)
                if actual_val != expected:
                    reasons.append(f"{label} {fact_key}={actual_val!r}, expected {expected!r}")

        # actual_model must match provenance model (not fabricated from request)
        # and must be nonblank
        prov = res.get("provenance", {})
        prov_model = ""
        if isinstance(prov, dict):
            prov_model = prov.get("model", "")
        facts_model = facts.get("actual_model", "")
        if not facts_model or not isinstance(facts_model, str) or not facts_model.strip():
            reasons.append(f"{label} actual_model is blank or missing")
        else:
            if prov_model and facts_model != prov_model:
                reasons.append(
                    f"{label} actual_model={facts_model!r} != provenance model={prov_model!r}"
                )
            if facts_model != env_model:
                reasons.append(
                    f"{label} actual_model mismatch: declared={env_model}, actual={facts_model}"
                )

    # ── Cross-arm comparison: both arms must agree on all facts ──
    if arm_facts.get("arm_a") and arm_facts.get("arm_b"):
        for fact_key in (
            "question",
            "timeout_seconds",
            "call_cap",
            "read_only",
            "ephemeral",
            "token_envelope",
            "token_cap_enforced",
            "generic_tools",
            "workspace_type",
            "workspace_initially_empty",
            "call_count",
            "isolated_workspace",
            "reasoning_effort",
            "max_tool_calls",
        ):
            val_a = arm_facts["arm_a"].get(fact_key)
            val_b = arm_facts["arm_b"].get(fact_key)
            if val_a != val_b:
                reasons.append(f"cross-arm mismatch: {fact_key}: arm_a={val_a!r}, arm_b={val_b!r}")

    # ── Cross-arm provenance backend must be identical ──
    prov_a = (
        arm_a_result.get("provenance", {})
        if isinstance(arm_a_result.get("provenance"), dict)
        else {}
    )
    prov_b = (
        arm_b_result.get("provenance", {})
        if isinstance(arm_b_result.get("provenance"), dict)
        else {}
    )
    backend_a = prov_a.get("backend", "")
    backend_b = prov_b.get("backend", "")
    if (
        isinstance(backend_a, str)
        and isinstance(backend_b, str)
        and backend_a.strip()
        and backend_b.strip()
        and backend_a != backend_b
    ):
        reasons.append(f"cross-arm backend mismatch: arm_a={backend_a!r}, arm_b={backend_b!r}")

    # ── Provenance markers for policy violations ──
    for label, res in (("arm_a", arm_a_result), ("arm_b", arm_b_result)):
        prov = res.get("provenance", {})
        if isinstance(prov, dict):
            if prov.get("workspace_type") not in (None, "tmp_empty", ""):
                reasons.append(
                    f"{label} workspace_type not tmp_empty: {prov.get('workspace_type')}"
                )
            if prov.get("token_cap_enforced") is True:
                reasons.append(f"{label} token_cap_enforced=True (policy overrun)")

    # ── Reject bool in resource_usage ──
    for label, res in (("arm_a", arm_a_result), ("arm_b", arm_b_result)):
        usage = res.get("resource_usage", {})
        if not isinstance(usage, dict) or not usage:
            reasons.append(f"{label} missing resource_usage")
        else:
            has_valid = False
            for k, v in usage.items():
                if isinstance(v, bool):
                    reasons.append(f"{label} resource_usage[{k}] is bool, not numeric")
                elif isinstance(v, (int, float)) and v > 0:
                    has_valid = True
            if not has_valid:
                reasons.append(f"{label} resource_usage has no positive numeric values")

    # ── No opaque_continuation ──
    for label, res in (("arm_a", arm_a_result), ("arm_b", arm_b_result)):
        opaque = res.get("opaque_continuation", {})
        if opaque:
            reasons.append(f"{label} has non-empty opaque_continuation")

    return {"comparable": len(reasons) == 0, "reasons": reasons}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. build_blind_packets — recursive fail-closed allowlisted projection
# ═══════════════════════════════════════════════════════════════════════════════


def build_blind_packets(
    batch: dict[str, Any],
    run_records: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    question = _GOLDEN_QUESTION
    if batch["pairs"]:
        question = batch["pairs"][0]["a"].get("question", question)

    blind_pairs: list[dict[str, Any]] = []
    for pair in batch["pairs"]:
        pid = pair["pair_id"]
        blind: dict[str, Any] = {"pair_id": pid}
        for arm_key in ("a", "b"):
            payload: dict[str, Any] = {}
            if run_records and pid in run_records:
                rec = run_records[pid].get(arm_key, {})
                raw_payload = rec.get("payload", {})
                payload = _sanitize_payload_for_blind(raw_payload)
            blind[arm_key] = {"label": arm_key.upper(), "payload": payload}
        blind_pairs.append(blind)

    result = {
        "case_id": "helium-supply-gap-m1",
        "common_question": question,
        "rubric": _BLIND_RUBRIC,
        "pairs": blind_pairs,
    }

    # Recursive leak assertion before returning
    _assert_no_blind_leaks(result)
    return result


def _assert_no_blind_leaks(obj: Any, path: str = "$") -> None:
    """Recursively verify no provider/FIN/identity fields or strings leak into blind packets."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = key.lower()
            for forbidden in _BLIND_FORBIDDEN_SUBSTR:
                if forbidden.lower() in key_lower:
                    raise ValueError(f"Blind packet leak: forbidden key '{key}' at {path}")
            _assert_no_blind_leaks(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _assert_no_blind_leaks(item, f"{path}[{idx}]")
    elif isinstance(obj, str):
        # Check string values for identity tokens using word-boundary split
        words = re.split(r"\W+", obj.lower())
        for word in words:
            if not word:
                continue
            for forbidden in _BLIND_FORBIDDEN_SUBSTR:
                if word == forbidden.lower():
                    raise ValueError(
                        f"Blind packet leak: identity token '{word}' in value at {path}"
                    )
    # scalars: skip


_BLIND_RUBRIC = {
    "criteria": [
        "evidence_integration",
        "analytical_depth",
        "clarity",
        "source_attribution",
        "safety_boundary_adherence",
    ],
    "instructions": (
        "For each pair, compare arm A and arm B and choose A, B, tie, or "
        "review_required. Judge based on the quality of the research analysis "
        "and display output, not on the underlying system or data source."
    ),
}


def _sanitize_payload_for_blind(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursive fail-closed projection: only allowlisted top-level keys survive."""
    return _recursive_blind_project(payload, is_top_level=True)


def _recursive_blind_project(obj: Any, *, is_top_level: bool = False) -> Any:
    """Recursively project to allowlisted shape, stripping all provider identity.

    Top-level: only _BLIND_ALLOWLISTED_TOP_KEYS survive.
    Nested: strip keys matching _BLIND_FORBIDDEN_SUBSTR.
    String values: redact identity tokens with [redacted].
    """
    if isinstance(obj, dict):
        if is_top_level:
            # Top level: ONLY allowlisted keys
            result: dict[str, Any] = {}
            for key in _BLIND_ALLOWLISTED_TOP_KEYS:
                if key in obj:
                    result[key] = _recursive_blind_project(obj[key], is_top_level=False)
            return result
        else:
            # Nested: strip forbidden keys
            result: dict[str, Any] = {}
            for key, value in obj.items():
                key_lower = key.lower()
                skip = False
                for forbidden in _BLIND_FORBIDDEN_SUBSTR:
                    if forbidden.lower() in key_lower:
                        skip = True
                        break
                if skip:
                    continue
                result[key] = _recursive_blind_project(value, is_top_level=False)
            return result
    elif isinstance(obj, list):
        return [_recursive_blind_project(item, is_top_level=False) for item in obj]
    elif isinstance(obj, str):
        # Redact identity tokens from string values (word-boundary match)
        result = obj
        for forbidden in _BLIND_FORBIDDEN_SUBSTR:
            # Only redact single-word identity tokens (skip compound like "planner_source")
            if "_" not in forbidden and " " not in forbidden:
                result = re.sub(
                    r"\b" + re.escape(forbidden) + r"\b",
                    "[redacted]",
                    result,
                    flags=re.IGNORECASE,
                )
        return result
    else:
        return obj


# ═══════════════════════════════════════════════════════════════════════════════
# 5. finalize — fail-closed, structured judgments
# ═══════════════════════════════════════════════════════════════════════════════


def finalize(
    batch: dict[str, Any],
    judgments: dict[str, Any],
    run_records: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    pairs = batch["pairs"]
    pair_ids = {p["pair_id"] for p in pairs}

    # ── Validate all pair IDs present ──
    judgment_ids = set(judgments.keys())
    if judgment_ids != pair_ids:
        missing = pair_ids - judgment_ids
        extra = judgment_ids - pair_ids
        msg_parts = []
        if missing:
            msg_parts.append(f"missing judgments: {sorted(missing)}")
        if extra:
            msg_parts.append(f"extra judgments: {sorted(extra)}")
        raise ValueError(f"Judgment set mismatch: {'; '.join(msg_parts)}")

    # ── run_records must be nonempty and cover all pairs ──
    if not run_records:
        raise ValueError("run_records is empty — cannot finalize without run data")
    if set(run_records.keys()) != pair_ids:
        raise ValueError("Run records must cover all pair IDs")

    # ── Every pair must have both arms ──
    for pid in pair_ids:
        rec = run_records.get(pid, {})
        for arm_key in ("a", "b"):
            if arm_key not in rec or not rec.get(arm_key):
                raise ValueError(f"Missing arm '{arm_key}' in run_records for {pid}")

    # ── Validate judgments ──
    for pid, j in judgments.items():
        if not isinstance(j, dict):
            raise ValueError(f"Judgment for {pid} must be a dict with 'choice' field")
        choice = j.get("choice", "")
        if choice not in _VALID_JUDGMENT_CHOICES:
            raise ValueError(
                f"Invalid judgment choice {choice!r} for {pid}; "
                f"must be one of {sorted(_VALID_JUDGMENT_CHOICES)}"
            )
        reason = j.get("reason", "")
        if not reason or not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"Judgment for {pid} must have a nonempty 'reason' string")
        confidence = j.get("confidence", "")
        if confidence not in _VALID_CONFIDENCE_LEVELS:
            raise ValueError(
                f"Invalid confidence {confidence!r} for {pid}; "
                f"must be one of {sorted(_VALID_CONFIDENCE_LEVELS)}"
            )

    # ── Build per-pair results ──
    all_runtime_ok = True
    all_comparable = True
    all_hard_gates_passed = True
    treatment_wins = 0
    total_pairs = len(pairs)
    revealed_mapping: dict[str, dict[str, str]] = {}
    per_pair: list[dict[str, Any]] = []

    for pair in pairs:
        pid = pair["pair_id"]
        mapping = pair["_mapping"]
        treatment_key = "a" if mapping["a"] == "treatment" else "b"

        revealed_mapping[pid] = {"A": mapping["a"], "B": mapping["b"]}

        arms_runtime_ok = True
        arms_gates: dict[str, dict[str, Any]] = {}
        arms_comparable = True
        arms_comp_detail: dict[str, Any] = {}

        # Run hard gates with arm-type awareness
        for arm_key in ("a", "b"):
            rec = run_records[pid].get(arm_key, {})
            if rec.get("status") != "ok":
                arms_runtime_ok = False
                all_runtime_ok = False
            arm_type = mapping[arm_key]
            arms_gates[arm_key] = validate_hard_gates(rec, arm_type=arm_type)
            if not arms_gates[arm_key]["all_passed"]:
                all_hard_gates_passed = False

        # Comparability
        a_rec = run_records[pid].get("a", {})
        b_rec = run_records[pid].get("b", {})
        arm_a = pair["a"]
        arm_a_budget = arm_a.get("budget", {})
        envelope: dict[str, Any] = {
            "model": arm_a.get("model", ""),
            "backend": arm_a.get("backend", "codex"),
            "question": arm_a.get("question", ""),
            "timeout_seconds": arm_a.get("timeout_seconds", 0),
        }
        # Preregister budget policy from reconstructed arm budget for comparability
        if "reasoning_effort" in arm_a_budget:
            envelope["reasoning_effort"] = arm_a_budget["reasoning_effort"]
        if "max_tool_calls" in arm_a_budget:
            envelope["max_tool_calls"] = arm_a_budget["max_tool_calls"]
        arms_comp_detail = evaluate_comparability(
            arm_a_result=a_rec, arm_b_result=b_rec, envelope=envelope
        )
        if not arms_comp_detail["comparable"]:
            arms_comparable = False
            all_comparable = False

        if not arms_runtime_ok:
            all_runtime_ok = False

        judgment = judgments[pid]
        choice = judgment.get("choice", "")
        if choice == "review_required":
            all_comparable = False

        treatment_won = choice == treatment_key.upper()
        if treatment_won:
            treatment_wins += 1

        per_pair.append(
            {
                "pair_id": pid,
                "revealed": revealed_mapping[pid],
                "judgment": judgment,
                "treatment_won": treatment_won,
                "runtime_ok": arms_runtime_ok,
                "comparable": arms_comparable,
                "comparability_detail": arms_comp_detail,
                "hard_gates": arms_gates,
            }
        )

    passed = all_runtime_ok and all_comparable and all_hard_gates_passed and treatment_wins >= 4

    return {
        "passed": passed,
        "all_runtime_ok": all_runtime_ok,
        "all_comparable": all_comparable,
        "all_hard_gates_passed": all_hard_gates_passed,
        "semantic_result": {
            "treatment_wins": treatment_wins,
            "total_pairs": total_pairs,
            "threshold": 4,
            "met": treatment_wins >= 4,
        },
        "revealed_mapping": revealed_mapping,
        "pairs_total": total_pairs,
        "pairs_detail": per_pair,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CLI — run subcommand
# ═══════════════════════════════════════════════════════════════════════════════


def _build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.effect_evaluation.fin_agent_runtime_m1",
        description="FIN Agent Runtime M1 Evaluation Harness",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Execute the 10-arm M1 evaluation batch")
    run_parser.add_argument("--model", required=True, type=str, help="Explicit model ID")
    run_parser.add_argument("--output-dir", required=True, type=str, help="Output directory")
    run_parser.add_argument(
        "--timeout", type=float, default=300.0, help="Timeout per arm (default: 300)"
    )

    finalize_parser = sub.add_parser("finalize", help="Finalize judgments and reveal mapping")
    finalize_parser.add_argument(
        "--run-record", required=True, type=str, help="Path to run_record.json"
    )
    finalize_parser.add_argument(
        "--judgments", required=True, type=str, help="Path to judgments JSON"
    )
    finalize_parser.add_argument(
        "--output", required=True, type=str, help="Path for finalization summary"
    )

    return parser


def execute_run(
    *,
    model: str,
    output_dir: str,
    timeout_seconds: float = 300.0,
    adapter_factory: Any = None,
) -> dict[str, Any]:
    out = Path(output_dir)

    rr_path = out / "run_record.json"
    bp_path = out / "blind_packets.json"
    if rr_path.exists():
        raise FileExistsError(f"run_record.json already exists in {output_dir}")
    if bp_path.exists():
        raise FileExistsError(f"blind_packets.json already exists in {output_dir}")

    batch = assemble_batch(model=model, timeout_seconds=timeout_seconds)

    if adapter_factory is None:
        from fin_analyse.guo_teacher_research.codex_runtime import CodexCliAgentRuntimeAdapter

        def adapter_factory(*, model="", workspace_path=".", timeout_seconds=300.0):
            return CodexCliAgentRuntimeAdapter(
                model=model,
                workspace_path=workspace_path,
                timeout_seconds=timeout_seconds,
            )

    run_records: dict[str, dict[str, dict[str, Any]]] = {}

    for pair in batch["pairs"]:
        pid = pair["pair_id"]
        run_records[pid] = {}

        for arm_key in ("a", "b"):
            arm = pair[arm_key]
            tmp_ws = tempfile.TemporaryDirectory(prefix=f"m1-{pid}-{arm_key}-")
            ws_path = tmp_ws.name

            try:
                # ── Measure empty workspace before call ──
                before_files = set(_os.listdir(ws_path)) if _os.path.isdir(ws_path) else set()

                adapter = adapter_factory(
                    model=model, workspace_path=ws_path, timeout_seconds=timeout_seconds
                )
                request = _to_agent_run_request(arm, model=model, timeout_seconds=timeout_seconds)
                result = adapter.run(request)

                # ── Scan workspace after call ──
                after_files = set(_os.listdir(ws_path)) if _os.path.isdir(ws_path) else set()
                new_files = after_files - before_files
                workspace_persisted = len(new_files) > 0

                # ── Build sanitized run_facts ──
                # Never fall back to requested model — missing/empty stays blank
                actual_model = result.provenance.get("model", "")
                arm_budget = arm.get("budget", {})

                # Prefer observed runtime_policy from provenance over arm-requested
                # budget (bounded fallback for fake adapters that don't set it).
                observed_policy = result.provenance.get("runtime_policy", {})
                if isinstance(observed_policy, dict) and observed_policy:
                    run_reasoning_effort = observed_policy.get(
                        "reasoning_effort", arm_budget.get("reasoning_effort", "")
                    )
                    run_max_tool_calls = observed_policy.get(
                        "max_tool_calls", arm_budget.get("max_tool_calls")
                    )
                else:
                    run_reasoning_effort = arm_budget.get("reasoning_effort", "")
                    run_max_tool_calls = arm_budget.get("max_tool_calls")

                run_facts = {
                    "actual_model": actual_model,
                    "question": arm.get("question", ""),
                    "timeout_seconds": timeout_seconds,
                    "call_count": 1,
                    "call_cap": 1,
                    "read_only": True,
                    "ephemeral": True,
                    "token_envelope": "provider_default",
                    "token_cap_enforced": False,
                    "generic_tools": "none",
                    "workspace_type": "tmp_empty",
                    "workspace_initially_empty": True,
                    "isolated_workspace": True,
                    "workspace_persisted": workspace_persisted,
                    "write_detected": workspace_persisted,
                    "reasoning_effort": run_reasoning_effort,
                    "max_tool_calls": run_max_tool_calls,
                }

                run_records[pid][arm_key] = {
                    "status": result.status,
                    "payload": result.payload,
                    "data_gaps": list(result.data_gaps),
                    "capability_trace": list(result.capability_trace),
                    "provenance": {
                        "backend": result.provenance.get("backend", ""),
                        "model": result.provenance.get("model", ""),
                        "read_only": True,
                        "isolated_workspace": True,
                        "workspace_persisted": workspace_persisted,
                        "write_detected": workspace_persisted,
                    },
                    "resource_usage": dict(result.resource_usage),
                    "opaque_continuation": dict(result.opaque_runtime_continuation)
                    if result.opaque_runtime_continuation
                    else {},
                    "run_facts": run_facts,
                }
            finally:
                tmp_ws.cleanup()

    # ── Build run_record.json ──
    run_record_pairs: list[dict[str, Any]] = []
    for pair in batch["pairs"]:
        pid = pair["pair_id"]
        run_record_pairs.append(
            {
                "pair_id": pid,
                "_mapping": dict(pair["_mapping"]),
                "a": run_records[pid]["a"],
                "b": run_records[pid]["b"],
            }
        )

    run_record = {
        "case_id": "helium-supply-gap-m1",
        "model": model,
        "backend": "codex",
        "timeout_seconds": timeout_seconds,
        "runtime_policy": {
            "reasoning_effort": "low",
            "max_tool_calls": 0,
        },
        "pairs": run_record_pairs,
    }

    out.mkdir(parents=True, exist_ok=True)
    rr_path.write_text(json.dumps(run_record, ensure_ascii=False, indent=2), encoding="utf-8")

    blind = build_blind_packets(batch, run_records)
    bp_path.write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")

    return run_record


def _to_agent_run_request(
    arm: dict[str, Any], *, model: str, timeout_seconds: float
) -> AgentRunRequest:
    return AgentRunRequest(
        use_case_ref=arm.get("use_case_ref", ""),
        question=arm.get("question", ""),
        context_pack=arm.get("context_pack", {}),
        capability_scope=arm.get("capability_scope", {}),
        product_contracts=arm.get("product_contracts", []),
        boundaries=arm.get("boundaries", {}),
        model=model,
        timeout_seconds=timeout_seconds,
        budget=arm.get("budget", {}),
        opaque_runtime_continuation=arm.get("opaque_continuation", {}),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# finalize CLI helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _run_finalize_cli(args: argparse.Namespace) -> None:
    run_record_path = Path(args.run_record)
    judgments_path = Path(args.judgments)
    output_path = Path(args.output)

    if output_path.exists():
        raise FileExistsError(f"Output file already exists: {args.output}")

    run_data = json.loads(run_record_path.read_text(encoding="utf-8"))
    judgments_data = json.loads(judgments_path.read_text(encoding="utf-8"))

    batch = _reconstruct_batch_from_run_record(run_data)
    run_records = _reconstruct_run_records(run_data)

    summary = finalize(batch, judgments_data, run_records)

    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Finalization complete: {output_path}")
    print(f"Passed: {summary['passed']}")
    print(f"Treatment wins: {summary['semantic_result']['treatment_wins']}/5")


def _reconstruct_batch_from_run_record(run_data: dict[str, Any]) -> dict[str, Any]:
    stored_timeout = run_data.get("timeout_seconds", 300.0)
    stored_policy = run_data.get("runtime_policy", {})
    stored_budget = {}
    if isinstance(stored_policy, dict) and stored_policy:
        stored_budget = dict(stored_policy)
    pairs: list[dict[str, Any]] = []
    for pr in run_data.get("pairs", []):
        pair = {
            "pair_id": pr["pair_id"],
            "_mapping": dict(pr.get("_mapping", {})),
            "a": {
                "question": pr.get("a", {}).get("run_facts", {}).get("question", ""),
                "model": run_data.get("model", ""),
                "timeout_seconds": stored_timeout,
                "budget": dict(stored_budget),
            },
            "b": {
                "question": pr.get("b", {}).get("run_facts", {}).get("question", ""),
                "model": run_data.get("model", ""),
                "timeout_seconds": stored_timeout,
                "budget": dict(stored_budget),
            },
        }
        pairs.append(pair)
    return {"pairs": pairs}


def _reconstruct_run_records(run_data: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for pr in run_data.get("pairs", []):
        pid = pr["pair_id"]
        records[pid] = {"a": pr.get("a", {}), "b": pr.get("b", {})}
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# __main__
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = _build_run_parser()
    args = parser.parse_args()
    if args.command == "run":
        execute_run(model=args.model, output_dir=args.output_dir, timeout_seconds=args.timeout)
        print(f"Run complete. Artifacts in {args.output_dir}")
    elif args.command == "finalize":
        _run_finalize_cli(args)
    else:
        parser.print_help()
        sys.exit(1)
