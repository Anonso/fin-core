"""Owner-only configuration for the FIN Codex route chain."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast
from urllib.parse import urlsplit, urlunsplit

import yaml

SCHEMA_VERSION = "fin.codex-routes/v2"
DEFAULT_CONFIG_PATH = Path.home() / "fin-data" / "codex_routes.yaml"
_MAX_CONFIG_BYTES = 64 * 1024
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_AUTH_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_API_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_API_PATH = re.compile(r"^(?:/[A-Za-z0-9._~:@%+=,-]+)*$")
_LEGACY_ROUTE_IDS = frozenset(
    {
        "proxy-apiclub",
        "proxy-codesonline",
        "proxy-deepseek",
        "proxy-primary",
        "proxy-fallback",
    }
)
_RESPONSES_SLOT_IDS = frozenset({"codex-proxy-a", "codex-proxy-b"})
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})

RouteAdapter = Literal["direct-codex", "codex-responses", "codex-provider"]
RouteWorkload = Literal["consultation", "review"]
ModelQuality = Literal["pinned", "degraded"]


class CodexRouteConfigError(ValueError):
    """The route policy is missing, insecure, or invalid."""


def is_codex_route_id(value: object) -> TypeGuard[str]:
    """Whether a value is a bounded, non-secret route identifier."""
    return (
        isinstance(value, str)
        and _SLUG.fullmatch(value) is not None
        and value.startswith(("direct-", "proxy-", "codex-"))
    )


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class RouteApi:
    base_url: str


@dataclass(frozen=True, slots=True)
class RouteModel:
    id: str
    quality: ModelQuality


@dataclass(frozen=True, slots=True)
class RouteAuth:
    path: Path
    key_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexRoute:
    id: str
    enabled: bool
    adapter: RouteAdapter
    workloads: tuple[RouteWorkload, ...]
    probe_timeout_seconds: float
    attempt_timeout_seconds: float
    model: RouteModel
    api: RouteApi | None = None
    auth: RouteAuth | None = None
    model_catalog: Path | None = None

    @property
    def provider_id(self) -> str:
        if self.adapter != "codex-provider":
            raise ValueError("codex route is not a configured provider")
        return self.id.replace("-", "_")

    @property
    def configuration_fingerprint(self) -> str:
        """Execution-affecting config identity, excluding ordering and policy."""
        identity = {
            "adapter": self.adapter,
            "api_base_url": self.api.base_url if self.api is not None else None,
            "auth_path": str(self.auth.path) if self.auth is not None else None,
            "auth_key_path": list(self.auth.key_path) if self.auth is not None else None,
            "model_catalog": (str(self.model_catalog) if self.model_catalog is not None else None),
            "model": self.model.id,
            "route": self.id,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RouteProbePolicy:
    reachable_ttl_seconds: float


@dataclass(frozen=True, slots=True)
class RouteCooldownPolicy:
    step_seconds: float
    max_seconds: float
    half_open_lease_seconds: float


@dataclass(frozen=True, slots=True)
class CodexRouteConfig:
    routes: tuple[CodexRoute, ...]
    probe: RouteProbePolicy
    cooldown: RouteCooldownPolicy
    digest: str
    reasoning_effort: str | None = None

    def enabled_routes(self, workload: RouteWorkload) -> tuple[CodexRoute, ...]:
        if workload not in {"consultation", "review"}:
            raise ValueError("codex route workload is invalid")
        return tuple(
            route for route in self.routes if route.enabled and workload in route.workloads
        )

    def route(self, route_id: str) -> CodexRoute:
        for route in self.routes:
            if route.id == route_id:
                return route
        raise KeyError(route_id)


def load_codex_route_config(path: str | Path = DEFAULT_CONFIG_PATH) -> CodexRouteConfig:
    """Load one immutable, strictly validated route-policy snapshot."""
    config_path = Path(path)
    text = _read_owner_only_text(config_path)
    try:
        if any(
            isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
            for token in yaml.scan(text)
        ):
            raise CodexRouteConfigError(
                "codex route config yaml aliases are not supported"
            )
        document = yaml.load(text, Loader=_UniqueKeyLoader)
    except CodexRouteConfigError:
        raise
    except yaml.YAMLError as error:
        raise CodexRouteConfigError("codex route config yaml is invalid") from error
    root = _mapping(document, "root")
    _keys(
        root,
        required={"schema_version", "routes", "probe", "cooldown"},
        allowed={"schema_version", "routes", "probe", "cooldown", "reasoning"},
        label="root",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise CodexRouteConfigError("codex route config schema is invalid")

    raw_routes = root["routes"]
    if not isinstance(raw_routes, list) or not 1 <= len(raw_routes) <= 32:
        raise CodexRouteConfigError("codex route list is invalid")
    routes = tuple(_parse_route(item) for item in raw_routes)
    route_ids = [route.id for route in routes]
    if len(route_ids) != len(set(route_ids)):
        raise CodexRouteConfigError("codex route ids are duplicated")
    probe = _parse_probe(root["probe"])
    cooldown = _parse_cooldown(root["cooldown"])
    reasoning_effort = _parse_reasoning(root.get("reasoning"))
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "routes": [_route_document(route) for route in routes],
        "probe": {
            "reachable_ttl_seconds": probe.reachable_ttl_seconds,
        },
        "cooldown": {
            "step_seconds": cooldown.step_seconds,
            "max_seconds": cooldown.max_seconds,
            "half_open_lease_seconds": cooldown.half_open_lease_seconds,
        },
    }
    if reasoning_effort is not None:
        canonical["reasoning"] = {"effort": reasoning_effort}
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CodexRouteConfig(
        routes=routes,
        probe=probe,
        cooldown=cooldown,
        digest=digest,
        reasoning_effort=reasoning_effort,
    )


def _parse_route(value: object) -> CodexRoute:
    item = _mapping(value, "route")
    allowed = {
        "id",
        "enabled",
        "adapter",
        "workloads",
        "probe_timeout_seconds",
        "attempt_timeout_seconds",
        "model",
        "api",
        "auth",
        "model_catalog",
    }
    required = allowed - {"api", "auth", "model_catalog"}
    _keys(item, required=required, allowed=allowed, label="route")
    route_id = _slug(item["id"], "route id")
    if route_id in _LEGACY_ROUTE_IDS:
        raise CodexRouteConfigError("legacy codex route id is not supported")
    enabled = item["enabled"]
    if not isinstance(enabled, bool):
        raise CodexRouteConfigError("codex route enabled flag is invalid")
    adapter = item["adapter"]
    if adapter not in {"direct-codex", "codex-responses", "codex-provider"}:
        raise CodexRouteConfigError("codex route adapter is invalid")
    if (adapter == "direct-codex") != route_id.startswith("direct-"):
        raise CodexRouteConfigError("codex route id and adapter are inconsistent")
    if adapter == "codex-responses" and route_id not in _RESPONSES_SLOT_IDS:
        raise CodexRouteConfigError("responses route id must use a fixed A/B slot")

    raw_workloads = item["workloads"]
    if not isinstance(raw_workloads, list) or not raw_workloads:
        raise CodexRouteConfigError("codex route workloads are invalid")
    if any(workload not in {"consultation", "review"} for workload in raw_workloads):
        raise CodexRouteConfigError("codex route workloads are invalid")
    if len(raw_workloads) != len(set(raw_workloads)):
        raise CodexRouteConfigError("codex route workloads are duplicated")
    if adapter == "codex-provider" and "review" in raw_workloads:
        raise CodexRouteConfigError("codex provider review workload is unsupported")
    workloads = cast(tuple[RouteWorkload, ...], tuple(raw_workloads))
    probe_timeout_seconds = _number(
        item["probe_timeout_seconds"], "probe timeout", minimum=1, maximum=60
    )
    attempt_timeout_seconds = _number(
        item["attempt_timeout_seconds"], "attempt timeout", minimum=30, maximum=1800
    )

    model_data = _mapping(item["model"], "route model")
    _exact_keys(model_data, {"id", "quality"}, "route model")
    model_id = model_data["id"]
    quality = model_data["quality"]
    if not isinstance(model_id, str) or _MODEL_ID.fullmatch(model_id) is None:
        raise CodexRouteConfigError("codex route model id is invalid")
    if quality not in {"pinned", "degraded"}:
        raise CodexRouteConfigError("codex route model quality is invalid")
    model = RouteModel(id=model_id, quality=cast(ModelQuality, quality))

    api: RouteApi | None = None
    auth: RouteAuth | None = None
    model_catalog: Path | None = None
    if adapter == "direct-codex":
        if {"api", "auth", "model_catalog"} & set(item):
            raise CodexRouteConfigError("direct codex route has provider configuration")
    else:
        if "api" not in item:
            raise CodexRouteConfigError("proxy codex route api is missing")
        api_data = _mapping(item["api"], "route api")
        _exact_keys(api_data, {"base_url"}, "route api")
        api = RouteApi(base_url=_base_url(api_data["base_url"]))
        if adapter == "codex-responses":
            if {"auth", "model_catalog"} & set(item):
                raise CodexRouteConfigError(
                    "responses codex route has native provider configuration"
                )
        else:
            if "auth" not in item or "model_catalog" not in item:
                raise CodexRouteConfigError("codex provider configuration is missing")
            auth_data = _mapping(item["auth"], "route auth")
            _exact_keys(auth_data, {"path", "key_path"}, "route auth")
            raw_key_path = auth_data["key_path"]
            if (
                not isinstance(raw_key_path, list)
                or not 1 <= len(raw_key_path) <= 4
                or any(
                    not isinstance(key, str) or _AUTH_KEY.fullmatch(key) is None
                    for key in raw_key_path
                )
            ):
                raise CodexRouteConfigError("codex route auth key path is invalid")
            auth = RouteAuth(
                path=_absolute_path(auth_data["path"], "auth"),
                key_path=tuple(raw_key_path),
            )
            model_catalog = _absolute_path(item["model_catalog"], "model catalog")
    return CodexRoute(
        id=route_id,
        enabled=enabled,
        adapter=cast(RouteAdapter, adapter),
        workloads=workloads,
        probe_timeout_seconds=probe_timeout_seconds,
        attempt_timeout_seconds=attempt_timeout_seconds,
        model=model,
        api=api,
        auth=auth,
        model_catalog=model_catalog,
    )


def _parse_probe(value: object) -> RouteProbePolicy:
    item = _mapping(value, "probe")
    _exact_keys(item, {"reachable_ttl_seconds"}, "probe")
    return RouteProbePolicy(
        reachable_ttl_seconds=_number(
            item["reachable_ttl_seconds"], "probe ttl", minimum=0, maximum=3600
        ),
    )


def _parse_cooldown(value: object) -> RouteCooldownPolicy:
    item = _mapping(value, "cooldown")
    _exact_keys(
        item,
        {"step_seconds", "max_seconds", "half_open_lease_seconds"},
        "cooldown",
    )
    step = _number(item["step_seconds"], "cooldown step", minimum=1, maximum=3600)
    maximum = _number(item["max_seconds"], "cooldown max", minimum=step, maximum=86400)
    lease = _number(item["half_open_lease_seconds"], "cooldown lease", minimum=1, maximum=3600)
    return RouteCooldownPolicy(
        step_seconds=step,
        max_seconds=maximum,
        half_open_lease_seconds=lease,
    )


def _parse_reasoning(value: object) -> str | None:
    """Parse the optional top-level ``reasoning.effort`` override."""

    if value is None:
        return None
    item = _mapping(value, "reasoning")
    _exact_keys(item, {"effort"}, "reasoning")
    effort = item["effort"]
    if not isinstance(effort, str) or effort not in _REASONING_EFFORTS:
        raise CodexRouteConfigError("codex route reasoning effort is invalid")
    return effort


def _read_owner_only_text(path: Path) -> str:
    try:
        parent = path.parent.lstat()
        named = path.lstat()
    except OSError as error:
        raise CodexRouteConfigError("codex route config is unavailable") from error
    if (
        not path.is_absolute()
        or Path(os.path.normpath(path)) != path
        or path.name in {"", ".", ".."}
    ):
        raise CodexRouteConfigError("codex route config path is invalid")
    for ancestor in path.parents:
        if ancestor == Path("/"):
            break
        try:
            if stat.S_ISLNK(ancestor.lstat().st_mode):
                raise CodexRouteConfigError("codex route config path is insecure")
        except OSError as error:
            raise CodexRouteConfigError("codex route config is unavailable") from error
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise CodexRouteConfigError("codex route config directory is insecure")
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o600
        or named.st_nlink != 1
        or not 0 < named.st_size <= _MAX_CONFIG_BYTES
    ):
        raise CodexRouteConfigError("codex route config file is insecure")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(named):
                raise CodexRouteConfigError("codex route config changed while opening")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(8192, _MAX_CONFIG_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_CONFIG_BYTES:
                    raise CodexRouteConfigError("codex route config is too large")
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            try:
                current = path.lstat()
            except OSError as error:
                raise CodexRouteConfigError("codex route config changed while reading") from error
            if _file_identity(after) != _file_identity(opened) or _file_identity(
                current
            ) != _file_identity(opened):
                raise CodexRouteConfigError("codex route config changed while reading")
            if len(data) > _MAX_CONFIG_BYTES:
                raise CodexRouteConfigError("codex route config is too large")
        finally:
            os.close(descriptor)
    except CodexRouteConfigError:
        raise
    except OSError as error:
        raise CodexRouteConfigError("codex route config is unavailable") from error
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise CodexRouteConfigError("codex route config encoding is invalid") from error


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CodexRouteConfigError(f"codex route config {label} is invalid")
    return cast(Mapping[str, object], value)


def _keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> None:
    keys = set(value)
    if not required <= keys or not keys <= allowed:
        raise CodexRouteConfigError(f"codex route config {label} keys are invalid")


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    _keys(value, required=expected, allowed=expected, label=label)


def _slug(value: object, label: str) -> str:
    if label == "route id":
        valid = is_codex_route_id(value)
    else:
        valid = isinstance(value, str) and _SLUG.fullmatch(value) is not None
    if not valid:
        raise CodexRouteConfigError(f"codex {label} is invalid")
    return cast(str, value)


def _base_url(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 512:
        raise CodexRouteConfigError("codex route api base url is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise CodexRouteConfigError("codex route api base url is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or _API_HOST.fullmatch(parsed.hostname) is None
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65_535)
        or parsed.query
        or parsed.fragment
    ):
        raise CodexRouteConfigError("codex route api base url is invalid")
    path = parsed.path.rstrip("/")
    if (
        _API_PATH.fullmatch(path) is None
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise CodexRouteConfigError("codex route api base url is invalid")
    netloc = parsed.hostname.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _number(value: object, label: str, *, minimum: float, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise CodexRouteConfigError(f"codex route {label} is invalid")
    return float(value)


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise CodexRouteConfigError(f"codex route {label} path is invalid")
    path = Path(value)
    if (
        not path.is_absolute()
        or Path(os.path.normpath(path)) != path
        or path.name in {"", ".", ".."}
    ):
        raise CodexRouteConfigError(f"codex route {label} path is invalid")
    return path


def _route_document(route: CodexRoute) -> dict[str, object]:
    document: dict[str, object] = {
        "id": route.id,
        "enabled": route.enabled,
        "adapter": route.adapter,
        "workloads": list(route.workloads),
        "probe_timeout_seconds": route.probe_timeout_seconds,
        "attempt_timeout_seconds": route.attempt_timeout_seconds,
        "model": {"id": route.model.id, "quality": route.model.quality},
    }
    if route.api is not None:
        document["api"] = {"base_url": route.api.base_url}
    if route.auth is not None:
        document["auth"] = {
            "path": str(route.auth.path),
            "key_path": list(route.auth.key_path),
        }
    if route.model_catalog is not None:
        document["model_catalog"] = str(route.model_catalog)
    return document
