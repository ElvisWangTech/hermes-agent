"""Control-plane authority: instance leases and committed snapshot manifests.

Single-node deployments keep this in one SQLite file. It is the same file as the
Pocket authority (``pocket-auth.sqlite``), so role, device, and instance state
commit in one place. A multi-replica control plane replaces this backend; the
callers and the fencing rules do not change.

Every write checks the lease in the same transaction that changes state. A lease
holder whose lease expired, or whose incarnation was replaced, cannot commit.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from gateway.coordination.errors import LeaseError, SnapshotError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cp_instances (
    profile_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    lease_expires REAL NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cp_api_keys (
    key_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    profile_id TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cp_usage (
    execution_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cp_usage_profile_day ON cp_usage(profile_id, day);
CREATE TABLE IF NOT EXISTS cp_occurrences (
    profile_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    scheduled_instant TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (profile_id, job_id, scheduled_instant)
);
CREATE TABLE IF NOT EXISTS cp_manifests (
    profile_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    object_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    min_reader_version INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Lease:
    profile_id: str
    instance_id: str
    incarnation: str


@dataclass(frozen=True)
class Manifest:
    profile_id: str
    revision: int
    object_key: str
    sha256: str
    size_bytes: int
    schema_version: int
    min_reader_version: int
    instance_id: str
    created_at: float


@dataclass(frozen=True)
class InstanceState:
    instance_id: str
    incarnation: str
    lease_expires: float
    dirty: bool


class ControlPlaneAuthority:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as database:
            database.executescript(_SCHEMA)
        path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        database.row_factory = sqlite3.Row
        try:
            yield database
        finally:
            database.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                yield database
            except BaseException:
                database.execute("ROLLBACK")
                raise
            database.execute("COMMIT")

    def acquire(self, profile_id: str, instance_id: str, ttl: float) -> Lease:
        """Take the profile's lease. Refused while another incarnation's lease is live."""
        now = self._clock()
        lease = Lease(profile_id, instance_id, secrets.token_hex(16))
        with self.transaction() as database:
            row = database.execute(
                "SELECT lease_expires FROM cp_instances WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if row is not None and row["lease_expires"] > now:
                raise LeaseError(
                    f"profile {profile_id!r} already has a live instance lease"
                )
            database.execute(
                """
                INSERT INTO cp_instances(profile_id, instance_id, incarnation, lease_expires, dirty)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(profile_id) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    incarnation = excluded.incarnation,
                    lease_expires = excluded.lease_expires,
                    dirty = 0
                """,
                (profile_id, instance_id, lease.incarnation, now + ttl),
            )
        return lease

    def renew(self, lease: Lease, ttl: float) -> bool:
        """Extend a live lease. An expired lease is never revived: its holder must stop."""
        now = self._clock()
        with self.transaction() as database:
            cursor = database.execute(
                """
                UPDATE cp_instances SET lease_expires = ?
                WHERE profile_id = ? AND incarnation = ? AND lease_expires > ?
                """,
                (now + ttl, lease.profile_id, lease.incarnation, now),
            )
            return cursor.rowcount == 1

    def release(self, lease: Lease) -> None:
        with self.transaction() as database:
            database.execute(
                "UPDATE cp_instances SET lease_expires = 0 WHERE profile_id = ? AND incarnation = ?",
                (lease.profile_id, lease.incarnation),
            )

    def require(self, lease: Lease) -> None:
        with self.connect() as database:
            _require_live(database, lease, self._clock())

    def mark_dirty(self, lease: Lease) -> None:
        with self.transaction() as database:
            _require_live(database, lease, self._clock())
            database.execute(
                "UPDATE cp_instances SET dirty = 1 WHERE profile_id = ?",
                (lease.profile_id,),
            )

    def instance_state(self, profile_id: str) -> InstanceState | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT instance_id, incarnation, lease_expires, dirty FROM cp_instances WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return InstanceState(
            row["instance_id"],
            row["incarnation"],
            row["lease_expires"],
            bool(row["dirty"]),
        )

    def read_manifest(self, profile_id: str) -> Manifest | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT * FROM cp_manifests WHERE profile_id = ?", (profile_id,)
            ).fetchone()
        return None if row is None else _manifest(row)

    def commit(
        self, lease: Lease, expected_revision: int, manifest: Manifest
    ) -> Manifest:
        """Publish ``manifest`` as the new head. Only the live holder, only on the revision it read."""
        if (
            manifest.revision != expected_revision + 1
            or manifest.profile_id != lease.profile_id
        ):
            raise SnapshotError(
                "manifest does not extend the revision it was built from"
            )
        with self.transaction() as database:
            _require_live(database, lease, self._clock())
            row = database.execute(
                "SELECT revision FROM cp_manifests WHERE profile_id = ?",
                (lease.profile_id,),
            ).fetchone()
            current = row["revision"] if row is not None else 0
            if current != expected_revision:
                raise SnapshotError(
                    f"manifest moved from revision {expected_revision} to {current}"
                )
            database.execute(
                """
                INSERT INTO cp_manifests(profile_id, revision, object_key, sha256, size_bytes,
                    schema_version, min_reader_version, instance_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    revision = excluded.revision, object_key = excluded.object_key,
                    sha256 = excluded.sha256, size_bytes = excluded.size_bytes,
                    schema_version = excluded.schema_version,
                    min_reader_version = excluded.min_reader_version,
                    instance_id = excluded.instance_id, created_at = excluded.created_at
                """,
                (
                    manifest.profile_id,
                    manifest.revision,
                    manifest.object_key,
                    manifest.sha256,
                    manifest.size_bytes,
                    manifest.schema_version,
                    manifest.min_reader_version,
                    manifest.instance_id,
                    manifest.created_at,
                ),
            )
            database.execute(
                "UPDATE cp_instances SET dirty = 0 WHERE profile_id = ?",
                (lease.profile_id,),
            )
        return manifest


def _require_live(database: sqlite3.Connection, lease: Lease, now: float) -> None:
    row = database.execute(
        "SELECT incarnation, lease_expires FROM cp_instances WHERE profile_id = ?",
        (lease.profile_id,),
    ).fetchone()
    if (
        row is None
        or row["incarnation"] != lease.incarnation
        or row["lease_expires"] <= now
    ):
        raise LeaseError(f"lease for profile {lease.profile_id!r} is not live")


def _manifest(row: sqlite3.Row) -> Manifest:
    return Manifest(
        profile_id=row["profile_id"],
        revision=row["revision"],
        object_key=row["object_key"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        schema_version=row["schema_version"],
        min_reader_version=row["min_reader_version"],
        instance_id=row["instance_id"],
        created_at=row["created_at"],
    )
