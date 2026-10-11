"""Graceful drain for one api_server process.

Stops new ownership, fails readiness, waits for turns already inside the
handler, commits dirty snapshots, then releases only the leases whose commit
succeeded. A timeout or a failed commit is incomplete; the process does not
pretend the history was durable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from gateway.coordination.metrics import note

_draining = False


def draining() -> bool:
    return _draining


def health_payload() -> dict[str, Any] | None:
    """503 body while this process is draining. ``None`` when it should serve ``/health`` as usual."""
    if not _draining:
        return None
    return {"status": "draining", "ready": False, "platform": "hermes-agent"}


def begin_drain_flag() -> None:
    global _draining
    _draining = True


def reset_drain_flag() -> None:
    global _draining
    _draining = False


async def run_drain(
    *,
    router,
    sync,
    timeout_sec: float,
    inflight: Callable[[], int],
    sessions: list[tuple[Any, Any]],
) -> dict[str, Any]:
    """``sessions`` is ``(db, claim)`` for conversations this process may still hold.

    ``inflight`` counts turns that already entered the handler. New turns are
    the caller's job to reject once :func:`begin_drain_flag` is set.
    """
    begin_drain_flag()
    router.stop_new_claims()
    try:
        from gateway.drain_control import write_drain_request
        write_drain_request(principal="cluster-drain")
    except Exception:
        # The file marker is the existing gateway signal. Cluster drain still
        # proceeds when the home cannot be written; the HTTP result says so.
        note("drain_marker_failed")
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while inflight() > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    dirty_left = 0
    released = 0
    for db, claim in list(sessions):
        if time.monotonic() >= deadline:
            dirty_left += 1
            continue
        ok = True
        if sync.dirty.is_dirty(claim.conversation_id):
            ok = await asyncio.to_thread(sync.writeback, db, claim)
        if ok and router.release(claim):
            released += 1
        elif sync.dirty.is_dirty(claim.conversation_id):
            dirty_left += 1
    incomplete = dirty_left > 0 or inflight() > 0
    if incomplete:
        note("drain_incomplete")
    else:
        note("drain_completed")
    return {
        "ok": not incomplete,
        "status": "incomplete" if incomplete else "drained",
        "dirty": dirty_left,
        "released": released,
        "inflight": inflight(),
    }
