"""Regression tests for current cognitive feedback fixes.

fb-a3c3cc758928: Remove false "防御与进攻策略对立" contradiction
fb-5141c12df566: Strengthen pork-social-retail pattern
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.runtime_state

_RUNTIME_ROOT_ENV = "FIN_TEST_COGNITION_RUNTIME_ROOT"


@pytest.fixture(scope="module")
def runtime_root() -> Path:
    configured_root = os.environ.get(_RUNTIME_ROOT_ENV)
    if not configured_root:
        pytest.fail(
            f"{_RUNTIME_ROOT_ENV} must be set to an explicit absolute cognition runtime root "
            "when running runtime_state tests",
            pytrace=False,
        )

    root = Path(configured_root)
    if not root.is_absolute():
        pytest.fail(
            f"{_RUNTIME_ROOT_ENV} must be an absolute path, got {configured_root!r}",
            pytrace=False,
        )
    return root


def test_current_synthesis_no_longer_labels_defense_to_attack_as_contradiction(
    runtime_root: Path,
):
    """fb-a3c3cc758928: The active synthesis (via latest.json) must not contain the false contradiction.

    The original syn-2026-06-30T070549.json is kept for audit and still contains the
    removed contradiction — this test validates the corrected version served to users.
    """
    # Read the active synthesis via latest.json pointer
    latest = json.loads(
        (runtime_root / "cross_article/syntheses/latest.json").read_text(encoding="utf-8")
    )
    active_id = latest["synthesis_id"]
    data = json.loads(
        (runtime_root / f"cross_article/syntheses/{active_id}.json").read_text(encoding="utf-8")
    )
    contradictions_text = json.dumps(
        data.get("cross_cluster_contradictions", []),
        ensure_ascii=False,
    )
    changes_text = json.dumps(data.get("viewpoint_changes", []), ensure_ascii=False)

    assert "防御与进攻策略对立" not in contradictions_text
    assert "五穷六绝" in changes_text
    assert "stance_evolution" in changes_text or "观点演化" in changes_text
    # Verify correction metadata is present
    assert data.get("correction", {}).get("feedback_id") == "fb-a3c3cc758928"
    assert data.get("previous_synthesis_id") == "syn-2026-06-30T070549"


def test_pork_social_retail_feedback_is_reflected_in_existing_pattern(runtime_root: Path):
    """fb-5141c12df566: The existing pattern must reflect pork-social-retail logic."""
    patterns = [
        json.loads(line)
        for line in (runtime_root / "cognition/cognitive_patterns.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    target = [
        p
        for p in patterns
        if p.get("pattern_id") == "pattern-guo-国补加码对消费与通胀相关资产的影响"
    ]

    assert len(target) == 1
    text = json.dumps(target[0], ensure_ascii=False)
    assert "猪肉" in text
    assert "社零" in text or "社会消费品零售总额" in text
    assert "猪周期" in text
    assert "fb-5141c12df566" in text


def test_no_duplicate_guo_pork_social_retail_pattern(runtime_root: Path):
    """Must not create a duplicate guo_pork_social_retail pattern."""
    patterns = [
        json.loads(line)
        for line in (runtime_root / "cognition/cognitive_patterns.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [p for p in patterns if p.get("pattern_id") == "guo_pork_social_retail"] == []
