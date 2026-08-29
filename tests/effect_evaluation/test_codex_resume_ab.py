"""Phase 3F：Codex resume A/B 盲评 harness 测试。

覆盖：60 对链×4 轮确定性派生、A/B 固定映射平衡、盲评包脱敏无泄漏、
双评审聚合（一致/分歧/第三人）、统计与启用决策门禁。
"""

from __future__ import annotations

import json

import pytest

from tools.effect_evaluation.codex_resume_ab import (
    _FIXED_MAPPING,
    _PAIR_COUNT,
    _STRATA,
    _TURNS_PER_CHAIN,
    AbSummary,
    AggregateVerdict,
    Judgment,
    aggregate_judgments,
    build_blind_packets,
    derive_chain_question_sets,
    summarize,
)


def _sample_records(pair_ids: tuple[str, ...]) -> dict[str, dict[str, dict]]:
    """构造演示运行记录：按 _FIXED_MAPPING 分配角色，resume 更快更省、质量胜出。"""
    records: dict[str, dict[str, dict]] = {}
    for index, pair_id in enumerate(pair_ids):
        mapping = _FIXED_MAPPING[pair_id]
        latency_bonus = 100 if index % 2 == 0 else 0  # 部分 pair resume 更快
        arms: dict[str, dict] = {}
        for arm_key, role in mapping.items():
            if role == "resume":
                arms[arm_key] = {
                    "payload": {
                        "display_product": {"summary": f"resume arm {arm_key} content"},
                        "provider": "should-be-stripped",
                        "session_id": "leak-token",
                    },
                    "latency_ms": 900 - latency_bonus,
                    "tokens": 600,
                    "turn_number": 4,
                }
            else:
                arms[arm_key] = {
                    "payload": {
                        "display_product": {"summary": f"baseline arm {arm_key} content"},
                        "provider": "should-be-stripped",
                    },
                    "latency_ms": 1200,
                    "tokens": 800,
                    "turn_number": 4,
                }
        records[pair_id] = arms
    return records


def _verdicts_for(pair_ids: tuple[str, ...], *, winner: str = "resume") -> list[AggregateVerdict]:
    """按 _FIXED_MAPPING 派生 winner 对应的 resolution（A/B），不硬编码 B=resume。"""
    verdicts: list[AggregateVerdict] = []
    for pid in pair_ids:
        mapping = _FIXED_MAPPING[pid]
        arm_key = next(k for k, role in mapping.items() if role == winner)
        resolution = arm_key.upper()
        verdicts.append(
            AggregateVerdict(
                pair_id=pid,
                resolution=resolution,
                judge_choices=(resolution, resolution),
                needs_third=False,
            )
        )
    return verdicts


def test_derive_questions_produces_60_chains_4_turns() -> None:
    sets = derive_chain_question_sets()
    assert len(sets) == _PAIR_COUNT
    for chain in sets:
        assert len(chain.turns) == _TURNS_PER_CHAIN
        assert chain.pair_id.startswith("pair-")
        assert chain.stratum in _STRATA
    # 每层 15 对
    per_stratum = dict.fromkeys(_STRATA, 0)
    for chain in sets:
        per_stratum[chain.stratum] += 1
    assert all(count == 15 for count in per_stratum.values())


def test_fixed_mapping_is_balanced_and_deterministic() -> None:
    assert len(_FIXED_MAPPING) == _PAIR_COUNT
    resume_a = sum(1 for m in _FIXED_MAPPING.values() if m["a"] == "resume")
    resume_b = sum(1 for m in _FIXED_MAPPING.values() if m["b"] == "resume")
    assert resume_a == resume_b == _PAIR_COUNT // 2
    # 同对 A/B 必须一 baseline 一 resume
    for mapping in _FIXED_MAPPING.values():
        assert set(mapping.values()) == {"baseline", "resume"}


