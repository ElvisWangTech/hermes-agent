"""Immutable snapshot blobs. Redis holds the only commit head.

The object name includes the content hash. A failed upload does not move the
manifest. Read-after-write checks that the bytes just stored are the bytes a
later owner will download.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from gateway.coordination.errors import SnapshotError


class ObjectStore(Protocol):
    def put_immutable(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...


class MemoryObjectStore:
    """Process-local stand-in for tests and for ``object_store.backend: none``."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_immutable(self, key: str, body: bytes) -> None:
        existing = self.objects.get(key)
        if existing is not None and existing != body:
            raise SnapshotError(f"object {key} already holds different bytes")
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise SnapshotError(f"object {key} is missing") from exc


class S3ObjectStore:
    """S3-compatible client. The endpoint is configuration, not a vendor SDK."""

    def __init__(self, *, endpoint: str, bucket: str, region: str, addressing_style: str, access_key: str, secret_key: str):
        import boto3
        from botocore.config import Config

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(s3={"addressing_style": addressing_style}),
        )

    def put_immutable(self, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/zstd")

    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()


class SnapshotBlobs:
    def __init__(self, store: ObjectStore):
        self._store = store

    def put(self, key: str, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        if digest not in key:
            raise SnapshotError("object key must contain the content hash")
        self._store.put_immutable(key, body)
        stored = self._store.get(key)
        if hashlib.sha256(stored).hexdigest() != digest:
            raise SnapshotError("object store read-after-write checksum mismatch")
        return digest

    def get_checked(self, key: str, expected_sha256: str) -> bytes:
        body = self._store.get(key)
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected_sha256:
            raise SnapshotError("snapshot checksum does not match the committed manifest")
        return body


def compress_payload(payload: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor(level=3).compress(payload)


def decompress_payload(payload: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdDecompressor().decompress(payload)
