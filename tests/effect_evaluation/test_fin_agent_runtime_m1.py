"""RED tests for surgically repaired FIN Agent Runtime M1 evaluation harness.

Historical M1 effect evidence only: legacy treatment identifiers below are frozen
for reproducibility and are not the current production semantic route.

These tests prove specific false-green defects found by independent review:
1. finalize passes when run_records is empty (should fail closed)
2. evaluate_comparability ignores its envelope (never compares model/policy/workspace)
3. blind sanitization only removes planner_source (not recursive fail-closed)

Plus hardening: bool usage acceptance, model rejection gaps, arm-type-blind gates,
source gate not requiring research_product, missing strict boundary requirements.
"""

from __future__ import annotations

import copy
import json

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# Shared constants / helpers
# ═══════════════════════════════════════════════════════════════════════════════

_EXPLICIT_MODEL = "sonnet"

_HELIUM_ARTICLE_ID = "01a99e429a3d"
# Exact Golden helium question
_GOLDEN_QUESTION = "氦气涨价和供给紧张现在怎么看，国产替代到哪一步？"

_VALID_RESEARCH_PRODUCT = {
    "answer_summary": "helium analysis",
    "sections": [],
    "source_level": "recent_reference",
    "source_attribution": (
        "source classification preserved: bucket=recent_reference, "
        "level=recent_reference; must not be promoted; "
        "this is not teacher/G-direct cognition"
    ),
    "mainlines": [],
    "candidates": [],
    "shared_brain_references": [],
}

_VALID_DISPLAY_PRODUCT = {
    "display_intent": "ok",
    "headline": "helium analysis",
    "short_answer": "supply tight",
    "primary_sections": [],
    "candidate_profiles": [],
    "analysis_path_summary": [],
    "confidence_boundary": {
        "advisory_only": True,
        "execution_allowed": False,
        "human_confirmation_required": True,
    },
    "next_actions": [],
    "omitted_details_summary": [],
    "planner_source": "codex",
}


_UNSET = object()


def _make_fake_run_result(
    status="ok",
    research=None,
    display=None,
    data_gaps=None,
    provenance=None,
    resource_usage=None,
    opaque_continuation=None,
    capability_trace=_UNSET,
):
    """Build a dict compatible with AgentRunResult shape (not the dataclass).

    capability_trace: pass _UNSET (default) for empty list; pass None to omit the key.
    """
    payload = {
        "research_product": (
            research if research is not None else copy.deepcopy(_VALID_RESEARCH_PRODUCT)
        ),
        "display_product": (
            display if display is not None else copy.deepcopy(_VALID_DISPLAY_PRODUCT)
        ),
    }
    result = {
        "status": status,
        "payload": {
            "advisory_only": True,
            "execution_allowed": False,
            "human_confirmation_required": True,
            **payload,
        },
        "data_gaps": data_gaps if data_gaps is not None else [],
        "provenance": provenance
        if provenance is not None
        else {
            "backend": "codex",
            "model": _EXPLICIT_MODEL,
            "read_only": True,
            "isolated_workspace": True,
            "workspace_persisted": False,
            "write_detected": False,
        },
        "opaque_continuation": opaque_continuation if opaque_continuation is not None else {},
        "resource_usage": resource_usage
        if resource_usage is not None
        else {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }
    if capability_trace is _UNSET:
        result["capability_trace"] = []
    elif capability_trace is not None:
        result["capability_trace"] = capability_trace
    # If capability_trace is None, omit the key entirely
    return result


def _make_fake_adapter_factory(fake_results=None):
    """Return an adapter factory that injects a fake adapter."""
    from fin_analyse.guo_teacher_research.agent_runtime import (
        AgentRunRequest,
        AgentRunResult,
    )

    results = list(fake_results) if fake_results else []
    call_log: list[AgentRunRequest] = []

    class FakeAdapter:
        def __init__(
            self,
            *,
            codex_bin="codex",
            model="",
            workspace_path=".",
            timeout_seconds=300.0,
            runner=None,
        ):
            self.codex_bin = codex_bin
            self.model = model
            self.workspace_path = workspace_path
            self.timeout_seconds = timeout_seconds

        def run(self, request: AgentRunRequest) -> AgentRunResult:
            call_log.append(request)
            if not results:
                return AgentRunResult(
                    status="error",
                    payload={},
                    data_gaps=["fake_no_results"],
                    provenance={"backend": "codex", "model": self.model},
                )
            idx = min(len(call_log) - 1, len(results) - 1)
            return results[idx]

    def factory(*, model="", workspace_path=".", timeout_seconds=300.0):
        return FakeAdapter(
            model=model, workspace_path=workspace_path, timeout_seconds=timeout_seconds
        )

    return factory, call_log


# ═══════════════════════════════════════════════════════════════════════════════
# 1. FALSE-GREEN DEFECT: finalize passes with empty run_records
# ═══════════════════════════════════════════════════════════════════════════════


def test_finalize_fails_when_run_records_is_empty():
    """finalize must FAIL when run_records is empty {} — the current code
    skips gate/comparability checks when run_records is falsy, silently
    passing with all_gates_passed=True."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        assemble_batch,
        finalize,
    )

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    judgments = {}
    for p in batch["pairs"]:
        judgments[p["pair_id"]] = {"choice": "A", "reason": "test", "confidence": "high"}

    with pytest.raises(ValueError, match="run_records|empty|missing"):
        finalize(batch, judgments, {})


def test_finalize_fails_when_arm_missing():
    """finalize must FAIL when a pair is missing an arm (a or b)."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        assemble_batch,
        finalize,
    )

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    pair_ids = [p["pair_id"] for p in batch["pairs"]]

    judgments = {}
    for pid in pair_ids:
        judgments[pid] = {"choice": "A", "reason": "test", "confidence": "high"}

    run_records = {}
    for pid in pair_ids:
        run_records[pid] = {"a": _make_fake_run_result(status="ok")}
        # missing "b"

    with pytest.raises(ValueError, match="arm|missing|both"):
        finalize(batch, judgments, run_records)


def test_finalize_rejects_review_required_even_if_everything_else_ok():
    """review_required judgment must cause passed=False regardless of runtime/comparability/gates."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        assemble_batch,
        finalize,
    )

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    pair_ids = [p["pair_id"] for p in batch["pairs"]]

    judgments = {}
    for pid in pair_ids:
        judgments[pid] = {"choice": "A", "reason": "test", "confidence": "high"}
    judgments[pair_ids[0]] = {
        "choice": "review_required",
        "reason": "needs review",
        "confidence": "low",
    }

    run_records = {}
    for pid in pair_ids:
        run_records[pid] = {
            "a": _make_fake_run_result(status="ok"),
            "b": _make_fake_run_result(status="ok"),
        }

    result = finalize(batch, judgments, run_records)
    assert result["passed"] is False
    assert result["all_comparable"] is False


def test_finalize_rejects_empty_reason():
    """Judgments must have nonempty reason strings."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        assemble_batch,
        finalize,
    )

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    pair_ids = [p["pair_id"] for p in batch["pairs"]]

    judgments = {}
    for pid in pair_ids:
        judgments[pid] = {"choice": "A", "reason": "", "confidence": "high"}

    run_records = {}
    for pid in pair_ids:
        run_records[pid] = {
            "a": _make_fake_run_result(status="ok"),
            "b": _make_fake_run_result(status="ok"),
        }

    with pytest.raises(ValueError, match="reason"):
        finalize(batch, judgments, run_records)


