"""In-process cluster counters.

Phase 1 records lease, proxy, snapshot, restore, and drain outcomes here and
emits them on the existing monitoring bus when that import works. Labels stay
free of user ids; usage attribution remains behind the opt-in gate.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_counters: dict[str, int] = {}
_gauges: dict[str, float] = {}
_restore_samples_ms: list[float] = []


def note(name: str, *, value: float | None = None, fields: dict[str, Any] | None = None) -> None:
    """Increment ``name``. ``value`` sets a gauge of the same name."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + 1
        if value is not None:
            _gauges[name] = value
        if name == "cold_restore_ms" and value is not None:
            _restore_samples_ms.append(value)
            del _restore_samples_ms[:-256]
    _emit(name, value=value, fields=fields or {})


def mark_gauge(name: str, value: float) -> None:
    with _lock:
        _gauges[name] = value


def snapshot() -> dict[str, Any]:
    """Copy of counters, gauges, and cold-restore P95 in milliseconds."""
    with _lock:
        samples = list(_restore_samples_ms)
        body = {"counters": dict(_counters), "gauges": dict(_gauges)}
    body["cold_restore_p95_ms"] = _p95(samples)
    return body


def reset_metrics() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()
        _restore_samples_ms.clear()


def _p95(samples: list[float]) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def _emit(name: str, *, value: float | None, fields: dict[str, Any]) -> None:
    try:
        from agent.monitoring.emitter import emit
    except Exception:
        return
    try:
        emit({
            "type": "cluster",
            "name": name,
            "value": value,
            "at": time.time(),
            **fields,
        })
    except Exception:
        return
