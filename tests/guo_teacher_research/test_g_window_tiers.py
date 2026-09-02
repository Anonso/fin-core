"""G-lane window tier mapping unit tests (owner 2026-09-02 values)."""

from __future__ import annotations

from fin_analyse.guo_teacher_research.window_config import (
    g_window_natural_days,
    g_window_tier,
    load_g_window_config,
)


def test_repo_config_has_owner_values() -> None:
    config = load_g_window_config()
    assert config.commentary_trading_days == 4
    assert config.special_report_days == 45
    assert config.good_question_days == 20
    assert config.historical_days == 60


def test_tier_mapping_and_natural_days() -> None:
    config = load_g_window_config()
    assert g_window_tier("星大派锐评") == "commentary"
    assert g_window_tier("星大派每日热点") == "commentary"
    assert g_window_tier("星大派特刊") == "special"
    assert g_window_tier("凤仙郡小故事") == "special"
    assert g_window_tier("星大派人脉") == "special"
    assert g_window_tier("版本强势英雄") == "special"
    assert g_window_tier("星大派好问题") == "qa"
    assert g_window_tier("重中之重") == "other"

    assert g_window_natural_days("星大派特刊", config) == 45
    assert g_window_natural_days("星大派人脉", config) == 45
    assert g_window_natural_days("星大派好问题", config) == 20
    assert g_window_natural_days("重中之重", config) == 60
