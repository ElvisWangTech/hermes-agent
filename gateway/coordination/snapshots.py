"""Snapshot, restore, and drain for one profile's node directory.

A snapshot pauses the instance, copies its directory, uploads an immutable object,
and commits the manifest under the lease. Restore runs before an instance mounts the
directory and swaps in the committed tree. An instance whose lease is no longer live
is stopped; it cannot commit or keep writing.
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import quote

from gateway.coordination.archive import pack_tree, unpack_tree
from gateway.coordination.authority import ControlPlaneAuthority, Lease, Manifest
from gateway.coordination.errors import ClusterError, LeaseError, SnapshotError
from gateway.coordination.host import NodeHost
from gateway.coordination.objects import ObjectStore, get_verified, put_verified

SCHEMA_VERSION = 1
READER_VERSION = 1


@dataclass(frozen=True)
class DrainReport:
    committed: tuple[str, ...]
    incomplete: tuple[tuple[str, str], ...]


def snapshot_object_key(
    namespace: str, profile_id: str, revision: int, sha256: str
) -> str:
    return (
        f"hermes/{quote(namespace, safe='')}/{quote(profile_id, safe='')}"
        f"/snapshots/{revision:012d}.{sha256}.tar.zst"
    )


class SnapshotService:
    def __init__(
        self,
        *,
        authority: ControlPlaneAuthority,
        objects: ObjectStore,
        host: NodeHost,
        namespace: str,
        lease_ttl: float,
        max_bytes: int,
        clock: Callable[[], float] = time.time,
    ):
        self._authority = authority
        self._objects = objects
        self._host = host
        self._namespace = namespace
        self._lease_ttl = lease_ttl
        self._max_bytes = max_bytes
        self._clock = clock
        self._draining = False

    def acquire(self, profile_id: str, instance_id: str) -> Lease:
        if self._draining:
            raise ClusterError("control plane is draining; no new instance leases")
        return self._authority.acquire(profile_id, instance_id, self._lease_ttl)

    def renew(self, lease: Lease) -> bool:
        return self._authority.renew(lease, self._lease_ttl)

    def release(self, lease: Lease) -> None:
        self._authority.release(lease)

    def note_activity(self, lease: Lease) -> None:
        self._authority.mark_dirty(lease)

    def snapshot(self, lease: Lease, *, resume: bool = True) -> Manifest:
        """Commit a new revision. ``resume=False`` leaves the instance paused for the caller to stop."""
        profile_id = lease.profile_id
        self._require_or_stop(lease)
        self._host.pause(lease.instance_id)
        try:
            payload = pack_tree(
                self._host.state_dir(profile_id), max_bytes=self._max_bytes
            )
            current = self._authority.read_manifest(profile_id)
            revision = (current.revision if current else 0) + 1
            digest = hashlib.sha256(payload).hexdigest()
            key = snapshot_object_key(self._namespace, profile_id, revision, digest)
            put_verified(self._objects, key, payload)
            manifest = Manifest(
                profile_id=profile_id,
                revision=revision,
                object_key=key,
                sha256=digest,
                size_bytes=len(payload),
                schema_version=SCHEMA_VERSION,
                min_reader_version=READER_VERSION,
                instance_id=lease.instance_id,
                created_at=self._clock(),
            )
            committed = self._authority.commit(lease, revision - 1, manifest)
        except LeaseError:
            self._host.stop(lease.instance_id)
            raise
        except Exception as exc:
            self._host.resume(lease.instance_id)
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError(f"snapshot failed: {exc}") from exc
        if resume:
            self._host.resume(lease.instance_id)
        return committed

    def snapshot_if_due(self, lease: Lease, interval: float) -> Manifest | None:
        state = self._authority.instance_state(lease.profile_id)
        if state is None or not state.dirty:
            return None
        current = self._authority.read_manifest(lease.profile_id)
        if current is not None and self._clock() - current.created_at < interval:
            return None
        return self.snapshot(lease)

    def restore(self, lease: Lease) -> bool:
        """Replace the node directory with the committed tree. False when nothing is committed yet."""
        profile_id = lease.profile_id
        self._require_or_stop(lease)
        manifest = self._authority.read_manifest(profile_id)
        if manifest is None:
            return False
        if manifest.min_reader_version > READER_VERSION:
            raise SnapshotError(
                f"manifest needs reader version {manifest.min_reader_version}; this control plane reads {READER_VERSION}"
            )
        body = get_verified(self._objects, manifest.object_key, manifest.sha256)
        dest = self._host.state_dir(profile_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.parent / f".{dest.name}.restore-{secrets.token_hex(4)}"
        unpack_tree(body, staging)
        try:
            self._require_or_stop(lease)
        except LeaseError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if dest.exists():
            dest.rename(dest.parent / f"{dest.name}.quarantine-{time.time_ns()}")
        staging.rename(dest)
        return True

    def drain(self, leases: Iterable[Lease]) -> DrainReport:
        """Stop new leases, commit dirty state, stop each instance, release its lease."""
        self._draining = True
        committed: list[str] = []
        incomplete: list[tuple[str, str]] = []
        for lease in leases:
            try:
                state = self._authority.instance_state(lease.profile_id)
                if state is not None and state.dirty:
                    self.snapshot(lease, resume=False)
                self._host.stop(lease.instance_id)
                self._authority.release(lease)
            except (ClusterError, OSError) as exc:
                incomplete.append((lease.profile_id, str(exc)))
            else:
                committed.append(lease.profile_id)
        return DrainReport(tuple(committed), tuple(incomplete))

    def _require_or_stop(self, lease: Lease) -> None:
        try:
            self._authority.require(lease)
        except LeaseError:
            self._host.stop(lease.instance_id)
            raise
