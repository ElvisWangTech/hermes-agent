"""Lease exclusivity and fencing in the control-plane authority."""

import pytest

from gateway.coordination.authority import ControlPlaneAuthority
from gateway.coordination.errors import LeaseError, SnapshotError

from .conftest import FakeClock, manifest


def test_expired_holder_is_fenced_out_by_the_next_incarnation(tmp_path):
    clock = FakeClock()
    authority = ControlPlaneAuthority(tmp_path / "a.sqlite", clock=clock)
    first = authority.acquire("role", "vm-1", ttl=10)
    with pytest.raises(LeaseError):
        authority.acquire("role", "vm-2", ttl=10)

    clock.now += 11
    authority.acquire("role", "vm-2", ttl=10)

    assert authority.renew(first, ttl=10) is False
    with pytest.raises(LeaseError):
        authority.mark_dirty(first)


def test_commit_needs_live_lease_and_the_revision_it_read(tmp_path):
    clock = FakeClock()
    authority = ControlPlaneAuthority(tmp_path / "a.sqlite", clock=clock)
    lease = authority.acquire("role", "vm-1", ttl=10)
    authority.commit(lease, 0, manifest("role", 1, "vm-1"))

    with pytest.raises(SnapshotError):
        authority.commit(lease, 0, manifest("role", 1, "vm-1"))

    authority.release(lease)
    with pytest.raises(LeaseError):
        authority.commit(lease, 1, manifest("role", 2, "vm-1"))
    assert authority.read_manifest("role").revision == 1
