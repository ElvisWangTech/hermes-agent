"""Snapshot, restore, and drain against a fake object store and node host."""

import shutil

import pytest

from gateway.coordination.errors import ClusterError, LeaseError, SnapshotError


def _write_state(env, profile: str, text: str) -> None:
    home = env.host.state_dir(profile)
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "memories" / "MEMORY.md").write_text(text)


def _read_state(env, profile: str) -> str:
    return (env.host.state_dir(profile) / "memories" / "MEMORY.md").read_text()


def test_committed_snapshot_restores_on_a_fresh_node_directory(env):
    lease = env.service.acquire("role", "vm-1")
    _write_state(env, "role", "first")
    committed = env.service.snapshot(lease)
    env.service.release(lease)
    assert committed.revision == 1

    # Node replaced: the old directory is gone, the new instance starts from nothing.
    shutil.rmtree(env.host.state_dir("role"))
    second = env.service.acquire("role", "vm-2")
    assert env.service.restore(second) is True
    assert _read_state(env, "role") == "first"


def test_failed_upload_commits_nothing_and_keeps_the_instance_running(env):
    lease = env.service.acquire("role", "vm-1")
    _write_state(env, "role", "unsaved")
    env.objects.fail_put = True

    with pytest.raises(SnapshotError):
        env.service.snapshot(lease)

    assert env.authority.read_manifest("role") is None
    assert ("resume", "vm-1") in env.host.calls
    env.authority.require(lease)


def test_zombie_instance_is_stopped_and_cannot_commit(env):
    old = env.service.acquire("role", "vm-1")
    env.clock.now += 91
    env.service.acquire("role", "vm-2")
    _write_state(env, "role", "stale")

    with pytest.raises(LeaseError):
        env.service.snapshot(old)

    assert ("stop", "vm-1") in env.host.calls
    assert env.authority.read_manifest("role") is None


def test_corrupt_object_is_refused_and_local_state_is_untouched(env):
    lease = env.service.acquire("role", "vm-1")
    _write_state(env, "role", "committed")
    env.service.snapshot(lease)
    env.service.release(lease)

    key = env.authority.read_manifest("role").object_key
    env.objects.objects[key] = b"tampered"
    _write_state(env, "role", "local draft")

    second = env.service.acquire("role", "vm-2")
    with pytest.raises(SnapshotError):
        env.service.restore(second)
    assert _read_state(env, "role") == "local draft"


def test_drain_commits_dirty_profiles_and_refuses_new_leases(env):
    lease = env.service.acquire("role", "vm-1")
    _write_state(env, "role", "drained")
    env.service.note_activity(lease)

    report = env.service.drain([lease])

    assert report.committed == ("role",)
    assert report.incomplete == ()
    assert ("stop", "vm-1") in env.host.calls
    assert env.authority.read_manifest("role").revision == 1
    with pytest.raises(ClusterError):
        env.service.acquire("other", "vm-9")


def test_oversized_state_is_refused_and_the_instance_resumes(env):
    env.service._max_bytes = 16
    lease = env.service.acquire("role", "vm-1")
    _write_state(env, "role", "this text is longer than sixteen bytes")

    with pytest.raises(SnapshotError):
        env.service.snapshot(lease)
    assert ("resume", "vm-1") in env.host.calls
    assert env.authority.read_manifest("role") is None
