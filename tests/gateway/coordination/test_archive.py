"""Snapshot archives: round trip, exclusions, and refusal of unsafe members."""

import io
import tarfile

import pytest

from gateway.coordination.archive import pack_tree, unpack_tree
from gateway.coordination.errors import SnapshotError


def _zstd(raw: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor().compress(raw)


def test_round_trip_keeps_state_and_drops_caches_and_links(tmp_path):
    source = tmp_path / "home"
    (source / "memories").mkdir(parents=True)
    (source / "memories" / "MEMORY.md").write_text("name: Xiaoman")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "x.pyc").write_bytes(b"cache")
    (source / "skills-link").symlink_to("/opt/common-skills")

    payload = pack_tree(source, max_bytes=1 << 20)
    restored = tmp_path / "restored"
    unpack_tree(payload, restored)

    assert (restored / "memories" / "MEMORY.md").read_text() == "name: Xiaoman"
    assert not (restored / "__pycache__").exists()
    assert not (restored / "skills-link").exists()


def test_restore_refuses_path_traversal_and_leaves_no_target(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 0
        archive.addfile(info, io.BytesIO(b""))
    payload = _zstd(buffer.getvalue())

    target = tmp_path / "state"
    with pytest.raises(SnapshotError):
        unpack_tree(payload, target)
    assert not target.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_pack_refuses_state_over_the_limit(tmp_path):
    source = tmp_path / "home"
    source.mkdir()
    (source / "big.bin").write_bytes(b"x" * 4096)

    with pytest.raises(SnapshotError):
        pack_tree(source, max_bytes=1024)
