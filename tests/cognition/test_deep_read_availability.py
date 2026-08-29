"""Tests for DeepReadAvailabilityService — read-only deep-read artifact observability.

TDD: tests must FAIL before implementation exists.

The service classifies each article's deep-read artifact state so cache-miss
paths (artifact missing / stale / corrupt / unreadable) are observable instead
of masquerading as success (decision map: 后台生成/新鲜度/可用率决定实际质量 —
需建立 artifact availability 与质量观测).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

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
    _write_index_entry(kb_root, article_id, article_path.name)
    return article_path


def _write_index_entry(kb_root: Path, article_id: str, file_name: str) -> None:
    """Append one canonical index entry for an article file."""
    index_path = kb_root / "index.json"
    data = {"articles": [], "updated": "2026-07-19T08:06:00+08:00", "total": 0}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("articles"), list):
                data = loaded
        except (OSError, ValueError):
            pass
    articles = [
        entry
        for entry in data["articles"]
        if not (isinstance(entry, dict) and entry.get("id") == article_id)
    ]
    articles.append({"id": article_id, "file": file_name})
    data["articles"] = articles
    data["total"] = len(articles)
    index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_artifact_pair(
    kb_root: Path,
    article_id: str,
    article_path: Path,
    *,
    content_hash: str | None = None,
) -> None:
    """Write a valid full/compact artifact pair bound to the article content."""
    root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    full_path = root / "full" / f"{article_id}.json"
    compact_path = root / "compact" / f"{article_id}.json"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    full_path.parent.chmod(0o700)
    compact_path.parent.chmod(0o700)
    effective_hash = content_hash or hashlib.sha256(article_path.read_bytes()).hexdigest()
    generated_at = "2026-07-19T08:06:00+08:00"
    common = {
        "artifact_version": "deep_read_artifact_v1",
        "article_id": article_id,
        "content_hash": effective_hash,
        "pipeline_version": "1.0.0",
        "generated_at": generated_at,
        "generation_id": "generation-availability",
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


# ── Tests ───────────────────────────────────────────────────────────────────


def test_classifies_ready_pair(kb_root):
    """An article with a fresh full/compact pair is READY."""
    article_id = "ready-article"
    article_path = _write_article(kb_root, article_id)
    _write_artifact_pair(kb_root, article_id, article_path)

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=[article_id])

    assert report.total == 1
    assert report.by_article[article_id].status == "READY"
    assert report.ready == 1
    assert report.missing == 0
    assert report.stale == 0
    assert report.corrupt == 0
    assert report.unreadable == 0
    assert report.availability_rate == 1.0


def test_classifies_ready_pair_with_date_prefixed_file(kb_root):
    """Production files are YYYYMMDD_<id>.md and resolve via the index 'file'."""
    article_id = "zsxq-22258825284858211"
    article_path = kb_root / "articles" / f"20260825_{article_id}.md"
    article_path.write_text(
        f"---\nid: {article_id}\ndate: 2026-08-25 14:17\ncolumn: 星大派锐评\n---\n\nContent.",
        encoding="utf-8",
    )
    _write_index_entry(kb_root, article_id, article_path.name)
    _write_artifact_pair(kb_root, article_id, article_path)

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "READY"
    assert report.ready == 1


def test_classifies_missing_artifact(kb_root):
    """An article with no artifact files is MISSING_ARTIFACT."""
    article_id = "missing-article"
    _write_article(kb_root, article_id)

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=[article_id])

    assert report.by_article[article_id].status == "MISSING_ARTIFACT"
    assert report.ready == 0
    assert report.missing == 1
    assert report.availability_rate == 0.0


def test_classifies_stale_pair_when_article_changed(kb_root):
    """An artifact pair bound to old content is STALE after the article is edited."""
    article_id = "stale-article"
    article_path = _write_article(kb_root, article_id)
    _write_artifact_pair(kb_root, article_id, article_path)
    # Article changes after artifact generation → content_hash no longer matches.
    _write_article(kb_root, article_id, content="# Changed\n\nNew content.")

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=[article_id])

    assert report.by_article[article_id].status == "STALE"
    assert report.stale == 1
    assert report.ready == 0


def test_classifies_corrupt_artifact(kb_root):
    """A full artifact that fails strict JSON parsing is CORRUPT."""
    article_id = "corrupt-article"
    _write_article(kb_root, article_id)
    root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    (root / "full").mkdir(parents=True, exist_ok=True)
    (root / "compact").mkdir(parents=True, exist_ok=True)
    (root / "full" / f"{article_id}.json").write_text("{not-json", encoding="utf-8")
    (root / "compact" / f"{article_id}.json").write_text("{also-not-json", encoding="utf-8")

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=[article_id])

    assert report.by_article[article_id].status == "CORRUPT"
    assert report.corrupt == 1


def test_classifies_corrupt_when_hash_matches_but_payload_invalid(kb_root):
    """Hash-matching but structurally invalid payloads are CORRUPT, not STALE."""
    article_id = "corrupt-payload"
    article_path = _write_article(kb_root, article_id)
    content_hash = hashlib.sha256(article_path.read_bytes()).hexdigest()
    root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    (root / "full").mkdir(parents=True, exist_ok=True)
    (root / "compact").mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    (root / "full").chmod(0o700)
    (root / "compact").chmod(0o700)
    common = {
        "artifact_version": "deep_read_artifact_v1",
        "article_id": article_id,
        "content_hash": content_hash,
        "pipeline_version": "1.0.0",
        "generated_at": "2026-07-19T08:06:00+08:00",
        "generation_id": "generation-corrupt-payload",
    }
    full_path = root / "full" / f"{article_id}.json"
    compact_path = root / "compact" / f"{article_id}.json"
    # payload is a list, not a dict → envelope validation fails despite hash match.
    full_path.write_text(json.dumps({**common, "detail": "full", "payload": []}))
    compact_path.write_text(json.dumps({**common, "detail": "compact", "payload": {}}))
    full_path.chmod(0o600)
    compact_path.chmod(0o600)

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=[article_id])

    assert report.by_article[article_id].status == "CORRUPT"
    assert report.corrupt == 1


def test_classifies_unknown_article(kb_root):
    """An article id that does not resolve to a file is UNKNOWN, not crash."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=["no-such-article"])

    assert report.by_article["no-such-article"].status == "UNKNOWN"
    assert report.unknown == 1


