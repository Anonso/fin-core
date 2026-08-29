"""Tests for DeepReadArtifactService — article deep-read artifact lifecycle.

TDD: tests must FAIL before implementation exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from fin_analyse.cognition.llm import CognitionCompletionControl
from fin_analyse.common.execution_control import ExecutionFence

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def kb_root():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "articles").mkdir(parents=True, exist_ok=True)
    (tmp / "runtime" / "cognition").mkdir(parents=True, exist_ok=True)
    return tmp


def _write_article(kb_root: Path, article_id: str, content: str = "# Test\n\nContent.") -> Path:
    article_path = kb_root / "articles" / f"{article_id}.md"
    article_path.write_text(
        f"---\nid: {article_id}\ndate: 2026-07-01 10:00\ncolumn: 星大派特刊\n---\n\n{content}",
        encoding="utf-8",
    )
    return article_path


def _make_mock_result(to_dict_value: dict):
    """Build a mock that mimics ZsxqApprenticeResult for the artifact service."""
    m = Mock()
    m.warnings = to_dict_value.get("warnings", [])
    m.to_dict.return_value = to_dict_value
    return m


def _setup_apprentice_patch(mock_class, to_dict_value: dict):
    """Configure a patched ZsxqCognitionApprentice class."""
    mock_instance = Mock()
    mock_instance.deep_read.return_value = _make_mock_result(to_dict_value)
    mock_class.return_value = mock_instance
    return mock_instance


def _write_valid_pair(kb_root: Path, article_id: str, article_path: Path) -> tuple[Path, Path]:
    content_hash = hashlib.sha256(article_path.read_bytes()).hexdigest()
    generated_at = "2026-07-19T08:06:00+08:00"
    generation_id = "generation-safe-read"
    root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    full_path = root / "full" / f"{article_id}.json"
    compact_path = root / "compact" / f"{article_id}.json"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    full_path.parent.chmod(0o700)
    compact_path.parent.chmod(0o700)
    common = {
        "artifact_version": "deep_read_artifact_v1",
        "article_id": article_id,
        "content_hash": content_hash,
        "pipeline_version": "1.0.0",
        "generated_at": generated_at,
        "generation_id": generation_id,
    }
    full_path.write_text(
        json.dumps({**common, "detail": "full", "payload": {"source": {}, "units": []}}),
        encoding="utf-8",
    )
    compact_path.write_text(
        json.dumps(
            {
                **common,
                "detail": "compact",
                "payload": {"article_id": article_id, "mapping_facts": {}},
            }
        ),
        encoding="utf-8",
    )
    full_path.chmod(0o600)
    compact_path.chmod(0o600)
    return full_path, compact_path


# ── TDD 1: ensure_artifacts generates full and compact ──────────────────────


def test_ensure_artifacts_generates_full_and_compact(kb_root):
    """ensure_artifacts must generate both full.json and compact.json artifacts."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-001"
    article_path = _write_article(kb_root, article_id)

    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )

    assert result["article_id"] == article_id
    assert result["status"] == "generated"
    assert "full_path" in result
    assert "compact_path" in result
    assert "content_hash" in result
    assert "generated_at" in result

    # Verify files exist
    full_path = Path(result["full_path"])
    compact_path = Path(result["compact_path"])
    assert full_path.exists(), f"Full artifact not found at {full_path}"
    assert compact_path.exists(), f"Compact artifact not found at {compact_path}"
    assert stat.S_IMODE(full_path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(full_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(compact_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(full_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(compact_path.stat().st_mode) == 0o600

    # Verify full artifact structure
    full_data = json.loads(full_path.read_text(encoding="utf-8"))
    assert full_data["artifact_version"] == "deep_read_artifact_v1"
    assert full_data["article_id"] == article_id
    assert full_data["detail"] == "full"
    assert "payload" in full_data
    assert "units" in full_data["payload"]

    # Verify compact artifact structure
    compact_data = json.loads(compact_path.read_text(encoding="utf-8"))
    assert compact_data["artifact_version"] == "deep_read_artifact_v1"
    assert compact_data["article_id"] == article_id
    assert compact_data["detail"] == "compact"
    assert full_data["generation_id"]
    assert compact_data["generation_id"] == full_data["generation_id"]
    assert compact_data["generated_at"] == full_data["generated_at"]
    assert "payload" in compact_data
    # Compact must NOT have full evidence chains
    assert "core_theses" in compact_data["payload"]
    assert "injectable_summary" in compact_data["payload"]
    assert "usage_boundary" in compact_data["payload"]


def test_late_controlled_generation_does_not_publish_artifact_pair(kb_root):
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "late-controlled-generation"
    article_path = _write_article(kb_root, article_id)
    fence = ExecutionFence(datetime.now(UTC) + timedelta(minutes=1))
    control = CognitionCompletionControl(fence=fence, checkpoint=lambda: None)
    publication_checks: list[datetime] = []
    payload = {
        "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
        "units": [],
        "evidence_chains": [],
        "theme_clusters": [],
        "clocks": [],
        "suggestions": [],
        "warnings": [],
    }

    @contextmanager
    def closed_publication(_fence, *, at: datetime):
        publication_checks.append(at)
        yield False

    with (
        patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as apprentice,
        patch.object(ExecutionFence, "publication", closed_publication),
    ):
        _setup_apprentice_patch(apprentice, payload)
        result = DeepReadArtifactService(kb_root).ensure_artifacts(
            article_id,
            article_path,
            control=control,
        )

    artifact_root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    assert result["status"] == "error"
    assert len(publication_checks) == 1
    assert not (artifact_root / "full" / f"{article_id}.json").exists()
    assert not (artifact_root / "compact" / f"{article_id}.json").exists()


def test_mixed_generation_pair_is_not_fresh(kb_root):
    """Full and compact artifacts must belong to one completed generation."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-mixed-generation"
    article_path = _write_article(kb_root, article_id)
    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )
        generated = service.ensure_artifacts(article_id, article_path)

    compact_path = Path(generated["compact_path"])
    compact = json.loads(compact_path.read_text(encoding="utf-8"))
    compact["generation_id"] = "interrupted-new-generation"
    compact_path.write_text(json.dumps(compact), encoding="utf-8")

    assert service.is_fresh(article_id, article_path) is False
    assert service.load_fresh_pair(article_id, article_path) is None


def test_public_fresh_pair_binds_source_and_compact_bytes(kb_root: Path) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "bounded-public-pair"
    article_path = _write_article(kb_root, article_id)
    _, compact_path = _write_valid_pair(kb_root, article_id, article_path)

    pair = DeepReadArtifactService(kb_root=kb_root).load_fresh_pair(article_id, article_path)

    assert pair is not None
    assert pair.content_hash == hashlib.sha256(article_path.read_bytes()).hexdigest()
    assert pair.compact_raw_sha256 == hashlib.sha256(compact_path.read_bytes()).hexdigest()
    assert pair.article_modified_at


def test_public_fresh_pair_rejects_oversized_artifact_symlink_and_hardlink(
    kb_root: Path,
) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "bounded-public-reject"
    article_path = _write_article(kb_root, article_id)
    _, compact_path = _write_valid_pair(kb_root, article_id, article_path)
    service = DeepReadArtifactService(kb_root=kb_root)

    compact_path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert service.load_fresh_pair(article_id, article_path) is None

    _write_valid_pair(kb_root, article_id, article_path)
    symlink = kb_root / "articles" / "source-symlink.md"
    symlink.symlink_to(article_path)
    assert service.load_fresh_pair(article_id, symlink) is None

    hardlink = kb_root / "articles" / "source-hardlink.md"
    os.link(article_path, hardlink)
    assert service.load_fresh_pair(article_id, hardlink) is None


def test_public_freshness_and_ensure_fail_closed_for_unsafe_article(kb_root: Path) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    target = _write_article(kb_root, "unsafe-source-target")
    unsafe = kb_root / "articles" / "unsafe-source-link.md"
    unsafe.symlink_to(target)
    service = DeepReadArtifactService(kb_root=kb_root)

    assert service.is_fresh("unsafe-source-link", unsafe) is False
    result = service.ensure_artifacts("unsafe-source-link", unsafe)
    assert result["status"] == "error"
    assert result["data_gaps"] == ["article_file_invalid"]


def test_public_fresh_pair_fails_closed_for_pathologically_deep_artifact_json(
    kb_root: Path,
) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "deep-artifact-json"
    article_path = _write_article(kb_root, article_id)
    _, compact_path = _write_valid_pair(kb_root, article_id, article_path)
    envelope = json.loads(compact_path.read_text(encoding="utf-8"))
    envelope.pop("payload")
    deep_payload = "[" * 10_000 + "0" + "]" * 10_000
    compact_path.write_text(
        json.dumps(envelope, separators=(",", ":"))[:-1] + ',"payload":' + deep_payload + "}",
        encoding="utf-8",
    )

    service = DeepReadArtifactService(kb_root=kb_root)
    assert service.load_fresh_pair(article_id, article_path) is None
    assert service.load_compact(article_id) is None
    assert service.is_fresh(article_id, article_path) is False


def test_public_artifact_reads_fail_closed_for_artifact_symlink_escape(kb_root: Path) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "artifact-link-escape"
    article_path = _write_article(kb_root, article_id)
    _, compact_path = _write_valid_pair(kb_root, article_id, article_path)
    outside = kb_root / "outside-compact.json"
    outside.write_bytes(compact_path.read_bytes())
    compact_path.unlink()
    compact_path.symlink_to(outside)

    service = DeepReadArtifactService(kb_root=kb_root)
    assert service.load_fresh_pair(article_id, article_path) is None
    assert service.load_compact(article_id) is None
    assert service.is_fresh(article_id, article_path) is False


@pytest.mark.parametrize("linked_directory", ["root", "full", "compact"])
def test_public_artifact_owner_rejects_symlinked_store_root(
    kb_root: Path,
    linked_directory: str,
) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "artifact-store-root-link"
    article_path = _write_article(kb_root, article_id)
    external_root = kb_root / "external-store"
    (external_root / "articles").mkdir(parents=True)
    external_full, external_compact = _write_valid_pair(
        external_root,
        article_id,
        article_path,
    )
    configured_root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    external_artifacts = external_root / "runtime" / "cognition" / "deep_read_artifacts"
    if linked_directory == "root":
        configured_root.symlink_to(external_artifacts, target_is_directory=True)
    else:
        configured_root.mkdir(mode=0o700)
        configured_root.chmod(0o700)
        other_directory = "compact" if linked_directory == "full" else "full"
        (configured_root / other_directory).mkdir(mode=0o700)
        (configured_root / linked_directory).symlink_to(
            external_artifacts / linked_directory,
            target_is_directory=True,
        )
    before = (external_full.read_bytes(), external_compact.read_bytes())
    service = DeepReadArtifactService(kb_root=kb_root)

    assert service.load_full(article_id) is None
    assert service.load_compact(article_id) is None
    assert service.is_fresh(article_id, article_path) is False
    assert service.load_fresh_pair(article_id, article_path) is None
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as apprentice:
        result = service.ensure_artifacts(article_id, article_path, force=True)

    assert result["status"] == "error"
    assert result["data_gaps"] == ["deep_read_artifact_store_invalid"]
    apprentice.assert_not_called()
    assert (external_full.read_bytes(), external_compact.read_bytes()) == before


def test_public_artifact_reads_reject_non_owner_only_store_directory(kb_root: Path) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "artifact-store-open-mode"
    article_path = _write_article(kb_root, article_id)
    full_path, _ = _write_valid_pair(kb_root, article_id, article_path)
    full_path.parent.chmod(0o750)
    service = DeepReadArtifactService(kb_root)

    assert service.load_full(article_id) is None
    assert service.load_compact(article_id) is None
    assert service.is_fresh(article_id, article_path) is False
    assert service.load_fresh_pair(article_id, article_path) is None


@pytest.mark.parametrize("attack_location", ["target", "legacy_tmp"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_atomic_writer_rejects_prepositioned_links_without_touching_victim(
    kb_root: Path,
    attack_location: str,
    link_kind: str,
) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = f"atomic-{attack_location}-{link_kind}"
    article_path = _write_article(kb_root, article_id)
    full_path, _ = _write_valid_pair(kb_root, article_id, article_path)
    victim = kb_root / f"victim-{attack_location}-{link_kind}.txt"
    victim.write_text("victim must remain byte-identical", encoding="utf-8")
    victim.chmod(0o600)
    attack_path = full_path if attack_location == "target" else full_path.with_suffix(".json.tmp")
    if attack_location == "target":
        full_path.unlink()
    if link_kind == "symlink":
        attack_path.symlink_to(victim)
    else:
        os.link(victim, attack_path)
    before = (
        victim.read_bytes(),
        stat.S_IMODE(victim.stat().st_mode),
        victim.stat().st_nlink,
    )

    service = DeepReadArtifactService(kb_root=kb_root)
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as apprentice:
        result = service.ensure_artifacts(article_id, article_path, force=True)

    assert result["status"] == "error"
    assert result["data_gaps"] == ["deep_read_artifact_store_invalid"]
    apprentice.assert_not_called()
    assert (
        victim.read_bytes(),
        stat.S_IMODE(victim.stat().st_mode),
        victim.stat().st_nlink,
    ) == before


def test_atomic_writer_rejects_group_writable_target_without_chmod_or_rewrite(
    kb_root: Path,
) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "atomic-unsafe-mode"
    article_path = _write_article(kb_root, article_id)
    full_path, _ = _write_valid_pair(kb_root, article_id, article_path)
    full_path.chmod(0o620)
    before = (full_path.read_bytes(), stat.S_IMODE(full_path.stat().st_mode))

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as apprentice:
        result = DeepReadArtifactService(kb_root).ensure_artifacts(
            article_id,
            article_path,
            force=True,
        )

    assert result["status"] == "error"
    assert result["data_gaps"] == ["deep_read_artifact_store_invalid"]
    apprentice.assert_not_called()
    assert (full_path.read_bytes(), stat.S_IMODE(full_path.stat().st_mode)) == before


def test_atomic_writer_removes_owned_random_temp_after_write_failure(kb_root: Path) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "atomic-write-failure"
    article_path = _write_article(kb_root, article_id)
    service = DeepReadArtifactService(kb_root=kb_root)
    result_payload = {
        "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
        "units": [],
        "evidence_chains": [],
        "theme_clusters": [],
        "clocks": [],
        "suggestions": [],
        "warnings": [],
    }

    with (
        patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as apprentice,
        patch(
            "fin_analyse.cognition.deep_read_artifacts.os.write",
            side_effect=OSError("injected write failure"),
        ),
    ):
        _setup_apprentice_patch(apprentice, result_payload)
        result = service.ensure_artifacts(article_id, article_path, force=True)

    artifact_root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    assert result["status"] == "error"
    assert result["data_gaps"] == ["deep_read_artifact_store_invalid"]
    assert not tuple(artifact_root.rglob(".*.tmp"))


def test_standalone_loaders_reject_non_mapping_payloads(kb_root: Path) -> None:
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "non-mapping-payload"
    article_path = _write_article(kb_root, article_id)
    full_path, compact_path = _write_valid_pair(kb_root, article_id, article_path)
    for path in (full_path, compact_path):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"] = []
        path.write_text(json.dumps(envelope), encoding="utf-8")
        path.chmod(0o600)

    service = DeepReadArtifactService(kb_root=kb_root)
    assert service.load_full(article_id) is None
    assert service.load_compact(article_id) is None
    assert service.load_fresh_pair(article_id, article_path) is None


# ── TDD 2: cache hit reuses fresh artifact ──────────────────────────────────


def test_ensure_artifacts_reuses_fresh_cache_without_calling_apprentice(kb_root):
    """When a fresh artifact exists, ensure_artifacts must return cache_hit
    without calling ZsxqCognitionApprentice.deep_read."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-002"
    article_path = _write_article(kb_root, article_id)

    service = DeepReadArtifactService(kb_root=kb_root)

    # First call: should generate
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result1 = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )
        assert result1["status"] == "generated"
        assert mock_class.called

    # Second call: should be cache_hit, NOT call apprentice
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class2:
        result2 = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )
        assert result2["status"] == "cache_hit"
        assert not mock_class2.called


def test_backend_unavailable_artifact_is_retried_instead_of_cached(kb_root):
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-backend-retry"
    article_path = _write_article(kb_root, article_id)
    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as first:
        _setup_apprentice_patch(
            first,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": ["LLM backend unavailable for deep_read"],
            },
        )
        first_status = service.ensure_artifacts(article_id, article_path)
        assert first_status["status"] == "retryable"
        assert first_status.get("warnings") == ["LLM backend unavailable for deep_read"]
        assert service.is_fresh(article_id, article_path) is False
        assert service.load_fresh_pair(article_id, article_path) is None

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as second:
        _setup_apprentice_patch(
            second,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [{"unit_id": "u1", "title": "Recovered unit"}],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result = service.ensure_artifacts(article_id, article_path)

    assert result["status"] == "generated"
    assert second.called


@pytest.mark.parametrize(
    "warning",
    [
        "LLM extraction failed: None",
        "LLM extraction error: malformed response",
    ],
)
def test_extraction_failure_artifact_is_retried_instead_of_cached(kb_root, warning):
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = f"test-art-extraction-failure-{warning.split(':', 1)[0][-5:]}"
    article_path = _write_article(kb_root, article_id)
    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as first:
        _setup_apprentice_patch(
            first,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [warning],
            },
        )
        first_status = service.ensure_artifacts(article_id, article_path)
        assert first_status["status"] == "retryable"
        assert first_status.get("warnings") == [warning]
        assert service.is_fresh(article_id, article_path) is False
        assert service.load_fresh_pair(article_id, article_path) is None

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as second:
        _setup_apprentice_patch(
            second,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [{"unit_id": "u1", "title": "Recovered unit"}],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result = service.ensure_artifacts(article_id, article_path)

    assert result["status"] == "generated"
    assert second.called


# ── TDD 3: stale artifact regenerates ───────────────────────────────────────


def test_stale_artifact_regenerates_when_content_hash_changes(kb_root):
    """When article content changes (content_hash mismatch), artifact must be regenerated."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-003"
    article_path = _write_article(kb_root, article_id, "Original content.")

    service = DeepReadArtifactService(kb_root=kb_root)

    # First call: generate
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result1 = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )
        assert result1["status"] == "generated"
        original_hash = result1["content_hash"]

    # Modify article content
    article_path.write_text(
        f"---\nid: {article_id}\ndate: 2026-07-01 10:00\ncolumn: 星大派特刊\n---\n\n# Modified\n\nNew content here.",
        encoding="utf-8",
    )

    # Second call with changed content: should regenerate
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class2:
        _setup_apprentice_patch(
            mock_class2,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [{"unit_id": "u1", "title": "new unit"}],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result2 = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )
        assert result2["status"] == "generated"
        assert result2["content_hash"] != original_hash


# ── TDD 4: load_compact returns compact payload ─────────────────────────────


def test_load_compact_returns_compact_payload(kb_root):
    """load_compact must return the compact payload with required fields."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-004"
    article_path = _write_article(kb_root, article_id)

    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {
                    "article_id": article_id,
                    "title": "Test Title",
                    "column": "星大派特刊",
                    "published_at": "2026-07-01",
                    "source_rank": "S",
                },
                "units": [
                    {
                        "unit_id": "u1",
                        "title": "Test Unit",
                        "thesis": "Test thesis",
                        "related_companies": ["华为"],
                        "related_topics": ["半导体"],
                        "confidence": 0.85,
                    }
                ],
                "evidence_chains": [],
                "theme_clusters": [
                    {
                        "cluster_id": "c1",
                        "name": "半导体",
                        "active_status": "active",
                        "unit_ids": ["u1"],
                        "core_theses": ["Test thesis"],
                    }
                ],
                "clocks": [],
                "suggestions": [
                    {
                        "suggestion_id": "s1",
                        "suggestion_level": "high",
                        "summary": "关注华为",
                        "tracking_indicators": ["revenue"],
                        "risk_boundaries": ["max 5%"],
                        "allowed_usage": ["research"],
                        "forbidden_usage": ["auto_trade"],
                        "confidence": 0.8,
                    }
                ],
                "warnings": [],
            },
        )

        service.ensure_artifacts(article_id=article_id, article_path=article_path, force=False)

    compact = service.load_compact(article_id)
    assert compact is not None
    assert compact["article_id"] == article_id
    assert compact["title"] == "Test Title"
    assert "core_theses" in compact
    assert "theme_clusters" in compact
    assert "suggestions" in compact
    assert "injectable_summary" in compact
    assert "usage_boundary" in compact
    # Compact must NOT have full evidence_chains
    assert "evidence_chains" not in compact