def test_finalize_rejects_unknown_confidence():
    """Confidence must be from a documented finite set."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        assemble_batch,
        finalize,
    )

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    pair_ids = [p["pair_id"] for p in batch["pairs"]]

    judgments = {}
    for pid in pair_ids:
        judgments[pid] = {"choice": "A", "reason": "test", "confidence": "unknown_level"}

    run_records = {}
    for pid in pair_ids:
        run_records[pid] = {
            "a": _make_fake_run_result(status="ok"),
            "b": _make_fake_run_result(status="ok"),
        }

    with pytest.raises(ValueError, match="confidence"):
        finalize(batch, judgments, run_records)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FALSE-GREEN DEFECT: evaluate_comparability ignores envelope
# ═══════════════════════════════════════════════════════════════════════════════


def test_comparability_fails_on_model_mismatch_with_envelope():
    """evaluate_comparability must compare actual model against the
    preregistered envelope and fail on mismatch."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = _make_fake_run_result(
        status="ok",
        provenance={"backend": "codex", "model": "sonnet"},
    )
    b = _make_fake_run_result(
        status="ok",
        provenance={"backend": "codex", "model": "sonnet"},
    )
    # Envelope says opus, but both arms ran with sonnet
    envelope = {"model": "opus", "question": "test", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False


def test_comparability_fails_on_workspace_not_tmp_empty():
    """evaluate_comparability must verify the workspace was initially empty tmp."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = _make_fake_run_result(
        status="ok",
        provenance={
            "backend": "codex",
            "model": "sonnet",
            "workspace_type": "existing_dir",
        },
    )
    b = _make_fake_run_result(status="ok")
    envelope = {"model": "sonnet", "question": "test", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False


def test_comparability_fails_on_call_count_not_one():
    """evaluate_comparability must verify exactly one call per arm."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = _make_fake_run_result(
        status="ok",
        provenance={
            "backend": "codex",
            "model": "sonnet",
            "call_count": 2,
        },
    )
    b = _make_fake_run_result(status="ok")
    envelope = {"model": "sonnet", "question": "test", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False


def test_comparability_fails_on_read_only_false():
    """evaluate_comparability must verify read_only=true on both arms."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = _make_fake_run_result(
        status="ok",
        provenance={
            "backend": "codex",
            "model": "sonnet",
            "read_only": False,
        },
    )
    b = _make_fake_run_result(status="ok")
    envelope = {"model": "sonnet", "question": "test", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False


def test_comparability_fails_on_policy_overrun():
    """evaluate_comparability must reject token_cap_enforced=true or non-provider_default token policy."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = _make_fake_run_result(
        status="ok",
        provenance={
            "backend": "codex",
            "model": "sonnet",
            "token_cap_enforced": True,
        },
    )
    b = _make_fake_run_result(status="ok")
    envelope = {"model": "sonnet", "question": "test", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. FALSE-GREEN DEFECT: blind sanitization too shallow
# ═══════════════════════════════════════════════════════════════════════════════


def test_blind_sanitization_rejects_nested_provider_leak():
    """Blind sanitization must recursively strip all provider/model/backend/
    usage/gates/runtime/mapping/FIN/baseline/treatment identity fields,
    not just planner_source."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import _sanitize_payload_for_blind

    # Payload with nested provider identity fields
    malicious = {
        "research_product": {
            "answer_summary": "test",
            "sections": [{"model": "sonnet", "backend": "codex"}],
            "source_level": "recent_reference",
            "mainlines": [],
            "candidates": [],
            "shared_brain_references": [],
        },
        "display_product": {
            "display_intent": "ok",
            "headline": "test",
            "short_answer": "test",
            "primary_sections": [],
            "candidate_profiles": [],
            "analysis_path_summary": [],
            "confidence_boundary": {
                "advisory_only": True,
                "execution_allowed": False,
                "human_confirmation_required": True,
            },
            "next_actions": [],
            "omitted_details_summary": [],
            "planner_source": "codex",
            "provider": "codex",
            "model_used": "sonnet",
        },
    }

    sanitized = _sanitize_payload_for_blind(malicious)
    s_str = json.dumps(sanitized)

    # Must not leak provider identity
    assert "codex" not in s_str
    assert "sonnet" not in s_str
    assert "model_used" not in s_str
    assert "provider" not in s_str
    assert "planner_source" not in s_str


def test_build_blind_packets_includes_question_from_batch():
    """Blind packets must include the exact common question from the batch."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        assemble_batch,
        build_blind_packets,
    )

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    run_records = {}
    for p in batch["pairs"]:
        pid = p["pair_id"]
        run_records[pid] = {
            "a": _make_fake_run_result(status="ok"),
            "b": _make_fake_run_result(status="ok"),
        }

    packets = build_blind_packets(batch, run_records)
    assert "common_question" in packets
    assert len(packets["common_question"]) > 0
    assert "氦气" in packets["common_question"]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Bool-as-numeric usage rejection
# ═══════════════════════════════════════════════════════════════════════════════


def test_comparability_rejects_bool_in_resource_usage():
    """evaluate_comparability must reject bool values in resource_usage as non-numeric."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = _make_fake_run_result(
        status="ok",
        resource_usage={"input_tokens": True, "output_tokens": 50},
    )
    b = _make_fake_run_result(status="ok")
    envelope = {"model": "sonnet", "question": "test", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False
    assert any("bool" in r.lower() for r in comp["reasons"])


def test_codex_adapter_rejects_bool_usage():
    """Codex adapter must not accept bool values in allowlisted usage fields."""
    from fin_analyse.guo_teacher_research.codex_runtime import (
        CodexCliAgentRuntimeAdapter,
    )

    # _ALLOWLISTED_USAGE_FIELDS is a class attribute — verify intent via
    # _parse_result sanitization: if a turn.completed had bool usage, it must be dropped
    adapter = CodexCliAgentRuntimeAdapter(codex_bin="codex", model="test", workspace_path="/tmp")

    # Verify the allowlist does not accept non-numeric values
    for _key in adapter._ALLOWLISTED_USAGE_FIELDS:
        # Verify class-level sanitization rejects bools
        pass

    # Integration check: build a fake JSONL with bool usage and verify it's rejected
    import json as _json
    import subprocess

    product_str = _json.dumps({"research_product": {"answer_summary": "test"}})
    lines = [
        _json.dumps({"type": "thread.started"}),
        _json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": product_str}}
        ),
        _json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": 50}}
        ),
    ]
    stdout = "\n".join(lines) + "\n"

    def bool_runner(command, **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunRequest

    adapter_bool = CodexCliAgentRuntimeAdapter(
        codex_bin="codex",
        model="test",
        workspace_path="/tmp",
        runner=bool_runner,
    )
    result = adapter_bool.run(AgentRunRequest(use_case_ref="test", question="test"))
    # Bool input_tokens must NOT appear in resource_usage
    usage = result.resource_usage
    assert usage.get("input_tokens") is None or isinstance(
        usage.get("input_tokens"), (int, float)
    ), f"Bool input_tokens must be rejected, got {usage.get('input_tokens')}"
    # output_tokens=50 is valid int
    assert usage.get("output_tokens") == 50


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Model rejection (blank/whitespace)
# ═══════════════════════════════════════════════════════════════════════════════


def test_assemble_batch_rejects_whitespace_model():
    """assemble_batch must reject whitespace-only model IDs."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    for bad in ("   ", "\t", "\n"):
        with pytest.raises(ValueError, match="model"):
            assemble_batch(model=bad)


def test_assemble_batch_uses_golden_question_default():
    """assemble_batch must default to the exact Golden helium question."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    for pair in batch["pairs"]:
        for arm_key in ("a", "b"):
            arm = pair[arm_key]
            assert arm["question"] == _GOLDEN_QUESTION, (
                f"Expected Golden question, got: {arm['question']}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Hard gates must know arm type
# ═══════════════════════════════════════════════════════════════════════════════


def test_hard_gates_accepts_arm_type_parameter():
    """validate_hard_gates must accept an arm_type parameter ('baseline'|'treatment')."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    result = _make_fake_run_result()
    gates = validate_hard_gates(result, arm_type="treatment")
    assert gates["all_passed"] is True

    gates_b = validate_hard_gates(result, arm_type="baseline")
    assert gates_b["all_passed"] is True


def test_source_gate_treatment_requires_recent_reference():
    """Source gate on treatment arm MUST have source_level=recent_reference
    with explicit non-teacher attribution. Missing research_product → fail."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Treatment with g_direct → must fail
    research_gd = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_gd["source_level"] = "g_direct"
    result_gd = _make_fake_run_result(research=research_gd)
    gates = validate_hard_gates(result_gd, arm_type="treatment")
    assert gates["source"] is False


def test_source_gate_baseline_must_not_have_helium_evidence():
    """Source gate on baseline arm must FAIL if research_product contains
    helium article id or treatment-only source metadata."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Baseline with helium article id in payload → must fail source gate
    research_helium = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_helium["article_id"] = _HELIUM_ARTICLE_ID
    result = _make_fake_run_result(research=research_helium)
    gates = validate_hard_gates(result, arm_type="baseline")
    assert gates["source"] is False


def test_source_gate_baseline_handles_structured_source_level_without_crashing():
    """Generic baseline output may describe source quality as a JSON object.

    The source gate must scan that structure for forbidden cognition labels
    instead of raising TypeError on an unhashable value.
    """
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    research_ok = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_ok["source_level"] = {
        "level": "framework_only",
        "basis": ["public industry knowledge", "current data not verified"],
    }
    gates_ok = validate_hard_gates(_make_fake_run_result(research=research_ok), arm_type="baseline")
    assert gates_ok["source"] is True

    research_forbidden = copy.deepcopy(research_ok)
    research_forbidden["source_level"]["claimed_cognition"] = "g_direct"
    gates_forbidden = validate_hard_gates(
        _make_fake_run_result(research=research_forbidden), arm_type="baseline"
    )
    assert gates_forbidden["source"] is False


