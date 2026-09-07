"""owner 2026-09-07：per-model extra_body（思考开关等供应商原语）的配置解析契约。"""

from __future__ import annotations

import pytest

from fin_analyse.claims.config_loader import (
    LLMConfigError,
    compile_backend_plan,
)


def _plans(models: dict):
    return compile_backend_plan(
        {"models": models, "vision": {}, "cross_validation": {}, "priorities": {}}
    )


def test_extra_body_parsed_and_bound_to_openai_plan():
    plans = _plans(
        {
            "glm53_flash": {
                "provider": "openai_compatible",
                "model": "glm-5.3-flash",
                "api_key": "${GLM_API_KEY}",
                "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                "enabled": True,
                "extra_body": {"thinking": {"type": "disabled"}},
                "timeout": 600,
            }
        }
    )
    assert len(plans) == 1
    assert plans[0].extra_body == {"thinking": {"type": "disabled"}}
    assert plans[0].timeout_seconds == 600.0


def test_extra_body_rejected_for_non_openai_adapter():
    with pytest.raises(LLMConfigError, match="unsupported adapter fields"):
        _plans(
            {
                "claude_cc": {
                    "provider": "anthropic",
                    "model": "glm-5.3",
                    "api_key": "k",
                    "enabled": True,
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            }
        )


def test_extra_body_must_be_non_empty_mapping():
    with pytest.raises(LLMConfigError, match="extra_body"):
        _plans(
            {
                "glm53_flash": {
                    "provider": "openai_compatible",
                    "model": "glm-5.3-flash",
                    "api_key": "k",
                    "enabled": True,
                    "extra_body": "disabled",
                }
            }
        )


def test_content_filter_routing_parsed_or_fail_open(tmp_path):
    from fin_analyse.claims.config_loader import content_filter_routing

    cfg = tmp_path / "llm.yaml"
    cfg.write_text(
        """
models: {}
routing:
  content_filter_skip:
    columns: ["每日热点"]
    skip_backends: [glm53_flash]
""",
        encoding="utf-8",
    )
    columns, backends = content_filter_routing(config_path=str(cfg))
    assert columns == ("每日热点",)
    assert backends == ("glm53_flash",)

    empty = tmp_path / "empty.yaml"
    empty.write_text("models: {}\n", encoding="utf-8")
    assert content_filter_routing(config_path=str(empty)) == ((), ())
    assert content_filter_routing(config_path=str(tmp_path / "missing.yaml")) == ((), ())
