"""read_article：按 article_id 取一篇 ZSXQ 文章的有界全文与分级元数据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fin_analyse.utils.markdown import parse_frontmatter

_MAX_BODY_CHARS = 20_000
_G_LAYER_COLUMNS = frozenset(
    {
        "星大派锐评",
        "星大派每日热点",
        "星大派特刊",
        "星大派好问题",
        "凤仙郡小故事",
        "星大派人脉",
        "版本强势英雄",
    }
)


class ArticleContentReader:
    """read_article 实现：canonical index 定位 + 正文截断返回。"""

    def __init__(self, knowledge_base_root: Path, *, max_chars: int = _MAX_BODY_CHARS) -> None:
        self._kb_root = Path(knowledge_base_root)
        self._max_chars = max_chars

    def _index_map(self) -> dict[str, dict[str, Any]] | None:
        index_path = self._kb_root / "index.json"
        if not index_path.exists():
            return None
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        rows = payload.get("articles") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        return {str(row.get("id", "")): row for row in rows if isinstance(row, dict) and row.get("id")}

    def read(self, request: Any) -> Any:
        from fin_analyse.read_capabilities.types import ProductionReadResult

        article_id = (request.article_id or "").strip()
        if not article_id:
            return ProductionReadResult(
                value={"status": "EMPTY"},
                data_gaps=("article_id_required",),
            )
        index = self._index_map()
        row = index.get(article_id) if index is not None else None
        if row is None:
            return ProductionReadResult(
                value={"status": "EMPTY", "article_id": article_id},
                data_gaps=("article_not_found",),
            )
        path = Path(str(row.get("path", "")))
        if not path.exists():
            return ProductionReadResult(
                value={"status": "EMPTY", "article_id": article_id},
                data_gaps=("article_file_unavailable",),
            )
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ProductionReadResult(
                value={"status": "EMPTY", "article_id": article_id},
                data_gaps=("article_file_unavailable",),
            )
        meta, body = parse_frontmatter(raw)
        if len(body) > self._max_chars:
            truncated = True
            body = body[: self._max_chars] + "\n…（截断）"
        else:
            truncated = False
        value: dict[str, object] = {
            "schema_version": "fin.article-read/v1",
            "source_boundary": "zsxq_articles_local",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "article_id": article_id,
            "title": str(row.get("title", "")),
            "column": str(row.get("column", "") or meta.get("column", "")),
            "layer": (
                "g"
                if str(row.get("column", "") or meta.get("column", "")) in _G_LAYER_COLUMNS
                else "reference"
            ),
            "date": str(row.get("date", ""))[:10] or str(meta.get("date", ""))[:10],
            "score": row.get("score", meta.get("score")),
            "is_qa": bool(meta.get("is_qa", row.get("is_qa", False))),
            "source_classification": str(
                (meta.get("metadata") or {}).get("classification", "")
                or (row.get("metadata") or {}).get("classification", "")
                or ""
            ),
            "char_count": len(body),
            "truncated": truncated,
            "content": body,
        }
        return ProductionReadResult(value=value, data_gaps=())
