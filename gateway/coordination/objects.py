"""Immutable snapshot objects in an S3-compatible bucket.

The object name carries the content hash. A write is read back and checked before
the manifest may name it, so a later owner downloads exactly the bytes that were
committed. The endpoint is configuration, not a vendor SDK.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from gateway.coordination.errors import SnapshotError


class ObjectStore(Protocol):
    def put_immutable(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...


class S3ObjectStore:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        addressing_style: str,
        access_key: str,
        secret_key: str,
    ):
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
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType="application/zstd"
        )

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()


def put_verified(store: ObjectStore, key: str, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    if digest not in key:
        raise SnapshotError("object key must contain the content hash")
    store.put_immutable(key, body)
    if hashlib.sha256(store.get(key)).hexdigest() != digest:
        raise SnapshotError("object store read-after-write checksum mismatch")
    return digest


def get_verified(store: ObjectStore, key: str, expected_sha256: str) -> bytes:
    body = store.get(key)
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise SnapshotError("snapshot checksum does not match the committed manifest")
    return body
