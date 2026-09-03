"""ZSXQ article search for the thin-server CLI (read_article_search).

懒构建 KnowledgeStore + KnowledgeQueryService（首次调用建索引），按问题
返回相关文章元数据与摘要。用于“封装/封测/行业点评有哪些文章”这类全文
检索诉求。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fin_analyse.claims import RuleBasedClaimExtractor
from fin_analyse.common.zsxq_jargon import (
    expand_query_terms,
)
from fin_analyse.common.zsxq_jargon import (
    jargon_notes as scan_jargon_notes,
)
from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter
from fin_analyse.knowledge.article_reader import G_LAYER_COLUMNS
from fin_analyse.knowledge.query import KnowledgeQueryRequest, KnowledgeQueryService
from fin_analyse.knowledge.store import KnowledgeStore

#: 扩展词带来的尾部加槽上限（纯加法：直接命中一个不丢，最多多给 4 条）。
_MAX_EXPANDED_EXTRA_HITS = 4


class ArticleKeywordSearchReader:
    """read_article_search：基于 TF-IDF 的本地文章检索。"""

    def __init__(self, knowledge_base_root: Path, *, max_results: int = 8) -> None:
        self._kb_root = Path(knowledge_base_root)
        self._max_results = max_results
        self._store: KnowledgeStore | None = None
        self._service: KnowledgeQueryService | None = None

    def _ensure(self) -> KnowledgeQueryService:
        if self._service is None:
            adapter = ZsxqMarkdownAdapter(self._kb_root)
            store = KnowledgeStore.from_adapter(adapter, RuleBasedClaimExtractor())
            self._store = store
            self._service = KnowledgeQueryService(store)
        return self._service

    @staticmethod
    def _excerpt(text: str, token: str, width: int = 120) -> str:
        index = text.find(token)
        if index < 0:
            return text[:width]
        start = max(0, index - width // 2)
        return text[start : start + width].replace("\n", " ")

    @staticmethod
    def _content_excerpt(text: str, question: str, width: int = 120) -> str:
        """用问题里的中文/英文词定位正文片段（不引入 TextSearch 依赖）。"""
        segments = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", question)
        candidates: list[str] = []
        for segment in segments:
            if len(segment) >= 4 and re.search(r"[\u4e00-\u9fa5]", segment):
                candidates.extend(
                    dict.fromkeys(segment[index : index + 2] for index in range(len(segment) - 1))
                )
            elif len(segment) >= 2:
                candidates.append(segment)
        for token in candidates:
            if token in text:
                return ArticleKeywordSearchReader._excerpt(text, token, width)
        return text[:width].replace("\n", " ")

    def read(self, request: Any) -> Any:
        from fin_analyse.read_capabilities.types import ProductionReadResult

        if not (self._kb_root / "articles").exists():
            return ProductionReadResult(
                value={"status": "EMPTY", "hits": []},
                data_gaps=("article_search_index_unavailable",),
            )
        try:
            service = self._ensure()
        except (OSError, ValueError):
            return ProductionReadResult(
                value={"status": "EMPTY", "hits": []},
                data_gaps=("article_search_index_unavailable",),
            )
        # 查询双向扩展（黑话设计落点 4）：原问句查询不动（direct-substring
        # 命中路径不受影响）。扩展词各以单独查询跑再合并——实测（9/3）追加进
        # 同一查询会被标准词的海量直接命中稀释（「科创50 科学家50」仍进不了
        # top12），单独成查才能召回。新增文档尾部追加，纯加法，不挤掉直接命中。
        expanded_terms = expand_query_terms(request.question)
        limit = max(self._max_results, 8)
        result_cap = self._max_results + (_MAX_EXPANDED_EXTRA_HITS if expanded_terms else 0)
        merged_hits: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        for query in (request.question, *expanded_terms):
            result = service.query(KnowledgeQueryRequest(query=query, window="180d", limit=limit))
            for hit in result.hits:
                doc_id = str(hit.get("document_id", ""))
                if not doc_id or doc_id in seen_doc_ids:
                    continue
                seen_doc_ids.add(doc_id)
                merged_hits.append(hit)
        store = getattr(self, "_store", None)
        hits: list[dict[str, object]] = []
        for hit in merged_hits:
            doc_id = str(hit.get("document_id", ""))
            doc = store.get_document(doc_id) if store is not None else None
            if doc is None:
                continue
            metadata = doc.metadata
            tags = metadata.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            tags = [str(tag) for tag in (tags or []) if str(tag).strip()][:8]
            entry: dict[str, object] = {
                "article_id": str(metadata.get("id") or doc.document_id),
                "title": str(hit.get("title") or doc.title)[:160],
                "column": str(metadata.get("column", "")),
                "date": str(metadata.get("date", ""))[:10],
                "score": metadata.get("score"),
                "tags": tags,
                "source_classification": str(metadata.get("source_classification", "")),
                "char_count": len(doc.content),
                "excerpt": self._content_excerpt(doc.content, request.question),
            }
            # 黑话旁注（落点 3 的检索面）：只对 G 层栏目摘要附加，excerpt 零改写。
            if str(metadata.get("column", "")) in G_LAYER_COLUMNS:
                jargon_notes = scan_jargon_notes(doc.content, limit=4)
                if jargon_notes:
                    entry["jargon_notes"] = jargon_notes
            hits.append(entry)
            if len(hits) >= result_cap:
                break
        value: dict[str, object] = {
            "schema_version": "fin.article-search/v1",
            "source_boundary": "zsxq_articles_local",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": "READY",
            "query": request.question,
            "hits": hits,
        }
        if expanded_terms:
            value["expanded_terms"] = expanded_terms
        gaps = () if hits else ("article_search_no_match",)
        return ProductionReadResult(value=value, data_gaps=gaps)
