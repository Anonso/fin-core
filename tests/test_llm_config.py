import os

import pytest

from fin_analyse.claims.backend_health import BackendCircuitBreaker
from fin_analyse.claims.config_loader import (
    LLMConfigError,
    compile_backend_plan,
    create_backends_from_config,
    load_llm_config,
)


def test_load_config_reads_enabled_models(tmp_path):
    config_path = tmp_path / "llm.yaml"
    config_path.write_text("""
models:
  test_model:
    provider: openai_compatible
    model: test-model-v1
    api_key: sk-test-key
    enabled: true
  disabled_model:
    provider: openai_compatible
    model: disabled-model
    api_key: sk-disabled
    enabled: false
cross_validation:
  min_agreement: 2
""")

    config = load_llm_config(str(config_path))

    assert "models" in config
    assert config["models"]["test_model"]["enabled"] is True
    assert config["models"]["disabled_model"]["enabled"] is False
    assert config["cross_validation"]["min_agreement"] == 2


def test_read_only_config_load_does_not_import_dotenv_into_process(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.parent.mkdir()
    config_path.write_text("""
models:
  observer:
    api_key: ${RUNTIME_TRUTH_TEST_SECRET}
    enabled: true
""")
    (tmp_path / ".env").write_text("RUNTIME_TRUTH_TEST_SECRET=must-not-enter-process\n")
    monkeypatch.delenv("RUNTIME_TRUTH_TEST_SECRET", raising=False)

    config = load_llm_config(str(config_path), load_dotenv=False)

    assert config["models"]["observer"]["api_key"] == ("${RUNTIME_TRUTH_TEST_SECRET}")
    assert "RUNTIME_TRUTH_TEST_SECRET" not in os.environ


def test_runtime_config_loads_owner_only_shared_llm_environment(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime-configs" / ("a" * 64)
    config_path = runtime_root / "config" / "llm.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("""
models:
  observer:
    api_key: ${RUNTIME_SHARED_LLM_TEST_KEY}
    enabled: true
""")
    shared_env = tmp_path / "llm.env"
    shared_env.write_text("RUNTIME_SHARED_LLM_TEST_KEY=sk-shared-test-key\n")
    shared_env.chmod(0o600)
    ambient_env = tmp_path / "ambient-llm.env"
    ambient_env.write_text("RUNTIME_SHARED_LLM_TEST_KEY=sk-ambient-test-key\n")
    ambient_env.chmod(0o600)
    (runtime_root / ".env").write_text(f"FIN_LLM_ENV_FILE={shared_env}\n")
    monkeypatch.setenv("FIN_LLM_ENV_FILE", str(ambient_env))
    monkeypatch.delenv("RUNTIME_SHARED_LLM_TEST_KEY", raising=False)

    config = load_llm_config(str(config_path))

    assert config["models"]["observer"]["api_key"] == "sk-shared-test-key"


def test_runtime_config_rejects_non_owner_only_shared_llm_environment(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime-configs" / ("a" * 64)
    config_path = runtime_root / "config" / "llm.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("""
models:
  observer:
    api_key: ${RUNTIME_UNSAFE_LLM_TEST_KEY}
    enabled: true
""")
    shared_env = tmp_path / "llm.env"
    shared_env.write_text("RUNTIME_UNSAFE_LLM_TEST_KEY=must-not-load\n")
    shared_env.chmod(0o644)
    (runtime_root / ".env").write_text(f"FIN_LLM_ENV_FILE={shared_env}\n")
    monkeypatch.delenv("FIN_LLM_ENV_FILE", raising=False)
    monkeypatch.delenv("RUNTIME_UNSAFE_LLM_TEST_KEY", raising=False)

    config = load_llm_config(str(config_path))

    assert config["models"]["observer"]["api_key"] == "${RUNTIME_UNSAFE_LLM_TEST_KEY}"
    assert "RUNTIME_UNSAFE_LLM_TEST_KEY" not in os.environ


def test_runtime_config_rejects_linked_shared_llm_environment(
    tmp_path,
    monkeypatch,
):
    for link_kind in ("symlink", "hardlink"):
        case_root = tmp_path / link_kind
        runtime_root = case_root / "runtime-configs" / ("a" * 64)
        config_path = runtime_root / "config" / "llm.yaml"
        config_path.parent.mkdir(parents=True)
        key = f"RUNTIME_{link_kind.upper()}_LLM_TEST_KEY"
        config_path.write_text(f"""
models:
  observer:
    api_key: ${{{key}}}
    enabled: true
""")
        target = case_root / "target.env"
        target.write_text(f"{key}=must-not-load\n")
        target.chmod(0o600)
        shared_env = case_root / "llm.env"
        if link_kind == "symlink":
            shared_env.symlink_to(target)
        else:
            os.link(target, shared_env)
        (runtime_root / ".env").write_text(f"FIN_LLM_ENV_FILE={shared_env}\n")
        monkeypatch.delenv(key, raising=False)

        config = load_llm_config(str(config_path))

        assert config["models"]["observer"]["api_key"] == f"${{{key}}}"
        assert key not in os.environ


def test_create_backends_skips_disabled_and_placeholder_keys(tmp_path):
    config_path = tmp_path / "llm.yaml"
    config_path.write_text("""
models:
  valid_model:
    provider: openai_compatible
    model: valid-v1
    api_key: sk-real-key
    enabled: true
  placeholder_key:
    provider: openai_compatible
    model: ph-v1
    api_key: sk-你的key
    enabled: true
  disabled:
    provider: openai_compatible
    model: dis-v1
    api_key: sk-real
    enabled: false
""")

    backends = create_backends_from_config(str(config_path))
    assert "valid_model" in backends
    assert "placeholder_key" not in backends
    assert "disabled" not in backends


def test_create_backends_skips_unresolved_environment_placeholders(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "llm.yaml"
    config_path.write_text("""
models:
  exact_unresolved:
    provider: openai_compatible
    model: unresolved-v1
    api_key: ${MISSING_LLM_TEST_KEY}
    base_url: ${MISSING_LLM_TEST_BASE_URL}
    enabled: true
  embedded_unresolved:
    provider: openai_compatible
    model: embedded-unresolved-v1
    api_key: prefix-${MISSING_LLM_TEST_KEY}
    base_url: https://${MISSING_LLM_TEST_BASE_URL}/v1
    enabled: true
""")
    monkeypatch.delenv("MISSING_LLM_TEST_KEY", raising=False)
    monkeypatch.delenv("MISSING_LLM_TEST_BASE_URL", raising=False)

    backends = create_backends_from_config(str(config_path))

    assert backends == {}


def test_create_backends_accepts_endpoint_only_and_excludes_vision_models(tmp_path):
    config_path = tmp_path / "llm.yaml"
    config_path.write_text("""
models:
  text:
    provider: openai_compatible
    model: gpt-5.6-sol
    api_key: ${MISSING_PRIMARY}
    endpoints:
      - name: codesonline
        api_key: sk-text
        base_url: https://text
    enabled: true
  mimo:
    model: mimo-v2.5
    api_key: sk-vision
    enabled: true
  glm-vision:
    model: glm-4v-flash
    api_key: sk-glm-vision
    enabled: true
  vision:
    model: qwen-vl
    api_key: sk-qwen
    enabled: true
""")

    backends = create_backends_from_config(str(config_path))

    assert "text" in backends
    assert backends["text"].endpoints[0]["name"] == "codesonline"
    assert "mimo" not in backends
    assert "glm-vision" not in backends
    assert "vision" not in backends


def test_create_backends_skips_backend_in_cooldown(tmp_path, monkeypatch):
    config_path = tmp_path / "llm.yaml"
    config_path.write_text("""
models:
  gpt5:
    provider: openai_compatible
    model: gpt-5.6-sol
    api_key: sk-real-key
    enabled: true
  deepseek:
    provider: openai_compatible
    model: deepseek-v4-pro
    api_key: sk-real-key
    enabled: true
""")
    breaker = BackendCircuitBreaker(failure_threshold=1, cooldown_seconds=600)
    breaker.record_failure("gpt5", {"error_type": "InternalServerError", "http_status": 500})
    monkeypatch.setattr(
        "fin_analyse.claims.config_loader.get_backend_circuit_breaker",
        lambda: breaker,
    )

    backends = create_backends_from_config(str(config_path))

    assert "gpt5" not in backends
    assert "deepseek" in backends


def test_backend_plan_is_immutable_and_uses_static_provider_id() -> None:
    plan = compile_backend_plan(
        {
            "models": {
                "research": {
                    "provider": "openai_compatible",
                    "model": "research-v1",
                    "api_key": "sk-test",
                    "base_url": "https://example.invalid/v1",
                    "enabled": True,
                }
            }
        }
    )

    assert plan[0].name == "research"
    assert plan[0].adapter_id == "openai_compatible"
    with pytest.raises(AttributeError):
        plan[0].model = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "model_config",
    (
        {
            "provider": "unknown-provider",
            "model": "x",
            "api_key": "sk-test",
            "enabled": True,
        },
        {
            "provider": "openai_compatible",
            "model": "x",
            "api_key": "sk-test",
            "enabled": True,
            "tier": "t0",
        },
    ),
)
def test_backend_plan_rejects_unknown_provider_and_dead_keys(model_config) -> None:
    with pytest.raises(LLMConfigError):
        compile_backend_plan({"models": {"bad": model_config}})


def test_kimi_looking_key_does_not_override_declared_adapter(tmp_path) -> None:
    config_path = tmp_path / "llm.yaml"
    config_path.write_text(
        """
models:
  explicit:
    provider: openai_compatible
    model: kimi-compatible
    api_key: sk-kimi-explicit
    base_url: https://example.invalid/v1
    enabled: true
"""
    )

    backend = create_backends_from_config(str(config_path))["explicit"]

    assert backend.__class__.__name__ == "OpenAICompatibleBackend"
