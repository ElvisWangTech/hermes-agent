"""Redis SessionRouter. Lua is the atomic form of ``protocol.MemoryCoordination``.

Every key in one script shares the conversation hash tag. A lost lease or a
moved manifest makes renew, release, and commit return false; callers fence
instead of writing unconditionally.
"""

from __future__ import annotations

import logging

from gateway.coordination.base import Claim, Manifest, Owner
from gateway.coordination.identity import (
    conversation_id_for,
    conversation_redis_keys,
    node_address_key,
    normalize_session_key,
)

logger = logging.getLogger(__name__)

# 1 = we hold the lease, 0 = someone else does. Manifest may be empty.
_CLAIM = """
local epoch_key, lease_key, manifest_key = KEYS[1], KEYS[2], KEYS[3]
local node_id, incarnation, ttl_ms = ARGV[1], ARGV[2], ARGV[3]
local manifest = redis.call('GET', manifest_key) or ''
local raw = redis.call('GET', lease_key)
if raw then
  local lease = cjson.decode(raw)
  if lease['node_id'] == node_id and lease['incarnation'] == incarnation then
    redis.call('PEXPIRE', lease_key, ttl_ms)
    return {1, tonumber(lease['owner_epoch']), manifest, node_id, incarnation, 0}
  end
  return {0, tonumber(lease['owner_epoch']), manifest, lease['node_id'], lease['incarnation'], 0}
end
local epoch = tonumber(redis.call('GET', epoch_key) or '0') + 1
redis.call('SET', epoch_key, tostring(epoch))
redis.call('SET', lease_key, cjson.encode({node_id=node_id, incarnation=incarnation, owner_epoch=epoch}), 'PX', ttl_ms)
return {1, epoch, manifest, node_id, incarnation, 1}
"""

_RENEW = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local lease = cjson.decode(raw)
if lease['node_id'] ~= ARGV[1] or lease['incarnation'] ~= ARGV[2] or tonumber(lease['owner_epoch']) ~= tonumber(ARGV[3]) then
  return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return 1
"""

_RELEASE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local lease = cjson.decode(raw)
if lease['node_id'] ~= ARGV[1] or lease['incarnation'] ~= ARGV[2] or tonumber(lease['owner_epoch']) ~= tonumber(ARGV[3]) then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""

# Reject when the lease identity moved or the committed revision is no longer the expected base.
_COMMIT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local lease = cjson.decode(raw)
if lease['node_id'] ~= ARGV[1] or lease['incarnation'] ~= ARGV[2] or tonumber(lease['owner_epoch']) ~= tonumber(ARGV[3]) then
  return 0
end
local expected_rev = tonumber(ARGV[4])
local expected_reset = tonumber(ARGV[5])
local incoming = cjson.decode(ARGV[6])
local current = redis.call('GET', KEYS[2])
if current then
  local man = cjson.decode(current)
  if tonumber(man['snapshot_revision']) ~= expected_rev or tonumber(man['reset_revision']) ~= expected_reset then
    return 0
  end
  if man['tombstone'] and not incoming['tombstone'] then
    return 0
  end
  if tonumber(incoming['snapshot_revision']) <= tonumber(man['snapshot_revision']) then
    return 0
  end
else
  if expected_rev ~= 0 or expected_reset ~= 0 then
    return 0
  end
end
redis.call('SET', KEYS[2], ARGV[6])
return 1
"""