def test_derive_questions_is_deterministic() -> None:
    first = derive_chain_question_sets()
    second = derive_chain_question_sets()
    assert [tuple(c.turns) for c in first] == [tuple(c.turns) for c in second]


def test_blind_packets_strip_sensitive_fields_and_tokens() -> None:
    records = _sample_records(("pair-plain_followup-01",))
    packet = build_blind_packets(records)
    # 敏感字段被剥除：provider/session_id 不存在
    serialized = json.dumps(packet, ensure_ascii=False)
    assert "should-be-stripped" not in serialized
    assert "session_id" not in serialized
    # 臂身份词被剥除（fail-closed）：resume/baseline 不作为评审可感知词
    assert "resume" not in serialized.lower()
    assert "baseline" not in serialized.lower()
    assert "rehydrate" not in serialized.lower()
    # 领域内容保留（中性文本）
    assert "arm A" in serialized or "arm B" in serialized
    # 顶层只保留 allowlist 键
    for pair in packet["pairs"]:
        for arm in ("a", "b"):
            assert set(pair[arm]["payload"].keys()) <= {"display_product", "question", "stratum"}


def test_aggregate_consistent_and_conflicting_judgments() -> None:
    judgments = [
        Judgment(pair_id="pair-plain_followup-01", judge_id="j1", choice="B", confidence="high"),
        Judgment(pair_id="pair-plain_followup-01", judge_id="j2", choice="B", confidence="medium"),
        Judgment(
            pair_id="pair-ellipsis_reference-01", judge_id="j1", choice="A", confidence="high"
        ),
        Judgment(
            pair_id="pair-ellipsis_reference-01", judge_id="j2", choice="B", confidence="high"
        ),
        Judgment(pair_id="pair-topic_switch-01", judge_id="j1", choice="tie", confidence="low"),
        Judgment(pair_id="pair-topic_switch-01", judge_id="j2", choice="tie", confidence="low"),
    ]
    verdicts = aggregate_judgments(judgments)
    by_pair = {v.pair_id: v for v in verdicts}
    assert by_pair["pair-plain_followup-01"].resolution == "B"
    assert by_pair["pair-plain_followup-01"].needs_third is False
    assert by_pair["pair-ellipsis_reference-01"].resolution == "review_required"
    assert by_pair["pair-ellipsis_reference-01"].needs_third is True
    assert by_pair["pair-topic_switch-01"].resolution == "tie"


def test_aggregate_rejects_invalid_judgment() -> None:
    bad = Judgment(pair_id="pair-x", judge_id="j1", choice="X", confidence="high")
    with pytest.raises(ValueError):
        aggregate_judgments([bad])


def test_summarize_approves_when_resume_wins_and_is_faster_cheaper() -> None:
    pair_ids = tuple(f"pair-{stratum}-{i:02d}" for stratum in _STRATA for i in range(1, 16))
    records = _sample_records(pair_ids)
    verdicts = _verdicts_for(pair_ids, winner="resume")
    summary = summarize(verdicts, records)
    assert isinstance(summary, AbSummary)
    assert summary.resume_wins == len(pair_ids)
    assert summary.baseline_wins == 0
    assert summary.resume_median_latency_ms is not None
    assert summary.baseline_median_latency_ms is not None
    assert summary.resume_median_latency_ms < summary.baseline_median_latency_ms
    assert summary.resume_failure_rate == 0.0
    assert summary.baseline_failure_rate == 0.0
    assert summary.decision == "APPROVE"


def test_summarize_rejects_when_no_latency_gain() -> None:
    pair_ids = tuple(f"pair-{stratum}-{i:02d}" for stratum in _STRATA for i in range(1, 16))
    records = _sample_records(pair_ids)
    # 抹掉 latency 收益：resume 与 baseline 同速（按角色，不按 arm key）
    for pair_id, arms in records.items():
        mapping = _FIXED_MAPPING[pair_id]
        for arm_key, role in mapping.items():
            if role == "resume":
                arms[arm_key]["latency_ms"] = 1200  # 与 baseline 相同
    verdicts = _verdicts_for(pair_ids, winner="resume")
    summary = summarize(verdicts, records)
    assert summary.decision == "B_REJECTED_NO_MEASURABLE_GAIN"


