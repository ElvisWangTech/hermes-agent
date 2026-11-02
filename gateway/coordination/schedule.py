"""Phase 4: wake a paused profile when one of its scheduled jobs is due.

The control plane only wakes the instance. The job itself runs in the profile's own
cron scheduler once the instance is running, and that scheduler's occurrence ledger
keeps it from firing twice. Here an occurrence is claimed before the wake, so a crash
after the claim is not retried on the next tick: it is marked ``interrupted``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from gateway.coordination.authority import ControlPlaneAuthority
from gateway.coordination.errors import ClusterError
from gateway.coordination.instances import InstanceManager


@dataclass(frozen=True)
class Occurrence:
    profile_id: str
    job_id: str
    scheduled_instant: str


class ScheduleSource(Protocol):
    def due(self, now: float) -> list[Occurrence]: ...


class FileScheduleSource:
    """Reads ``cron/jobs.json`` under each profile's node directory. Read-only."""

    def __init__(self, node_root: Path, profiles: Callable[[], list[str]]):
        self._root = node_root
        self._profiles = profiles

    def due(self, now: float) -> list[Occurrence]:
        due: list[Occurrence] = []
        for profile_id in self._profiles():
            path = self._root / profile_id / "cron" / "jobs.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for job in data.get("jobs", []) if isinstance(data, dict) else []:
                if not isinstance(job, dict) or not job.get("enabled", True):
                    continue
                instant = _parse_instant(job.get("next_run_at"))
                if instant is None or instant.timestamp() > now:
                    continue
                due.append(
                    Occurrence(profile_id, str(job.get("id", "")), instant.isoformat())
                )
        return [occurrence for occurrence in due if occurrence.job_id]


class OccurrenceLedger:
    def __init__(self, authority: ControlPlaneAuthority):
        self._authority = authority

    def claim(self, occurrence: Occurrence, now: float) -> bool:
        with self._authority.transaction() as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO cp_occurrences(profile_id, job_id, scheduled_instant, status, claimed_at) "
                "VALUES (?, ?, ?, 'claimed', ?)",
                (
                    occurrence.profile_id,
                    occurrence.job_id,
                    occurrence.scheduled_instant,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def finish(self, occurrence: Occurrence, status: str) -> None:
        if status not in {"woken", "failed"}:
            raise ClusterError(f"unknown occurrence status {status!r}")
        with self._authority.transaction() as database:
            database.execute(
                "UPDATE cp_occurrences SET status = ? WHERE profile_id = ? AND job_id = ? AND scheduled_instant = ?",
                (
                    status,
                    occurrence.profile_id,
                    occurrence.job_id,
                    occurrence.scheduled_instant,
                ),
            )

    def interrupt_claimed(self) -> int:
        """Call once at control-plane start: claims left open by a previous process cannot be known to have woken."""
        with self._authority.transaction() as database:
            cursor = database.execute(
                "UPDATE cp_occurrences SET status = 'interrupted' WHERE status = 'claimed'"
            )
            return cursor.rowcount


class WakeScheduler:
    def __init__(
        self,
        *,
        ledger: OccurrenceLedger,
        source: ScheduleSource,
        manager: InstanceManager,
        clock: Callable[[], float] = time.time,
    ):
        self._ledger = ledger
        self._source = source
        self._manager = manager
        self._clock = clock

    def tick(self) -> list[Occurrence]:
        woken: list[Occurrence] = []
        now = self._clock()
        for occurrence in self._source.due(now):
            if not self._ledger.claim(occurrence, now):
                continue
            try:
                self._manager.ensure_running(occurrence.profile_id)
            except ClusterError:
                self._ledger.finish(occurrence, "failed")
                continue
            self._ledger.finish(occurrence, "woken")
            woken.append(occurrence)
        return woken


def _parse_instant(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
