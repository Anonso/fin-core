"""Static guard: production code must not contain legacy Knowledge Query paths.

Checks that all upper-layer callers use KnowledgeQueryService,
not AnalysisService.search_articles() or direct TextSearch.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_production_callers_use_knowledge_query_service_not_legacy_paths():
    production_files = [
        path
        for path in (ROOT / "fin_analyse").rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    source_by_path = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in production_files
    }

    assert "def search_articles(" not in source_by_path["fin_analyse/gateway/services.py"]

    legacy_search_articles_callers = [
        path
        for path, source in source_by_path.items()
        if ".search_articles(" in source
    ]
    assert legacy_search_articles_callers == []

    allowed_text_search_files = {
        "fin_analyse/knowledge/search.py",
        "fin_analyse/knowledge/query.py",
        "fin_analyse/context/search.py",
    }
    direct_text_search_callers = [
        path
        for path, source in source_by_path.items()
        if path not in allowed_text_search_files
        and (
            "TextSearch(" in source
            or "from fin_analyse.knowledge.search import TextSearch" in source
            or "from .search import TextSearch" in source
        )
    ]
    assert direct_text_search_callers == []

    assert "from fin_analyse.knowledge.search import TextSearch" not in _read(
        "fin_analyse/gateway/mcp_server.py"
    )
    assert "from .search import TextSearch" not in _read("fin_analyse/knowledge/qa.py")
