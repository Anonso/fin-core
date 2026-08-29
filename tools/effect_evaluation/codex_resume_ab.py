"""Codex resume A/B blind evaluation harness（Phase 3F）。

对照 semantic-rehydrate baseline（每轮新 ephemeral Codex）与 literal resume
（同 chain 内 ``codex exec resume`` 同一 session）在 **60 对链 × 4 轮** 上的
质量/延迟/成本差异，盲评输出，不因“resume 标签”偏置。

设计基线：

- 问题链：60 对链，每对两条链（A/B）共享同一 4 轮追问序列（确定性派生），
  只有 runtime 策略不同——A/B 臂映射在准备期预注册并固定（M1 同款
  ``_FIXED_MAPPING``），评审只见脱敏 payload。
- 4 层 strata（各 15 对）：普通追问 / 省略指代 / 换题 / 时间敏感 follow-up，
  覆盖 Phase 2 主线的典型延续形态。
- 盲评：``build_blind_packets`` 复用 M1 的 allowlist 投影 + 递归泄漏断言；
  两位独立评审各给 A/B/tie/review_required + 置信度；分歧由第三人裁决。
- 硬门禁：resume 质量不劣于 baseline、失败率不升高、latency/cost 有可观察
  收益才允许默认启用；否则永久停在 semantic rehydrate（B_REJECTED）。
- 本工具只产生脱敏运行记录与盲评包，不触碰 production 路由。

安全边界：评审包递归剥除 provider/FIN/identity/session 字段与字符串 token；
不包含 prompt、transcript、credential、持仓或账户原文。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

_PAIR_COUNT = 60
_TURNS_PER_CHAIN = 4
_STRATA = (
    "plain_followup",
    "ellipsis_reference",
    "topic_switch",
    "time_sensitive",
)
_STRATA_SIZE = _PAIR_COUNT // len(_STRATA)  # 15 对 / 层

# A/B 臂映射在准备期固定；pair id 派生自 stratum + 序号，评审无感知
_FIXED_MAPPING: dict[str, dict[str, str]] = {}
_pair_index = 0
for stratum in _STRATA:
    for offset in range(_STRATA_SIZE):
        _pair_index += 1
        pair_id = f"pair-{stratum}-{offset + 1:02d}"
        if _pair_index % 2 == 1:
            _FIXED_MAPPING[pair_id] = {"a": "baseline", "b": "resume"}
        else:
            _FIXED_MAPPING[pair_id] = {"a": "resume", "b": "baseline"}

_VALID_JUDGMENT_CHOICES: frozenset[str] = frozenset({"A", "B", "tie", "review_required"})
_VALID_CONFIDENCE_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

# 盲评包递归剥除的敏感子串（key 与 value token 双查）——
# 含臂身份词（baseline/resume/rehydrate），评审不得感知臂归属
_BLIND_FORBIDDEN_SUBSTR: tuple[str, ...] = (
    "session_id",
    "runtime_identity",
    "identity_hash",
    "provider",
    "model",
    "route",
    "credential",
    "token",
    "prompt",
    "transcript",
    "account",
    "portfolio",
    "position",
    "principal",
    "continuation",
    "baseline",
    "resume",
    "rehydrate",
)

# 盲评 allowlist：评审只看 display product 的领域内容
_BLIND_ALLOWLISTED_TOP_KEYS: frozenset[str] = frozenset(
    {
        "display_product",
        "question",
        "stratum",
    }
)

_BLIND_RUBRIC: dict[str, Any] = {
    "criteria": [
        "evidence_integration",
        "analytical_depth",
        "clarity",
        "source_attribution",
        "continuity_quality",
        "safety_boundary_adherence",
    ],
    "instructions": (
        "For each pair, compare arm A and arm B and choose A, B, tie, or "
        "review_required. Judge the research quality of the FIN display output "
        "for the FINAL turn of the chain — not the underlying runtime strategy."
    ),
}


@dataclass(frozen=True)
class ChainQuestionSet:
    """一条链的 4 轮确定性追问序列（同对 A/B 共享）。

    ``replicate_id`` 显式建模同一 stratum 模板的重复试验（每层 15 条 =
    15 个独立 replicate，共享同一条 4 轮模板但视为独立样本）。
    """

    pair_id: str
    stratum: str
    replicate_id: int
    turns: tuple[str, ...]


# 确定性 4 轮追问模板（按 stratum）——首轮问题相同，后续轮次分层派生
_TURN_TEMPLATES: dict[str, tuple[str, str, str, str]] = {
    "plain_followup": (
        "简述当前市场对氢能源板块的主要分歧点。",
        "上述分歧中，哪个因素当前影响最大？",
        "该因素在最近一个月有何边际变化？",
        "综合来看，短期与中期分别应关注什么？",
    ),
    "ellipsis_reference": (
        "分析A公司近期盈利质量的变化。",
        "它主要由什么驱动？",
        "这种驱动能持续吗？",
        "相比同行，它的优势与风险分别在哪？",
    ),
    "topic_switch": (
        "当前宏观流动性环境如何？",
        "这对成长股估值有什么含义？",
        "换个话题：新能源车渗透率的最新趋势？",
        "该趋势对产业链各环节的利润分配有何影响？",
    ),
    "time_sensitive": (
        "今天市场整体表现如何？",
        "现在呢？有什么新的变化？",
        "再确认一下：当前时刻的最新情况？",
        "截至此刻，最值得注意的边际信息是什么？",
    ),
}


def derive_chain_question_sets() -> list[ChainQuestionSet]:
    """确定性生成 60 对链的追问序列（A/B 共享同序列）。"""
    result: list[ChainQuestionSet] = []
    for stratum in _STRATA:
        template = _TURN_TEMPLATES[stratum]
        for offset in range(_STRATA_SIZE):
            pair_id = f"pair-{stratum}-{offset + 1:02d}"
            turns = tuple(template)
            result.append(
                ChainQuestionSet(
                    pair_id=pair_id,
                    stratum=stratum,
                    replicate_id=offset + 1,
                    turns=turns,
                )
            )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 盲评包构造（复用 M1 的 allowlist + 泄漏断言模式）
# ═══════════════════════════════════════════════════════════════════════════════


def build_blind_packets(
    run_records: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    """从运行记录构造脱敏盲评包。

    ``run_records``: pair_id -> arm(a/b) -> {payload, latency_ms, tokens,
    turn_number}。只接受 ``turn_number == 4``（最终 turn）的 display payload
    供评审；缺失或非 4 一律拒绝（fail-closed，不信任调用方只传 turn4）。
    """
    blind_pairs: list[dict[str, Any]] = []
    for pair_id in sorted(run_records):
        arms = run_records[pair_id]
        blind: dict[str, Any] = {"pair_id": pair_id}
        for arm_key in ("a", "b"):
            rec = arms.get(arm_key, {})
            if rec.get("turn_number") != _TURNS_PER_CHAIN:
                raise ValueError(
                    f"blind packet requires final turn ({_TURNS_PER_CHAIN}) record: {pair_id}/{arm_key}"
                )
            payload = _sanitize_payload_for_blind(rec.get("payload", {}))
            blind[arm_key] = {"label": arm_key.upper(), "payload": payload}
        blind_pairs.append(blind)

    result: dict[str, Any] = {
        # case_id 中性化：不泄露臂身份（baseline/resume）
        "case_id": "fin-runtime-comparison-blind-3f",
        "pair_count": len(blind_pairs),
        "rubric": _BLIND_RUBRIC,
        "pairs": blind_pairs,
    }
    _assert_no_blind_leaks(result)
    return result


def _sanitize_payload_for_blind(payload: dict[str, Any]) -> dict[str, Any]:
    """递归 fail-closed 投影：只保留 allowlist 顶层键。"""
    projected = _recursive_blind_project(payload, is_top_level=True)
    if not isinstance(projected, dict):
        raise ValueError("blind payload projection must be a dict")
    return projected


def _recursive_blind_project(obj: Any, *, is_top_level: bool = False) -> Any:
    if isinstance(obj, dict):
        projected: dict[str, Any] = {}
        for key, value in obj.items():
            if is_top_level and key not in _BLIND_ALLOWLISTED_TOP_KEYS:
                continue
            projected[key] = _recursive_blind_project(value)
        return projected
    if isinstance(obj, list):
        return [_recursive_blind_project(item) for item in obj]
    if isinstance(obj, str):
        # 值级 fail-closed：任何 forbidden 词以词边界出现 → 抹掉整个叶节点。
        # 只替换词本身会让 `API_TOKEN=secret` 保留实际值 secret；叶节点
        # 一旦命中禁词即整体 [redacted]。
        for forbidden in _BLIND_FORBIDDEN_SUBSTR:
            if re.search(rf"\b{re.escape(forbidden)}\b", obj, flags=re.IGNORECASE):
                return "[redacted]"
        return obj
    return obj


def _assert_no_blind_leaks(obj: Any, path: str = "$") -> None:
    """递归验证盲评包无敏感 key/字符串 token 泄漏。"""
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
        words = re.split(r"\W+", obj.lower())
        for word in words:
            if not word:
                continue
            for forbidden in _BLIND_FORBIDDEN_SUBSTR:
                if word == forbidden.lower():
                    raise ValueError(
                        f"Blind packet leak: identity token '{word}' in value at {path}"
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# 判决聚合（双评审 + 第三人裁决）
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Judgment:
    pair_id: str
    judge_id: str
    choice: str
    confidence: str


@dataclass(frozen=True)
class AggregateVerdict:
    pair_id: str
    resolution: str  # A / B / tie / review_required
    judge_choices: tuple[str, ...]
    needs_third: bool


def aggregate_judgments(judgments: list[Judgment]) -> list[AggregateVerdict]:
    """双评审聚合：一致即定；任一 review_required 或 A vs B 分歧需第三人。

    第三人裁决不在本函数内执行——输出 ``needs_third`` 供调用方收集第三方
    判决后重跑。逐条校验 choice/confidence 域，非法即抛；每 pair 必须恰好
    两位不同评审者（拒绝同一 judge_id 重复伪造一致）。
    """
    by_pair: dict[str, list[Judgment]] = {}
    for judgment in judgments:
        _validate_judgment(judgment)
        by_pair.setdefault(judgment.pair_id, []).append(judgment)

    verdicts: list[AggregateVerdict] = []
    for pair_id in sorted(by_pair):
        pair_judgments = by_pair[pair_id]
        judge_ids = tuple(j.judge_id for j in pair_judgments)
        if len(set(judge_ids)) != 2 or len(pair_judgments) != 2:
            raise ValueError(f"pair {pair_id} requires exactly two distinct judges")
        choices = tuple(j.choice for j in pair_judgments)
        if "review_required" in choices:
            verdicts.append(
                AggregateVerdict(
                    pair_id=pair_id,
                    resolution="review_required",
                    judge_choices=choices,
                    needs_third=True,
                )
            )
        elif choices[0] == choices[1]:
            verdicts.append(
                AggregateVerdict(
                    pair_id=pair_id,
                    resolution=choices[0],
                    judge_choices=choices,
                    needs_third=False,
                )
            )
        else:
            verdicts.append(
                AggregateVerdict(
                    pair_id=pair_id,
                    resolution="review_required",
                    judge_choices=choices,
                    needs_third=True,
                )
            )
    return verdicts


def _validate_judgment(judgment: Judgment) -> None:
    if judgment.choice not in _VALID_JUDGMENT_CHOICES:
        raise ValueError(f"invalid judgment choice: {judgment.choice}")
    if judgment.confidence not in _VALID_CONFIDENCE_LEVELS:
        raise ValueError(f"invalid confidence level: {judgment.confidence}")


# ═══════════════════════════════════════════════════════════════════════════════
# 统计与启用决策
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class AbSummary:
    total_pairs: int
    resume_wins: int
    baseline_wins: int
    ties: int
    review_required: int
    resume_median_latency_ms: float | None
    baseline_median_latency_ms: float | None
    resume_failure_rate: float
    baseline_failure_rate: float
    resume_tokens: int | None
    baseline_tokens: int | None
    decision: str


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def summarize(
    verdicts: list[AggregateVerdict],
    run_records: dict[str, dict[str, dict[str, Any]]],
) -> AbSummary:
    """汇总质量/延迟/成本并给启用决策（fail-closed cohort 校验）。

    启用前必须：verdict 覆盖全部 60 对、每对双臂记录完整、verdict 与记录
    一一对应、无未知 arm。质量胜负按 ``_FIXED_MAPPING[pair_id]`` 逐对解盲
    归因——不是把所有 B/A 简单计为 resume/baseline。

    - 质量：resume_wins >= baseline_wins 且 review_required 占比 <= 10%
      视为质量非劣。
    - 失败率：resume 不高于 baseline。
    - 延迟：resume 中位延迟 < baseline 中位延迟 × 0.95（5% 收益）才认可。
    - 成本：resume tokens <= baseline tokens（有可观察收益）。
    全部满足 → APPROVE；否则 B_REJECTED_NO_MEASURABLE_GAIN。
    """
    # 1) cohort 完整性：verdict 与 records 必须覆盖全部预注册对
    expected_pair_ids = set(_FIXED_MAPPING)
    verdict_pair_ids = {v.pair_id for v in verdicts}
    record_pair_ids = set(run_records)
    if verdict_pair_ids != expected_pair_ids:
        raise ValueError(
            f"verdict cohort incomplete: missing {sorted(expected_pair_ids - verdict_pair_ids)}"
        )
    if record_pair_ids != expected_pair_ids:
        raise ValueError(
            f"record cohort incomplete: missing {sorted(expected_pair_ids - record_pair_ids)}"
        )
    if len(verdicts) != len({v.pair_id for v in verdicts}):
        raise ValueError("duplicate verdict pair_id")

    # 2) 逐对解盲归因胜者
    resume_wins = 0
    baseline_wins = 0
    ties = 0
    review_required = 0
    for verdict in verdicts:
        mapping = _FIXED_MAPPING[verdict.pair_id]
        if verdict.needs_third:
            review_required += 1
            continue
        if verdict.resolution not in ("A", "B", "tie"):
            raise ValueError(f"unexpected resolution: {verdict.resolution}")
        if verdict.resolution == "tie":
            ties += 1
            continue
        winner_role = mapping[verdict.resolution.lower()]
        if winner_role == "resume":
            resume_wins += 1
        elif winner_role == "baseline":
            baseline_wins += 1
        else:
            raise ValueError(f"unknown winner role: {winner_role}")

    # 3) 指标：双臂必须存在且可归因
    resume_latency: list[float] = []
    baseline_latency: list[float] = []
    resume_failures = 0
    baseline_failures = 0
    resume_total = 0
    baseline_total = 0
    resume_tokens: list[int] = []
    baseline_tokens: list[int] = []
    for pair_id, arms in run_records.items():
        mapping = _FIXED_MAPPING[pair_id]
        if set(arms) != {"a", "b"}:
            raise ValueError(f"pair {pair_id} requires both arms a and b")
        for arm_key, rec in arms.items():
            role = mapping[arm_key]
            latency = rec.get("latency_ms")
            tokens = rec.get("tokens")
            failed = bool(rec.get("failed"))
            if role == "resume":
                resume_total += 1
                if failed:
                    resume_failures += 1
                elif isinstance(latency, (int, float)):
                    resume_latency.append(float(latency))
                if isinstance(tokens, int):
                    resume_tokens.append(tokens)
            elif role == "baseline":
                baseline_total += 1
                if failed:
                    baseline_failures += 1
                elif isinstance(latency, (int, float)):
                    baseline_latency.append(float(latency))
                if isinstance(tokens, int):
                    baseline_tokens.append(tokens)
            else:
                raise ValueError(f"unknown role: {role}")

    resume_median = _median(resume_latency)
    baseline_median = _median(baseline_latency)
    resume_failure_rate = resume_failures / resume_total if resume_total else 0.0
    baseline_failure_rate = baseline_failures / baseline_total if baseline_total else 0.0

    quality_ok = resume_wins >= baseline_wins and review_required <= max(1, _PAIR_COUNT // 10)
    failure_ok = resume_failure_rate <= baseline_failure_rate
    latency_ok = (
        resume_median is not None
        and baseline_median is not None
        and resume_median < baseline_median * 0.95
    )
    cost_ok = (
        bool(resume_tokens) and bool(baseline_tokens) and sum(resume_tokens) <= sum(baseline_tokens)
    )
    decision = (
        "APPROVE"
        if quality_ok and failure_ok and latency_ok and cost_ok
        else "B_REJECTED_NO_MEASURABLE_GAIN"
    )

    return AbSummary(
        total_pairs=len(verdicts),
        resume_wins=resume_wins,
        baseline_wins=baseline_wins,
        ties=ties,
        review_required=review_required,
        resume_median_latency_ms=resume_median,
        baseline_median_latency_ms=baseline_median,
        resume_failure_rate=resume_failure_rate,
        baseline_failure_rate=baseline_failure_rate,
        resume_tokens=sum(resume_tokens) if resume_tokens else None,
        baseline_tokens=sum(baseline_tokens) if baseline_tokens else None,
        decision=decision,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex resume A/B blind evaluation (3F)")
    parser.add_argument(
        "--derive-questions",
        action="store_true",
        help="导出 60 对链 × 4 轮追问序列（确定性）到 stdout",
    )
    parser.add_argument(
        "--build-blind",
        metavar="RUN_RECORDS_JSON",
        help="从运行记录 JSON 构造脱敏盲评包并写 stdout",
    )
    parser.add_argument(
        "--aggregate",
        metavar="JUDGMENTS_JSON",
        help="聚合双评审判决（A/B/tie/review_required + needs_third）",
    )
    parser.add_argument(
        "--summarize",
        metavar="RUN_RECORDS_JSON",
        help="汇总质量/延迟/成本并给启用决策（需与 --verdicts 联用）",
    )
    parser.add_argument("--verdicts", metavar="VERDICTS_JSON")
    args = parser.parse_args(argv)

    if args.derive_questions:
        sets = derive_chain_question_sets()
        payload = {
            "schema_version": "codex-resume-ab-questions.v1",
            "pair_count": len(sets),
            "strata": list(_STRATA),
            "turns_per_chain": _TURNS_PER_CHAIN,
            "mapping": _FIXED_MAPPING,
            "chains": [vars(q) for q in sets],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.build_blind:
        records = json.loads(Path(args.build_blind).read_text(encoding="utf-8"))
        packet = build_blind_packets(records)
        print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.aggregate:
        raw = json.loads(Path(args.aggregate).read_text(encoding="utf-8"))
        judgments = [
            Judgment(
                pair_id=j["pair_id"],
                judge_id=j["judge_id"],
                choice=j["choice"],
                confidence=j["confidence"],
            )
            for j in raw["judgments"]
        ]
        for j in judgments:
            _validate_judgment(j)
        verdicts = aggregate_judgments(judgments)
        print(
            json.dumps(
                {
                    "schema_version": "codex-resume-ab-verdicts.v1",
                    "verdicts": [vars(v) for v in verdicts],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.summarize:
        records = json.loads(Path(args.summarize).read_text(encoding="utf-8"))
        verdicts_raw = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))["verdicts"]
        verdicts = [
            AggregateVerdict(
                pair_id=v["pair_id"],
                resolution=v["resolution"],
                judge_choices=tuple(v["judge_choices"]),
                needs_third=v["needs_third"],
            )
            for v in verdicts_raw
        ]
        summary = summarize(verdicts, records)
        print(json.dumps(vars(summary), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    parser.error("specify one of --derive-questions / --build-blind / --aggregate / --summarize")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
