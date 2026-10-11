"""In-process atomic router. This is the spec the Redis Lua scripts mirror.

One lock stands in for Redis's single-threaded script execution. Two router
facades can share one store, which is how tests simulate two nodes. Lease
expiry is explicit so a test can move time without sleeping.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

from gateway.coordination.base import Claim, Manifest, Owner
from gateway.coordination.identity import conversation_id_for, conversation_redis_keys, node_address_key, normalize_session_key


class MemoryCoordination:
    """Epoch, lease, and manifest for every conversation, plus node addresses."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._epoch: dict[str, int] = {}
        self._lease: dict[str, tuple[dict, float]] = {}
        self._manifest: dict[str, str] = {}
        self._address: dict[str, tuple[str, float]] = {}

    def now(self) -> float:
        return self._clock()

    def claim(
        self,
        *,
        scope: str,
        session_key: str,
        node_id: str,
        incarnation: str,
        ttl: float,
        accepting: bool,
    ) -> tuple[bool, Owner | None, Manifest | None, bool, bool]:
        """Return ``(is_mine, owner, manifest, acquired, took_over)``.

        A live lease for this incarnation renews without bumping the epoch.
        A free lease bumps the epoch. ``accepting=False`` never takes a free lease.
        ``took_over`` is true only on the epoch increment.
        """
        conversation_id = conversation_id_for(session_key)
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            self._drop_expired_locked()
            manifest = Manifest.from_json(self._manifest.get(slot))
            current = self._lease.get(slot)
            if current is not None:
                body, _expires = current
                owner = Owner(body["node_id"], body["incarnation"], int(body["owner_epoch"]))
                if owner.node_id == node_id and owner.incarnation == incarnation:
                    self._lease[slot] = (body, self.now() + ttl)
                    return True, owner, manifest, True, False
                return False, owner, manifest, False, False
            if not accepting:
                return False, None, manifest, False, False
            epoch = self._epoch.get(slot, 0) + 1
            self._epoch[slot] = epoch
            owner = Owner(node_id, incarnation, epoch)
            self._lease[slot] = (
                {"node_id": node_id, "incarnation": incarnation, "owner_epoch": epoch},
                self.now() + ttl,
            )
            return True, owner, manifest, True, True

    def lookup_owner(self, *, scope: str, session_key: str) -> str | None:
        conversation_id = conversation_id_for(session_key)
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            self._drop_expired_locked()
            current = self._lease.get(slot)
            if current is None:
                return None
            return str(current[0]["node_id"])

    def renew(self, *, scope: str, conversation_id: str, owner: Owner, ttl: float) -> bool:
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            self._drop_expired_locked()
            return self._touch_locked(slot, owner, ttl, delete=False)

    def release(self, *, scope: str, conversation_id: str, owner: Owner) -> bool:
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            self._drop_expired_locked()
            return self._touch_locked(slot, owner, 0, delete=True)

    def commit(
        self,
        *,
        scope: str,
        conversation_id: str,
        owner: Owner,
        expected_snapshot_revision: int,
        expected_reset_revision: int,
        manifest: Manifest,
    ) -> bool:
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            self._drop_expired_locked()
            current = self._lease.get(slot)
            if current is None or not _lease_matches(current[0], owner):
                return False
            stored = Manifest.from_json(self._manifest.get(slot))
            if stored is None:
                if expected_snapshot_revision != 0 or expected_reset_revision != 0:
                    return False
            else:
                if stored.snapshot_revision != expected_snapshot_revision or stored.reset_revision != expected_reset_revision:
                    return False
                if stored.tombstone and not manifest.tombstone:
                    return False
                if manifest.snapshot_revision <= stored.snapshot_revision:
                    return False
            self._manifest[slot] = manifest.to_json()
            return True

    def read_manifest(self, *, scope: str, conversation_id: str) -> Manifest | None:
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            return Manifest.from_json(self._manifest.get(slot))

    def put_address(self, *, scope: str, node_id: str, incarnation: str, address: str, ttl: float) -> None:
        key = node_address_key(scope, node_id, incarnation)
        with self._lock:
            self._address[key] = (address, self.now() + ttl)

    def get_address(self, *, scope: str, node_id: str, incarnation: str) -> str | None:
        key = node_address_key(scope, node_id, incarnation)
        with self._lock:
            self._drop_expired_locked()
            found = self._address.get(key)
            return None if found is None else found[0]

    def seed_manifest(self, *, scope: str, conversation_id: str, manifest: Manifest) -> None:
        """Test hook: install a committed head without a lease."""
        slot = conversation_redis_keys(scope, conversation_id)[0]
        with self._lock:
            self._manifest[slot] = manifest.to_json()

    def _touch_locked(self, slot: str, owner: Owner, ttl: float, *, delete: bool) -> bool:
        current = self._lease.get(slot)
        if current is None or not _lease_matches(current[0], owner):
            return False
        if delete:
            del self._lease[slot]
        else:
            self._lease[slot] = (current[0], self.now() + ttl)
        return True

    def _drop_expired_locked(self) -> None:
        now = self.now()
        for slot, (_body, expires) in list(self._lease.items()):
            if expires <= now:
                del self._lease[slot]
        for key, (_address, expires) in list(self._address.items()):
            if expires <= now:
                del self._address[key]


