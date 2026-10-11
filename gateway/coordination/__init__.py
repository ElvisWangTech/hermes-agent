"""Process-wide cluster entry points. Disabled deployments get the local router.

Routers and session sync are cached per cluster scope, not per local home path,
so two mounts of the same namespace and profile_id share one manifest.
"""

from __future__ import annotations

import logging

from gateway.coordination.config import load_cluster_config, require_cluster_ready, reset_config_cache
from gateway.coordination.dirty import DirtyTracker, Watermarks
from gateway.coordination.heartbeat import reset_heartbeat
from gateway.coordination.identity import process_incarnation, reset_process_incarnation, scope_token
from gateway.coordination.local import LocalSessionRouter
from gateway.coordination.snapshot_store import MemoryObjectStore, S3ObjectStore, SnapshotBlobs

logger = logging.getLogger(__name__)

_local: LocalSessionRouter | None = None
_routers: dict[tuple, object] = {}
_syncs: dict[tuple, object] = {}


def get_router():
    """Local router when the cluster is off. Redis router when it is on and ready."""
    cfg = load_cluster_config()
    if not cfg.enabled:
        return _local_router()
    require_cluster_ready(cfg)
    from gateway.coordination.redis_backend import RedisSessionRouter, redis_client

    key = (cfg.namespace, cfg.profile_id, cfg.redis_url, cfg.resolved_node_id(), process_incarnation())
    router = _routers.get(key)
    if router is None:
        router = RedisSessionRouter(
            redis_client(cfg.redis_url),
            scope=scope_token(cfg.namespace, cfg.profile_id),
            node_id=cfg.resolved_node_id(),
            incarnation=process_incarnation(),
        )
        _routers[key] = router
    return router


def get_session_sync():
    """Snapshot orchestrator for the current profile. Requires an enabled cluster."""
    from gateway.coordination.session_sync import SessionSync

    cfg = require_cluster_ready()
    scope = scope_token(cfg.namespace, cfg.profile_id)
    key = (scope, process_incarnation())
    sync = _syncs.get(key)
    if sync is not None:
        return sync
    router = get_router()
    store = _object_store(cfg)
    from hermes_constants import get_hermes_home

    sync = SessionSync(
        router=router,
        blobs=SnapshotBlobs(store),
        dirty=DirtyTracker(),
        watermarks=Watermarks(get_hermes_home() / "cluster" / "watermarks.json"),
        profile_id=cfg.profile_id,
        scope=scope,
    )
    try:
        router.register_address(
            cfg.resolved_node_id(), process_incarnation(), cfg.resolved_advertise_url(), cfg.affinity.lease_ttl_sec,
        )
    except Exception:
        logger.warning("cluster address registration failed", exc_info=True)
    _syncs[key] = sync
    return sync


def reset_coordination_for_tests() -> None:
    """Drop process caches. Tests that move identity or config call this."""
    global _local
    _local = None
    _routers.clear()
    _syncs.clear()
    reset_config_cache()
    reset_process_incarnation()
    reset_heartbeat()
    from gateway.coordination.drain import reset_drain_flag
    from gateway.coordination.metrics import reset_metrics

    reset_drain_flag()
    reset_metrics()


def _local_router() -> LocalSessionRouter:
    global _local
    if _local is None:
        _local = LocalSessionRouter()
    return _local


def _object_store(cfg):
    store = cfg.object_store
    if store.backend == "s3":
        return S3ObjectStore(
            endpoint=store.endpoint,
            bucket=store.bucket,
            region=store.region,
            addressing_style=store.addressing_style,
            access_key=store.access_key,
            secret_key=store.secret_key,
        )
    return MemoryObjectStore()
