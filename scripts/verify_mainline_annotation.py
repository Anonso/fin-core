"""机验主线标注草稿：摘录 span 逐字 ⊆ 原文 + 起草硬约束预检（部件2）。

设计门 g-mainline-growth-v1 部件2 的机器验证半边；owner 终审追加进标注文档
之前、之后都可跑。任何失败 exit 1（fail-closed：不入档、rebuild 不放行），
本脚本自身从不改写标注文档。

Checks:
  1. Whole-payload validation —— generate_cognition_mainline_readmodel 必须
     成功（覆盖 as_of ≥ 本批最大 published_at、节点前缀非降、来源表形状、
     PIT 字段；违反即整份拒绝）。
  2. Excerpt spans —— 每个单元的非空 ``g_original_quote`` /
     ``source_material_quote`` 必须逐字出现在其来源文章里（KB 根 +
     article_ref 解析）。先精确子串，失败再做空白折叠比对（段落换行差异）。
  3. Node coverage —— 每个 G_ORIGINAL 单元必须被至少一条 evolution 节点行
     覆盖（payload unit_refs，任意 change type），否则它永远不会出现在投影
     里（实弹验收会假失败）。混合/AI 辅助单元只提示不计失败——投影按
     source_nature 本就只投 G_ORIGINAL。

引句与文章正文绝不打印（用户数据不入日志），失败行只带 unit_id/字段/ref。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    CognitionMainlineReadModelReader,
    generate_cognition_mainline_readmodel,
)
from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

_ARTICLE_REF_PREFIX = "knowledge-base/"
_QUOTE_FIELDS = ("g_original_quote", "source_material_quote")


def _collapse(text: str) -> str:
    return "".join(text.split())


def _article_path(kb_root: Path, article_ref: str) -> Path | None:
    if not article_ref.startswith(_ARTICLE_REF_PREFIX):
        return None
    return kb_root / article_ref[len(_ARTICLE_REF_PREFIX) :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--annotation",
        type=Path,
        default=None,
        help="标注文档路径（默认 canonical KB 根 manual-annotations/g-cognition-mainline.md）",
    )
    parser.add_argument(
        "--kb-root",
        type=Path,
        default=None,
        help="知识库根（默认 knowledge_root 缝解析）",
    )
    args = parser.parse_args(argv)

    kb_root = args.kb_root if args.kb_root is not None else default_knowledge_base_root()
    annotation = (
        args.annotation
        if args.annotation is not None
        else kb_root / "manual-annotations" / "g-cognition-mainline.md"
    )

    state_root = Path(
        __import__("os").environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    readmodel_root = state_root / "fin-analyse" / "cognition-mainline-readmodel-v1"
    generation = 1
    readout = CognitionMainlineReadModelReader(readmodel_root).read()
    if readout.payload:
        generation = int(readout.generation) + 1

    # ── check 1: whole-payload validation（fail-closed 全量校验）──
    try:
        payload = generate_cognition_mainline_readmodel(
            annotation,
            generation=generation,
        )
    except Exception as exc:  # noqa: BLE001 - typed CLI boundary
        print(f"FAIL whole-payload-validation: {type(exc).__name__}: {exc}")
        return 1
    print(
        f"ok whole-payload-validation: units={len(payload['units'])} "
        f"generation={generation}"
    )

    failures = 0
    sources = {
        str(source.get("source_id")): source for source in payload.get("sources", [])
    }
    article_texts: dict[str, str | None] = {}

    # ── check 2: excerpt spans 逐字 ⊆ 原文 ──
    for unit in payload.get("units", []):
        source = sources.get(str(unit.get("source_ref")))
        if source is None:
            print(f"FAIL unit {unit.get('unit_id')}: source_ref unresolved")
            failures += 1
            continue
        article_ref = str(source.get("article_ref"))
        if article_ref not in article_texts:
            path = _article_path(kb_root, article_ref)
            try:
                article_texts[article_ref] = path.read_text(encoding="utf-8") if path else None
            except OSError:
                article_texts[article_ref] = None
        text = article_texts[article_ref]
        for field_name in _QUOTE_FIELDS:
            quote = unit.get(field_name)
            if not quote:
                continue
            if text is None:
                print(
                    f"FAIL unit {unit.get('unit_id')} {field_name}: "
                    f"source article unreadable ({article_ref})"
                )
                failures += 1
                continue
            quote_text = str(quote)
            if quote_text in text:
                print(f"ok unit {unit.get('unit_id')} {field_name}: exact")
            elif _collapse(quote_text) in _collapse(text):
                print(f"ok unit {unit.get('unit_id')} {field_name}: whitespace-normalized")
            else:
                print(
                    f"FAIL unit {unit.get('unit_id')} {field_name}: "
                    f"excerpt not found in source ({article_ref})"
                )
                failures += 1

    # ── check 3: node coverage（G_ORIGINAL 才硬失败）──
    covered = {
        str(unit_id)
        for node in payload.get("evolution", [])
        for unit_id in node.get("unit_refs", [])
    }
    for unit in payload.get("units", []):
        if str(unit.get("unit_id")) in covered:
            continue
        source = sources.get(str(unit.get("source_ref"))) or {}
        nature = str(source.get("source_nature"))
        if nature == "G_ORIGINAL":
            print(
                f"FAIL unit {unit.get('unit_id')}: G_ORIGINAL unit not covered by "
                "any evolution node row (would never project)"
            )
            failures += 1
        else:
            print(
                f"info unit {unit.get('unit_id')}: {nature} unit without node row "
                "(projection excludes it by nature; ok)"
            )

    if failures:
        print(f"RESULT: FAIL ({failures} finding(s))")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
