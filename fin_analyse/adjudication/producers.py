"""市场实践回放线提名的收件箱 producer 适配（D-051 追记 #2，owner 2026-09-06 拍板接入）。

只读消费 cognition-replay 公开工件（``$STATE/fin-analyse/cognition-replay-evidence/
nominations-<batch>.json`` + 标注文档），**不改回放模块任何代码/工件**。工件格式契约
归 cognition-replay 特性所有；本适配按 D-051 注册协议冻结的判据消费：

- 只看**最新批次**（跨日以最新 as-of 提名为权威、旧档留痕——设计稿既定语义）；
- 完成信号 = 标注文档含「批次 <batch>」落账节标记，**或**该批全部 ``unit_id``
  已出现在标注文档（任一满足即 landed——前者覆盖「提案被 owner 裁决为不落」
  的整批处理形态）。
- 解析失败 = 真相未知（SKIPPED），不碰 inbox（不猜、不误报）。

同文件承载评分记录 needs_review 闭环扫描（D-051 追记 #3）：复用
``ingestion.instrument_scores`` 规范化 loader 只读计数，title 只含计数不含
标的名（硬边界 3，详情走本地 ``manage_instrument_scores.py list``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ReplayNominationScanResult",
    "NeedsReviewScanResult",
    "scan_replay_nominations",
    "scan_needs_review",
]


@dataclass(frozen=True)
class ReplayNominationScanResult:
    """Content-free batch view for the adjudication inbox."""

    disposition: str  # SCANNED | SKIPPED
    reason: str | None = None
    batch: str | None = None
    proposals: int = 0
    landed: bool = False
    nominations_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "reason": self.reason,
            "batch": self.batch,
            "proposals": self.proposals,
            "landed": self.landed,
            "nominations_path": self.nominations_path,
        }


def scan_replay_nominations(
    *,
    evidence_root: Path,
    annotation_path: Path,
) -> ReplayNominationScanResult:
    """Latest-batch landing check over public replay artifacts (read-only)."""

    try:
        files = sorted(evidence_root.glob("nominations-*.json"))
    except OSError:
        return ReplayNominationScanResult(disposition="SKIPPED", reason="evidence_root_unreadable")
    if not files:
        return ReplayNominationScanResult(disposition="SKIPPED", reason="no_nominations")

    parsed: list[tuple[str, Path, list[str]]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            batch = str(payload["batch"])
            proposals = [
                str(entry["unit_id"])
                for entry in payload["nominations"]
                if isinstance(entry, dict) and entry.get("unit_id")
            ]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return ReplayNominationScanResult(disposition="SKIPPED", reason="nominations_unparseable")
        parsed.append((batch, path, proposals))

    parsed.sort(key=lambda item: item[0])
    batch, path, unit_ids = parsed[-1]
    try:
        annotation_text = annotation_path.read_text(encoding="utf-8")
    except OSError:
        return ReplayNominationScanResult(disposition="SKIPPED", reason="annotation_unreadable")

    landed = (
        f"批次 {batch}" in annotation_text
        or all(unit_id in annotation_text for unit_id in unit_ids)
    )
    return ReplayNominationScanResult(
        disposition="SCANNED",
        batch=batch,
        proposals=len(unit_ids),
        landed=landed,
        nominations_path=str(path),
    )


@dataclass(frozen=True)
class NeedsReviewScanResult:
    """Count-only view of pending instrument-score records (no instrument data)."""

    disposition: str  # SCANNED | SKIPPED
    reason: str | None = None
    pending: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "reason": self.reason,
            "pending": self.pending,
            "total": self.total,
        }


def scan_needs_review(*, registry_path: Path) -> NeedsReviewScanResult:
    """Count ``needs_review`` score records via the canonical loader (read-only).

    registry 文件不存在 = 还没有任何评分记录（真相明确 = 无待决），SCANNED 且
    pending=0；解析层容错由 ``load_records`` 既有语义承担（坏行跳过）。
    """

    from fin_analyse.ingestion.instrument_scores import load_records

    if not Path(registry_path).exists():
        return NeedsReviewScanResult(disposition="SCANNED", pending=0, total=0)
    try:
        records = load_records(Path(registry_path))
    except OSError:
        return NeedsReviewScanResult(disposition="SKIPPED", reason="registry_unreadable")
    pending = sum(
        1 for row in records.values() if row.get("status") == "needs_review"
    )
    return NeedsReviewScanResult(
        disposition="SCANNED", pending=pending, total=len(records)
    )
