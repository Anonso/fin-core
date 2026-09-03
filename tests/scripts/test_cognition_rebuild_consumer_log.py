"""Cognition rebuild wrapper inside the ZSXQ consumer: isolated audit log."""

from __future__ import annotations

from pathlib import Path


def test_rebuild_audit_line_written_under_isolated_state_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The wrapper writes an owner-only audit line, never stdout."""

    from scripts.consume_zsxq_capture_folder import _rebuild_cognition_mainline

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # 缝一并 mock：default_knowledge_base_root 硬编码真实 HOME/共享根，
    # 不理会 XDG 覆盖——不 mock 则单测隐式依赖本机生产 KB 存在。
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.default_knowledge_base_root",
        lambda: tmp_path / "kb",
    )

    class _Result:
        def to_dict(self) -> dict[str, object]:
            return {
                "schema_version": "fin.cognition-mainline-rebuild/v1",
                "disposition": "PUBLISHED",
                "candidate_identity": "b" * 64,
                "generation": 2,
            }

    def fake_rebuild(**kwargs: object) -> _Result:
        return _Result()

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.rebuild_if_stale",
        fake_rebuild,
    )

    result = _rebuild_cognition_mainline()

    assert result["disposition"] == "PUBLISHED"
    log_path = tmp_path / "state" / "fin-analyse" / "cognition-mainline-rebuild.v1.jsonl"
    assert log_path.exists()
    assert '"disposition": "PUBLISHED"' in log_path.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""


def test_rebuild_wrapper_survives_audit_sink_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    """A missing audit sink never changes the rebuild result or raises."""

    from scripts.consume_zsxq_capture_folder import _rebuild_cognition_mainline

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.default_knowledge_base_root",
        lambda: tmp_path / "kb",
    )

    class _Result:
        def to_dict(self) -> dict[str, object]:
            return {
                "schema_version": "fin.cognition-mainline-rebuild/v1",
                "disposition": "FAILED",
                "reason": "annotation_invalid:ValueError",
            }

    def fake_rebuild(**kwargs: object) -> _Result:
        return _Result()

    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.rebuild_if_stale",
        fake_rebuild,
    )
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder._append_rebuild_audit",
        lambda payload, state_root: (_ for _ in ()).throw(OSError("sink down")),
    )

    result = _rebuild_cognition_mainline()

    assert result["disposition"] == "FAILED"
    assert result["reason"] == "annotation_invalid:ValueError"
