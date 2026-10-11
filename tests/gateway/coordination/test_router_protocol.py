"""Phase 1 affinity: one conversation, one owner, epoch fencing."""

from __future__ import annotations

import pytest

from gateway.coordination.base import Manifest
from gateway.coordination.identity import conversation_redis_keys, scope_token
from gateway.coordination.local import LocalSessionRouter
from gateway.coordination.protocol import MemoryCoordination, MemorySessionRouter


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _router(store, name: str) -> MemorySessionRouter:
    return MemorySessionRouter(store, scope="scope", node_id=name, incarnation=f"inc-{name}")


def _manifest(revision: int, *, tombstone: bool = False, conversation_id: str = "conv") -> Manifest:
    return Manifest(
        schema_version=1, min_reader_version=1, conversation_id=conversation_id,
        reset_revision=0, tip_session_id="s1", lineage_session_ids=("s1",),
        owner_epoch=1, snapshot_revision=revision, object_key=f"k-{revision}",
        sha256=f"h{revision}", exported_at="t", message_count=1, tombstone=tombstone,
    )


def test_local_router_always_owns_and_drain_blocks_new_keys():
    router = LocalSessionRouter(node_id="local")
    first = router.claim("chat", "local", 90)
    assert first.is_mine and first.acquired
    assert router.lookup("chat") == "local"
    router.stop_new_claims()
    assert router.claim("chat", "local", 90).is_mine
    blocked = router.claim("other", "local", 90)
    assert not blocked.is_mine and not blocked.acquired
    assert router.release(first)


def test_second_node_waits_until_the_lease_expires_then_bumps_epoch():
    clock = _Clock()
    store = MemoryCoordination(clock=clock)
    owner = _router(store, "a")
    other = _router(store, "b")
    first = owner.claim("chat", "a", 10)
    assert first.took_over and first.owner.owner_epoch == 1
    again = owner.claim("chat", "a", 10)
    assert again.is_mine and not again.took_over and again.owner.owner_epoch == 1
    blocked = other.claim("chat", "b", 10)
    assert not blocked.is_mine and blocked.owner.node_id == "a" and blocked.owner.owner_epoch == 1
    clock.t = 10
    taken = other.claim("chat", "b", 10)
    assert taken.is_mine and taken.took_over and taken.owner.owner_epoch == 2
    assert not owner.release(first)
    assert other.release(taken)


def test_commit_is_fenced_by_lease_identity_and_revision():
    store = MemoryCoordination()
    owner = _router(store, "a")
    claim = owner.claim("chat", "a", 30)
    assert owner.commit_manifest(claim, 0, 0, _manifest(1, conversation_id=claim.conversation_id))
    assert not owner.commit_manifest(claim, 0, 0, _manifest(2, conversation_id=claim.conversation_id))
    assert owner.commit_manifest(claim, 1, 0, _manifest(2, conversation_id=claim.conversation_id))
    assert claim.owner.owner_epoch == 1
    assert owner.read_manifest(claim.conversation_id).snapshot_revision == 2


def test_stale_owner_cannot_commit_after_takeover():
    clock = _Clock()
    store = MemoryCoordination(clock=clock)
    first = _router(store, "a")
    second = _router(store, "b")
    claim_a = first.claim("chat", "a", 10)
    clock.t = 11
    claim_b = second.claim("chat", "b", 10)
    assert second.commit_manifest(claim_b, 0, 0, _manifest(1, conversation_id=claim_b.conversation_id))
    assert not first.commit_manifest(claim_a, 0, 0, _manifest(1, conversation_id=claim_a.conversation_id))
    stored = second.read_manifest(claim_b.conversation_id)
    assert stored is not None and stored.snapshot_revision == 1


def test_tombstone_is_not_replaced_by_a_live_snapshot():
    store = MemoryCoordination()
    owner = _router(store, "a")
    claim = owner.claim("chat", "a", 30)
    tomb = _manifest(1, tombstone=True, conversation_id=claim.conversation_id)
    store.seed_manifest(scope="scope", conversation_id=claim.conversation_id, manifest=tomb)
    live = _manifest(2, conversation_id=claim.conversation_id)
    assert not owner.commit_manifest(claim, 1, 0, live)


def test_drain_does_not_take_a_free_lease():
    store = MemoryCoordination()
    owner = _router(store, "a")
    held = owner.claim("kept", "a", 30)
    owner.stop_new_claims()
    blocked = owner.claim("fresh", "a", 30)
    assert not blocked.acquired and not blocked.is_mine
    renewed = owner.claim("kept", "a", 30)
    assert renewed.is_mine and renewed.owner.owner_epoch == held.owner.owner_epoch


def test_conversation_keys_share_one_hash_slot():
    keys = conversation_redis_keys(scope_token("ns", "profile"), "abc")
    tags = [key[key.index("{") + 1:key.index("}")] for key in keys]
    assert len(set(tags)) == 1
    assert "/" not in scope_token("ns", "profile")
    assert scope_token("/tmp/a", "p") == scope_token("/tmp/a", "p")
    assert scope_token("ns", "p1") != scope_token("ns", "p2")


def test_empty_session_key_is_rejected():
    router = LocalSessionRouter(node_id="local")
    with pytest.raises(ValueError):
        router.claim("  ", "local", 10)
