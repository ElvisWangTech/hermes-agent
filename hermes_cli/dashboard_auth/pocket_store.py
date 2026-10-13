"""Single-host authority for profile-bound Pocket device credentials."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class PocketAuthority:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS pocket_roles (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS pocket_codes (
                    digest TEXT PRIMARY KEY, role_id TEXT NOT NULL,
                    expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS pocket_devices (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
                    role_id TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
            """)
        path.chmod(0o600)

    @contextmanager
    def _connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        try:
            with database:
                yield database
        finally:
            database.close()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def issue_code(self, profile: str, *, ttl: int = 600) -> dict:
        if not profile or not 1 <= ttl <= 3600:
            raise ValueError("Invalid profile or code lifetime")
        code = "pc_" + secrets.token_urlsafe(24)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT OR IGNORE INTO pocket_roles(id, profile) VALUES (?, ?)",
                (secrets.token_hex(16), profile),
            )
            role = database.execute(
                "SELECT id, active FROM pocket_roles WHERE profile = ?", (profile,),
            ).fetchone()
            if not role["active"]:
                raise ValueError("Role is disabled")
            database.execute(
                "INSERT INTO pocket_codes(digest, role_id, expires) VALUES (?, ?, ?)",
                (self._digest(code), role["id"], time.time() + ttl),
            )
        return {"code": code, "profile_id": role["id"], "expires_in": ttl}

    def exchange(self, code: str) -> dict:
        token = "pd_" + secrets.token_urlsafe(32)
        device_id = secrets.token_hex(16)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            role = database.execute("""
                SELECT r.id FROM pocket_codes c JOIN pocket_roles r ON r.id = c.role_id
                WHERE c.digest = ? AND c.used = 0 AND c.expires > ? AND r.active = 1
            """, (self._digest(code), time.time())).fetchone()
            if role is None:
                raise ValueError("Connection code is invalid or expired")
            database.execute("UPDATE pocket_codes SET used = 1 WHERE digest = ?", (self._digest(code),))
            database.execute(
                "INSERT INTO pocket_devices(id, digest, role_id) VALUES (?, ?, ?)",
                (device_id, self._digest(token), role["id"]),
            )
        return {"token": token, "device_id": device_id, "profile_id": role["id"]}

    def verify(self, token: str) -> dict | None:
        with self._connect() as database:
            row = database.execute("""
                SELECT d.id AS device_id, r.id AS profile_id, r.profile
                FROM pocket_devices d JOIN pocket_roles r ON r.id = d.role_id
                WHERE d.digest = ? AND d.revoked = 0 AND r.active = 1
            """, (self._digest(token),)).fetchone()
        return dict(row) if row else None

    def management_snapshot(self) -> dict:
        with self._connect() as database:
            roles = [dict(row) for row in database.execute(
                "SELECT id, profile, active FROM pocket_roles ORDER BY profile"
            )]
            devices = [dict(row) for row in database.execute(
                "SELECT id, role_id, revoked FROM pocket_devices ORDER BY rowid DESC"
            )]
            codes = [dict(row) for row in database.execute(
                "SELECT rowid AS id, role_id, expires, used FROM pocket_codes ORDER BY rowid DESC"
            )]
        return {"roles": roles, "devices": devices, "codes": codes, "server_time": time.time()}

    def revoke_code(self, code_id: int) -> bool:
        with self._connect() as database:
            return database.execute(
                "UPDATE pocket_codes SET used = 1 WHERE rowid = ?", (code_id,),
            ).rowcount > 0

    def revoke_device(self, device_id: str, profile_id: str) -> None:
        with self._connect() as database:
            database.execute(
                "UPDATE pocket_devices SET revoked = 1 WHERE id = ? AND role_id = ?",
                (device_id, profile_id),
            )

    def disable_role(self, profile_id: str) -> None:
        with self._connect() as database:
            database.execute("UPDATE pocket_roles SET active = 0 WHERE id = ?", (profile_id,))
            database.execute("UPDATE pocket_devices SET revoked = 1 WHERE role_id = ?", (profile_id,))
            database.execute("UPDATE pocket_codes SET used = 1 WHERE role_id = ?", (profile_id,))