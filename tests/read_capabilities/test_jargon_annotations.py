"""黑话译注三落点集成测试：读侧旁注 / 查询双向扩展 / 工件与 prompt 字段。

fixture 测试走真实词表（config/zsxq_jargon.json，owner 维护）；生产 KB
探针在 KB 缺席的机器上单独跳过。不变量：原文零改写、无命中不附加、
speculative 不注入不召回。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService
from fin_analyse.cognition.thesis_extractor import _jargon_prompt_part
from fin_analyse.knowledge.article_reader import ArticleContentReader
from fin_analyse.knowledge.article_search import ArticleKeywordSearchReader
from fin_analyse.read_capabilities.types import ProductionReadRequest

_AS_OF = datetime(2026, 9, 3, tzinfo=UTC)

_REAL_KB = Path.home() / ".local" / "share" / "fin-analyse" / "shared" / "knowledge-base"
_PROBE_ARTICLE_ID = "zsxq-22258842281448251"  # 2026-09-03 锐评（科学家50/大光/2光）

_real_kb_required = pytest.mark.skipif(
    not _REAL_KB.is_dir(), reason="production knowledge base not present"
)


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    articles = root / "articles"
    articles.mkdir(parents=True)
    fixtures = [
        (
            "20260903_zsxq-j1.md",
            {
                "id": "zsxq-j1",
                "title": "锐评 fixture",
                "column": "星大派锐评",
                "date": "2026-09-03 09:51",
                "score": 8.8,
            },
            "# 锐评 fixture\n科学家50调整，大光继续拿，2光跌出性价比。\n",
        ),
        (
            "20260902_zsxq-j2.md",
            {
                "id": "zsxq-j2",
                "title": "普通栏 fixture",
                "column": "普通",
                "date": "2026-09-02 10:00",
                "score": 7.0,
            },
            "# 普通栏 fixture\n家人和柚子都在场。\n",
        ),
        (
            "20260901_zsxq-p.md",
            {
                "id": "zsxq-p",
                "title": "锐评无黑话 fixture",
                "column": "星大派锐评",
                "date": "2026-09-01 10:00",
                "score": 7.5,
            },
            "# 锐评无黑话 fixture\n今天没聊标的。\n",
        ),
    ]
    index_rows = []
    for filename, row, body in fixtures:
        path = articles / filename
        frontmatter = "\n".join(f"{key}: {value}" for key, value in row.items())
        path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
        index_rows.append({**row, "path": str(path)})
    (root / "index.json").write_text(json.dumps({"articles": index_rows}), encoding="utf-8")
    return root


def _read_article(root: Path, article_id: str):
    return ArticleContentReader(root).read(
        ProductionReadRequest(question="读文章", article_id=article_id, as_of=_AS_OF)
    )


def _search(root: Path, question: str):
    return ArticleKeywordSearchReader(root).read(
        ProductionReadRequest(question=question, as_of=_AS_OF)
    )


# ── read_article 读侧旁注 ────────────────────────────────────────────────


def test_read_article_g_layer_gets_jargon_notes(tmp_path: Path) -> None:
    result = _read_article(_kb(tmp_path), "zsxq-j1")
    assert result.data_gaps == ()
    value = result.value
    assert value["layer"] == "g"
    notes = {note["term"]: note for note in value["jargon_notes"]}
    assert notes["科学家50"] == {
        "term": "科学家50",
        "meaning": "科创50",
        "confidence": "owner_confirmed",
    }
    assert notes["大光"]["meaning"] == "光模块"
    assert notes["2光"]["meaning"] == "光纤"
    # 原文零改写：content 原样，译注只在附加字段
    assert "科学家50调整" in value["content"]
    assert "译注" not in value["content"]
    assert "科创50" not in value["content"]


def test_read_article_reference_layer_stays_clean(tmp_path: Path) -> None:
    # 普通栏含黑话词也不注（G 层门槛，避免「家人/英雄」误注）
    result = _read_article(_kb(tmp_path), "zsxq-j2")
    assert result.value["layer"] == "reference"
    assert "jargon_notes" not in result.value


def test_read_article_g_layer_no_hit_has_no_key(tmp_path: Path) -> None:
    result = _read_article(_kb(tmp_path), "zsxq-p")
    assert result.value["layer"] == "g"
    assert "jargon_notes" not in result.value


# ── read_article_search 查询双向扩展 + 摘要旁注 ─────────────────────────


def test_search_expansion_recalls_jargon_only_article(tmp_path: Path) -> None:
    # 原文只写"科学家50"，检索"科创50"经扩展命中
    result = _search(_kb(tmp_path), "科创50怎么看")
    assert result.data_gaps == ()
    assert result.value["expanded_terms"] == ["科学家50"]
    hit = next(h for h in result.value["hits"] if h["article_id"] == "zsxq-j1")
    # G 层栏目命中附摘要旁注；excerpt 零改写
    assert {n["term"] for n in hit["jargon_notes"]} >= {"科学家50", "大光", "2光"}
    assert "科创50" not in hit["excerpt"]


def test_search_non_g_hit_has_no_jargon_notes(tmp_path: Path) -> None:
    result = _search(_kb(tmp_path), "柚子和家人")
    hit = next(h for h in result.value["hits"] if h["article_id"] == "zsxq-j2")
    assert "jargon_notes" not in hit


# ── deep-read compact 工件字段（确定性，无 LLM） ────────────────────────


def _full_result(title: str, content: str) -> dict[str, object]:
    return {
        "source": {
            "title": title,
            "content": content,
            "column": "星大派锐评",
            "published_at": "2026-09-03 09:51",
            "source_rank": "t0_xingdapai",
        },
        "units": [],
        "theme_clusters": [],
        "suggestions": [],
    }


def test_compact_payload_carries_jargon_notes() -> None:
    payload = DeepReadArtifactService._build_compact_payload(
        _full_result("锐评", "科学家50调整，大光继续拿。"),
        article_id="zsxq-j1",
        generated_at="2026-09-03T10:00:00+00:00",
        generation_id="g1",
    )
    # 长词优先：科学家50 命中，其中的"科学家"不重复出现
    assert {
        "term": "科学家50",
        "meaning": "科创50",
        "confidence": "owner_confirmed",
    } in payload["jargon_notes"]
    assert all(note["term"] != "科学家" for note in payload["jargon_notes"])


def test_compact_payload_without_hits_has_no_key() -> None:
    payload = DeepReadArtifactService._build_compact_payload(
        _full_result("锐评", "今天没聊标的。"),
        article_id="zsxq-x",
        generated_at="2026-09-03T10:00:00+00:00",
        generation_id="g2",
    )
    assert "jargon_notes" not in payload


# ── 生成侧 prompt 注入 ───────────────────────────────────────────────────


def test_prompt_part_injects_verified_wording() -> None:
    part = _jargon_prompt_part("锐评", "科学家50调整。")
    assert part.startswith("# 本文命中黑话对照")
    assert "- 科学家50 = 科创50" in part
    assert "不得写入 evidence" in part
    # 无命中返回空串，prompt 与旧版逐字节一致
    assert _jargon_prompt_part("锐评", "今天没聊标的。") == ""


# ── 生产 KB 探针（交接验证清单） ─────────────────────────────────────────


@_real_kb_required
def test_real_wordlist_schema_and_evidence_resolve() -> None:
    payload = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "zsxq_jargon.json").read_text(
            encoding="utf-8"
        )
    )
    entries = payload["entries"]
    assert len({entry["term"] for entry in entries}) == len(entries)
    valid_confidences = {"owner_confirmed", "corpus_inferred", "speculative"}
    articles_dir = _REAL_KB / "articles"
    for entry in entries:
        assert entry["confidence"] in valid_confidences, entry["term"]
        assert str(entry["term"]).strip() and str(entry["meaning"]).strip()
        for evidence_id in entry.get("evidence", []):
            assert any(articles_dir.glob(f"*{evidence_id}.md")), (
                f"dead evidence link: {entry['term']} -> {evidence_id}"
            )


@_real_kb_required
def test_real_0903_probe_read_article_annotation() -> None:
    result = ArticleContentReader(_REAL_KB).read(
        ProductionReadRequest(question="9月3日锐评", article_id=_PROBE_ARTICLE_ID, as_of=_AS_OF)
    )
    assert result.data_gaps == ()
    value = result.value
    assert value["layer"] == "g"
    notes = {note["term"]: note for note in value.get("jargon_notes", [])}
    assert notes["科学家50"]["meaning"] == "科创50"
    assert notes["大光"]["meaning"] == "光模块"
    assert notes["2光"]["meaning"] == "光纤"
    # evidence/原文未被改写
    assert "科学家50" in value["content"]
    assert "科创50" not in value["content"]


@_real_kb_required
def test_real_0903_probe_search_kc50_hits() -> None:
    result = ArticleKeywordSearchReader(_REAL_KB).read(
        ProductionReadRequest(question="科创50", as_of=_AS_OF)
    )
    hit_ids = {hit["article_id"] for hit in result.value["hits"]}
    assert _PROBE_ARTICLE_ID in hit_ids
