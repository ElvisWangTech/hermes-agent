"""Snapshot commit and replace-import. The returning node must see the other node's turns."""

from __future__ import annotations

import pytest

from gateway.coordination.dirty import DirtyTracker, Watermarks
from gateway.coordination.protocol import MemoryCoordination, MemorySessionRouter
from gateway.coordination.session_sync import SessionSync
from gateway.coordination.snapshot_store import MemoryObjectStore, SnapshotBlobs
from hermes_state import SessionDB


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _sync(router, tmp_path, name: str) -> SessionSync:
    sync = SessionSync(
        router=router, blobs=SnapshotBlobs(MemoryObjectStore()), dirty=DirtyTracker(),
        watermarks=Watermarks(tmp_path / name / "watermarks.json"), profile_id="default", scope="scope",
    )
    sync.use_codec(lambda data: data, lambda data: data)
    return sync


def _pair(tmp_path):
    store = MemoryCoordination()
    blobs = SnapshotBlobs(MemoryObjectStore())
    owner = MemorySessionRouter(store, scope="scope", node_id="a", incarnation="ia")
    other = MemorySessionRouter(store, scope="scope", node_id="b", incarnation="ib")

    def make(router, label):
        sync = SessionSync(
            router=router, blobs=blobs, dirty=DirtyTracker(),
            watermarks=Watermarks(tmp_path / label / "watermarks.json"),
            profile_id="default", scope="scope",
        )
        sync.use_codec(lambda data: data, lambda data: data)
        return sync

    return make(owner, "a"), make(other, "b"), owner, other


def _seed(db: SessionDB, session_id: str, text: str, session_key: str = "chat") -> None:
    db.create_session(session_id, source="api_server", session_key=session_key)
    db.append_message(session_id, "user", text)


def _texts(db: SessionDB, session_id: str) -> list[str]:
    return [row.get("content") for row in db.get_messages(session_id)]


def test_replace_overwrites_and_isolates_uncommitted_fork(tmp_path):
    home_a = SessionDB(db_path=tmp_path / "a.db")
    home_b = SessionDB(db_path=tmp_path / "b.db")
    try:
        _seed(home_a, "s1", "one")
        _seed(home_b, "s1", "stale")
        home_b.create_session("fork", source="api_server", session_key="chat")
        home_b.append_message("fork", "user", "local-only")
        exported = home_a.export_session_lineage("s1", include_inactive=True)
        result = home_b.import_sessions_replace(
            exported["segments"], session_key="chat", lineage_session_ids=exported["lineage_session_ids"],
        )
        assert result["ok"] and result["isolated"] == 1
        assert _texts(home_b, "s1") == ["one"]
        fork = home_b.get_session("fork")
        assert fork["session_key"] in (None, "")
        assert fork["end_reason"] == "cluster_uncommitted_fork"
        home_b.append_message("s1", "assistant", "two")
        exported_b = home_b.export_session_lineage("s1", include_inactive=True)
        back = home_a.import_sessions_replace(
            exported_b["segments"], session_key="chat", lineage_session_ids=exported_b["lineage_session_ids"],
        )
        assert back["ok"]
        assert _texts(home_a, "s1") == ["one", "two"]
    finally:
        home_a.close()
        home_b.close()


def test_dirty_clear_keeps_a_newer_edit():
    dirty = DirtyTracker()
    dirty.mark("c")
    captured = dirty.capture("c")
    dirty.mark("c")
    assert not dirty.clear_if_unchanged("c", captured)
    assert dirty.is_dirty("c")
    assert dirty.clear_if_unchanged("c", dirty.capture("c"))
    assert not dirty.is_dirty("c")


def test_writeback_then_takeover_restores_the_committed_turn(tmp_path):
    sync_a, sync_b, _router_a, router_b = _pair(tmp_path)
    db_a = SessionDB(db_path=tmp_path / "node-a.db")
    db_b = SessionDB(db_path=tmp_path / "node-b.db")
    try:
        _seed(db_a, "s1", "from-a")
        claim = sync_a.router.claim("chat", "a", 30)
        sync_a.mark_dirty(claim.conversation_id)
        assert sync_a.writeback(db_a, claim)
        assert not sync_a.dirty.is_dirty(claim.conversation_id)
        sync_a.router.release(claim)
        taken = router_b.claim("chat", "b", 30)
        assert taken.took_over and taken.manifest is not None
        sync_b.restore(db_b, taken)
        assert _texts(db_b, "s1") == ["from-a"]
        # A second restore with a matching watermark does not need the blob.
        sync_b.restore(db_b, taken)
        assert _texts(db_b, "s1") == ["from-a"]
    finally:
        db_a.close()
        db_b.close()


def test_upload_without_commit_is_not_the_restored_history(tmp_path):
    clock = _Clock()
    store = MemoryCoordination(clock=clock)
    blobs = SnapshotBlobs(MemoryObjectStore())
    router_a = MemorySessionRouter(store, scope="scope", node_id="a", incarnation="ia")
    router_b = MemorySessionRouter(store, scope="scope", node_id="b", incarnation="ib")

    def make(router, label):
        sync = SessionSync(
            router=router, blobs=blobs, dirty=DirtyTracker(),
            watermarks=Watermarks(tmp_path / label / "watermarks.json"),
            profile_id="default", scope="scope",
        )
        sync.use_codec(lambda data: data, lambda data: data)
        return sync

    sync_a, sync_b = make(router_a, "a"), make(router_b, "b")
    db_a = SessionDB(db_path=tmp_path / "a2.db")
    db_b = SessionDB(db_path=tmp_path / "b2.db")
    try:
        _seed(db_a, "s1", "committed")
        claim_a = router_a.claim("chat", "a", 5)
        sync_a.mark_dirty(claim_a.conversation_id)
        assert sync_a.writeback(db_a, claim_a)
        db_a.append_message("s1", "user", "not-yet")
        sync_a.mark_dirty(claim_a.conversation_id)
        clock.t = 5
        claim_b = router_b.claim("chat", "b", 30)
        assert claim_b.took_over
        assert not sync_a.writeback(db_a, claim_a)
        sync_b.restore(db_b, claim_b)
        assert _texts(db_b, "s1") == ["committed"]
    finally:
        db_a.close()
        db_b.close()


def test_explicit_session_id_must_match_the_conversation(tmp_path):
    store = MemoryCoordination()
    router = MemorySessionRouter(store, scope="scope", node_id="a", incarnation="ia")
    sync = SessionSync(
        router=router, blobs=SnapshotBlobs(MemoryObjectStore()), dirty=DirtyTracker(),
        watermarks=Watermarks(tmp_path / "watermarks.json"),
        profile_id="default", scope="scope",
    )
    claim = router.claim("chat", "a", 30)
    sync.check_session_id(claim, "s1")
    sync.check_session_id(claim, "s1")
    with pytest.raises(Exception) as caught:
        sync.check_session_id(claim, "other")
    assert getattr(caught.value, "code", "") == "cluster_session_mismatch"
