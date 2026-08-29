"""TF-IDF based semantic search over knowledge base."""

import math
import re
from collections import Counter

from .store import KnowledgeStore


class TextSearch:
    """Simple TF-IDF search over documents and evidence."""

    def __init__(self, store: KnowledgeStore):
        self.store = store
        self._doc_terms: dict[str, list[str]] = {}
        self._idf: dict[str, float] = {}
        self._build_index()

    def _tokenize(self, text: str) -> list[str]:
        """Simple Chinese + English tokenizer."""
        # Chinese: split by non-Chinese chars, keep 2-char+ terms
        tokens = []
        # Split into segments
        segments = re.split(r"[，。！？\s,\.!\?\n\[\]\(\)（）\"\"\'\'：:；;、0-9]+", text)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            # For Chinese text, use bigrams
            if re.search(r"[一-鿿]", seg):
                for i in range(len(seg) - 1):
                    tokens.append(seg[i : i + 2])
                if len(seg) >= 2:
                    tokens.append(seg)
            # For English/all-alphabetic, use words
            elif seg.isalpha() and len(seg) >= 3:
                tokens.append(seg.lower())

        return tokens

    def _build_index(self):
        doc_term_counts: dict[str, Counter] = {}
        doc_freq: Counter = Counter()

        for doc in self.store.documents:
            text = doc.title + " " + doc.content
            tokens = self._tokenize(text)
            unique = set(tokens)
            doc_term_counts[doc.document_id] = Counter(tokens)
            doc_freq.update(unique)

        total_docs = len(self.store.documents)
        self._idf = {}
        for term, df in doc_freq.items():
            self._idf[term] = math.log((total_docs + 1) / (df + 1)) + 1.0

        self._doc_terms = {did: list(tc.elements()) for did, tc in doc_term_counts.items()}

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        # TF-IDF scoring
        scores: dict[str, float] = {}
        for doc_id, doc_tokens in self._doc_terms.items():
            tf = Counter(doc_tokens)
            score = sum(tf.get(t, 0) * self._idf.get(t, 0.5) for t in query_tokens)
            if score > 0:
                scores[doc_id] = score

        # Rank by score descending; ties broken by date descending (newest first)
        doc_map = {d.document_id: d for d in self.store.documents}

        def _sort_key(item: tuple[str, float]) -> tuple[float, str]:
            doc_id, score = item
            doc = doc_map.get(doc_id)
            date = doc.metadata.get("date", "") if doc is not None else ""
            return (score, date)

        ranked = sorted(
            scores.items(),
            key=_sort_key,
            reverse=True,
        )[:top_k]
        return [
            {
                "document_id": doc_id,
                "score": round(score, 3),
                "title": doc_map[doc_id].title[:100] if doc_id in doc_map else "?",
                "date": doc_map[doc_id].metadata.get("date", "") if doc_id in doc_map else "",
            }
            for doc_id, score in ranked
        ]
