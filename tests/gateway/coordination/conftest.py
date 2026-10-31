from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.coordination.authority import ControlPlaneAuthority, Manifest
from gateway.coordination.errors import SnapshotError
from gateway.coordination.snapshots import SnapshotService


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeObjects:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_put = False

    def put_immutable(self, key: str, body: bytes) -> None:
        if self.fail_put:
            raise OSError("upload refused")
        existing = self.objects.get(key)
        if existing is not None and existing != body:
            raise SnapshotError(f"object {key} already holds different bytes")
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise SnapshotError(f"object {key} is missing")
        return self.objects[key]


class FakeHost:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[str, str]] = []

    def state_dir(self, profile_id: str) -> Path:
        return self.root / profile_id

    def pause(self, instance_id: str) -> None:
        self.calls.append(("pause", instance_id))

    def resume(self, instance_id: str) -> None:
        self.calls.append(("resume", instance_id))

    def stop(self, instance_id: str) -> None:
        self.calls.append(("stop", instance_id))


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    authority = ControlPlaneAuthority(
        tmp_path / "control" / "pocket-auth.sqlite", clock=clock
    )
    objects = FakeObjects()
    host = FakeHost(tmp_path / "nodes")
    service = SnapshotService(
        authority=authority,
        objects=objects,
        host=host,
        namespace="lab",
        lease_ttl=90,
        max_bytes=1 << 20,
        clock=clock,
    )
    return SimpleNamespace(
        clock=clock, authority=authority, objects=objects, host=host, service=service
    )


def manifest(
    profile_id: str, revision: int, instance_id: str, created_at: float = 1000.0
) -> Manifest:
    return Manifest(
        profile_id=profile_id,
        revision=revision,
        object_key=f"hermes/lab/{profile_id}/snapshots/{revision:012d}.{'a' * 64}.tar.zst",
        sha256="a" * 64,
        size_bytes=1,
        schema_version=1,
        min_reader_version=1,
        instance_id=instance_id,
        created_at=created_at,
    )
