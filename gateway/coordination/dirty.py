"""Dirty sequence and the local watermark cache.

The watermark is not a cluster source. It only lets an owner skip a download
when the local transcript was replaced from the committed revision and nothing
has been appended since. A missing file means "download".
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from gateway.coordination.base import Manifest


class DirtyTracker:
    """Monotonic per-conversation sequence. Clearing an old export leaves a newer edit dirty."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._cleared: dict[str, int] = {}

    def mark(self, conversation_id: str) -> int:
        with self._lock:
            value = self._seq.get(conversation_id, 0) + 1
            self._seq[conversation_id] = value
            return value

    def is_dirty(self, conversation_id: str) -> bool:
        with self._lock:
            return self._seq.get(conversation_id, 0) != self._cleared.get(conversation_id, 0)

    def capture(self, conversation_id: str) -> int:
        with self._lock:
            return self._seq.get(conversation_id, 0)

    def clear_if_unchanged(self, conversation_id: str, captured: int) -> bool:
        """Clear only when no edit landed after ``captured``."""
        with self._lock:
            if self._seq.get(conversation_id, 0) != captured:
                return False
            self._cleared[conversation_id] = captured
            return True

    def dirty_ids(self) -> list[str]:
        with self._lock:
            return [key for key, seq in self._seq.items() if seq != self._cleared.get(key, 0)]


class Watermarks:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def matches(self, conversation_id: str, manifest: Manifest) -> bool:
        row = self._read().get(conversation_id)
        if not isinstance(row, dict):
            return False
        return (
            int(row.get("snapshot_revision") or -1) == manifest.snapshot_revision
            and str(row.get("sha256") or "") == manifest.sha256
            and not row.get("dirty")
        )

    def save(self, conversation_id: str, manifest: Manifest) -> None:
        with self._lock:
            body = self._read_unlocked()
            body[conversation_id] = {
                "snapshot_revision": manifest.snapshot_revision,
                "sha256": manifest.sha256,
                "dirty": False,
            }
            self._write_unlocked(body)

    def _read(self) -> dict:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_unlocked(self, body: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)
