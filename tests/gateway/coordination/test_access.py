"""Phase 2: API keys, device credentials, rate limits, and the soft quota."""

import sqlite3

import pytest

from gateway.coordination.access import (
    AccessControl,
    AccessDenied,
    QuotaExceeded,
    RateLimited,
    Unauthenticated,
)


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def access(env):
    devices = {"pd_good": {"profile_id": "role-a", "device_id": "dev-1"}}
    clock = FakeClock()
    control = AccessControl(
        env.authority,
        pepper="test-pepper",
        verify_device=lambda token: devices.get(token),
        rate_per_sec=1.0,
        burst=2.0,
        daily_token_limit=100,
        clock=clock,
        monotonic=clock,
    )
    return control


def test_api_key_is_resolved_and_stored_only_as_a_digest(access, env):
    key = access.create_api_key("role-a")

    principal = access.resolve(key)

    assert (principal.profile_id, principal.kind) == ("role-a", "api_key")
    with sqlite3.connect(env.authority.path) as database:
        stored = [row[0] for row in database.execute("SELECT digest FROM cp_api_keys")]
    assert key not in stored
    assert all(len(digest) == 64 for digest in stored)


def test_revoked_key_and_unknown_device_are_unauthenticated(access):
    key = access.create_api_key("role-a")
    access.revoke_api_key(access.resolve(key).credential_id)

    with pytest.raises(Unauthenticated):
        access.resolve(key)
    with pytest.raises(Unauthenticated):
        access.resolve("pd_forged")


def test_a_device_cannot_be_authorized_for_another_profile(access):
    principal = access.resolve("pd_good")

    access.authorize(principal, "role-a")
    with pytest.raises(AccessDenied):
        access.authorize(principal, "role-b")


def test_burst_beyond_the_rate_is_limited_per_credential(access):
    principal = access.resolve("pd_good")

    access.admit(principal)
    access.admit(principal)
    with pytest.raises(RateLimited):
        access.admit(principal)


def test_replayed_execution_is_counted_once_and_the_quota_closes(access):
    assert access.record_usage("exec-1", "role-a", 80) is True
    assert access.record_usage("exec-1", "role-a", 80) is False
    assert access.used_today("role-a") == 80

    access.preflight("role-a")
    access.record_usage("exec-2", "role-a", 20)
    with pytest.raises(QuotaExceeded):
        access.preflight("role-a")
