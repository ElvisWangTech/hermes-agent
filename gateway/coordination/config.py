"""``cluster.*`` for the control plane, read from the same raw YAML the gateway loader reads.

Secrets stay in the profile secret scope (``.env``): ``OBJECT_STORE_ACCESS_KEY`` and
``OBJECT_STORE_SECRET_KEY``. An absent section means disabled.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.coordination.errors import ClusterConfigError

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


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
class LeaseConfig:
    ttl_sec: float = 90.0


@dataclass(frozen=True)
class SnapshotConfig:
    interval_sec: float = 600.0
    max_size_mb: float = 2048.0


@dataclass(frozen=True)
class ClusterConfig:
    enabled: bool = False
    namespace: str = ""
    object_store: ObjectStoreConfig = field(default_factory=ObjectStoreConfig)
    lease: LeaseConfig = field(default_factory=LeaseConfig)
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    drain_timeout_sec: float = 120.0


def load_cluster_config(home: Path) -> ClusterConfig:
    from gateway.config_loader import read_yaml_layers

    try:
        loaded = read_yaml_layers(home) or {}
    except Exception:
        loaded = {}
    raw = loaded.get("cluster") if isinstance(loaded, dict) else None
    return parse_cluster_section(raw, secrets=_read_secrets())


def parse_cluster_section(
    raw: Any, *, secrets: dict[str, str] | None = None
) -> ClusterConfig:
    """Build a config from a mapping. Non-mappings and junk values fall back to defaults."""
    data = raw if isinstance(raw, dict) else {}
    secrets = secrets or {}
    store = _section(data, "object_store")
    lease = _section(data, "lease")
    snapshot = _section(data, "snapshot")
    return ClusterConfig(
        enabled=_as_bool(data.get("enabled"), False),
        namespace=str(data.get("namespace") or "").strip(),
        object_store=ObjectStoreConfig(
            backend=str(store.get("backend") or "none").strip().lower() or "none",
            endpoint=str(store.get("endpoint") or "").strip(),
            bucket=str(store.get("bucket") or "").strip(),
            region=str(store.get("region") or "us-east-1").strip() or "us-east-1",
            addressing_style=str(store.get("addressing_style") or "path").strip()
            or "path",
            access_key=secrets.get("OBJECT_STORE_ACCESS_KEY", ""),
            secret_key=secrets.get("OBJECT_STORE_SECRET_KEY", ""),
        ),
        lease=LeaseConfig(ttl_sec=_as_float(lease.get("ttl_sec"), 90.0)),
        snapshot=SnapshotConfig(
            interval_sec=_as_float(snapshot.get("interval_sec"), 600.0),
            max_size_mb=_as_float(snapshot.get("max_size_mb"), 2048.0),
        ),
        drain_timeout_sec=_as_float(data.get("drain_timeout_sec"), 120.0),
    )


def require_cluster_ready(cfg: ClusterConfig) -> ClusterConfig:
    """Disabled configs pass. An enabled config with anything missing raises."""
    if not cfg.enabled:
        return cfg
    missing = []
    if not cfg.namespace:
        missing.append("cluster.namespace")
    store = cfg.object_store
    if store.backend != "s3":
        missing.append("cluster.object_store.backend=s3")
    else:
        if not store.endpoint:
            missing.append("cluster.object_store.endpoint")
        if not store.bucket:
            missing.append("cluster.object_store.bucket")
        if not store.access_key:
            missing.append("OBJECT_STORE_ACCESS_KEY")
        if not store.secret_key:
            missing.append("OBJECT_STORE_SECRET_KEY")
    if missing:
        raise ClusterConfigError("cluster.enabled requires " + ", ".join(missing))
    for module in ("boto3", "zstandard"):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise ClusterConfigError(
                f"cluster.enabled requires the optional dependency {module!r} "
                f"(install the 'cluster' extra). Import failed: {exc}"
            ) from exc
    return cfg


def _read_secrets() -> dict[str, str]:
    return {
        name: _secret(name)
        for name in ("OBJECT_STORE_ACCESS_KEY", "OBJECT_STORE_SECRET_KEY")
    }


def _secret(name: str) -> str:
    import os

    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:
        return os.environ.get(name, "").strip()
    try:
        value = get_secret(name, "")
    except UnscopedSecretError:
        return ""
    return str(value or "").strip()


def _section(data: dict, name: str) -> dict:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


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
    return parsed if parsed > 0 else default