def test_summarize_rejects_when_resume_loses_quality() -> None:
    pair_ids = tuple(f"pair-{stratum}-{i:02d}" for stratum in _STRATA for i in range(1, 16))
    records = _sample_records(pair_ids)
    verdicts = _verdicts_for(pair_ids, winner="baseline")  # baseline 全胜
    summary = summarize(verdicts, records)
    assert summary.decision == "B_REJECTED_NO_MEASURABLE_GAIN"


def test_summarize_failure_rate_gate() -> None:
    pair_ids = tuple(f"pair-{stratum}-{i:02d}" for stratum in _STRATA for i in range(1, 16))
    records = _sample_records(pair_ids)
    # 让一个 resume 臂失败（按角色，不按 arm key）
    first_pair = pair_ids[0]
    mapping = _FIXED_MAPPING[first_pair]
    resume_arm = next(k for k, role in mapping.items() if role == "resume")
    records[first_pair][resume_arm]["failed"] = True
    verdicts = _verdicts_for(pair_ids, winner="resume")
    summary = summarize(verdicts, records)
    assert summary.resume_failure_rate == 1.0 / _PAIR_COUNT
    assert summary.baseline_failure_rate == 0.0
    assert summary.decision == "B_REJECTED_NO_MEASURABLE_GAIN"


def test_summarize_rejects_incomplete_cohort() -> None:
    """cohort 不完整（缺对/缺臂/重复 verdict）→ 抛错而非 APPROVE。"""
    all_pairs = tuple(f"pair-{stratum}-{i:02d}" for stratum in _STRATA for i in range(1, 16))
    records = _sample_records(all_pairs)
    verdicts = _verdicts_for(all_pairs, winner="resume")
    # 缺一对
    with pytest.raises(ValueError):
        summarize(verdicts[:-1], records)
    # 缺一个臂
    partial = dict(records)
    partial[all_pairs[0]] = {k: v for k, v in records[all_pairs[0]].items() if k == "a"}
    with pytest.raises(ValueError):
        summarize(verdicts, partial)
    # 重复 verdict pair_id
    duplicated = list(verdicts) + [verdicts[0]]
    with pytest.raises(ValueError):
        summarize(duplicated, records)


def test_aggregate_requires_two_distinct_judges() -> None:
    """同一 judge_id 重复不能伪造一致。"""
    judgments = [
        Judgment(pair_id="pair-plain_followup-01", judge_id="j1", choice="B", confidence="high"),
        Judgment(pair_id="pair-plain_followup-01", judge_id="j1", choice="B", confidence="high"),
    ]
    with pytest.raises(ValueError):
        aggregate_judgments(judgments)


def test_aggregate_review_required_precedence() -> None:
    """两位评审都选 review_required → needs_third=True（不是 False）。"""
    judgments = [
        Judgment(
            pair_id="pair-plain_followup-01",
            judge_id="j1",
            choice="review_required",
            confidence="low",
        ),
        Judgment(
            pair_id="pair-plain_followup-01",
            judge_id="j2",
            choice="review_required",
            confidence="low",
        ),
    ]
    verdicts = aggregate_judgments(judgments)
    assert verdicts[0].needs_third is True
    assert verdicts[0].resolution == "review_required"


def test_blind_packets_reject_non_final_turn() -> None:
    """非 turn-4 记录拒绝（fail-closed，不信任调用方只传 turn4）。"""
    records = _sample_records(("pair-plain_followup-01",))
    records["pair-plain_followup-01"]["a"]["turn_number"] = 3
    with pytest.raises(ValueError):
        build_blind_packets(records)