# ── TDD 5: load_full returns full payload ───────────────────────────────────


def test_load_full_returns_full_payload(kb_root):
    """load_full must return the full artifact payload."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-005"
    article_path = _write_article(kb_root, article_id)

    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [{"unit_id": "u1"}],
                "evidence_chains": [{"chain_id": "c1"}],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        service.ensure_artifacts(article_id=article_id, article_path=article_path, force=False)

    full = service.load_full(article_id)
    assert full is not None
    assert "units" in full
    assert "evidence_chains" in full
    assert "theme_clusters" in full
    assert "clocks" in full
    assert "suggestions" in full
    assert full["units"][0]["unit_id"] == "u1"


# ── TDD 6: missing article returns error status ─────────────────────────────


def test_ensure_artifacts_missing_article_returns_error(kb_root):
    """When article file doesn't exist, ensure_artifacts returns status=error."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    service = DeepReadArtifactService(kb_root=kb_root)
    result = service.ensure_artifacts(
        article_id="nonexistent",
        article_path=kb_root / "articles" / "nonexistent.md",
        force=False,
    )

    assert result["article_id"] == "nonexistent"
    assert result["status"] == "error"
    assert "warnings" in result


# ── TDD 7: force=True always regenerates ────────────────────────────────────


def test_ensure_artifacts_force_regenerates(kb_root):
    """force=True must always regenerate, even when fresh artifact exists."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-007"
    article_path = _write_article(kb_root, article_id)

    service = DeepReadArtifactService(kb_root=kb_root)

    # First call: generate
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )
        result1 = service.ensure_artifacts(
            article_id=article_id, article_path=article_path, force=False
        )
        assert result1["status"] == "generated"

    # Second call with force=True: should regenerate
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class2:
        _setup_apprentice_patch(
            mock_class2,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [{"unit_id": "u2"}],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )
        result2 = service.ensure_artifacts(
            article_id=article_id, article_path=article_path, force=True
        )
        assert result2["status"] == "generated"
        assert mock_class2.called


# ── TDD 8: atomic write — no partial files ──────────────────────────────────


def test_atomic_write_no_tmp_files_left(kb_root):
    """After ensure_artifacts succeeds, no .tmp files should remain."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-008"
    article_path = _write_article(kb_root, article_id)

    service = DeepReadArtifactService(kb_root=kb_root)

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )
        service.ensure_artifacts(article_id=article_id, article_path=article_path)

    artifacts_dir = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    tmp_files = list(artifacts_dir.glob("**/*.tmp"))
    assert len(tmp_files) == 0, f"Found leftover .tmp files: {tmp_files}"


