"""裁决收件箱配置加载（家规 6：会变的旋钮进 config/adjudication.yaml）。

missing file = 内置默认（静默），对齐 window_config 惯例。
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = _PROJECT_ROOT / "config" / "adjudication.yaml"

_DEFAULT_ESCALATE_AFTER_DAYS = 3
_DEFAULT_G_LAG_DAYS_THRESHOLD = 2

__all__ = ["CONFIG_PATH", "load_adjudication_config"]


def load_adjudication_config(path: Path | None = None) -> dict:
    """Load inbox tuning values; missing file = built-in defaults (silent)."""

    config_path = path or CONFIG_PATH
    payload: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError(f"adjudication_config_invalid: {config_path}")
            payload = loaded
    digest = payload.get("digest") or {}
    batch = payload.get("g_annotation_batch") or {}
    if not isinstance(digest, dict) or not isinstance(batch, dict):
        raise ValueError("adjudication_config_section_invalid")
    return {
        "escalate_after_days": _positive_int(
            digest.get("escalate_after_days", _DEFAULT_ESCALATE_AFTER_DAYS)
        ),
        "g_lag_days_threshold": _positive_int(
            batch.get("lag_days_threshold", _DEFAULT_G_LAG_DAYS_THRESHOLD)
        ),
    }


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("adjudication_config_value_invalid")
    return value
