from fin_analyse.context.adapter import ExternalContextAdapter
from fin_analyse.context.models import ExternalContextRecord


def _record():
    return ExternalContextRecord(
        record_id="dragon_tiger:600519:2026-06-23",
        source="eastmoney_datacenter",
        category="event",
        ticker="600519",
        title="贵州茅台龙虎榜",
        summary="日涨幅偏离值达7%，机构净买入1000万",
        occurred_at="2026-06-23",
        url="https://data.eastmoney.com/stock/lhb.html",
        metadata={"reason": "日涨幅偏离值达7%"},
        raw={"SECURITY_CODE": "600519"},
    )


def test_to_documents_marks_external_context_source():
    adapter = ExternalContextAdapter()
    docs = adapter.to_documents([_record()])

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_id == "external-context"
    assert doc.external_id == "dragon_tiger:600519:2026-06-23"
    assert doc.title == "贵州茅台龙虎榜"
    assert "仅供学徒认知参考" in doc.content
    assert doc.metadata["category"] == "event"
    assert doc.metadata["is_decision_factor"] is False


def test_to_evidence_uses_external_context_type():
    adapter = ExternalContextAdapter()
    evidence = adapter.to_evidence([_record()])

    assert len(evidence) == 1
    item = evidence[0]
    assert item.source_id == "external-context"
    assert item.evidence_type == "external_context"
    assert item.document_id == "external-context:dragon_tiger:600519:2026-06-23"
    assert "机构净买入" in item.content
    assert item.metadata["source"] == "eastmoney_datacenter"