def test_mixed_report_aggregates_counts(kb_root):
    """Multiple articles aggregate into one availability report."""
    ready_id = "ready-mixed"
    ready_path = _write_article(kb_root, ready_id)
    _write_artifact_pair(kb_root, ready_id, ready_path)
    missing_id = "missing-mixed"
    _write_article(kb_root, missing_id)

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    report = service.report(article_ids=[ready_id, missing_id])

    assert report.total == 2
    assert report.ready == 1
    assert report.missing == 1
    assert report.availability_rate == 0.5


def test_report_requires_article_ids(kb_root):
    """report() refuses an empty article id list (no silent no-op)."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    with pytest.raises(ValueError):
        service.report(article_ids=[])


def test_report_is_pure_read_only(kb_root):
    """report() must not create artifacts, directories, or any state."""
    article_id = "readonly-article"
    _write_article(kb_root, article_id)
    artifacts_root = kb_root / "runtime" / "cognition" / "deep_read_artifacts"
    before = set(kb_root.rglob("*"))

    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    service.report(article_ids=[article_id])

    after = set(kb_root.rglob("*"))
    assert after == before
    assert not artifacts_root.exists()


def test_unsafe_article_id_cannot_escape_artifact_root(kb_root):
    """Path traversal: unsafe ids must not be interpolated into artifact paths."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    service = DeepReadAvailabilityService(kb_root=kb_root)
    traversal = "../../etc/passwd"
    report = service.report(article_ids=[traversal])

    # The traversal id is rejected before any artifact path interpolation
    # (UNKNOWN via _resolve_article escape guard); no files outside the KB
    # root may be created or read.
    assert report.by_article[traversal].status == "UNKNOWN"
    for candidate in (kb_root / "etc", kb_root.parent / "etc"):
        assert not candidate.exists()


