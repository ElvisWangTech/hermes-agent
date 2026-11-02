"""Phase 2: principals, API keys, rate limits, and the soft token quota.

A request resolves to one principal bound to one profile. API keys are stored as
HMAC digests (the pepper lives in the secret scope, never in the database). Pocket
device credentials are verified by the existing Pocket authority, passed in as a
callable so this module does not depend on the dashboard package.

The quota is soft: preflight reads the day's total, and usage is recorded after
execution under its ``execution_id``, so a retry or a replayed report counts once.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from gateway.coordination.authority import ControlPlaneAuthority
from gateway.coordination.errors import ClusterError

API_KEY_PREFIX = "hk_"
DEVICE_TOKEN_PREFIX = "pd_"


class Unauthenticated(ClusterError):
    """The credential is missing, unknown, or revoked."""


class AccessDenied(ClusterError):
    """The credential is valid but not allowed on the requested profile."""


class RateLimited(ClusterError):
    """A credential or profile exceeded its request rate."""


class QuotaExceeded(ClusterError):
    """The profile has used its daily token allowance."""


@dataclass(frozen=True)
class Principal:
    profile_id: str
    kind: str  # "api_key" | "device"
    credential_id: str


class TokenBucket:
    def __init__(
        self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic
    ):
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._state.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - last) * self._rate)
            if tokens < 1:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1, now)
            return True


class AccessControl:
    def __init__(
        self,
        authority: ControlPlaneAuthority,
        *,
        pepper: str,
        verify_device: Callable[[str], dict | None],
        rate_per_sec: float = 10.0,
        burst: float = 20.0,
        daily_token_limit: int = 0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not pepper:
            raise ClusterError("API key pepper is required")
        self._authority = authority
        self._pepper = pepper.encode()
        self._verify_device = verify_device
        self._daily_limit = daily_token_limit
        self._clock = clock
        self._credential_bucket = TokenBucket(rate_per_sec, burst, monotonic)
        self._profile_bucket = TokenBucket(rate_per_sec * 2, burst * 2, monotonic)

    def _digest(self, key: str) -> str:
        return hmac.new(self._pepper, key.encode(), hashlib.sha256).hexdigest()

    def create_api_key(self, profile_id: str) -> str:
        """Return the plaintext key once. Only its digest is stored."""
        if not profile_id:
            raise ClusterError("profile_id is required")
        key = API_KEY_PREFIX + secrets.token_urlsafe(32)
        with self._authority.transaction() as database:
            database.execute(
                "INSERT INTO cp_api_keys(key_id, digest, profile_id, revoked, created_at) VALUES (?, ?, ?, 0, ?)",
                (secrets.token_hex(8), self._digest(key), profile_id, self._clock()),
            )
        return key

    def revoke_api_key(self, key_id: str) -> None:
        with self._authority.transaction() as database:
            database.execute(
                "UPDATE cp_api_keys SET revoked = 1 WHERE key_id = ?", (key_id,)
            )

    def resolve(self, credential: str) -> Principal:
        if credential.startswith(API_KEY_PREFIX):
            with self._authority.connect() as database:
                row = database.execute(
                    "SELECT key_id, profile_id FROM cp_api_keys WHERE digest = ? AND revoked = 0",
                    (self._digest(credential),),
                ).fetchone()
            if row is None:
                raise Unauthenticated("API key is invalid or revoked")
            return Principal(row["profile_id"], "api_key", row["key_id"])
        if credential.startswith(DEVICE_TOKEN_PREFIX):
            identity = self._verify_device(credential)
            if identity is None:
                raise Unauthenticated("Device credential is invalid or revoked")
            return Principal(identity["profile_id"], "device", identity["device_id"])
        raise Unauthenticated("Unknown credential type")

    def authorize(self, principal: Principal, requested_profile: str) -> None:
        if requested_profile != principal.profile_id:
            raise AccessDenied("Profile access denied")

    def admit(self, principal: Principal) -> None:
        """Rate-limit by credential and by profile. Call after ``resolve``, before routing."""
        if not self._credential_bucket.take(
            f"{principal.kind}:{principal.credential_id}"
        ):
            raise RateLimited("request rate exceeded for this credential")
        if not self._profile_bucket.take(principal.profile_id):
            raise RateLimited("request rate exceeded for this profile")

    def preflight(self, profile_id: str) -> None:
        if self._daily_limit <= 0:
            return
        if self.used_today(profile_id) >= self._daily_limit:
            raise QuotaExceeded("daily token allowance is used up")

    def used_today(self, profile_id: str) -> int:
        with self._authority.connect() as database:
            row = database.execute(
                "SELECT COALESCE(SUM(tokens), 0) AS total FROM cp_usage WHERE profile_id = ? AND day = ?",
                (profile_id, _utc_day(self._clock())),
            ).fetchone()
        return int(row["total"])

    def record_usage(self, execution_id: str, profile_id: str, tokens: int) -> bool:
        """Count one execution. Returns False when this execution was already recorded."""
        if not execution_id or tokens < 0:
            raise ClusterError(
                "usage needs an execution_id and a non-negative token count"
            )
        now = self._clock()
        with self._authority.transaction() as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO cp_usage(execution_id, profile_id, day, tokens, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (execution_id, profile_id, _utc_day(now), tokens, now),
            )
            return cursor.rowcount == 1


def _utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
