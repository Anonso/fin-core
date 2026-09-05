"""Load LLM configuration from YAML file and environment variables.

Supports ${ENV_VAR} syntax in YAML values for secrets.
Priority: Environment variables > YAML config file.
"""

import json
import logging
import math
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple, cast

from .backend_health import get_backend_circuit_breaker
from .llm_extractor import LLMBackend
from .openai_backend import OpenAICompatibleBackend

logger = logging.getLogger(__name__)
_ENV_REFERENCE = re.compile(r"\$\{(\w+)\}")
_AUTHJSON_REFERENCE = re.compile(r"^\$\{AUTHJSON:([A-Za-z0-9_.-]+)\}$")


class LLMConfigError(ValueError):
    """The active LLM backend plan violates FIN's closed configuration."""


class BackendEndpointPlan(NamedTuple):
    name: str
    api_key: str
    base_url: str | None
    model: str | None
    reasoning_effort: str | None


class BackendPlan(NamedTuple):
    name: str
    adapter_id: str
    model: str
    api_key: str
    base_url: str | None
    endpoints: tuple[BackendEndpointPlan, ...]
    reasoning_effort: str | None
    max_tokens: int
    timeout_seconds: float | None


_CONFIG_KEYS = {"models", "vision", "cross_validation", "priorities"}
_MODEL_KEYS = {
    "provider",
    "model",
    "api_key",
    "base_url",
    "enabled",
    "endpoints",
    "reasoning_effort",
    "max_tokens",
    "timeout",
}
_ENDPOINT_KEYS = {"name", "api_key", "base_url", "model", "reasoning_effort"}
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
_STATIC_ADAPTER_IDS = frozenset({"openai_compatible", "anthropic", "hermes"})


def _resolve_env(value: Any) -> Any:
    """Resolve ${ENV_VAR} patterns in string values."""
    if not isinstance(value, str):
        return value
    authjson_match = _AUTHJSON_REFERENCE.fullmatch(value)
    if authjson_match:
        return _read_authjson_api_key(authjson_match.group(1))
    matches = _ENV_REFERENCE.findall(value)
    if matches and len(matches) == 1 and value.strip() == f"${{{matches[0]}}}":
        return os.environ.get(matches[0], value)
    for var in matches:
        env_val = os.environ.get(var)
        if env_val is None:
            continue
        value = value.replace(f"${{{var}}}", env_val)
    return value


def _authjson_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _read_authjson_api_key(entry: str) -> str:
    """owner-only 读 opencode auth.json 的 <entry>.key，供 ${AUTHJSON:<entry>}。

    owner 2026-08-30 指令：opencode 的 DS 端点失败时换 auth.json 里的秘钥。
    与 _load_owner_only_dotenv_file 同级边界：绝对路径、非符号链接、regular
    file、属主本人、nlink==1、无 group/other 权限位、读取前后 fstat 一致。
    任何不符/失败返回空串（下游端点按缺凭据跳过），绝不放宽，值不入日志。
    """
    path = _authjson_path()
    if not path.is_absolute():
        return ""
    descriptor = -1
    payload: Any = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077 != 0
        ):
            return ""
        stream = os.fdopen(descriptor, encoding="utf-8")
        descriptor = -1
        with stream:
            payload = json.load(stream)
            after = os.fstat(stream.fileno())
        linked = os.stat(path, follow_symlinks=False)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        _file_identity(before) != _file_identity(after)
        or _file_identity(after) != _file_identity(linked)
    ):
        return ""
    item = payload.get(entry) if isinstance(payload, dict) else None
    key = item.get("key") if isinstance(item, dict) else None
    return str(key) if isinstance(key, str) else ""


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        import json

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _walk(obj):
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        return _resolve_env(obj)

    return cast(dict[str, Any], _walk(raw))


