from fin_analyse.ingestion.models import Evidence, RawDocument, SourceInfo


def test_raw_document_has_stable_id_from_source_and_external_id():
    doc = RawDocument(
        source_id="zsxq",
        external_id="2026-06-18_0855",
        title="测试文章",
        content="正文",
        url="https://example.com/a",
        metadata={"score": 8.8},
    )

    assert doc.document_id == "zsxq:2026-06-18_0855"


def test_evidence_references_source_and_document():
    evidence = Evidence(
        evidence_id="e1",
        source_id="zsxq",
        document_id="zsxq:doc1",
        evidence_type="text_chunk",
        content="华为海思获信创认证",
        metadata={"line_start": 12},
    )

    assert evidence.source_id == "zsxq"
    assert evidence.document_id == "zsxq:doc1"
    assert evidence.evidence_type == "text_chunk"


def test_source_info_declares_reliability_and_freshness_policy():
    source = SourceInfo(
        source_id="zsxq",
        name="知识星球",
        source_type="paid_community",
        reliability=0.75,
        freshness_policy="article_default",
    )

    assert source.source_id == "zsxq"
    assert source.reliability == 0.75