def test_missing_index_is_unknown(kb_root):
    """Without an index entry, an article file is not guessed by name."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "no-index-article"
    (kb_root / "articles" / f"{article_id}.md").write_text(
        f"---\nid: {article_id}\ndate: 2026-07-01 10:00\n---\n\nContent.",
        encoding="utf-8",
    )

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"
    assert report.by_article[article_id].detail == "article not resolved"


def test_duplicate_index_id_is_unknown(kb_root):
    """Two index entries sharing one id cannot resolve to one article."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "dup-article"
    first = kb_root / "articles" / "20260825_dup-article.md"
    first.write_text(
        f"---\nid: {article_id}\ndate: 2026-08-25 10:00\n---\n\nFirst.",
        encoding="utf-8",
    )
    second = kb_root / "articles" / "20260826_dup-article.md"
    second.write_text(
        f"---\nid: {article_id}\ndate: 2026-08-26 10:00\n---\n\nSecond.",
        encoding="utf-8",
    )
    (kb_root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {"id": article_id, "file": first.name},
                    {"id": article_id, "file": second.name},
                ],
                "updated": "2026-08-26T10:00:00+08:00",
                "total": 2,
            }
        ),
        encoding="utf-8",
    )

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"


def test_malformed_index_entry_is_unknown(kb_root):
    """An entry without a usable 'file'/'path' cannot resolve."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "malformed-entry"
    index_path = kb_root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "articles": [{"id": article_id, "file": 123}],
                "updated": "2026-08-26T10:00:00+08:00",
                "total": 1,
            }
        ),
        encoding="utf-8",
    )

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"


@pytest.mark.parametrize(
    "file_name",
    ("../../escape.md", "/abs/escape.md", "sub/dir.md", "..", ".", ""),
)
def test_escaping_file_name_is_unknown(kb_root, file_name):
    """Absolute, traversal, and nested file names are rejected by the index."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "escape-article"
    index_path = kb_root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "articles": [{"id": article_id, "file": file_name}],
                "updated": "2026-08-26T10:00:00+08:00",
                "total": 1,
            }
        ),
        encoding="utf-8",
    )

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"


def test_symlinked_article_is_unknown(kb_root, tmp_path):
    """A symlinked article file is not an owner-only regular file."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "symlink-article"
    outside = tmp_path / "outside.md"
    outside.write_text(
        f"---\nid: {article_id}\ndate: 2026-08-26 10:00\n---\n\nContent.",
        encoding="utf-8",
    )
    link = kb_root / "articles" / f"{article_id}.md"
    link.symlink_to(outside)
    _write_index_entry(kb_root, article_id, link.name)

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"


def test_index_entry_with_missing_file_is_unknown(kb_root):
    """An entry whose file does not exist resolves to UNKNOWN, not a crash."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "missing-file-article"
    _write_index_entry(kb_root, article_id, "20260826_missing-file-article.md")

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"


def test_article_identity_mismatch_is_unknown(kb_root):
    """The file's embedded id must equal the index id."""
    from fin_analyse.cognition.deep_read_availability import DeepReadAvailabilityService

    article_id = "identity-article"
    article_path = kb_root / "articles" / f"{article_id}.md"
    article_path.write_text(
        "---\nid: some-other-article\ndate: 2026-08-26 10:00\n---\n\nContent.",
        encoding="utf-8",
    )
    _write_index_entry(kb_root, article_id, article_path.name)

    report = DeepReadAvailabilityService(kb_root=kb_root).report([article_id])

    assert report.by_article[article_id].status == "UNKNOWN"
