"""Reference-lane window loader unit tests (owner 2026-09-02)."""

from __future__ import annotations

import json
from pathlib import Path

from fin_analyse.guo_teacher_research.window_config import reference_window_days


def test_window_days_for_known_columns(tmp_path: Path) -> None:
    config = tmp_path / "windows.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "fin.zsxq-reference-windows/v1",
                "default_days": 60,
                "windows": {
                    "普通": {"days": 60, "unit": "natural"},
                    "星大派好问题": {"days": 20, "unit": "natural"},
                },
            }
        ),
        encoding="utf-8",
    )
    assert reference_window_days("普通", config_path=config) == 60
    assert reference_window_days("星大派好问题", config_path=config) == 20
    assert reference_window_days("未归类列", config_path=config) == 60


def test_window_days_missing_or_malformed_falls_back(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert reference_window_days("普通", config_path=missing) == 60

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    assert reference_window_days("普通", config_path=malformed) == 60
