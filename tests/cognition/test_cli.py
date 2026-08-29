"""CLI smoke tests for fin-cognition."""

from click.testing import CliRunner

from fin_analyse.cognition import cli
from fin_analyse.cognition.trace_verifier import TraceVerificationReport


class FakeVerifyService:
    def __init__(self, runtime_root=None, llm_helper=None):
        self.runtime_root = runtime_root
        self.llm_helper = llm_helper

    def verify_low_confidence_traces(
        self,
        *,
        threshold: float,
        limit: int,
        resume: bool,
        teacher_id: str | None,
    ) -> TraceVerificationReport:
        assert threshold == 0.5
        assert limit == 3
        assert resume is True
        assert teacher_id == "guo"
        return TraceVerificationReport(
            selected_count=3,
            verified_count=3,
            keep_count=1,
            revise_count=2,
            reject_count=0,
            skipped_count=0,
            error_count=0,
            verification_ids=["tv-1", "tv-2", "tv-3"],
        )


def test_verify_traces_cli(monkeypatch):
    monkeypatch.setattr(cli, "CognitiveService", FakeVerifyService)

    result = CliRunner().invoke(
        cli.main,
        ["verify-traces", "--threshold", "0.5", "--limit", "3", "--resume"],
    )

    assert result.exit_code == 0
    assert "selected=3 verified=3 keep=1 revise=2 reject=0 skipped=0 errors=0" in result.output
    assert "verification_ids: ['tv-1', 'tv-2', 'tv-3']" in result.output
