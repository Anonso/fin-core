"""Migration guard: CognitiveService must use CognitionWriteGateService.

This test file verifies that CognitiveService no longer directly calls
apply_persona_gate() or _is_teacher_cognition_evidence(), and instead
delegates to CognitionWriteGateService.evaluate_evidence().
"""

import ast
from pathlib import Path

from fin_analyse.cognition.models import EvidenceItem, SourceLabel
from fin_analyse.cognition.service import CognitiveService
from fin_analyse.cognition.write_gate import (
    CognitionWriteGateResult,
    CognitionWriteGateService,
)


class TestCognitiveServiceUsesCognitionWriteGateService:
    """CognitiveService must import and use CognitionWriteGateService."""

    def test_cognitive_service_imports_write_gate_service(self):
        """CognitiveService module must import CognitionWriteGateService."""
        path = Path("fin_analyse/cognition/service.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        imports_write_gate = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "fin_analyse.cognition.write_gate"
            ):
                for alias in node.names:
                    if alias.name == "CognitionWriteGateService":
                        imports_write_gate = True

        assert imports_write_gate, (
            "CognitiveService must import CognitionWriteGateService "
            "from fin_analyse.cognition.write_gate"
        )

    def test_cognitive_service_no_direct_apply_persona_gate(self):
        """CognitiveService must NOT directly call apply_persona_gate."""
        path = Path("fin_analyse/cognition/service.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        forbidden = {"apply_persona_gate", "_is_teacher_cognition_evidence"}
        offenders: list[str] = []

        for node in ast.walk(tree):
            # Check imports from persona_gate
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "fin_analyse.cognition.persona_gate"
            ):
                for alias in node.names:
                    if alias.name in forbidden:
                        offenders.append(
                            f"forbidden import {alias.name} line {node.lineno}"
                        )
            # Check direct calls
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden
            ):
                    offenders.append(
                        f"forbidden call {node.func.id} line {node.lineno}"
                    )

        assert not offenders, (
            "direct persona gate helper use remains in CognitiveService: "
            + "; ".join(offenders)
        )

    def test_cognitive_service_has_write_gate_service_attribute(self):
        """CognitiveService instance must have a _write_gate_service attribute."""
        svc = CognitiveService()
        assert hasattr(svc, "_write_gate_service"), (
            "CognitiveService must have _write_gate_service attribute"
        )
        assert isinstance(svc._write_gate_service, CognitionWriteGateService), (
            "_write_gate_service must be a CognitionWriteGateService instance"
        )


class TestCognitionWriteGateResult:
    """CognitionWriteGateResult must exist with required fields."""

    def test_result_has_required_fields(self):
        """Result must have evidence_id, gate_decision, write_target,
        is_teacher_cognition, gated_evidence."""
        from fin_analyse.cognition.persona_gate import PersonaGateDecision

        decision = PersonaGateDecision(
            evidence_id="ev-test",
            allows_persona=True,
            category="star_teacher_original",
            source_classification="teacher_original",
            confidence=0.9,
            half_life_class="medium_logic",
            reasons=["test"],
        )
        evidence = EvidenceItem(
            evidence_id="ev-test",
            source_type="zsxq_article",
            source_id="art-test",
            title="测试",
            content="测试内容",
            author="郭老师",
            published_at="2026-06-27",
            collected_at="2026-06-27",
            companies=["测试公司"],
            topics=["测试主题"],
            source_label=SourceLabel("teacher_original", "guo", 0.8, []),
            reliability=0.8,
            metadata={},
        )

        result = CognitionWriteGateResult(
            evidence_id="ev-test",
            gate_decision=decision,
            write_target="persona",
            is_teacher_cognition=True,
            gated_evidence=evidence,
        )

        assert result.evidence_id == "ev-test"
        assert result.write_target == "persona"
        assert result.is_teacher_cognition is True
        assert result.gated_evidence is evidence