def _parse_dotenv(lines: Iterable[str]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in entries:
            entries[key] = value
    return entries


def _apply_dotenv(
    entries: dict[str, str],
    *,
    override_keys: frozenset[str] = frozenset(),
) -> None:
    for key, value in entries.items():
        if key not in os.environ or key in override_keys:
            os.environ[key] = value


def _load_dotenv_file(
    env_path: Path,
    *,
    override_keys: frozenset[str] = frozenset(),
) -> frozenset[str]:
    try:
        with open(env_path, encoding="utf-8") as f:
            entries = _parse_dotenv(f)
        _apply_dotenv(entries, override_keys=override_keys)
        logger.debug("Loaded .env from %s", env_path)
    except OSError:
        return frozenset()
    return frozenset(entries)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_nlink,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _load_owner_only_dotenv_file(path: Path) -> bool:
    if not path.is_absolute():
        return False
    descriptor = -1
    try:
        canonical = path.resolve(strict=True)
        if canonical != path:
            return False
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077 != 0
        ):
            return False
        stream = os.fdopen(descriptor, encoding="utf-8")
        descriptor = -1
        with stream:
            entries = _parse_dotenv(stream)
            after = os.fstat(stream.fileno())
        linked = os.stat(path, follow_symlinks=False)
    except (OSError, RuntimeError, UnicodeError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(
        linked
    ):
        return False
    _apply_dotenv(entries)
    return True


def _load_dotenv(project_root: Path) -> None:
    """Load runtime environment files, setting only keys not already present."""
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    primary_keys = _load_dotenv_file(
        env_path,
        override_keys=frozenset({"FIN_LLM_ENV_FILE"}),
    )
    if "FIN_LLM_ENV_FILE" not in primary_keys:
        return

    shared_env_value = os.environ.get("FIN_LLM_ENV_FILE")
    if not shared_env_value:
        return
    shared_env = Path(shared_env_value)
    if not _load_owner_only_dotenv_file(shared_env):
        logger.warning("Skipped unsafe FIN_LLM_ENV_FILE")


def load_llm_config(
    config_path: str | None = None,
    *,
    load_dotenv: bool = True,
) -> dict[str, Any]:
    if config_path is None:
        config_path = os.environ.get(
            "LLM_CONFIG_PATH",
            str(Path(__file__).resolve().parent.parent.parent / "config" / "llm.yaml"),
        )
    path = Path(config_path)
    if path.exists():
        # Runtime constructors retain the legacy dotenv behavior. Read-only
        # observers opt out so a status query cannot mutate process state.
        if load_dotenv:
            _load_dotenv(path.parent.parent)
            # runtime-configs 内容寻址路径没有 project .env；FIN_LLM_ENV_FILE
            # 独立注入时直接加载（owner-only 校验），保证 ${ENV} 可解析。
            shared_env_value = os.environ.get("FIN_LLM_ENV_FILE")
            if shared_env_value:
                _load_owner_only_dotenv_file(Path(shared_env_value))
        return _load_yaml(path)
    return {}


def get_backend_priority(
    config: Mapping[str, Any],
    tier: str,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve the candidate backend order for *tier* from the `priorities` section.

    家规规则 6：会变的候选顺序进配置不写死代码。Missing or malformed
    sections degrade to *fallback* so callers keep their hardcoded defaults.
    """
    priorities = config.get("priorities")
    if not isinstance(priorities, Mapping):
        return fallback
    names = priorities.get(tier)
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name for name in names)
    ):
        return fallback
    return tuple(names)


def configured_backend_order(tier: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Candidate backend order for *tier* from llm.yaml, read-only.

    Uses ``load_dotenv=False`` so an order lookup never mutates process
    environment state; any read failure degrades to *fallback*.
    """
    try:
        config = load_llm_config(load_dotenv=False)
    except Exception as exc:
        logger.warning("Could not read LLM priorities for tier '%s': %s", tier, exc)
        return fallback
    return get_backend_priority(config, tier, fallback)


def compile_backend_plan(config: Mapping[str, object]) -> tuple[BackendPlan, ...]:
    if not set(config).issubset(_CONFIG_KEYS):
        raise LLMConfigError("LLM config contains unknown top-level keys")
    models = config.get("models", {})
    vision = config.get("vision", {})
    if not isinstance(models, Mapping) or not isinstance(vision, Mapping):
        raise LLMConfigError("LLM models and vision config must be mappings")
    # 视觉专用模型不进文本 plan 池（结构事实集；识图链顺序见 llm.yaml vision.chain，
    # glm53_flash 等文本/识图共享条目不受排除影响；
    # mimo-token-plan 2026-09-02 新增：仅识图，不入文本池）
    vision_only = {"mimo", "glm-vision", "vision", "mimo-token-plan"}
    plans: list[BackendPlan] = []
    for name, raw_model in models.items():
        if not isinstance(name, str) or not name or not isinstance(raw_model, Mapping):
            raise LLMConfigError("LLM model entry is invalid")
        if not set(raw_model).issubset(_MODEL_KEYS):
            raise LLMConfigError(f"LLM model '{name}' contains unknown keys")
        enabled = raw_model.get("enabled")
        if not isinstance(enabled, bool):
            raise LLMConfigError(f"LLM model '{name}' requires a boolean enabled flag")
        if name in vision_only:
            continue
        adapter_id = raw_model.get("provider")
        model = raw_model.get("model")
        if adapter_id not in _STATIC_ADAPTER_IDS or not isinstance(model, str) or not model:
            raise LLMConfigError(f"LLM model '{name}' selects an uninstalled adapter")
        api_key = raw_model.get("api_key", "")
        base_url = raw_model.get("base_url")
        if not isinstance(api_key, str) or not isinstance(base_url, (str, type(None))):
            raise LLMConfigError(f"LLM model '{name}' credentials are invalid")
        reasoning_effort = raw_model.get("reasoning_effort")
        if reasoning_effort is not None and reasoning_effort not in _REASONING_EFFORTS:
            raise LLMConfigError(f"LLM model '{name}' reasoning effort is invalid")
        max_tokens = raw_model.get("max_tokens", 4096)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 65536:
            raise LLMConfigError(f"LLM model '{name}' max_tokens is invalid")
        timeout = raw_model.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise LLMConfigError(f"LLM model '{name}' timeout is invalid")
            timeout = float(timeout)
            if not math.isfinite(timeout) or not 0 < timeout <= 3600:
                raise LLMConfigError(f"LLM model '{name}' timeout is invalid")
        endpoints = _compile_backend_endpoints(name, raw_model.get("endpoints", ()))
        if adapter_id != "openai_compatible" and (
            endpoints or base_url is not None or reasoning_effort is not None or max_tokens != 4096
        ):
            raise LLMConfigError(f"LLM model '{name}' uses unsupported adapter fields")
        if enabled:
            plans.append(
                BackendPlan(
                    name=name,
                    adapter_id=cast(str, adapter_id),
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    endpoints=endpoints,
                    reasoning_effort=cast(str | None, reasoning_effort),
                    max_tokens=max_tokens,
                    timeout_seconds=cast(float | None, timeout),
                )
            )
    return tuple(plans)


def _compile_backend_endpoints(
    model_name: str, raw_endpoints: object
) -> tuple[BackendEndpointPlan, ...]:
    if raw_endpoints in (None, ()):
        return ()
    if not isinstance(raw_endpoints, list):
        raise LLMConfigError(f"LLM model '{model_name}' endpoints must be a list")
    endpoints: list[BackendEndpointPlan] = []
    seen: set[str] = set()
    required_endpoint_keys = {"name", "api_key", "base_url"}
    for raw_endpoint in raw_endpoints:
        if (
            not isinstance(raw_endpoint, Mapping)
            or not set(raw_endpoint).issubset(_ENDPOINT_KEYS)
            or not required_endpoint_keys.issubset(raw_endpoint)
        ):
            raise LLMConfigError(f"LLM model '{model_name}' endpoint is invalid")
        name = raw_endpoint.get("name")
        api_key = raw_endpoint.get("api_key")
        base_url = raw_endpoint.get("base_url")
        model = raw_endpoint.get("model")
        reasoning_effort = raw_endpoint.get("reasoning_effort")
        if (
            not isinstance(name, str)
            or not name
            or name in seen
            or not isinstance(api_key, str)
            or not isinstance(base_url, (str, type(None)))
            or not isinstance(model, (str, type(None)))
            or (isinstance(model, str) and not model)
            or not isinstance(reasoning_effort, (str, type(None)))
            or (
                reasoning_effort is not None
                and reasoning_effort not in _REASONING_EFFORTS
            )
        ):
            raise LLMConfigError(f"LLM model '{model_name}' endpoint is invalid")
        seen.add(name)
        endpoints.append(
            BackendEndpointPlan(name, api_key, base_url, model, reasoning_effort)
        )
    return tuple(endpoints)


def _configured_text(value: str | None) -> bool:
    return bool(
        value
        and "你的" not in value
        and _ENV_REFERENCE.search(value) is None
    )


def _openai_backend(plan: BackendPlan) -> LLMBackend:
    endpoints = [
        {
            "name": item.name,
            "api_key": item.api_key,
            "base_url": item.base_url,
            "model": item.model,
            "reasoning_effort": item.reasoning_effort,
        }
        for item in plan.endpoints
        if _configured_text(item.api_key)
        and (item.base_url is None or _configured_text(item.base_url))
    ]
    return OpenAICompatibleBackend(
        model=plan.model,
        api_key=plan.api_key,
        base_url=plan.base_url,
        endpoints=endpoints,
        reasoning_effort=plan.reasoning_effort,
        max_tokens=plan.max_tokens,
        timeout=plan.timeout_seconds,
        backend_name=plan.name,
    )


def _anthropic_backend(plan: BackendPlan) -> LLMBackend:
    from .claude_backend import ClaudeBackend

    return ClaudeBackend(model=plan.model, api_key=plan.api_key)


def _hermes_backend(plan: BackendPlan) -> LLMBackend:
    from .hermes_backend import create_hermes_backend

    return create_hermes_backend(model=plan.model)


_BACKEND_ADAPTER_CATALOG: Final[
    dict[str, Callable[[BackendPlan], LLMBackend]]
] = {
    "openai_compatible": _openai_backend,
    "anthropic": _anthropic_backend,
    "hermes": _hermes_backend,
}


def _plan_is_configured(plan: BackendPlan) -> bool:
    if plan.adapter_id == "hermes":
        return True
    endpoint_ready = any(
        _configured_text(endpoint.api_key)
        and (endpoint.base_url is None or _configured_text(endpoint.base_url))
        for endpoint in plan.endpoints
    )
    primary_ready = _configured_text(plan.api_key) and (
        plan.base_url is None or _configured_text(plan.base_url)
    )
    return bool(endpoint_ready or primary_ready)


def create_backends_from_config(config_path: str | None = None) -> dict[str, LLMBackend]:
    """Create LLM backends from YAML config file.

    Returns dict of {name: LLMBackend} for all enabled models.
    """
    config = load_llm_config(config_path)
    backends: dict[str, LLMBackend] = {}
    for plan in compile_backend_plan(config):
        if not _plan_is_configured(plan):
            # BUG-038：enabled 模型凭据未配置必须 fail-visible。静默缩池会让
            # t0/t1/cognition 池无声变短，且 provider_health 的口径相反（报
            # 「已配置」），故障只剩回退可观察。
            logger.warning(
                "LLM backend '%s' skipped: enabled but credentials not configured "
                "(api_key/base_url missing, '你的' placeholder, or unresolved "
                "${ENV} reference — check env file / FIN_LLM_ENV_FILE)",
                plan.name,
            )
            continue
        try:
            backends[plan.name] = _BACKEND_ADAPTER_CATALOG[plan.adapter_id](plan)
        except Exception as e:
            logger.warning("Failed to init backend '%s': %s", plan.name, e)

    filtered = get_backend_circuit_breaker().filter_available(backends)
    skipped = sorted(set(backends) - set(filtered))
    if skipped:
        logger.warning("LLM backends skipped due to cooldown: %s", ", ".join(skipped))
    return filtered


def get_cross_validation_config(config_path: str | None = None) -> dict[str, Any]:
    config = load_llm_config(config_path)
    return cast(
        dict[str, Any], config.get("cross_validation", {"min_agreement": 2, "agreement_boost": 0.1})
    )
