"""Publish a drafted G batch as the final-review manifest (MCP v1.4 local half).

起草会话机验通过后调用：把起草稿解析成
``$STATE/fin-analyse/adjudication-inbox-v1/g-batch-draft.v1.jsonl``（0600，
逐行一个单元 + 首行 ``_meta``），``g_draft_list`` 据此向 owner 飞书呈报，
owner ``g_draft_verdict`` 的裁决由 mainline_batch_merge 消费。manifest 是
唯一新增 durable 面；发布不改写任何知识库内容（纯读起草稿 + 0600 state）。
已合并批次（MERGED.json 在场）拒绝重发；不同批次 manifest 在场时拒绝
覆盖（owner 可能正在飞书里审），同批重发幂等。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    generate_cognition_mainline_readmodel,
)
from fin_analyse.guo_teacher_research.mainline_batch_merge import (
    DRAFT_DOC_NAME,
    MANIFEST_NAME,
    MERGED_MARKER_NAME,
    MainlineBatchMergeError,
    latest_draft_dir,
)
from fin_analyse.runtime.state_roots import (
    adjudication_inbox_state_root,
    ensure_private_state_directory,
)

_CST = ZoneInfo("Asia/Shanghai")
_MODE_EN_TO_ZH = {
    "current_observation": "当前观察",
    "historical_analysis": "历史分析",
    "structural_analysis": "结构分析",
    "forecast": "预测",
    "scenario": "情景",
    "object_mapping": "对象映射",
    "action_layer_not_cognition": "行动层",
}


def _topic_id_from_ref(article_ref: str) -> str:
    name = article_ref.rsplit("/", 1)[-1]
    return name.removesuffix(".md").split("zsxq-")[-1]


_UNIT_TITLE_RE = re.compile(r"^### (CU-\d{4}-[A-Z]?\d{2})：(.+)$")


def _unit_titles(draft_text: str) -> dict[str, str]:
    """单元标题只在 md 标题行（readmodel payload 不带 title），从起草稿取。"""

    titles: dict[str, str] = {}
    for line in draft_text.splitlines():
        match = _UNIT_TITLE_RE.match(line)
        if match is not None:
            titles[match.group(1)] = match.group(2).strip()
    return titles


def publish_manifest(*, draft_dir: Path, state_root: Path | None = None) -> dict[str, object]:
    """Parse the draft and atomically (re)write the final-review manifest."""

    if (draft_dir / MERGED_MARKER_NAME).exists():
        raise MainlineBatchMergeError(f"batch already merged: {draft_dir.name}")
    draft_path = draft_dir / DRAFT_DOC_NAME
    if not draft_path.exists():
        raise MainlineBatchMergeError(f"draft document missing: {draft_path}")
    payload = generate_cognition_mainline_readmodel(draft_path)
    sources = {
        str(source["source_id"]): str(source["article_ref"])
        for source in payload["sources"]
    }
    units = payload["units"]
    titles = _unit_titles(draft_path.read_text(encoding="utf-8"))

    inbox_root = ensure_private_state_directory(adjudication_inbox_state_root())
    manifest_path = inbox_root / MANIFEST_NAME
    batch = draft_dir.name
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            existing = json.loads(line)
            if existing.get("unit_id") == "_meta" and existing.get("batch") != batch:
                raise MainlineBatchMergeError(
                    f"manifest holds another batch ({existing.get('batch')}); "
                    "owner may be mid-review — resolve it first"
                )
            break

    lines: list[str] = [
        json.dumps(
            {
                "unit_id": "_meta",
                "batch": batch,
                "draft_dir": str(draft_dir),
                "draft_doc": str(draft_path),
                "units": len(units),
                "draft_as_of": str(payload["as_of"]),
                "published_at": datetime.now(_CST).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    ]
    for unit in units:
        article_ref = sources[str(unit["source_ref"])]
        title = titles.get(str(unit["unit_id"]), str(unit["unit_id"]))
        lines.append(
            json.dumps(
                {
                    "unit_id": unit["unit_id"],
                    "batch": batch,
                    "date": str(unit["published_at"])[:10],
                    "title": title,
                    "statement": title,
                    "excerpt": unit["g_original_quote"],
                    "cognition_mode": _MODE_EN_TO_ZH.get(
                        str(unit["cognition_mode"]), str(unit["cognition_mode"])
                    ),
                    "source_id": unit["source_ref"],
                    "topic_id": _topic_id_from_ref(article_ref),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    tmp_path = manifest_path.with_suffix(".jsonl.tmp")
    descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(tmp_path, manifest_path)
    return {
        "disposition": "PUBLISHED",
        "batch": batch,
        "units": len(units),
        "manifest": str(manifest_path),
        "draft_as_of": str(payload["as_of"]),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--draft-dir", type=Path, default=None, help="起草稿目录（默认 state 下最新 g-batch-draft-*）")
    parser.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args(argv)
    state_root = args.state_root or Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    draft_dir = args.draft_dir or latest_draft_dir(state_root)
    try:
        report = publish_manifest(draft_dir=draft_dir, state_root=state_root)
    except MainlineBatchMergeError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
