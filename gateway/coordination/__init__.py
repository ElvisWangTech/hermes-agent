"""Control-plane state base for sandbox-per-profile hosting. See docs/distributed/phase1-design.md."""

from __future__ import annotations

from pathlib import Path

from gateway.coordination.authority import ControlPlaneAuthority
from gateway.coordination.config import ClusterConfig, require_cluster_ready
from gateway.coordination.errors import ClusterError
from gateway.coordination.host import NodeHost
from gateway.coordination.objects import ObjectStore, S3ObjectStore
from gateway.coordination.snapshots import SnapshotService


def create_snapshot_service(
    cfg: ClusterConfig,
    *,
    authority: Path,
    host: NodeHost,
    objects: ObjectStore | None = None,
) -> SnapshotService:
    """``authority`` is the control-plane SQLite file, shared with the Pocket authority."""
    if not cfg.enabled:
        raise ClusterError(
            "cluster.enabled is false; the control plane is not configured"
        )
    require_cluster_ready(cfg)
    if objects is None:
        store = cfg.object_store
        objects = S3ObjectStore(
            endpoint=store.endpoint,
            bucket=store.bucket,
            region=store.region,
            addressing_style=store.addressing_style,
            access_key=store.access_key,
            secret_key=store.secret_key,
        )
    return SnapshotService(
        authority=ControlPlaneAuthority(authority),
        objects=objects,
        host=host,
        namespace=cfg.namespace,
        lease_ttl=cfg.lease.ttl_sec,
        max_bytes=int(cfg.snapshot.max_size_mb * 1024 * 1024),
    )
