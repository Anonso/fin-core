from fin_analyse.ingestion.adapters import SourceAdapter
from fin_analyse.ingestion.models import ParseArtifact, RawDocument, SourceInfo


class FakeAdapter:
    @property
    def source_info(self):
        return SourceInfo(
            source_id="fake",
            name="Fake",
            source_type="test",
            reliability=1.0,
            freshness_policy="static",
        )

    def fetch(self, since=None):
        return [RawDocument(source_id="fake", external_id="1", title="T", content="C")]

    def parse(self, document):
        return [
            ParseArtifact(
                artifact_id="a1",
                source_id=document.source_id,
                document_id=document.document_id,
                artifact_type="text",
                content=document.content,
            )
        ]


def test_fake_adapter_satisfies_protocol():
    adapter: SourceAdapter = FakeAdapter()
    docs = adapter.fetch()
    artifacts = adapter.parse(docs[0])

    assert adapter.source_info.source_id == "fake"
    assert docs[0].document_id == "fake:1"
    assert artifacts[0].document_id == "fake:1"
