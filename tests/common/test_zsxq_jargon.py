"""ZSXQ 黑话词表 loader 单元测试（fixture 词表，不依赖生产 KB）。"""

from __future__ import annotations

import json
from pathlib import Path

from fin_analyse.common.zsxq_jargon import (
    expand_query_terms,
    jargon_hits,
    jargon_note_lines,
    jargon_notes,
    load_jargon_entries,
)


def _write_config(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "jargon.json"
    path.write_text(
        json.dumps(
            {"schema_version": "fin.zsxq-jargon/v1", "entries": entries},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _fixture_entries() -> list[dict[str, object]]:
    return [
        {
            "term": "科学家50",
            "meaning": "科创50",
            "kind": "指代",
            "confidence": "owner_confirmed",
            "evidence": ["zsxq-1"],
            "note": "",
        },
        {"term": "科学家", "meaning": "科创", "confidence": "corpus_inferred"},
        {"term": "河神", "meaning": "未定", "confidence": "speculative"},
        {"term": "柚子", "meaning": "游资", "confidence": "owner_confirmed"},
        {"term": "老登", "meaning": "旧质生产力", "confidence": "owner_confirmed"},
        # 畸形条目：缺 meaning / 重复 term / 未知档位
        {"term": "坏条目", "meaning": "", "confidence": "owner_confirmed"},
        {"term": "柚子", "meaning": "重复词目", "confidence": "owner_confirmed"},
        {"term": "怪档位", "meaning": "未知档位按推测", "confidence": "banana"},
    ]


def test_load_filters_speculative_and_malformed(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _fixture_entries())
    entries = load_jargon_entries(config_path=path)
    terms = [entry.term for entry in entries]
    assert terms == ["科学家50", "科学家", "柚子", "老登"]

    with_spec = load_jargon_entries(include_speculative=True, config_path=path)
    assert [entry.term for entry in with_spec] == [
        "科学家50",
        "科学家",
        "河神",
        "柚子",
        "老登",
        "怪档位",
    ]
    assert with_spec[-1].confidence == "speculative"


def test_load_missing_or_malformed_file_returns_empty(tmp_path: Path) -> None:
    assert load_jargon_entries(config_path=tmp_path / "absent.json") == ()

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_jargon_entries(config_path=bad) == ()

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"entries": "not-a-list"}), encoding="utf-8")
    assert load_jargon_entries(config_path=wrong) == ()


def test_hits_longest_match_suppresses_overlap(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _fixture_entries())
    entries = load_jargon_entries(config_path=path)

    hits = jargon_hits("科学家50聊的是大光", entries=entries)
    # 长词优先：科学家50 命中后，其中的"科学家"不再单独命中
    assert [hit["term"] for hit in hits] == ["科学家50"]
    assert hits[0]["start"] == 0
    assert hits[0]["end"] == 5

    # 短词独立出现时照常命中；多命中按位置排序
    hits = jargon_hits("老登和柚子", entries=entries)
    assert [(hit["term"], hit["start"]) for hit in hits] == [("老登", 0), ("柚子", 3)]

    # max_hits 有界
    assert [hit["term"] for hit in jargon_hits("老登和柚子", entries=entries, max_hits=1)] == [
        "老登"
    ]
    assert jargon_hits("", entries=entries) == []


def test_notes_narrow_contract_dedup(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _fixture_entries())
    entries = load_jargon_entries(config_path=path)

    notes = jargon_notes("科学家50崩了，科学家50再聊大光", entries=entries)
    assert notes == [{"term": "科学家50", "meaning": "科创50", "confidence": "owner_confirmed"}]
    assert set(notes[0]) == {"term", "meaning", "confidence"}

    notes = jargon_notes("老登和柚子和老登", entries=entries)
    assert [note["term"] for note in notes] == ["老登", "柚子"]


def test_note_lines_mark_corpus_inferred(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _fixture_entries())
    entries = load_jargon_entries(config_path=path)

    lines = jargon_note_lines("科学家聊大光", entries=entries)
    assert lines == ["- 科学家 = 科创（语料推测）"]

    lines = jargon_note_lines("老登", entries=entries)
    assert lines == ["- 老登 = 旧质生产力"]


def test_expand_query_terms_bidirectional_gated(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _fixture_entries())
    entries = load_jargon_entries(config_path=path)

    # 标准义 → 黑话
    assert expand_query_terms("科创50怎么看", entries=entries) == ["科学家50"]
    # 黑话 → 标准义
    assert expand_query_terms("科学家50崩了吗", entries=entries) == ["科创50"]
    # 反向串已在问句中：不重复加
    assert expand_query_terms("科学家50即科创50", entries=entries) == []
    # 短词（<3 字）不扩：避免多义词误召回
    assert expand_query_terms("柚子今天动了", entries=entries) == []
    # speculative 不参与召回
    assert expand_query_terms("河神到底是谁", entries=entries) == []
    # 无命中不扩展
    assert expand_query_terms("黄金周报", entries=entries) == []
    # 有界
    many = [
        {"term": f"长词甲{i}号", "meaning": f"标准义{i}", "confidence": "owner_confirmed"}
        for i in range(10)
    ]
    many_path = tmp_path / "jargon_many.json"
    many_path.write_text(json.dumps({"entries": many}, ensure_ascii=False), encoding="utf-8")
    entries_many = load_jargon_entries(config_path=many_path)
    question = " ".join(f"长词甲{i}号" for i in range(10))
    assert len(expand_query_terms(question, entries=entries_many)) == 6