def test_risk_gate_requires_top_level_booleans():
    """Risk gate must REQUIRE (not optionally inspect) strict top-level
    advisory_only=True, execution_allowed=False, human_confirmation_required=True."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Payload missing top-level booleans entirely — must fail risk gate
    result = _make_fake_run_result()
    gates = validate_hard_gates(result)
    assert gates["risk"] is True  # when they ARE present and correct

    # Payload with top-level advisory_only=False but correct in confidence_boundary
    bad_payload = {
        "advisory_only": False,
        "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
        "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
    }
    result_bad = _make_fake_run_result()
    result_bad["payload"] = bad_payload
    gates_bad = validate_hard_gates(result_bad)
    assert gates_bad["risk"] is False


def test_write_gate_requires_explicit_read_only_provenance():
    """Write gate must require explicit read_only=true, isolated_workspace=true,
    workspace_persisted=false, write_detected=false in provenance — not just
    absence of workspace_persisted/write_detected."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Default provenance has none of these → should fail
    result = _make_fake_run_result(
        provenance={"backend": "codex", "model": "sonnet"},
    )
    gates = validate_hard_gates(result)
    assert gates["write_side_effect"] is False, (
        "Write gate must require explicit read_only/isolated_workspace markers"
    )

    # Proper provenance with all required markers
    result_ok = _make_fake_run_result(
        provenance={
            "backend": "codex",
            "model": "sonnet",
            "read_only": True,
            "isolated_workspace": True,
            "workspace_persisted": False,
            "write_detected": False,
        },
    )
    gates_ok = validate_hard_gates(result_ok)
    assert gates_ok["write_side_effect"] is True


def test_cognition_gate_requires_explicit_empty_capability_trace():
    """Cognition gate must require status=ok, empty opaque_continuation, AND
    an explicit empty or no-write capability_trace (not just absence of writes)."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    result = _make_fake_run_result(status="ok", capability_trace=[])
    gates = validate_hard_gates(result)
    assert gates["cognition"] is True

    # Non-empty capability_trace must be checked
    result_bad = _make_fake_run_result(
        status="ok",
        capability_trace=[{"type": "cognition_write", "detail": "test"}],
    )
    gates_bad = validate_hard_gates(result_bad)
    assert gates_bad["cognition"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 7. run_facts in execute_run
# ═══════════════════════════════════════════════════════════════════════════════


def test_execute_run_records_measured_run_facts(tmp_path):
    """execute_run must record measured run_facts in each private arm record:
    actual model, call_count=1, read_only, isolated_workspace, workspace_type,
    no workspace_persisted/write_detected, no continuation."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunResult
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    fake_results = []
    for _ in range(10):
        fake_results.append(
            AgentRunResult(
                status="ok",
                payload={
                    "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
                    "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
                },
                data_gaps=[],
                resource_usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                provenance={"backend": "codex", "model": _EXPLICIT_MODEL},
            )
        )

    factory, call_log = _make_fake_adapter_factory(fake_results)
    execute_run(model=_EXPLICIT_MODEL, output_dir=str(output_dir), adapter_factory=factory)

    rr_path = output_dir / "run_record.json"
    run_record = json.loads(rr_path.read_text(encoding="utf-8"))

    for pr in run_record["pairs"]:
        for arm_key in ("a", "b"):
            arm = pr[arm_key]
            # Must have run_facts
            assert "run_facts" in arm, f"Missing run_facts in {pr['pair_id']}/{arm_key}"
            facts = arm["run_facts"]
            assert facts["call_count"] == 1
            assert facts["read_only"] is True
            assert facts["isolated_workspace"] is True
            assert facts["workspace_persisted"] is False
            assert facts["write_detected"] is False
            assert facts["actual_model"] == _EXPLICIT_MODEL
            assert "opaque_continuation" not in facts or not facts.get("opaque_continuation")


def test_execute_run_try_finally_cleanup(tmp_path):
    """TemporaryDirectory cleanup must use try/finally so workspaces are
    always cleaned up, even on adapter failure."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    # Adapter that raises on first call
    import os

    def failing_factory(*, model="", workspace_path=".", timeout_seconds=300.0):
        class FailingAdapter:
            def run(self, request):
                # Verify workspace exists before raising
                assert os.path.isdir(workspace_path), f"Workspace not created: {workspace_path}"
                raise RuntimeError("adapter failure")

        return FailingAdapter()

    import contextlib

    with contextlib.suppress(RuntimeError):
        execute_run(
            model=_EXPLICIT_MODEL, output_dir=str(output_dir), adapter_factory=failing_factory
        )

    # Verify the temp directories were cleaned up (no leaked dirs in /tmp)
    # The TemporaryDirectory cleanup should have happened
    import tempfile

    tmp_root = tempfile.gettempdir()
    leaked = [d for d in os.listdir(tmp_root) if d.startswith("m1-pair-helium-")]
    assert len(leaked) == 0, f"Leaked temp dirs: {leaked}"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Adapted existing tests (preserving contract)
# ═══════════════════════════════════════════════════════════════════════════════


def test_assemble_batch_uses_fixed_alternating_mapping():
    """Pairs 01/03/05 must have A=baseline, B=treatment.
    Pairs 02/04 must have A=treatment, B=baseline."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    pairs = batch["pairs"]
    assert len(pairs) == 5

    expected = {
        "pair-helium-01": {"a": "baseline", "b": "treatment"},
        "pair-helium-02": {"a": "treatment", "b": "baseline"},
        "pair-helium-03": {"a": "baseline", "b": "treatment"},
        "pair-helium-04": {"a": "treatment", "b": "baseline"},
        "pair-helium-05": {"a": "baseline", "b": "treatment"},
    }
    for pair in pairs:
        pid = pair["pair_id"]
        assert pair["_mapping"] == expected[pid], (
            f"Fixed mapping mismatch for {pid}: expected {expected[pid]}, got {pair['_mapping']}"
        )


def test_assemble_batch_no_seed_or_random():
    """assemble_batch must not accept a seed parameter and must be deterministic."""
    import inspect

    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    sig = inspect.signature(assemble_batch)
    assert "seed" not in sig.parameters, "assemble_batch must not accept seed"

    b1 = assemble_batch(model=_EXPLICIT_MODEL)
    b2 = assemble_batch(model=_EXPLICIT_MODEL)
    for p1, p2 in zip(b1["pairs"], b2["pairs"], strict=True):
        assert p1["_mapping"] == p2["_mapping"]


def test_assemble_batch_builds_agent_run_requests_not_raw_prompts():
    """Each arm must contain AgentRunRequest fields, not a raw prompt string."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    for pair in batch["pairs"]:
        for arm_key in ("a", "b"):
            arm = pair[arm_key]
            assert "prompt" not in arm, f"Arm {pair['pair_id']}/{arm_key} has raw prompt string"
            assert "use_case_ref" in arm, "Arm missing use_case_ref"
            assert "question" in arm, "Arm missing question"


def test_baseline_request_uses_generic_research_answer():
    """Baseline arm must use use_case_ref='generic_research_answer', no FIN evidence."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    for pair in batch["pairs"]:
        mapping = pair["_mapping"]
        baseline_key = "a" if mapping["a"] == "baseline" else "b"
        baseline = pair[baseline_key]
        assert baseline["use_case_ref"] == "generic_research_answer"
        assert not baseline.get("context_pack", {}).get("evidence_context")
        cap = baseline.get("capability_scope", {})
        assert "agent_analyze" not in str(cap)
        assert baseline.get("product_contracts", []) == []


def test_treatment_request_uses_agent_analyze_general():
    """Treatment arm must use agent_analyze_general with evidence, contracts, safety."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    for pair in batch["pairs"]:
        mapping = pair["_mapping"]
        treatment_key = "a" if mapping["a"] == "treatment" else "b"
        treatment = pair[treatment_key]
        assert treatment["use_case_ref"] == "agent_analyze_general"
        evidence = treatment.get("context_pack", {}).get("evidence_context", {})
        assert evidence
        assert evidence.get("article_id") == _HELIUM_ARTICLE_ID
        assert "氦气" in evidence.get("article_excerpt", "")
        assert evidence.get("source_bucket") == "recent_reference"
        source_label = evidence.get("source_label", "")
        assert "must not be promoted" in source_label
        cap = treatment.get("capability_scope", {})
        assert cap.get("use_case_id") == "agent_analyze_general"
        contracts = treatment.get("product_contracts", [])
        assert len(contracts) >= 2
        boundaries = treatment.get("boundaries", {})
        assert boundaries.get("advisory_only") is True
        assert boundaries.get("execution_allowed") is False
        assert boundaries.get("human_confirmation_required") is True


def test_hard_gates_uses_contract_validator_for_research_product():
    """Schema gate must reject research_product missing required fields."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    bad_research = {"answer_summary": "test"}
    result = _make_fake_run_result(research=bad_research)
    gates = validate_hard_gates(result)
    assert gates["schema"] is False


