"""Acquire, restore, and commit one conversation.

Restore runs before the turn and refuses to serve when the committed blob
cannot be applied. Writeback is serialized per conversation. A commit that
loses the lease or the base revision leaves the blob orphaned and the dirty
bit set.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from gateway.coordination.base import MIN_READER_VERSION, SNAPSHOT_SCHEMA_VERSION, Claim, Manifest
from gateway.coordination.dirty import DirtyTracker, Watermarks
from gateway.coordination.errors import RoutingError, SnapshotError
from gateway.coordination.identity import object_key
from gateway.coordination.snapshot_store import SnapshotBlobs, compress_payload, decompress_payload

logger = logging.getLogger(__name__)


class SessionSync:
    def __init__(
        self, *, router, blobs: SnapshotBlobs, dirty: DirtyTracker, watermarks: Watermarks,
        profile_id: str, scope: str,
    ):
        self.router = router
        self._blobs = blobs
        self.dirty = dirty
        self._watermarks = watermarks
        self.profile_id = profile_id
        self.scope = scope
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._pending_tips: dict[str, str] = {}
        self._fenced: set[str] = set()
        self._db = None
        self._fence_guard = threading.Lock()
        self._compress = compress_payload
        self._decompress = decompress_payload

    def use_codec(self, compress, decompress) -> None:
        """Test hook. Production uses zstd."""
        self._compress = compress
        self._decompress = decompress

    def fence(self, conversation_id: str) -> None:
        with self._fence_guard:
            self._fenced.add(conversation_id)

    def fenced(self, conversation_id: str) -> bool:
        with self._fence_guard:
            return conversation_id in self._fenced

    def note_tip(self, conversation_id: str, session_id: str) -> None:
        if session_id:
            self._pending_tips[conversation_id] = session_id

    def check_session_id(self, claim: Claim, session_id: str | None) -> None:
        """Reject an explicit id that belongs to a different committed conversation.

        With no manifest yet, the first id seen becomes the pending tip so the
        client can echo it before the first snapshot commits. A second, different
        id is rejected. The claim key stays the session key.
        """
        if not session_id:
            return
        known = set()
        if claim.manifest is not None and not claim.manifest.tombstone:
            known |= claim.manifest.known_session_ids()
        pending = self._pending_tips.get(claim.conversation_id)
        if pending:
            known.add(pending)
        if known and session_id not in known:
            raise RoutingError(
                "X-Hermes-Session-Id does not belong to this conversation",
                code="cluster_session_mismatch",
                http_status=409,
            )
        if not known:
            self._pending_tips[claim.conversation_id] = session_id

    def restore(self, db, claim: Claim) -> None:
        """Replace the local lineage from the committed blob, or skip when the watermark matches."""
        manifest = claim.manifest
        if manifest is None:
            return
        if manifest.tombstone:
            raise SnapshotError("conversation is tombstoned and cannot be resumed", code="cluster_tombstone")
        if manifest.schema_version != SNAPSHOT_SCHEMA_VERSION or manifest.min_reader_version > MIN_READER_VERSION:
            raise SnapshotError("snapshot schema is not readable by this build", code="snapshot_schema")
        if self._watermarks.matches(claim.conversation_id, manifest) and not self.dirty.is_dirty(claim.conversation_id):
            if manifest.tip_session_id:
                self._pending_tips[claim.conversation_id] = manifest.tip_session_id
            return
        import time

        from gateway.coordination.metrics import note

        started = time.monotonic()
        body = self._blobs.get_checked(manifest.object_key, manifest.sha256)
        try:
            payload = json.loads(self._decompress(body))
        except Exception as exc:
            raise SnapshotError("snapshot payload is not readable") from exc
        if not isinstance(payload, dict) or payload.get("conversation_id") != claim.conversation_id:
            raise SnapshotError("snapshot conversation id does not match the manifest")
        segments = payload.get("segments") or []
        lineage = [str(item) for item in (payload.get("lineage_session_ids") or [])]
        result = db.import_sessions_replace(
            segments, session_key=claim.session_key, lineage_session_ids=lineage,
        )
        if not result.get("ok"):
            raise SnapshotError("replace import failed: " + str(result.get("errors") or "unknown"))
        self._watermarks.save(claim.conversation_id, manifest)
        if manifest.tip_session_id:
            self._pending_tips[claim.conversation_id] = manifest.tip_session_id
        note("cold_restore_ms", value=(time.monotonic() - started) * 1000)
        note("cold_restore")

    def attach_db(self, db) -> None:
        """Remember the process session store so the dirty scan can flush without a request."""
        if db is not None:
            self._db = db

    def bound_db(self):
        return self._db

    def mark_dirty(self, conversation_id: str) -> None:
        self.dirty.mark(conversation_id)

    def writeback(self, db, claim: Claim) -> bool:
        """Upload one immutable blob and commit the manifest. False keeps the dirty bit."""
        if self.fenced(claim.conversation_id) or not self.dirty.is_dirty(claim.conversation_id):
            return not self.fenced(claim.conversation_id)
        lock = self._lock_for(claim.conversation_id)
        with lock:
            return self._writeback_locked(db, claim)

    def _writeback_locked(self, db, claim: Claim) -> bool:
        from gateway.coordination.metrics import note

        if self.fenced(claim.conversation_id):
            return False
        captured = self.dirty.capture(claim.conversation_id)
        if captured == 0 and not self.dirty.is_dirty(claim.conversation_id):
            return True
        tip = _tip_session(db, claim.session_key)
        if tip is None:
            return True
        exported = db.export_session_lineage(tip, include_inactive=True)
        if not exported:
            note("snapshot_export_empty")
            return False
        fresh = self.router.read_manifest(claim.conversation_id)
        if fresh is not None and fresh.tombstone:
            note("snapshot_rejected_tombstone")
            return False
        base_revision = 0 if fresh is None else fresh.snapshot_revision
        base_reset = 0 if fresh is None else fresh.reset_revision
        owner_epoch = claim.owner.owner_epoch if claim.owner is not None else 0
        payload = _payload(exported, claim, base_reset)
        try:
            body = self._compress(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except Exception:
            note("snapshot_compress_failed")
            logger.warning("cluster snapshot compress failed conversation=%s", claim.conversation_id, exc_info=True)
            return False
        import hashlib

        digest = hashlib.sha256(body).hexdigest()
        key = object_key(self.scope, claim.conversation_id, owner_epoch, base_revision + 1, digest)
        try:
            self._blobs.put(key, body)
        except SnapshotError:
            note("snapshot_upload_failed")
            logger.warning("cluster snapshot upload failed conversation=%s", claim.conversation_id, exc_info=True)
            return False
        manifest = Manifest(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            min_reader_version=MIN_READER_VERSION,
            conversation_id=claim.conversation_id,
            reset_revision=base_reset,
            tip_session_id=str(payload["tip_session_id"]),
            lineage_session_ids=tuple(payload["lineage_session_ids"]),
            owner_epoch=owner_epoch,
            snapshot_revision=base_revision + 1,
            object_key=key,
            sha256=digest,
            exported_at=datetime.now(timezone.utc).isoformat(),
            message_count=int(payload["message_count"]),
            tombstone=False,
        )
        if not self.router.commit_manifest(claim, base_revision, base_reset, manifest):
            note("snapshot_commit_rejected")
            return False
        self.dirty.clear_if_unchanged(claim.conversation_id, captured)
        self._watermarks.save(claim.conversation_id, manifest)
        self._pending_tips[claim.conversation_id] = manifest.tip_session_id
        note("snapshot_committed")
        return True

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(conversation_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[conversation_id] = lock
            return lock


def _payload(exported: dict, claim: Claim, reset_revision: int) -> dict:
    segments = list(exported.get("segments") or [exported])
    lineage = [str(item) for item in (exported.get("lineage_session_ids") or []) if item]
    if not lineage:
        lineage = [str(segment.get("id")) for segment in segments if segment.get("id")]
    tip = str(exported.get("id") or (lineage[-1] if lineage else ""))
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "min_reader_version": MIN_READER_VERSION,
        "conversation_id": claim.conversation_id,
        "reset_revision": reset_revision,
        "tip_session_id": tip,
        "lineage_session_ids": lineage,
        "session_key": claim.session_key,
        "segments": segments,
        "message_count": int(exported.get("message_count") or 0),
    }


def _tip_session(db, session_key: str) -> str | None:
    try:
        row = db.find_latest_gateway_session_for_peer(source="api_server", session_key=session_key)
    except Exception:
        logger.warning("cluster tip lookup failed", exc_info=True)
        return None
    if not row:
        return None
    session_id = row.get("id") if isinstance(row, dict) else None
    return str(session_id) if session_id else None
