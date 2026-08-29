from __future__ import annotations

from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.codex_route_config import load_codex_route_config


def _write_owner_only_config(path: Path, text: str) -> None:
    path.parent.chmod(0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_loads_enabled_routes_in_declared_order(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(
        config_path,
        """\
schema_version: fin.codex-routes/v2
routes:
  - id: direct-primary
    enabled: true
    adapter: direct-codex
    workloads: [consultation, review]
    probe_timeout_seconds: 60
    attempt_timeout_seconds: 600
    model:
      id: gpt-5.6-sol
      quality: pinned
  - id: codex-proxy-a
    enabled: true
    adapter: codex-responses
    workloads: [consultation]
    probe_timeout_seconds: 60
    attempt_timeout_seconds: 600
    api:
      base_url: https://ai.apiclub.top
    model:
      id: gpt-5.6-sol
      quality: pinned
probe:
  reachable_ttl_seconds: 1800
cooldown:
  step_seconds: 900
  max_seconds: 3600
  half_open_lease_seconds: 300
""",
    )

    config = load_codex_route_config(config_path)

    assert [route.id for route in config.enabled_routes("consultation")] == [
        "direct-primary",
        "codex-proxy-a",
    ]
    assert [route.id for route in config.enabled_routes("review")] == ["direct-primary"]
    assert config.routes[0].probe_timeout_seconds == 60.0
    assert config.routes[0].attempt_timeout_seconds == 600.0
    assert config.routes[1].api is not None
    assert config.routes[1].api.base_url == "https://ai.apiclub.top"
    assert config.cooldown.step_seconds == 900.0


def test_loads_native_codex_provider_entirely_from_one_route_block(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    auth_path = tmp_path / "opencode-auth.json"
    model_catalog = tmp_path / "models.json"
    _write_owner_only_config(
        config_path,
        f"""\
schema_version: fin.codex-routes/v2
routes:
  - id: codex-open
    enabled: true
    adapter: codex-provider
    workloads: [consultation]
    probe_timeout_seconds: 60
    attempt_timeout_seconds: 600
    api:
      base_url: https://opencode.ai/zen/go/v1
    auth:
      path: {auth_path}
      key_path: [opencode-go, key]
    model_catalog: {model_catalog}
    model:
      id: deepseek-v4-flash
      quality: degraded
probe:
  reachable_ttl_seconds: 1800
cooldown:
  step_seconds: 900
  max_seconds: 3600
  half_open_lease_seconds: 300
""",
    )

    route = load_codex_route_config(config_path).route("codex-open")

    assert route.adapter == "codex-provider"
    assert route.provider_id == "codex_open"
    assert route.auth is not None
    assert route.auth.path == auth_path
    assert route.auth.key_path == ("opencode-go", "key")
    assert route.model_catalog == model_catalog


def test_native_codex_provider_rejects_unsupported_review_workload(
    tmp_path: Path,
) -> None:
    # Self-contained document: the example config evolves with route surgery
    # (D-018/D-019), so mutating it by string replace silently no-ops.  The
    # invariant under test lives in the loader, not in the example file.
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(
        config_path,
        """\
schema_version: fin.codex-routes/v2
routes:
  - id: codex-glm
    enabled: true
    adapter: codex-provider
    workloads: [consultation, review]
    probe_timeout_seconds: 60
    attempt_timeout_seconds: 900
    api:
      base_url: "https://open.bigmodel.cn/api/v1"
    model:
      id: glm-5.3
      quality: pinned
probe:
  reachable_ttl_seconds: 1800
cooldown:
  step_seconds: 900
  max_seconds: 3600
  half_open_lease_seconds: 300
""",
    )

    with pytest.raises(ValueError, match="review"):
        load_codex_route_config(config_path)


def test_execution_configuration_fingerprint_tracks_api_and_model_not_policy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    template = """\
schema_version: fin.codex-routes/v2
routes:
  - id: codex-proxy-a
    enabled: {enabled}
    adapter: codex-responses
    workloads: [consultation]
    probe_timeout_seconds: {probe_timeout}
    attempt_timeout_seconds: 600
    api:
      base_url: {base_url}
    model:
      id: {model}
      quality: pinned
probe:
  reachable_ttl_seconds: 1800
cooldown:
  step_seconds: {cooldown_step}
  max_seconds: 3600
  half_open_lease_seconds: 300
"""

    def fingerprint(**overrides: object) -> str:
        values = {
            "enabled": "true",
            "probe_timeout": 60,
            "base_url": "https://ai.apiclub.top/v1",
            "model": "gpt-5.6-sol",
            "cooldown_step": 900,
            **overrides,
        }
        _write_owner_only_config(config_path, template.format(**values))
        return load_codex_route_config(config_path).routes[0].configuration_fingerprint

    baseline = fingerprint()
    assert fingerprint(enabled="false") == baseline
    assert fingerprint(probe_timeout=30, cooldown_step=600) == baseline
    assert fingerprint(base_url="https://backup.example/v1") != baseline
    assert fingerprint(model="gpt-5.6-terra") != baseline


@pytest.mark.parametrize(
    "replacement",
    (
        "unknown: true\n",
        "schema_version: fin.codex-routes/v2\nschema_version: duplicate\n",
    ),
)
def test_rejects_unknown_or_duplicate_yaml_keys(tmp_path: Path, replacement: str) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(config_path, replacement)

    with pytest.raises(ValueError):
        load_codex_route_config(config_path)


def test_rejects_insecure_file_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(config_path, "schema_version: fin.codex-routes/v2\n")
    config_path.chmod(0o644)

    with pytest.raises(ValueError, match="insecure"):
        load_codex_route_config(config_path)


@pytest.mark.parametrize(
    "document",
    (
        "schema_version: &schema fin.codex-routes/v2\n",
        "schema_version: fin.codex-routes/v2\nprobe: &probe {}\ncopy: *probe\n",
        "schema_version: fin.codex-routes/v2\nbase: &base {}\nmerged:\n  <<: *base\n",
    ),
)
def test_rejects_yaml_anchors_aliases_and_merges(tmp_path: Path, document: str) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(config_path, document)

    with pytest.raises(ValueError, match="yaml"):
        load_codex_route_config(config_path)


def test_repository_example_is_a_valid_consultation_catalog(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "config" / "codex_routes.yaml.example"
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(config_path, source.read_text(encoding="utf-8"))

    config = load_codex_route_config(config_path)

    assert [route.id for route in config.routes] == [
        "codex-glm",
        "codex-open",
    ]
    assert [route.id for route in config.enabled_routes("consultation")] == [
        "codex-glm",
        "codex-open",
    ]
    opencode = config.route("codex-open")
    assert opencode.adapter == "codex-provider"
    assert opencode.model.id == "deepseek-v4-pro"


def test_route_config_loads_optional_reasoning_effort(tmp_path: Path) -> None:
    """顶层可选 reasoning.effort 必须被解析进配置摘要。"""

    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(
        config_path,
        """\
schema_version: fin.codex-routes/v2
reasoning:
  effort: xhigh
routes:
  - id: direct-primary
    enabled: true
    adapter: direct-codex
    workloads: [consultation, review]
    probe_timeout_seconds: 60
    attempt_timeout_seconds: 600
    model:
      id: gpt-5.6-sol
      quality: pinned
probe:
  reachable_ttl_seconds: 1800
cooldown:
  step_seconds: 900
  max_seconds: 3600
  half_open_lease_seconds: 300
""",
    )

    config = load_codex_route_config(config_path)

    assert config.reasoning_effort == "xhigh"


@pytest.mark.parametrize("bad", ("ultra-extra", "xHIGH", "turbo", ""))
def test_route_config_rejects_invalid_reasoning_effort(
    tmp_path: Path,
    bad: str,
) -> None:
    config_path = tmp_path / "codex_routes.yaml"
    _write_owner_only_config(
        config_path,
        f"""\
schema_version: fin.codex-routes/v2
reasoning:
  effort: {bad}
routes:
  - id: direct-primary
    enabled: true
    adapter: direct-codex
    workloads: [consultation, review]
    probe_timeout_seconds: 60
    attempt_timeout_seconds: 600
    model:
      id: gpt-5.6-sol
      quality: pinned
probe:
  reachable_ttl_seconds: 1800
cooldown:
  step_seconds: 900
  max_seconds: 3600
  half_open_lease_seconds: 300
""",
    )

    with pytest.raises(ValueError):
        load_codex_route_config(config_path)
