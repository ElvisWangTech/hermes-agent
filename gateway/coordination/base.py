"""Session affinity types shared by the local, memory, and Redis routers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol

SNAPSHOT_SCHEMA_VERSION = 1
MIN_READER_VERSION = 1


@dataclass(frozen=True)
class Owner:
    """Full lease identity. Renew, release, and commit compare all three fields."""

    node_id: str
    incarnation: str
    owner_epoch: int

    def matches(self, other: "Owner") -> bool:
        return (
            self.node_id == other.node_id
            and self.incarnation == other.incarnation
            and self.owner_epoch == other.owner_epoch
        )


@dataclass(frozen=True)
class Manifest:
    """Committed conversation head. The blob it names is immutable; this record is the only head."""

    schema_version: int
    min_reader_version: int
    conversation_id: str
    reset_revision: int
    tip_session_id: str
    lineage_session_ids: tuple[str, ...]
    owner_epoch: int
    snapshot_revision: int
    object_key: str
    sha256: str
    exported_at: str
    message_count: int
    tombstone: bool = False

    def to_json(self) -> str:
        body = asdict(self)
        body["lineage_session_ids"] = list(self.lineage_session_ids)
        return json.dumps(body, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> "Manifest | None":
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("manifest must be an object")
        lineage = tuple(str(item) for item in (data.get("lineage_session_ids") or []))
        return cls(
            schema_version=int(data.get("schema_version") or 0),
            min_reader_version=int(data.get("min_reader_version") or 0),
            conversation_id=str(data.get("conversation_id") or ""),
            reset_revision=int(data.get("reset_revision") or 0),
            tip_session_id=str(data.get("tip_session_id") or ""),
            lineage_session_ids=lineage,
            owner_epoch=int(data.get("owner_epoch") or 0),
            snapshot_revision=int(data.get("snapshot_revision") or 0),
            object_key=str(data.get("object_key") or ""),
            sha256=str(data.get("sha256") or ""),
            exported_at=str(data.get("exported_at") or ""),
            message_count=int(data.get("message_count") or 0),
            tombstone=bool(data.get("tombstone")),
        )

    def known_session_ids(self) -> set[str]:
        ids = set(self.lineage_session_ids)
        if self.tip_session_id:
            ids.add(self.tip_session_id)
        return ids


@dataclass(frozen=True)
class Claim:
    """Result of ``claim``. ``is_mine`` is this process's incarnation, not merely the same node name."""

    session_key: str
    conversation_id: str
    is_mine: bool
    owner: Owner | None
    manifest: Manifest | None
    address: str | None = None
    # False when drain refused a new ownership. Distinct from "someone else owns it".
    acquired: bool = False
    # True only when this call became owner by incrementing the epoch.
    # A renew of the live lease is the hot cache and must not reload the blob.
    took_over: bool = False


class SessionRouter(Protocol):
    """Affinity primitive. Implementations compare owner, incarnation, and epoch together."""

    def claim(self, session_key: str, node_id: str, ttl: float) -> Claim: ...

    def lookup(self, session_key: str) -> str | None: ...

    def renew(self, claim: Claim, ttl: float) -> bool: ...

    def release(self, claim: Claim) -> bool: ...

    def stop_new_claims(self) -> None: ...

    def commit_manifest(
        self,
        claim: Claim,
        expected_snapshot_revision: int,
        expected_reset_revision: int,
        manifest: Manifest,
    ) -> bool: ...

    def read_manifest(self, conversation_id: str) -> Manifest | None: ...

    def register_address(self, node_id: str, incarnation: str, address: str, ttl: float) -> None: ...

    def lookup_address(self, node_id: str, incarnation: str) -> str | None: ...

    def owned_claims(self) -> list[Claim]: ...
