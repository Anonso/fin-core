from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_zsxq_exposes_one_supported_manual_scrape_entrypoint() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    catalog = (REPO_ROOT / "docs/architecture/internal-module-catalog.md").read_text(
        encoding="utf-8"
    )

    assert not (REPO_ROOT / "fin_analyse/scraper/cli.py").exists()
    assert not (REPO_ROOT / "fin_analyse/ingestion/zsxq_batch.py").exists()
    assert not (REPO_ROOT / "scripts/priority_watch.py").exists()
    assert not (REPO_ROOT / "scripts/scraper_recovery.py").exists()
    assert not (REPO_ROOT / "scripts/scraper_supervisor.py").exists()
    direct_script_tokens = (
        "CdpBridgeScraper",
        "run_incremental_with_result(",
        "run_priority_scan(",
    )
    for script in (REPO_ROOT / "scripts").glob("*.py"):
        source = script.read_text(encoding="utf-8")
        assert not any(token in source for token in direct_script_tokens), script
    assert "fin-scraper" not in pyproject
    assert "fin-scraper" not in makefile
    assert re.search(r"^(scrape|columns):", makefile, flags=re.MULTILINE) is None
    assert "`fin-scraper`" not in catalog
    assert "python -m fin_analyse.scraper.scheduled_run" in catalog
