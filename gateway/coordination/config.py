"""``cluster.*`` from the same raw YAML the gateway loader reads.

Secrets stay in the profile secret scope (``.env``): ``REDIS_URL``,
``CLUSTER_INTERNAL_TOKEN``, ``OBJECT_STORE_ACCESS_KEY``, ``OBJECT_STORE_SECRET_KEY``.
Absent YAML means disabled. This parser does not validate dependencies; startup
does, and only when ``enabled`` is true.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from gateway.coordination.identity import resolve_node_id

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
_cache: dict[str, tuple] = {}


@dataclass(frozen=True)
class ObjectStoreConfig:
    backend: str = "none"
    endpoint: str = ""
    bucket: str = ""
    region: str = "us-east-1"
    addressing_style: str = "path"
    access_key: str = ""
    secret_key: str = ""


@dataclass(frozen=True)
class AffinityConfig:
    lease_ttl_sec: float = 90.0
    heartbeat_sec: float = 30.0


@dataclass(frozen=True)
class SnapshotConfig:
    on_turn_end: bool = True
    dirty_periodic_sec: float = 300.0


@dataclass(frozen=True)
class ProxyConfig:
    timeout_sec: float = 600.0
    allow_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClusterConfig:
    enabled: bool = False
    namespace: str = ""
    profile_id: str = ""
    node_id: str = ""
    advertise_url: str = ""
    object_store: ObjectStoreConfig = field(default_factory=ObjectStoreConfig)
    affinity: AffinityConfig = field(default_factory=AffinityConfig)
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    drain_timeout_sec: float = 120.0
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    redis_url: str = ""
    internal_token: str = ""

    def resolved_node_id(self) -> str:
        return resolve_node_id(self.node_id)

    def resolved_advertise_url(self) -> str:
        explicit = (self.advertise_url or "").strip()
        if explicit:
            return explicit.rstrip("/")
        port = os.environ.get("API_SERVER_PORT", "8642").strip() or "8642"
        return f"http://{self.resolved_node_id()}:{port}"


def cluster_enabled() -> bool:
    return load_cluster_config().enabled


def load_cluster_config() -> ClusterConfig:
    """Parse the current profile's YAML. Cached on home path and file stamp."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    path = home / "config.yaml"
    try:
        stat = path.stat()
        stamp = (str(home), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (str(home), None, None)
    cached = _cache.get("cfg")
    if cached and cached[0] == stamp:
        return cached[1]
    raw = _read_cluster_section(home)
    cfg = parse_cluster_section(raw, secrets=_read_secrets())
    _cache["cfg"] = (stamp, cfg)
    return cfg


def parse_cluster_section(raw: Any, *, secrets: dict[str, str] | None = None) -> ClusterConfig:
    """Build a config from a mapping. Non-mappings and junk values become the disabled default."""
    data = raw if isinstance(raw, dict) else {}
    secrets = secrets or {}
    store = data.get("object_store") if isinstance(data.get("object_store"), dict) else {}
    affinity = data.get("affinity") if isinstance(data.get("affinity"), dict) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
    proxy = data.get("proxy") if isinstance(data.get("proxy"), dict) else {}
    hosts = proxy.get("allow_hosts") or []
    if isinstance(hosts, str):
        hosts = [hosts]
    allow = tuple(str(item).strip().lower() for item in hosts if str(item).strip()) if isinstance(hosts, list) else ()
    return ClusterConfig(
        enabled=_as_bool(data.get("enabled"), False),
        namespace=str(data.get("namespace") or "").strip(),
        profile_id=str(data.get("profile_id") or "").strip(),
        node_id=str(data.get("node_id") or "").strip(),
        advertise_url=str(data.get("advertise_url") or "").strip(),
        object_store=ObjectStoreConfig(
            backend=str(store.get("backend") or "none").strip().lower() or "none",
            endpoint=str(store.get("endpoint") or "").strip(),
            bucket=str(store.get("bucket") or "").strip(),
            region=str(store.get("region") or "us-east-1").strip() or "us-east-1",
            addressing_style=str(store.get("addressing_style") or "path").strip() or "path",
            access_key=secrets.get("OBJECT_STORE_ACCESS_KEY", ""),
            secret_key=secrets.get("OBJECT_STORE_SECRET_KEY", ""),
        ),
        affinity=AffinityConfig(
            lease_ttl_sec=_as_float(affinity.get("lease_ttl_sec"), 90.0),
            heartbeat_sec=_as_float(affinity.get("heartbeat_sec"), 30.0),
        ),
        snapshot=SnapshotConfig(
            on_turn_end=_as_bool(snapshot.get("on_turn_end"), True),
            dirty_periodic_sec=_as_float(snapshot.get("dirty_periodic_sec"), 300.0),
        ),
        drain_timeout_sec=_as_float(data.get("drain_timeout_sec"), 120.0),
        proxy=ProxyConfig(
            timeout_sec=_as_float(proxy.get("timeout_sec"), 600.0),
            allow_hosts=allow,
        ),
        redis_url=secrets.get("REDIS_URL", ""),
        internal_token=secrets.get("CLUSTER_INTERNAL_TOKEN", ""),
    )


def require_cluster_ready(cfg: ClusterConfig | None = None) -> ClusterConfig:
    """Fail closed when the cluster is enabled but a required piece is missing.

    Disabled configs return immediately. An enabled config with a missing extra
    raises rather than running half-open.
    """
    from gateway.coordination.errors import ClusterConfigError

    cfg = load_cluster_config() if cfg is None else cfg
    if not cfg.enabled:
        return cfg
    missing = []
    if not cfg.namespace:
        missing.append("cluster.namespace")
    if not cfg.profile_id:
        missing.append("cluster.profile_id")
    if not cfg.redis_url:
        missing.append("REDIS_URL")
    if not cfg.internal_token:
        missing.append("CLUSTER_INTERNAL_TOKEN")
    store = cfg.object_store
    if store.backend == "s3":
        if not store.endpoint:
            missing.append("cluster.object_store.endpoint")
        if not store.bucket:
            missing.append("cluster.object_store.bucket")
        if not store.access_key:
            missing.append("OBJECT_STORE_ACCESS_KEY")
        if not store.secret_key:
            missing.append("OBJECT_STORE_SECRET_KEY")
    elif store.backend not in {"s3", "none"}:
        missing.append("cluster.object_store.backend")
    if missing:
        raise ClusterConfigError("cluster.enabled requires " + ", ".join(missing))
    _require_extra("redis", "redis")
    _require_extra("zstandard", "zstandard")
    if store.backend == "s3":
        _require_extra("boto3", "boto3")
    return cfg


def reset_config_cache() -> None:
    _cache.clear()


def _require_extra(module: str, distribution: str) -> None:
    import importlib

    from gateway.coordination.errors import ClusterConfigError

    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise ClusterConfigError(
            f"cluster.enabled requires the optional dependency {distribution!r} "
            f"(install the 'cluster' extra). Import failed: {exc}"
        ) from exc


def _read_cluster_section(home) -> Any:
    from gateway.config_loader import read_yaml_layers

    try:
        loaded = read_yaml_layers(home) or {}
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded.get("cluster")


def _read_secrets() -> dict[str, str]:
    names = (
        "REDIS_URL",
        "CLUSTER_INTERNAL_TOKEN",
        "OBJECT_STORE_ACCESS_KEY",
        "OBJECT_STORE_SECRET_KEY",
    )
    return {name: _secret(name) for name in names}


def _secret(name: str) -> str:
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:
        return os.environ.get(name, "").strip()
    try:
        value = get_secret(name, "")
    except UnscopedSecretError:
        return ""
    return str(value or "").strip()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
        return default
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed
