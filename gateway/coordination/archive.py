"""Pack and unpack a profile's state directory as zstd-compressed tar.

Symlinks are not captured. The common-skills overlay is recreated at VM start, and
an absolute link into the host would not survive a restore on another node.
Restore rejects links, absolute names, and ``..`` outright.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from gateway.coordination.errors import SnapshotError

_EXCLUDED_NAMES = frozenset({"__pycache__", ".pytest_cache"})


def pack_tree(root: Path, *, max_bytes: int) -> bytes:
    if not root.is_dir():
        raise SnapshotError(f"state directory {root} does not exist")
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in _EXCLUDED_NAMES)
            here = Path(current)
            for name in sorted(dirs + files):
                path = here / name
                if path.is_symlink() or name in _EXCLUDED_NAMES:
                    continue
                if path.is_file():
                    total += path.stat().st_size
                    if total > max_bytes:
                        raise SnapshotError(
                            f"state exceeds the snapshot limit of {max_bytes} bytes"
                        )
                archive.add(
                    path, arcname=path.relative_to(root).as_posix(), recursive=False
                )
    return _compress(buffer.getvalue())


def unpack_tree(payload: bytes, dest: Path) -> None:
    if dest.exists():
        raise SnapshotError(f"restore target {dest} already exists")
    try:
        raw = tarfile.open(fileobj=io.BytesIO(_decompress(payload)), mode="r:")
        members = raw.getmembers()
    except tarfile.TarError as exc:
        raise SnapshotError("snapshot is not a valid tar archive") from exc
    for member in members:
        if not (member.isfile() or member.isdir()):
            raise SnapshotError(f"snapshot contains a non-regular entry: {member.name}")
        parts = Path(member.name).parts
        if Path(member.name).is_absolute() or ".." in parts or not parts:
            raise SnapshotError(f"snapshot contains an unsafe path: {member.name}")
    dest.mkdir(parents=True)
    raw.extractall(dest, members=members)
    raw.close()


def _compress(payload: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor(level=3).compress(payload)


def _decompress(payload: bytes) -> bytes:
    import zstandard

    try:
        return zstandard.ZstdDecompressor().decompress(payload)
    except zstandard.ZstdError as exc:
        raise SnapshotError("snapshot is not a valid zstd stream") from exc