def _lease_matches(body: dict, owner: Owner) -> bool:
    return (
        body.get("node_id") == owner.node_id
        and body.get("incarnation") == owner.incarnation
        and int(body.get("owner_epoch") or 0) == owner.owner_epoch
    )


class MemorySessionRouter:
    """``SessionRouter`` over :class:`MemoryCoordination` for one node incarnation."""

    def __init__(
        self,
        store: MemoryCoordination,
        *,
        scope: str,
        node_id: str,
        incarnation: str,
    ):
        self._store = store
        self._scope = scope
        self._node_id = node_id
        self._incarnation = incarnation
        self._accepting = True
        self._held: dict[str, Claim] = {}

    def claim(self, session_key: str, node_id: str, ttl: float) -> Claim:
        key = normalize_session_key(session_key)
        # This object is one incarnation. ``node_id`` is the protocol argument
        # and is ignored when it disagrees: a router cannot claim as someone else.
        del node_id
        is_mine, owner, manifest, acquired, took_over = self._store.claim(
            scope=self._scope,
            session_key=key,
            node_id=self._node_id,
            incarnation=self._incarnation,
            ttl=ttl,
            accepting=self._accepting,
        )
        conversation_id = conversation_id_for(key)
        address = None
        if owner is not None and not is_mine:
            address = self._store.get_address(scope=self._scope, node_id=owner.node_id, incarnation=owner.incarnation)
        claim = Claim(
            session_key=key,
            conversation_id=conversation_id,
            is_mine=is_mine,
            owner=owner,
            manifest=manifest,
            address=address,
            acquired=acquired,
            took_over=took_over,
        )
        if is_mine:
            self._held[conversation_id] = claim
        return claim

    def lookup(self, session_key: str) -> str | None:
        return self._store.lookup_owner(scope=self._scope, session_key=normalize_session_key(session_key))

    def renew(self, claim: Claim, ttl: float) -> bool:
        if claim.owner is None:
            return False
        ok = self._store.renew(scope=self._scope, conversation_id=claim.conversation_id, owner=claim.owner, ttl=ttl)
        if ok:
            self._held[claim.conversation_id] = claim
        else:
            self._held.pop(claim.conversation_id, None)
        return ok

    def release(self, claim: Claim) -> bool:
        if claim.owner is None:
            return False
        ok = self._store.release(scope=self._scope, conversation_id=claim.conversation_id, owner=claim.owner)
        if ok:
            self._held.pop(claim.conversation_id, None)
        return ok

    def stop_new_claims(self) -> None:
        self._accepting = False

    def commit_manifest(
        self,
        claim: Claim,
        expected_snapshot_revision: int,
        expected_reset_revision: int,
        manifest: Manifest,
    ) -> bool:
        if claim.owner is None:
            return False
        return self._store.commit(
            scope=self._scope,
            conversation_id=claim.conversation_id,
            owner=claim.owner,
            expected_snapshot_revision=expected_snapshot_revision,
            expected_reset_revision=expected_reset_revision,
            manifest=manifest,
        )

    def read_manifest(self, conversation_id: str) -> Manifest | None:
        return self._store.read_manifest(scope=self._scope, conversation_id=conversation_id)

    def register_address(self, node_id: str, incarnation: str, address: str, ttl: float) -> None:
        self._store.put_address(scope=self._scope, node_id=node_id, incarnation=incarnation, address=address, ttl=ttl)

    def lookup_address(self, node_id: str, incarnation: str) -> str | None:
        return self._store.get_address(scope=self._scope, node_id=node_id, incarnation=incarnation)

    def owned_claims(self) -> list[Claim]:
        return list(self._held.values())


def lease_json(owner: Owner) -> str:
    return json.dumps(
        {"node_id": owner.node_id, "incarnation": owner.incarnation, "owner_epoch": owner.owner_epoch},
        separators=(",", ":"),
    )