class RedisSessionRouter:
    """One node incarnation against a shared Redis."""

    def __init__(self, client, *, scope: str, node_id: str, incarnation: str):
        self._client = client
        self._scope = scope
        self._node_id = node_id
        self._incarnation = incarnation
        self._accepting = True
        self._held: dict[str, Claim] = {}

    def claim(self, session_key: str, node_id: str, ttl: float) -> Claim:
        del node_id
        key = normalize_session_key(session_key)
        conversation_id = conversation_id_for(key)
        epoch_key, lease_key, manifest_key = conversation_redis_keys(self._scope, conversation_id)
        # Drain must not take a free lease. Renew in place when we already hold it.
        if not self._accepting:
            owner, manifest = self._read_lease(lease_key, manifest_key)
            if owner is not None and owner.node_id == self._node_id and owner.incarnation == self._incarnation:
                held = Claim(key, conversation_id, True, owner, manifest, None, acquired=True, took_over=False)
                ok = self.renew(held, ttl)
                return Claim(key, conversation_id, ok, owner if ok else None, manifest, None, acquired=ok, took_over=False)
            address = None if owner is None else self.lookup_address(owner.node_id, owner.incarnation)
            return Claim(key, conversation_id, False, owner, manifest, address, acquired=False, took_over=False)
        raw = self._client.eval(
            _CLAIM, 3, epoch_key, lease_key, manifest_key,
            self._node_id, self._incarnation, str(int(ttl * 1000)),
        )
        is_mine = int(raw[0]) == 1
        owner = Owner(str(raw[3]), str(raw[4]), int(raw[1]))
        manifest = _manifest_or_none(raw[2])
        took_over = len(raw) > 5 and int(raw[5]) == 1
        address = None if is_mine else self.lookup_address(owner.node_id, owner.incarnation)
        claim = Claim(key, conversation_id, is_mine, owner, manifest, address, acquired=is_mine, took_over=took_over)
        if is_mine:
            self._held[conversation_id] = claim
        return claim

    def lookup(self, session_key: str) -> str | None:
        key = normalize_session_key(session_key)
        _epoch, lease_key, _manifest = conversation_redis_keys(self._scope, conversation_id_for(key))
        owner, _manifest_obj = self._read_lease(lease_key, _manifest)
        return None if owner is None else owner.node_id

    def renew(self, claim: Claim, ttl: float) -> bool:
        if claim.owner is None:
            return False
        _epoch, lease_key, _manifest = conversation_redis_keys(self._scope, claim.conversation_id)
        ok = int(self._client.eval(
            _RENEW, 1, lease_key,
            claim.owner.node_id, claim.owner.incarnation, str(claim.owner.owner_epoch), str(int(ttl * 1000)),
        )) == 1
        if ok:
            self._held[claim.conversation_id] = claim
        else:
            self._held.pop(claim.conversation_id, None)
            logger.warning("cluster lease renew failed conversation=%s", claim.conversation_id)
        return ok

    def release(self, claim: Claim) -> bool:
        if claim.owner is None:
            return False
        _epoch, lease_key, _manifest = conversation_redis_keys(self._scope, claim.conversation_id)
        ok = int(self._client.eval(
            _RELEASE, 1, lease_key,
            claim.owner.node_id, claim.owner.incarnation, str(claim.owner.owner_epoch),
        )) == 1
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
        _epoch, lease_key, manifest_key = conversation_redis_keys(self._scope, claim.conversation_id)
        ok = int(self._client.eval(
            _COMMIT, 2, lease_key, manifest_key,
            claim.owner.node_id, claim.owner.incarnation, str(claim.owner.owner_epoch),
            str(expected_snapshot_revision), str(expected_reset_revision), manifest.to_json(),
        )) == 1
        if ok:
            self._held[claim.conversation_id] = Claim(
                claim.session_key, claim.conversation_id, True, claim.owner, manifest, claim.address, True,
            )
        return ok

    def read_manifest(self, conversation_id: str) -> Manifest | None:
        _epoch, _lease, manifest_key = conversation_redis_keys(self._scope, conversation_id)
        return _manifest_or_none(self._client.get(manifest_key))

    def register_address(self, node_id: str, incarnation: str, address: str, ttl: float) -> None:
        self._client.set(node_address_key(self._scope, node_id, incarnation), address, px=int(ttl * 1000))

    def lookup_address(self, node_id: str, incarnation: str) -> str | None:
        value = self._client.get(node_address_key(self._scope, node_id, incarnation))
        return str(value) if value else None

    def owned_claims(self) -> list[Claim]:
        return list(self._held.values())

    def _read_lease(self, lease_key: str, manifest_key: str) -> tuple[Owner | None, Manifest | None]:
        raw = self._client.get(lease_key)
        manifest = _manifest_or_none(self._client.get(manifest_key))
        if not raw:
            return None, manifest
        import json
        body = json.loads(raw)
        return Owner(str(body["node_id"]), str(body["incarnation"]), int(body["owner_epoch"])), manifest


def _manifest_or_none(raw) -> Manifest | None:
    if not raw:
        return None
    return Manifest.from_json(raw if isinstance(raw, str) else raw.decode("utf-8"))


def redis_client(url: str):
    """Lazy redis client. Importing this module does not import redis."""
    import redis

    return redis.Redis.from_url(url, decode_responses=True)