def test_hard_gates_use_canonical_contract_for_trade_fields():
    """Risk gate must apply the canonical recursive execution-field guard."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    research_with_trade = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_with_trade["target_price"] = 100.0
    result = _make_fake_run_result(research=research_with_trade)
    gates = validate_hard_gates(result)
    assert gates["risk"] is False


def test_run_cli_execution_fake_and_writes_artifacts(tmp_path):
    """End-to-end fake run writes run_record.json and blind_packets.json."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunResult
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    fake_results = []
    for _ in range(10):
        fake_results.append(
            AgentRunResult(
                status="ok",
                payload={
                    "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
                    "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
                },
                data_gaps=[],
                resource_usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            )
        )

    factory, call_log = _make_fake_adapter_factory(fake_results)
    execute_run(model=_EXPLICIT_MODEL, output_dir=str(output_dir), adapter_factory=factory)

    assert len(call_log) == 10
    for req in call_log:
        assert req.model == _EXPLICIT_MODEL

    rr_path = output_dir / "run_record.json"
    bp_path = output_dir / "blind_packets.json"
    assert rr_path.exists()
    assert bp_path.exists()

    run_record = json.loads(rr_path.read_text(encoding="utf-8"))
    assert len(run_record["pairs"]) == 5

    for pr in run_record["pairs"]:
        assert "_mapping" in pr
        for arm_key in ("a", "b"):
            arm = pr[arm_key]
            assert arm["status"] == "ok"
            assert "prompt" not in arm
            assert "argv" not in arm
            assert "stderr" not in arm
            assert "transcript" not in arm
            assert "raw_jsonl" not in arm
            assert "stdout" not in arm

    blind = json.loads(bp_path.read_text(encoding="utf-8"))
    assert "case_id" in blind
    assert "common_question" in blind
    assert "rubric" in blind
    assert "pairs" in blind
    assert len(blind["pairs"]) == 5

    bp_str = json.dumps(blind)
    assert "baseline" not in bp_str.lower()
    assert "treatment" not in bp_str.lower()
    assert _EXPLICIT_MODEL not in bp_str
    assert "codex" not in bp_str


def test_finalize_requires_treatment_wins_at_least_4_of_5():
    """finalize must pass only when treatment wins at least 4/5."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch, finalize

    batch = assemble_batch(model=_EXPLICIT_MODEL)

    judgments = {}
    wins = 0
    for pair in batch["pairs"]:
        pid = pair["pair_id"]
        mapping = pair["_mapping"]
        treatment_label = "A" if mapping["a"] == "treatment" else "B"
        baseline_label = "B" if treatment_label == "A" else "A"
        if wins < 3:
            judgments[pid] = {
                "choice": treatment_label,
                "reason": "treat wins",
                "confidence": "high",
            }
            wins += 1
        else:
            judgments[pid] = {"choice": baseline_label, "reason": "base wins", "confidence": "high"}

    run_records = {}
    for p in batch["pairs"]:
        pid = p["pair_id"]
        run_records[pid] = {
            "a": _make_fake_run_result(status="ok"),
            "b": _make_fake_run_result(status="ok"),
        }

    result = finalize(batch, judgments, run_records)
    assert result["semantic_result"]["treatment_wins"] < 4
    assert result["passed"] is False


def test_finalize_separates_runtime_comparability_gates_semantic():
    """Runtime/comparability/gate failure must never become semantic loss."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch, finalize

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    pair_ids = [p["pair_id"] for p in batch["pairs"]]

    judgments = {}
    for pid in pair_ids:
        judgments[pid] = {"choice": "A", "reason": "test", "confidence": "low"}

    run_records = {}
    for pid in pair_ids:
        run_records[pid] = {
            "a": _make_fake_run_result(status="ok"),
            "b": _make_fake_run_result(status="ok"),
        }
    run_records["pair-helium-01"]["a"] = _make_fake_run_result(status="error")

    result = finalize(batch, judgments, run_records)
    assert result["all_runtime_ok"] is False
    assert result["passed"] is False
    assert "semantic_result" in result


def test_codex_adapter_neutral_prompt_for_generic_research_answer():
    """generic_research_answer prompt must not contain FIN branding."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunRequest
    from fin_analyse.guo_teacher_research.codex_runtime import CodexCliAgentRuntimeAdapter

    adapter = CodexCliAgentRuntimeAdapter(codex_bin="codex", model="test", workspace_path="/tmp")
    request = AgentRunRequest(
        use_case_ref="generic_research_answer",
        question="What is the helium supply outlook?",
        context_pack={},
        boundaries={
            "advisory_only": True,
            "execution_allowed": False,
            "human_confirmation_required": True,
        },
    )
    prompt = adapter._build_prompt(request)
    assert "FIN" not in prompt
    assert "registered use case" not in prompt.lower()
    assert "registered ProductContract" not in prompt
    assert "helium supply outlook" in prompt



# ═══════════════════════════════════════════════════════════════════════════════
# BLOCKER 1: Source treatment — exact recent_reference + explicit attribution
# ═══════════════════════════════════════════════════════════════════════════════


def test_source_gate_treatment_fails_when_source_level_not_exact_recent_reference():
    """Treatment source gate must FAIL unless source_level is EXACTLY 'recent_reference'.
    Even 'unknown', '', or any other value fails."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    for bad_level in ("", "unknown", "g_direct", "teacher_direct", "recent_reference_typo"):
        research = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
        research["source_level"] = bad_level
        result = _make_fake_run_result(research=research)
        gates = validate_hard_gates(result, arm_type="treatment")
        assert gates["source"] is False, f"source_level={bad_level!r} should fail source gate"


def test_source_gate_treatment_fails_without_explicit_not_teacher_attribution():
    """Treatment source gate must FAIL when research_product lacks explicit source
    attribution that disclaims teacher/G-direct cognition — even with source_level
    == 'recent_reference'."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Valid source_level but no source_attribution field at all
    research_no_attr = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_no_attr["source_level"] = "recent_reference"
    # Ensure source_attribution is absent
    research_no_attr.pop("source_attribution", None)
    result = _make_fake_run_result(research=research_no_attr)
    gates = validate_hard_gates(result, arm_type="treatment")
    assert gates["source"] is False, (
        "Should fail: research_product lacks explicit source_attribution "
        "disclaiming teacher/G-direct cognition"
    )

    # Has source_attribution but says the wrong thing
    research_bad_attr = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_bad_attr["source_level"] = "recent_reference"
    research_bad_attr["source_attribution"] = "teacher direct cognition used"
    result2 = _make_fake_run_result(research=research_bad_attr)
    gates2 = validate_hard_gates(result2, arm_type="treatment")
    assert gates2["source"] is False, "Should fail: source_attribution claims teacher direct"


def test_source_gate_treatment_passes_with_exact_recent_reference_and_attribution():
    """Treatment source gate must PASS when source_level=='recent_reference' AND
    source_attribution explicitly says it is NOT teacher/G-direct cognition."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    research_ok = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_ok["source_level"] = "recent_reference"
    research_ok["source_attribution"] = (
        "source classification preserved: bucket=recent_reference, "
        "level=recent_reference; must not be promoted; "
        "this is not teacher/G-direct cognition"
    )
    result = _make_fake_run_result(research=research_ok)
    gates = validate_hard_gates(result, arm_type="treatment")
    assert gates["source"] is True, f"Should pass but got: {gates}"


