"""Replace a compression lineage in one transaction.

``import_sessions`` skips ids that already exist. A node that picks up a
conversation after another node appended turns must overwrite those rows, or
the returning node keeps the stale copy. Uncommitted local rows that share the
conversation key and are not in the committed lineage are archived and unbound
so they cannot win a later peer lookup.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SessionReplaceMixin:
    def import_sessions_replace(
        self,
        sessions: list,
        *,
        session_key: str,
        lineage_session_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Overwrite ``sessions`` and rebind ``session_key`` onto that lineage.

        Validation runs before any delete. A rejected payload leaves the local
        rows untouched. The write is one ``_execute_write`` transaction.
        """
        if not isinstance(sessions, list):
            raise ValueError("sessions must be a list")
        if not (session_key or "").strip():
            raise ValueError("session_key is required")
        normalized, errors = self._validate_import_payload(sessions)
        if errors:
            return {"ok": False, "replaced": 0, "isolated": 0, "detached": 0, "errors": errors}
        keep = {str(item) for item in (lineage_session_ids or []) if item}
        keep.update(str(item["session"]["id"]) for item in normalized)

        def _do(conn):
            isolated = _isolate_uncommitted(conn, session_key.strip(), keep)
            parent_updates: list[tuple[str, str]] = []
            replaced: list[str] = []
            for item in normalized:
                raw = item["session"]
                session_id = str(raw.get("id") or "").strip()
                _purge_session(conn, session_id)
                self._import_session_row(conn, raw, item["messages"], session_id)
                parent_id = str(raw.get("parent_session_id") or "").strip()
                if parent_id:
                    parent_updates.append((session_id, parent_id))
                replaced.append(session_id)
            detached = self._attach_import_parents(conn, parent_updates)
            for session_id in replaced:
                conn.execute(
                    "UPDATE sessions SET session_key = ?, source = COALESCE(NULLIF(source, ''), 'api_server') WHERE id = ?",
                    (session_key.strip(), session_id),
                )
            return {
                "ok": True, "replaced": len(replaced), "isolated": isolated, "detached": detached,
                "replaced_ids": replaced, "errors": [],
            }

        return self._execute_write(_do)


def _isolate_uncommitted(conn, session_key: str, keep: set[str]) -> int:
    rows = conn.execute("SELECT id FROM sessions WHERE session_key = ?", (session_key,)).fetchall()
    isolated = 0
    for row in rows:
        session_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        if session_id in keep:
            continue
        conn.execute(
            "UPDATE sessions SET archived = 1, end_reason = ?, session_key = NULL WHERE id = ?",
            ("cluster_uncommitted_fork", session_id),
        )
        isolated += 1
    return isolated


def _purge_session(conn, session_id: str) -> None:
    """Remove one session and its rows so the replace insert does not collide."""
    conn.execute(
        "UPDATE sessions SET parent_session_id = NULL WHERE id = ? OR parent_session_id = ?",
        (session_id, session_id),
    )
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"):
        if not name or not _IDENT.match(name) or name in {"sessions", "messages", "sqlite_sequence"}:
            continue
        if sql and "VIRTUAL" in sql.upper():
            continue
        columns = [info[1] for info in conn.execute(f"PRAGMA table_info({name})")]
        if "session_id" in columns:
            conn.execute(f"DELETE FROM {name} WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
