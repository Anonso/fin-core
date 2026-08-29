"""Source adapter protocol."""

from typing import Protocol

from .models import ParseArtifact, RawDocument, SourceInfo


class SourceAdapter(Protocol):
    @property
    def source_info(self) -> SourceInfo: ...

    def fetch(self, since: object | None = None) -> list[RawDocument]: ...

    def parse(self, document: RawDocument) -> list[ParseArtifact]: ...