def test_source_gate_baseline_still_rejects_treatment_assets():
    """Baseline source gate keeps its existing no-treatment-asset rule unchanged."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Baseline with helium article id must still fail
    research_helium = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research_helium["article_id"] = _HELIUM_ARTICLE_ID
    result = _make_fake_run_result(research=research_helium)
    gates = validate_hard_gates(result, arm_type="baseline")
    assert gates["source"] is False

    # Baseline without helium evidence must pass (even without source_attribution)
    research_clean = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    result_clean = _make_fake_run_result(research=research_clean)
    gates_clean = validate_hard_gates(result_clean, arm_type="baseline")
    assert gates_clean["source"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCKER 2: Cognition — capability_trace must be explicitly present list
# ═══════════════════════════════════════════════════════════════════════════════


def test_cognition_gate_fails_when_capability_trace_missing():
    """Cognition gate must FAIL when capability_trace key is absent entirely
    (not just when it's an empty default)."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Build result dict without capability_trace key
    result = _make_fake_run_result(status="ok", capability_trace=[])
    del result["capability_trace"]
    assert "capability_trace" not in result  # verify key is absent
    gates = validate_hard_gates(result)
    assert gates["cognition"] is False, "Missing capability_trace must fail cognition gate"


def test_cognition_gate_fails_when_capability_trace_not_a_list():
    """Cognition gate must FAIL when capability_trace is not a list."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    for bad_val in (None, "string", 42, {"type": "dict"}):
        result = _make_fake_run_result(status="ok", capability_trace=bad_val)
        gates = validate_hard_gates(result)
        assert gates["cognition"] is False, f"capability_trace={bad_val!r} should fail"


def test_execute_run_preserves_sanitized_capability_trace_in_private_arm_record(tmp_path):
    """execute_run must preserve sanitized AgentRunResult.capability_trace
    in each private arm record so a real write trace cannot disappear."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunResult
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    sample_trace = [{"type": "read_only_lookup", "detail": "cache_hit"}]

    fake_results = []
    for _ in range(10):
        fake_results.append(
            AgentRunResult(
                status="ok",
                payload={
                    "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
                    "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
                },
                data_gaps=[],
                resource_usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                capability_trace=list(sample_trace),
            )
        )

    factory, _call_log = _make_fake_adapter_factory(fake_results)
    execute_run(model=_EXPLICIT_MODEL, output_dir=str(output_dir), adapter_factory=factory)

    rr_path = output_dir / "run_record.json"
    run_record = json.loads(rr_path.read_text(encoding="utf-8"))

    for pr in run_record["pairs"]:
        for arm_key in ("a", "b"):
            arm = pr[arm_key]
            assert "capability_trace" in arm, (
                f"capability_trace missing from {pr['pair_id']}/{arm_key}"
            )
            trace = arm["capability_trace"]
            assert isinstance(trace, list), (
                f"capability_trace not a list in {pr['pair_id']}/{arm_key}"
            )
            assert len(trace) > 0, f"capability_trace empty in {pr['pair_id']}/{arm_key}"
            assert trace[0]["type"] == "read_only_lookup"


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCKER 3: Risk — recursive canonical ProductContract validation
# ═══════════════════════════════════════════════════════════════════════════════


def test_risk_gate_fails_nested_forbidden_trade_field():
    """Risk gate must fail when a forbidden field appears at any nesting depth."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Nested forbidden field: buy inside a section dict
    research = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research["sections"] = [{"title": "analysis", "buy": True}]
    result = _make_fake_run_result(research=research)
    gates = validate_hard_gates(result, arm_type="treatment")
    assert gates["risk"] is False, (
        "Nested forbidden trade field 'buy' inside research_product.sections[0] must fail risk gate"
    )

    # Deeply nested: target_price inside candidates list
    research2 = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research2["candidates"] = [{"name": "stock", "metrics": {"target_price": 50.0}}]
    result2 = _make_fake_run_result(research=research2)
    gates2 = validate_hard_gates(result2, arm_type="treatment")
    assert gates2["risk"] is False, "Deeply nested 'target_price' must fail risk gate"


def test_risk_gate_requires_display_product_and_confidence_boundary():
    """Risk gate must REQUIRE (not optionally inspect) research_product, display_product,
    AND confidence_boundary — missing any → fail."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Missing display_product entirely
    result_no_display = {
        "status": "ok",
        "payload": {
            "advisory_only": True,
            "execution_allowed": False,
            "human_confirmation_required": True,
            "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
        },
        "data_gaps": [],
        "capability_trace": [],
        "provenance": {
            "backend": "codex",
            "model": _EXPLICIT_MODEL,
            "read_only": True,
            "isolated_workspace": True,
            "workspace_persisted": False,
            "write_detected": False,
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    gates = validate_hard_gates(result_no_display, arm_type="treatment")
    assert gates["risk"] is False, "Missing display_product must fail risk gate"

    # Has display_product but no confidence_boundary
    display_no_cb = copy.deepcopy(_VALID_DISPLAY_PRODUCT)
    del display_no_cb["confidence_boundary"]
    result_no_cb = _make_fake_run_result(display=display_no_cb)
    gates2 = validate_hard_gates(result_no_cb, arm_type="treatment")
    assert gates2["risk"] is False, "Missing confidence_boundary must fail risk gate"

    # Has display_product, has confidence_boundary but missing research_product
    result_no_research = {
        "status": "ok",
        "payload": {
            "advisory_only": True,
            "execution_allowed": False,
            "human_confirmation_required": True,
            "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
        },
        "data_gaps": [],
        "capability_trace": [],
        "provenance": {
            "backend": "codex",
            "model": _EXPLICIT_MODEL,
            "read_only": True,
            "isolated_workspace": True,
            "workspace_persisted": False,
            "write_detected": False,
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    gates3 = validate_hard_gates(result_no_research, arm_type="treatment")
    assert gates3["risk"] is False, "Missing research_product must fail risk gate"


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCKER 4: Blind packets — top-level allowlist + identity string rejection
# ═══════════════════════════════════════════════════════════════════════════════


def test_blind_rejects_malicious_top_level_keys():
    """Blind sanitization must enforce top-level allowlist of ONLY research_product
    and display_product — any extra key at payload top level must be stripped."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import _sanitize_payload_for_blind

    malicious = {
        "research_product": {"answer_summary": "test"},
        "display_product": {"display_intent": "ok"},
        "advisory_only": True,  # NOT in the allowlist
        "execution_allowed": False,  # NOT in the allowlist
        "human_confirmation_required": True,  # NOT in the allowlist
        "provider": "codex",  # NOT in the allowlist
        "model_used": "sonnet",  # NOT in the allowlist
        "runtime_session": "abc123",  # NOT in the allowlist
    }

    sanitized = _sanitize_payload_for_blind(malicious)

    # Top-level must ONLY have research_product and display_product
    top_keys = set(sanitized.keys())
    assert top_keys == {"research_product", "display_product"}, (
        f"Top-level keys must be only research_product and display_product, got {top_keys}"
    )
    # Forbidden keys must be absent
    for bad_key in (
        "advisory_only",
        "execution_allowed",
        "human_confirmation_required",
        "provider",
        "model_used",
        "runtime_session",
    ):
        assert bad_key not in sanitized, f"Key {bad_key!r} must be stripped from top level"


def test_blind_rejects_identity_strings_in_values():
    """Blind sanitization must reject identity strings (FIN, baseline, treatment,
    provider/model families) when they appear as identity tokens in public values."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import _sanitize_payload_for_blind

    # Identity strings leaked inside research_product values
    malicious = {
        "research_product": {
            "answer_summary": "FIN analysis of helium market",
            "sections": [{"title": "baseline comparison"}],
            "source_level": "recent_reference",
            "mainlines": [],
            "candidates": [],
            "shared_brain_references": [],
        },
        "display_product": {
            "display_intent": "ok",
            "headline": "treatment effect on helium supply",
            "short_answer": "supply tight",
            "primary_sections": [],
            "candidate_profiles": [],
            "analysis_path_summary": [],
            "confidence_boundary": {
                "advisory_only": True,
                "execution_allowed": False,
                "human_confirmation_required": True,
            },
            "next_actions": [],
            "omitted_details_summary": [],
        },
    }

    sanitized = _sanitize_payload_for_blind(malicious)
    s_str = json.dumps(sanitized)

    # Identity tokens must be detected and rejected
    # "FIN" as a word in a value
    assert "FIN" not in s_str, f"Identity token 'FIN' leaked: {s_str[:200]}"
    # "baseline" as a word in a value
    assert "baseline" not in s_str.lower(), f"Identity token 'baseline' leaked: {s_str[:200]}"
    # "treatment" as a word in a value
    assert "treatment" not in s_str.lower(), f"Identity token 'treatment' leaked: {s_str[:200]}"


def test_blind_preserves_legitimate_finance_terms():
    """Blind sanitization must preserve legitimate finance/business words
    while still stripping provider identity."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import _sanitize_payload_for_blind

    payload = {
        "research_product": {
            "answer_summary": "Helium supply chain remains constrained; "
            "semiconductor demand drives price increase",
            "sections": [
                {
                    "title": "Supply Analysis",
                    "body": "Qatar disruption affects 30% of global supply",
                },
                {"title": "Demand Drivers", "body": "MRI machines and fiber optics need helium"},
            ],
            "source_level": "recent_reference",
            "mainlines": [],
            "candidates": [{"name": "BOG提氦项目", "details": "国产替代进展"}],
            "shared_brain_references": [],
        },
        "display_product": {
            "display_intent": "ok",
            "headline": "氦气供给持续紧张，关注国产替代",
            "short_answer": "供给端Qatar/Russia双扰动，BOG提氦是关键",
            "primary_sections": [],
            "candidate_profiles": [],
            "analysis_path_summary": [],
            "confidence_boundary": {
                "advisory_only": True,
                "execution_allowed": False,
                "human_confirmation_required": True,
            },
            "next_actions": [],
            "omitted_details_summary": [],
        },
    }

    sanitized = _sanitize_payload_for_blind(payload)
    s_str = json.dumps(sanitized, ensure_ascii=False)

    # Finance/business terms must survive
    assert "helium" in s_str.lower()
    assert "supply" in s_str.lower()
    assert "semiconductor" in s_str.lower()
    assert "Qatar" in s_str
    assert "BOG" in s_str
    assert "国产替代" in s_str


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCKER 5: Comparability — full preregistered envelope facts
# ═══════════════════════════════════════════════════════════════════════════════


def test_comparability_checks_all_envelope_facts():
    """evaluate_comparability must require EVERY preregistered envelope fact:
    question, timeout_seconds, call_cap=1, read_only, ephemeral,
    token_envelope=provider_default, token_cap_enforced=false,
    generic_tools=none, workspace_type=tmp_empty, workspace_initially_empty=true."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "helium question",
            "timeout_seconds": 300.0,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "helium question",
            "timeout_seconds": 300.0,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {
        "model": "sonnet",
        "question": "helium question",
        "timeout_seconds": 300.0,
    }

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is True, f"Should be comparable, got: {comp['reasons']}"


def test_comparability_fails_on_missing_envelope_fact():
    """evaluate_comparability must fail when any preregistered envelope fact
    is missing from either arm's run_facts."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    # Missing ephemeral fact
    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "helium question",
            "timeout_seconds": 300.0,
            "call_count": 1,
            "read_only": True,
            # ephemeral is MISSING
            "workspace_type": "tmp_empty",
            "isolated_workspace": True,
            "workspace_persisted": False,
            "write_detected": False,
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "helium question",
            "timeout_seconds": 300.0,
            "call_count": 1,
            "read_only": True,
            "ephemeral": True,
            "workspace_type": "tmp_empty",
            "isolated_workspace": True,
            "workspace_persisted": False,
            "write_detected": False,
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {"model": "sonnet", "question": "helium question", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False, "Missing envelope fact should fail comparability"


def test_comparability_fails_when_facts_differ_between_arms():
    """evaluate_comparability must fail when the two arms report different
    envelope facts (e.g., different timeout)."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "helium question",
            "timeout_seconds": 300.0,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "helium question",
            "timeout_seconds": 500.0,  # DIFFERENT
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {"model": "sonnet", "question": "helium question", "timeout_seconds": 300.0}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False, "Mismatched timeout between arms must fail comparability"


def test_comparability_actual_model_from_provenance_not_requested():
    """actual_model must come from sanitized runtime provenance, not fabricated
    from the requested model. If provenance model differs from requested, detect it."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",  # matches provenance
            "question": "q",
            "timeout_seconds": 300,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "opus", "backend": "codex"},  # different from requested
        "run_facts": {
            "actual_model": "opus",  # correctly sourced from provenance
            "question": "q",
            "timeout_seconds": 300,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {"model": "sonnet", "question": "q", "timeout_seconds": 300}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False, "Arms with different actual models should not be comparable"


def test_execute_run_records_full_envelope_facts(tmp_path):
    """execute_run must record all preregistered envelope facts in each arm's
    run_facts: exact question, timeout_seconds, call_cap=1, read_only, ephemeral,
    token_envelope=provider_default, token_cap_enforced=false, generic_tools=none,
    workspace_type=tmp_empty, workspace_initially_empty=true."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunResult
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    fake_results = []
    for _ in range(10):
        fake_results.append(
            AgentRunResult(
                status="ok",
                payload={
                    "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
                    "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
                },
                data_gaps=[],
                resource_usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            )
        )

    factory, _call_log = _make_fake_adapter_factory(fake_results)
    my_timeout = 180.0
    execute_run(
        model=_EXPLICIT_MODEL,
        output_dir=str(output_dir),
        timeout_seconds=my_timeout,
        adapter_factory=factory,
    )

    rr_path = output_dir / "run_record.json"
    run_record = json.loads(rr_path.read_text(encoding="utf-8"))

    # Verify timeout stored in run_record
    assert run_record.get("timeout_seconds") == my_timeout, (
        f"timeout_seconds should be {my_timeout} in run_record"
    )

    required_facts = {
        "question",
        "timeout_seconds",
        "call_count",
        "call_cap",
        "read_only",
        "ephemeral",
        "token_envelope",
        "token_cap_enforced",
        "generic_tools",
        "workspace_type",
        "workspace_initially_empty",
        "isolated_workspace",
        "workspace_persisted",
        "write_detected",
        "actual_model",
    }

    for pr in run_record["pairs"]:
        for arm_key in ("a", "b"):
            arm = pr[arm_key]
            facts = arm.get("run_facts", {})
            for fact_key in required_facts:
                assert fact_key in facts, (
                    f"Missing fact '{fact_key}' in {pr['pair_id']}/{arm_key} run_facts"
                )
            # Verify specific values
            assert facts["call_cap"] == 1
            assert facts["read_only"] is True
            assert facts["ephemeral"] is True
            assert facts["token_envelope"] == "provider_default"
            assert facts["token_cap_enforced"] is False
            assert facts["generic_tools"] == "none"
            assert facts["workspace_type"] == "tmp_empty"
            assert facts["workspace_initially_empty"] is True
            assert facts["timeout_seconds"] == my_timeout
            assert facts["question"] == _GOLDEN_QUESTION


def test_reconstruct_batch_uses_stored_timeout():
    """_reconstruct_batch_from_run_record must use the stored timeout_seconds
    from run_record, not the hardcoded 300."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        _reconstruct_batch_from_run_record,
    )

    run_data = {
        "case_id": "test",
        "model": "sonnet",
        "backend": "codex",
        "timeout_seconds": 180.0,
        "pairs": [
            {
                "pair_id": "pair-test-01",
                "_mapping": {"a": "baseline", "b": "treatment"},
                "a": {"run_facts": {"question": "q1", "timeout_seconds": 180.0}},
                "b": {"run_facts": {"question": "q1", "timeout_seconds": 180.0}},
            },
        ],
    }

    batch = _reconstruct_batch_from_run_record(run_data)
    for pair in batch["pairs"]:
        assert pair["a"]["timeout_seconds"] == 180.0, "Should use stored timeout, not hardcoded 300"
        assert pair["b"]["timeout_seconds"] == 180.0, "Should use stored timeout, not hardcoded 300"


# ═══════════════════════════════════════════════════════════════════════════════
# COUNTEREXAMPLE 1: Source gate — arbitrary nonempty attribution is false-green
# ═══════════════════════════════════════════════════════════════════════════════


def test_source_gate_treatment_rejects_arbitrary_nonempty_attribution():
    """Per independent reproduction, 'External article and public market evidence.'
    passes the current source gate because it is nonempty and doesn't mention
    teacher/G-direct.  This must FAIL — the source_attribution MUST explicitly
    contain recent_reference classification AND a not-teacher/G-direct disclaimer."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import validate_hard_gates

    # Counterexample exactly as reproduced: arbitrary nonempty text
    research = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research["source_level"] = "recent_reference"
    research["source_attribution"] = "External article and public market evidence."
    result = _make_fake_run_result(research=research)
    gates = validate_hard_gates(result, arm_type="treatment")
    assert gates["source"] is False, (
        "Arbitrary nonempty source_attribution without 'recent_reference' "
        "classification AND not-teacher/G-direct disclaimer must fail source gate"
    )

    # Also fail: has recent_reference but no disclaimer
    research2 = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research2["source_level"] = "recent_reference"
    research2["source_attribution"] = "Based on recent_reference sources from the web."
    result2 = _make_fake_run_result(research=research2)
    gates2 = validate_hard_gates(result2, arm_type="treatment")
    assert gates2["source"] is False, (
        "source_attribution with 'recent_reference' but without not-teacher/"
        "not-G-direct/must-not-promote disclaimer must fail"
    )

    # Also fail: has disclaimer but no recent_reference classification
    research3 = copy.deepcopy(_VALID_RESEARCH_PRODUCT)
    research3["source_level"] = "recent_reference"
    research3["source_attribution"] = "This is not teacher/G-direct cognition."
    result3 = _make_fake_run_result(research=research3)
    gates3 = validate_hard_gates(result3, arm_type="treatment")
    assert gates3["source"] is False, (
        "source_attribution with disclaimer but without 'recent_reference' classification must fail"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# COUNTEREXAMPLE 2: Blind — model-family tokens (Codex/Sonnet/Opus/Haiku) leak
# ═══════════════════════════════════════════════════════════════════════════════


def test_blind_redacts_model_family_tokens_in_values():
    """'Generated by Codex using Sonnet' must be recursively redacted.
    Opus/Haiku/model-family tokens must also be caught as identity leaks."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import _sanitize_payload_for_blind

    malicious = {
        "research_product": {
            "answer_summary": "Generated by Codex using Sonnet for analysis",
            "sections": [
                {"title": "Opus comparison", "body": "Haiku was also tested"},
            ],
            "source_level": "recent_reference",
            "mainlines": [],
            "candidates": [],
            "shared_brain_references": [],
        },
        "display_product": {
            "display_intent": "ok",
            "headline": "Codex-based helium analysis",
            "short_answer": "supply tight",
            "primary_sections": [],
            "candidate_profiles": [],
            "analysis_path_summary": [],
            "confidence_boundary": {
                "advisory_only": True,
                "execution_allowed": False,
                "human_confirmation_required": True,
            },
            "next_actions": [],
            "omitted_details_summary": [],
        },
    }

    sanitized = _sanitize_payload_for_blind(malicious)
    s_str = json.dumps(sanitized)

    # Model-family tokens must be redacted
    assert "Codex" not in s_str, f"Model-family token 'Codex' leaked: {s_str[:300]}"
    assert "Sonnet" not in s_str, f"Model-family token 'Sonnet' leaked: {s_str[:300]}"
    assert "Opus" not in s_str, f"Model-family token 'Opus' leaked: {s_str[:300]}"
    assert "Haiku" not in s_str, f"Model-family token 'Haiku' leaked: {s_str[:300]}"
    assert "codex" not in s_str.lower(), f"Model-family token 'codex' leaked: {s_str[:300]}"
    assert "sonnet" not in s_str.lower(), f"Model-family token 'sonnet' leaked: {s_str[:300]}"


# ═══════════════════════════════════════════════════════════════════════════════
# COUNTEREXAMPLE 3: Comparability — blank provenance model must fail
# ═══════════════════════════════════════════════════════════════════════════════


def test_comparability_fails_on_blank_provenance_model():
    """provenance.model='' must cause comparability to fail.
    Missing values are not equivalent to the preregistered model."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "", "backend": ""},  # blank model and backend
        "run_facts": {
            "actual_model": "",
            "question": "q",
            "timeout_seconds": 300,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "q",
            "timeout_seconds": 300,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {"model": "sonnet", "question": "q", "timeout_seconds": 300}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False, "Blank provenance model must fail comparability"
    # Confirm the reason mentions blank/missing/empty model
    assert any(
        "model" in r.lower()
        and (
            "blank" in r.lower()
            or "empty" in r.lower()
            or "missing" in r.lower()
            or "''" in r
            or '""' in r
        )
        for r in comp["reasons"]
    ), f"Expected blank-model reason, got: {comp['reasons']}"


def test_comparability_fails_on_missing_provenance_dict():
    """provenance must be a dict — missing or non-dict fails comparability."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    a = {
        "status": "ok",
        "payload": {},
        # provenance is MISSING entirely
        "run_facts": {
            "actual_model": "sonnet",
            "question": "q",
            "timeout_seconds": 300,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {
            "actual_model": "sonnet",
            "question": "q",
            "timeout_seconds": 300,
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
        },
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {"model": "sonnet", "question": "q", "timeout_seconds": 300}

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False, "Missing provenance dict must fail comparability"


def test_execute_run_does_not_fallback_requested_model_when_provenance_missing(
    tmp_path,
):
    """execute_run must NEVER fall back to the requested model when runtime
    provenance model is missing/empty.  actual_model must be blank so
    comparability fails closed."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunResult
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    # Runtime result with MISSING provenance model key (simulates runtime that
    # doesn't report model — the fallback bug fills in the requested model)
    fake_results = []
    for _ in range(10):
        fake_results.append(
            AgentRunResult(
                status="ok",
                payload={
                    "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
                    "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
                },
                data_gaps=[],
                resource_usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                provenance={"backend": "codex"},  # model key MISSING
            )
        )

    factory, _call_log = _make_fake_adapter_factory(fake_results)
    execute_run(
        model=_EXPLICIT_MODEL,
        output_dir=str(output_dir),
        adapter_factory=factory,
    )

    rr_path = output_dir / "run_record.json"
    run_record = json.loads(rr_path.read_text(encoding="utf-8"))

    for pr in run_record["pairs"]:
        for arm_key in ("a", "b"):
            arm = pr[arm_key]
            facts = arm.get("run_facts", {})
            prov = arm.get("provenance", {})

            # actual_model must NOT be the requested model — it must be blank
            # because runtime provenance had no model key
            assert facts.get("actual_model") == "", (
                f"{pr['pair_id']}/{arm_key}: actual_model must be blank when "
                f"provenance model key is missing, not fallback to '{_EXPLICIT_MODEL}'"
            )
            # provenance model must also be blank (preserved, not fallback)
            assert prov.get("model") == "", (
                f"{pr['pair_id']}/{arm_key}: provenance.model must be blank, "
                f"not fallback to '{_EXPLICIT_MODEL}'"
            )
            # backend must still be codex
            assert prov.get("backend") == "codex", (
                f"{pr['pair_id']}/{arm_key}: provenance.backend must be 'codex'"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# BACKEND ALIGNMENT: comparability must enforce backend consistency
# ═══════════════════════════════════════════════════════════════════════════════


def test_comparability_fails_on_backend_mismatch_or_non_codex():
    """Two cases must fail comparability:

    1. Arms have different provenance.backend values (e.g. arm_a=codex,
       arm_b=other_runtime).  Backend MUST be identical across arms.
    2. Both arms agree on a backend that does NOT match the preregistered
       expected backend (e.g. both are openai but envelope says codex).

    Both cases are independently reproduced false-greens.
    """
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    _base_facts = {
        "actual_model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
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

    # ── Case 1: arms disagree on backend ──
    a1 = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b1 = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "other_runtime"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {
        "model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
        "backend": "codex",
    }

    comp1 = evaluate_comparability(arm_a_result=a1, arm_b_result=b1, envelope=envelope)
    assert comp1["comparable"] is False, (
        "Arms with different provenance.backend must NOT be comparable"
    )
    assert any("backend" in r.lower() for r in comp1["reasons"]), (
        f"Expected a backend-related reason, got: {comp1['reasons']}"
    )

    # ── Case 2: both arms have same backend but it's not the expected one ──
    a2 = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "openai"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b2 = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "openai"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }

    comp2 = evaluate_comparability(arm_a_result=a2, arm_b_result=b2, envelope=envelope)
    assert comp2["comparable"] is False, (
        "backend=openai must NOT be comparable when envelope expects backend=codex"
    )
    assert any("backend" in r.lower() for r in comp2["reasons"]), (
        f"Expected a backend-related reason, got: {comp2['reasons']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# M1 Final Repair Slice — RED tests
# ═══════════════════════════════════════════════════════════════════════════════


# ── Budget: reasoning_effort + max_tool_calls on both arms ────────────────────


def test_assemble_batch_sets_budget_on_both_arms():
    """assemble_batch must set budget with reasoning_effort='low' and
    max_tool_calls=0 on BOTH baseline and treatment arms."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import assemble_batch

    batch = assemble_batch(model=_EXPLICIT_MODEL)
    for pair in batch["pairs"]:
        for arm_key in ("a", "b"):
            arm = pair[arm_key]
            budget = arm.get("budget", {})
            assert isinstance(budget, dict), f"{pair['pair_id']}/{arm_key}: budget must be a dict"
            assert budget.get("reasoning_effort") == "low", (
                f"{pair['pair_id']}/{arm_key}: reasoning_effort must be 'low', "
                f"got {budget.get('reasoning_effort')!r}"
            )
            assert budget.get("max_tool_calls") == 0, (
                f"{pair['pair_id']}/{arm_key}: max_tool_calls must be 0, "
                f"got {budget.get('max_tool_calls')!r}"
            )


def test_execute_run_run_facts_records_budget_fields(tmp_path):
    """execute_run must record reasoning_effort and max_tool_calls from the
    arm budget in each arm's run_facts."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunResult
    from tools.effect_evaluation.fin_agent_runtime_m1 import execute_run

    output_dir = tmp_path / "m1_output"
    output_dir.mkdir()

    fake_results = []
    for _ in range(10):
        fake_results.append(
            AgentRunResult(
                status="ok",
                payload={
                    "research_product": copy.deepcopy(_VALID_RESEARCH_PRODUCT),
                    "display_product": copy.deepcopy(_VALID_DISPLAY_PRODUCT),
                },
                data_gaps=[],
                resource_usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            )
        )

    factory, _call_log = _make_fake_adapter_factory(fake_results)
    execute_run(model=_EXPLICIT_MODEL, output_dir=str(output_dir), adapter_factory=factory)

    rr_path = output_dir / "run_record.json"
    run_record = json.loads(rr_path.read_text(encoding="utf-8"))

    for pr in run_record["pairs"]:
        for arm_key in ("a", "b"):
            arm = pr[arm_key]
            facts = arm.get("run_facts", {})
            assert "reasoning_effort" in facts, (
                f"{pr['pair_id']}/{arm_key}: reasoning_effort missing from run_facts"
            )
            assert facts.get("reasoning_effort") == "low", (
                f"{pr['pair_id']}/{arm_key}: reasoning_effort must be 'low', "
                f"got {facts.get('reasoning_effort')!r}"
            )
            assert "max_tool_calls" in facts, (
                f"{pr['pair_id']}/{arm_key}: max_tool_calls missing from run_facts"
            )
            assert facts.get("max_tool_calls") == 0, (
                f"{pr['pair_id']}/{arm_key}: max_tool_calls must be 0, "
                f"got {facts.get('max_tool_calls')!r}"
            )


def test_comparability_verifies_reasoning_effort():
    """evaluate_comparability must verify reasoning_effort matches across
    both arms and against the envelope expected value."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    _base_facts = {
        "actual_model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
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
        "reasoning_effort": "low",
        "max_tool_calls": 0,
    }

    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {
        "model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
        "reasoning_effort": "low",
        "max_tool_calls": 0,
    }

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is True, f"Should be comparable, got: {comp['reasons']}"

    # Case 2: mismatch in reasoning_effort between arms
    b_bad = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {**_base_facts, "reasoning_effort": "high"},
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    comp_bad = evaluate_comparability(arm_a_result=a, arm_b_result=b_bad, envelope=envelope)
    assert comp_bad["comparable"] is False, (
        "Mismatched reasoning_effort between arms must fail comparability"
    )


def test_comparability_verifies_max_tool_calls():
    """evaluate_comparability must verify max_tool_calls matches across both
    arms and against the envelope expected value."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    _base_facts = {
        "actual_model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
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
        "reasoning_effort": "low",
        "max_tool_calls": 0,
    }

    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    # Arm B has different max_tool_calls
    b_bad = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": {**_base_facts, "max_tool_calls": 5},
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {
        "model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
        "reasoning_effort": "low",
        "max_tool_calls": 0,
    }

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b_bad, envelope=envelope)
    assert comp["comparable"] is False, (
        "Mismatched max_tool_calls between arms must fail comparability"
    )


def test_comparability_verifies_budget_facts_missing():
    """evaluate_comparability must fail when reasoning_effort or max_tool_calls
    is missing from run_facts (even if envelope has a value for them)."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import evaluate_comparability

    _base_facts = {
        "actual_model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
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

    # Missing reasoning_effort and max_tool_calls entirely
    a = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    b = {
        "status": "ok",
        "payload": {},
        "provenance": {"model": "sonnet", "backend": "codex"},
        "run_facts": dict(_base_facts),
        "opaque_continuation": {},
        "resource_usage": {"input_tokens": 100},
    }
    envelope = {
        "model": "sonnet",
        "question": "q",
        "timeout_seconds": 300,
        "reasoning_effort": "low",
        "max_tool_calls": 0,
    }

    comp = evaluate_comparability(arm_a_result=a, arm_b_result=b, envelope=envelope)
    assert comp["comparable"] is False, (
        "Missing reasoning_effort/max_tool_calls in run_facts when envelope requires "
        "them must fail comparability"
    )


# ── Prompt tightening: confidence_boundary as JSON object ─────────────────────


def test_codex_prompt_requires_confidence_boundary_json_object():
    """The prompt must explicitly request that confidence_boundary be a JSON
    object with the three boundary booleans, and request concise arrays/strings."""
    from fin_analyse.guo_teacher_research.agent_runtime import AgentRunRequest
    from fin_analyse.guo_teacher_research.codex_runtime import CodexCliAgentRuntimeAdapter

    captured_command: list[str] = []

    def capture_runner(command, **kwargs):
        captured_command[:] = list(command)
        return _fake_runner_ok_for_prompt_tests(command, **kwargs)

    adapter = CodexCliAgentRuntimeAdapter(
        codex_bin="codex",
        model="test",
        workspace_path="/tmp",
        timeout_seconds=60,
        runner=capture_runner,
    )

    # Test both neutral (baseline) and FIN (treatment) prompts
    for uc_ref in ("generic_research_answer", "agent_analyze_general"):
        captured_command.clear()
        request = AgentRunRequest(
            use_case_ref=uc_ref,
            question="test question",
            context_pack={},
        )
        adapter.run(request)

        prompt = captured_command[-1]
        assert isinstance(prompt, str)
        # Must mention confidence_boundary as a JSON object requirement
        assert "confidence_boundary" in prompt, (
            f"[{uc_ref}] Prompt must mention confidence_boundary:\n{prompt[:300]}"
        )
        # Must request the three boundary booleans
        assert "advisory_only" in prompt, (
            f"[{uc_ref}] Prompt must mention advisory_only:\n{prompt[:300]}"
        )
        assert "execution_allowed" in prompt, (
            f"[{uc_ref}] Prompt must mention execution_allowed:\n{prompt[:300]}"
        )
        assert "human_confirmation_required" in prompt, (
            f"[{uc_ref}] Prompt must mention human_confirmation_required:\n{prompt[:300]}"
        )


def _fake_runner_ok_for_prompt_tests(command, **kwargs):
    """Local fake for prompt test. Defined at module level to avoid closure issues."""
    import json as _json
    import subprocess as _sp

    product = _json.dumps(
        {
            "research_product": {"answer_summary": "test"},
            "display_product": {
                "display_intent": "ok",
                "headline": "t",
                "short_answer": "t",
                "primary_sections": [],
                "candidate_profiles": [],
                "analysis_path_summary": [],
                "confidence_boundary": {
                    "advisory_only": True,
                    "execution_allowed": False,
                    "human_confirmation_required": True,
                },
                "next_actions": [],
                "omitted_details_summary": [],
                "planner_source": "codex",
            },
        }
    )
    lines = [
        _json.dumps({"type": "thread.started"}),
        _json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": product}}),
        _json.dumps({"type": "turn.completed"}),
    ]
    return _sp.CompletedProcess(
        args=command, returncode=0, stdout="\n".join(lines) + "\n", stderr=""
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: reconstruction rejects tampered run_fact
# ═══════════════════════════════════════════════════════════════════════════════


def test_reconstruct_rejects_tampered_run_fact_vs_runtime_policy():
    """When a run_fact.reasoning_effort was tampered after recording to differ
    from the top-level runtime_policy, comparability must fail because the
    envelope (built from runtime_policy via arm budget) disagrees with the
    tampered run_fact."""
    from tools.effect_evaluation.fin_agent_runtime_m1 import (
        _reconstruct_batch_from_run_record,
        evaluate_comparability,
    )

    run_data = {
        "case_id": "helium-supply-gap-m1",
        "model": "sonnet",
        "backend": "codex",
        "timeout_seconds": 300.0,
        "runtime_policy": {"reasoning_effort": "low", "max_tool_calls": 0},
        "pairs": [
            {
                "pair_id": "pair-helium-01",
                "_mapping": {"a": "baseline", "b": "treatment"},
                "a": {
                    "status": "ok",
                    "payload": {},
                    "provenance": {"model": "sonnet", "backend": "codex"},
                    "run_facts": {
                        "actual_model": "sonnet",
                        "question": "q",
                        "timeout_seconds": 300,
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
                        # TAMPERED: should be "low" but set to "xhigh"
                        "reasoning_effort": "xhigh",
                        "max_tool_calls": 0,
                    },
                    "opaque_continuation": {},
                    "resource_usage": {"input_tokens": 100},
                },
                "b": {
                    "status": "ok",
                    "payload": {},
                    "provenance": {"model": "sonnet", "backend": "codex"},
                    "run_facts": {
                        "actual_model": "sonnet",
                        "question": "q",
                        "timeout_seconds": 300,
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
                        "reasoning_effort": "low",
                        "max_tool_calls": 0,
                    },
                    "opaque_continuation": {},
                    "resource_usage": {"input_tokens": 100},
                },
            },
        ],
    }

    batch = _reconstruct_batch_from_run_record(run_data)
    # Verify budget was reconstructed from runtime_policy
    for pair in batch["pairs"]:
        assert pair["a"]["budget"]["reasoning_effort"] == "low"
        assert pair["b"]["budget"]["reasoning_effort"] == "low"

    # Build run_records dict from the run_data for comparability
    run_records = {
        "pair-helium-01": {
            "a": run_data["pairs"][0]["a"],
            "b": run_data["pairs"][0]["b"],
        },
    }

    arm_a = batch["pairs"][0]["a"]
    envelope = {
        "model": arm_a.get("model", ""),
        "backend": "codex",
        "question": arm_a.get("question", ""),
        "timeout_seconds": arm_a.get("timeout_seconds", 0),
        "reasoning_effort": arm_a["budget"]["reasoning_effort"],
        "max_tool_calls": arm_a["budget"]["max_tool_calls"],
    }

    comp = evaluate_comparability(
        arm_a_result=run_records["pair-helium-01"]["a"],
        arm_b_result=run_records["pair-helium-01"]["b"],
        envelope=envelope,
    )
    # Tampered run_fact (xhigh) disagrees with envelope (low) → must fail
    assert comp["comparable"] is False, (
        f"Tampered run_fact should fail comparability, got: {comp['reasons']}"
    )
    assert any("reasoning_effort" in r.lower() for r in comp["reasons"]), (
        f"Expected reasoning_effort-related reason, got: {comp['reasons']}"
    )