# ── TDD 9: unsafe article_id path traversal protection ─────────────────────


def test_unsafe_article_id_does_not_write_outside_artifact_root(kb_root):
    """Unsafe article_id (../, absolute path) must not write outside artifact root."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    artifacts_root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"

    # Verify safe key mapping for traversal attempts
    traversal_ids = [
        "../../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows",
        "",
        ".",
        "..",
    ]

    for raw_id in traversal_ids:
        safe_key = DeepReadArtifactService._safe_artifact_key(raw_id)
        # Safe key must not contain "/" or "\\" or be "." or ".."
        assert "/" not in safe_key, f"Unsafe id {raw_id!r} produced key with slash: {safe_key!r}"
        assert "\\" not in safe_key, (
            f"Unsafe id {raw_id!r} produced key with backslash: {safe_key!r}"
        )
        assert safe_key not in (".", ".."), (
            f"Unsafe id {raw_id!r} produced reserved key: {safe_key!r}"
        )

    # Verify _resolve_in_dir rejects escape attempts
    with pytest.raises(ValueError, match="escape"):
        DeepReadArtifactService._resolve_in_dir(
            artifacts_root / "full",
            "../../../etc/passwd.json",
        )


def test_unsafe_article_id_artifact_stays_in_root(kb_root):
    """Artifacts for unsafe article_id are still written inside artifact root."""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "../../../etc/passwd"
    article_path = _write_article(kb_root, "safe-art-010", "Test content.")

    service = DeepReadArtifactService(kb_root=kb_root)
    safe_key = service._safe_artifact_key(article_id)
    assert safe_key.startswith("unsafe_"), f"Expected unsafe_ prefix, got {safe_key!r}"

    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )

    assert result["status"] == "generated"
    full_path = Path(result["full_path"])
    compact_path = Path(result["compact_path"])
    artifacts_root = (kb_root / "runtime" / "cognition" / "deep_read_artifacts").resolve()
    assert full_path.resolve().is_relative_to(artifacts_root), (
        f"Full artifact escaped root: {full_path}"
    )
    assert compact_path.resolve().is_relative_to(artifacts_root), (
        f"Compact artifact escaped root: {compact_path}"
    )

    # Verify raw article_id is preserved in envelope (not the safe key)
    full_data = json.loads(full_path.read_text(encoding="utf-8"))
    assert full_data["article_id"] == article_id


# ── TDD 10: compact artifact version/pipeline/detail mismatch → stale ──────


def test_compact_version_mismatch_causes_regenerate(kb_root):
    """When compact artifact has mismatched artifact_version or pipeline_version
    or detail, both artifacts must be regenerated."""
    from datetime import UTC, datetime

    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    article_id = "test-art-011"
    article_path = _write_article(kb_root, article_id, "Compact version test content.")

    service = DeepReadArtifactService(kb_root=kb_root)
    safe_key = service._safe_artifact_key(article_id)

    # Pre-create a full artifact with correct versions...
    content_hash = service._compute_content_hash(article_path)
    artifacts_full_dir = kb_root / "runtime" / "cognition" / "deep_read_artifacts" / "full"
    artifacts_compact_dir = kb_root / "runtime" / "cognition" / "deep_read_artifacts" / "compact"
    artifacts_full_dir.mkdir(parents=True)
    artifacts_compact_dir.mkdir(parents=True)
    artifacts_full_dir.parent.chmod(0o700)
    artifacts_full_dir.chmod(0o700)
    artifacts_compact_dir.chmod(0o700)

    full_envelope = {
        "artifact_version": "deep_read_artifact_v1",
        "article_id": article_id,
        "content_hash": content_hash,
        "pipeline_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "detail": "full",
        "payload": {"units": [], "source": {}},
    }
    full_path = artifacts_full_dir / f"{safe_key}.json"
    full_path.write_text(json.dumps(full_envelope, ensure_ascii=False), encoding="utf-8")
    full_path.chmod(0o600)

    # ...but compact has WRONG pipeline_version
    compact_envelope = {
        "artifact_version": "deep_read_artifact_v1",
        "article_id": article_id,
        "content_hash": content_hash,
        "pipeline_version": "0.9.0",  # <-- mismatch
        "generated_at": datetime.now(UTC).isoformat(),
        "detail": "compact",
        "payload": {"article_id": article_id, "core_theses": []},
    }
    compact_path = artifacts_compact_dir / f"{safe_key}.json"
    compact_path.write_text(json.dumps(compact_envelope, ensure_ascii=False), encoding="utf-8")
    compact_path.chmod(0o600)

    # Freshness should be false → ensure_artifacts regenerates
    with patch("fin_analyse.cognition.zsxq_apprentice.ZsxqCognitionApprentice") as mock_class:
        _setup_apprentice_patch(
            mock_class,
            {
                "source": {"article_id": article_id, "title": "Test", "column": "星大派特刊"},
                "units": [],
                "evidence_chains": [],
                "theme_clusters": [],
                "clocks": [],
                "suggestions": [],
                "warnings": [],
            },
        )

        result = service.ensure_artifacts(
            article_id=article_id,
            article_path=article_path,
            force=False,
        )

    assert result["status"] == "generated", (
        f"Expected regeneration on compact version mismatch, got {result['status']}"
    )
    assert mock_class.called, "Apprentice must be called for regeneration"


# ── G 方法论层验收 1: compact 保留 methodology_rules 输入合同 ────────────────


def _methodology_full_result(article_id: str) -> dict:
    """生产形态 full_result:含 methodology_rule 与普通论点单元。"""
    return {
        "source": {
            "article_id": article_id,
            "title": "凤仙郡小故事:卡口与供需",
            "column": "凤仙郡小故事",
            "published_at": "2026-08-10T10:00:00+08:00",
            "source_rank": "T0",
        },
        "units": [
            {
                "unit_id": "u-method-1",
                "source_id": "zsxq-82255442558588260",
                "teacher_id": "guo",
                "unit_type": "methodology_rule",
                "title": "卡口分析",
                "thesis": "先识别卡口环节,再看供需缺口是否传导到价格",
                "original_evidence": ["老师原话:卡口是……"],
                "apprentice_interpretation": "推演:该规则可迁移到其他板块",
                "confidence": 0.8,
                "related_companies": [],
                "related_topics": ["半导体", "卡口"],
                "theme_cluster_ids": [],
                "usage_policy": {},
                "created_at": "2026-08-10T10:05:00+08:00",
                "metadata": {"extractor": "llm"},
            },
            {
                "unit_id": "u-thesis-1",
                "source_id": "zsxq-82255442558588260",
                "teacher_id": "guo",
                "unit_type": "strategic_thesis",
                "title": "硬科技更强",
                "thesis": "半导体底部修复中",
                "original_evidence": ["原文……"],
                "apprentice_interpretation": "",
                "confidence": 0.7,
                "related_companies": [],
                "related_topics": ["半导体"],
                "theme_cluster_ids": [],
                "usage_policy": {},
                "created_at": "2026-08-10T10:05:00+08:00",
                "metadata": {"extractor": "rule"},
            },
        ],
        "theme_clusters": [],
        "suggestions": [],
        "warnings": [],
    }


def test_build_compact_payload_keeps_methodology_rules_contract():
    """methodology_rule 单元进入 compact.methodology_rules 且合同字段齐全。"""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    payload = DeepReadArtifactService._build_compact_payload(
        _methodology_full_result("art-m1"),
        "art-m1",
        "2026-08-12T10:00:00+00:00",
        generation_id="gen-m1",
    )
    assert payload["generation_id"] == "gen-m1"
    rules = payload["methodology_rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["title"] == "卡口分析"
    assert rule["rule"] == "先识别卡口环节,再看供需缺口是否传导到价格"
    assert rule["teacher_quote"] == "老师原话:卡口是……"
    assert rule["apprentice_interpretation"] == "推演:该规则可迁移到其他板块"
    assert rule["related_topics"] == ["半导体", "卡口"]
    assert rule["confidence"] == 0.8
    assert rule["source_id"] == "zsxq-82255442558588260"
    assert rule["article_id"] == "art-m1"
    assert rule["published_at"] == "2026-08-10T10:00:00+08:00"
    assert rule["generation_id"] == "gen-m1"
    # 既有字段语义不变:unit_count 与 core_theses 都计入 methodology_rule
    assert payload["unit_count"] == 2
    assert len(payload["core_theses"]) == 2


def test_build_compact_payload_methodology_rules_absent_and_empty():
    """无 methodology_rule 单元 → 空数组;generation_id 透传;不 panic。"""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    full = _methodology_full_result("art-m2")
    full["units"] = [full["units"][1]]  # 只留 strategic_thesis
    payload = DeepReadArtifactService._build_compact_payload(
        full, "art-m2", "2026-08-12T10:00:00+00:00", generation_id="gen-m2"
    )
    assert payload["methodology_rules"] == []
    assert payload["generation_id"] == "gen-m2"
    assert payload["unit_count"] == 1

    empty = {"source": full["source"], "units": [], "theme_clusters": [], "suggestions": []}
    payload2 = DeepReadArtifactService._build_compact_payload(
        empty, "art-m3", "2026-08-12T10:00:00+00:00", generation_id="gen-m3"
    )
    assert payload2["methodology_rules"] == []
    assert payload2["unit_count"] == 0


def test_build_compact_payload_skips_malformed_methodology_rules():
    """畸形 methodology_rule 单元(缺 thesis)逐条跳过,不影响其余。"""
    from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

    full = _methodology_full_result("art-m4")
    full["units"].append(
        {
            "unit_id": "u-method-bad",
            "source_id": "zsxq-1",
            "teacher_id": "guo",
            "unit_type": "methodology_rule",
            "title": "",
            "thesis": "",
            "original_evidence": [],
            "apprentice_interpretation": "",
            "confidence": 0.5,
            "related_companies": [],
            "related_topics": [],
            "theme_cluster_ids": [],
            "usage_policy": {},
            "created_at": "",
            "metadata": {},
        }
    )
    payload = DeepReadArtifactService._build_compact_payload(
        full, "art-m4", "2026-08-12T10:00:00+00:00", generation_id="gen-m4"
    )
    rules = payload["methodology_rules"]
    assert len(rules) == 1  # 只保留合法条目
    assert rules[0]["title"] == "卡口分析"
