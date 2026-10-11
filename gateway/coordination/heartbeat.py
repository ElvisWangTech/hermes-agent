"""Lease renewal. A failed renew fences the conversation and cancels its turn.

The protocol guarantees the history commit, not exactly-once tool side effects.
Cancelling the in-flight task is best-effort; work already sent to a tool is
not rolled back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from gateway.coordination.metrics import note

logger = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}
_turns: dict[str, asyncio.Task] = {}


def watch_turn(conversation_id: str, task: asyncio.Task | None) -> None:
    if task is not None:
        _turns[conversation_id] = task


def unwatch_turn(conversation_id: str, task: asyncio.Task | None) -> None:
    if _turns.get(conversation_id) is task:
        _turns.pop(conversation_id, None)


def arm(conversation_id: str, renew: Callable[[], bool], interval: float, on_failure: Callable[[], None]) -> None:
    """Start one renew loop per conversation. A newer arm replaces the previous task."""
    existing = _tasks.get(conversation_id)
    if existing is not None and not existing.done():
        return
    _tasks[conversation_id] = asyncio.create_task(
        _loop(conversation_id, renew, interval, on_failure), name=f"cluster-lease-{conversation_id[:12]}",
    )


async def _loop(conversation_id: str, renew: Callable[[], bool], interval: float, on_failure: Callable[[], None]) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            ok = await asyncio.to_thread(renew)
            if ok:
                continue
            note("lease_renew_failed")
            logger.warning("cluster lease renew failed; fencing conversation=%s", conversation_id)
            on_failure()
            turn = _turns.get(conversation_id)
            if turn is not None and not turn.done():
                turn.cancel()
            return
    except asyncio.CancelledError:
        return


def reset_heartbeat() -> None:
    for task in list(_tasks.values()):
        if not task.done():
            task.cancel()
    _tasks.clear()
    _turns.clear()
