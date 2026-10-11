"""Cluster identity: scope, conversation id, and Redis/S3 key layout.

``hermes_home_key()`` is a local path and is never written into these keys.
Scope is a hash of ``namespace`` + ``profile_id`` so two homes for the same
logical profile share one manifest, and a path cannot leak into the key.

Redis Cluster hashes on the ``{...}`` tag. Epoch, lease, and manifest for one
conversation share that tag so one Lua script touches a single slot.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import uuid

_UNSAFE_KEY = re.compile(r"[\r\n\x00]")
_INCARNATION: str | None = None


def normalize_session_key(value: str) -> str:
    """Strip a client session key. Empty or control-bearing keys are not conversations."""
    text = (value or "").strip()
    if not text or _UNSAFE_KEY.search(text):
        raise ValueError("invalid session key")
    return text


def conversation_id_for(session_key: str) -> str:
    """Stable conversation id for a normalized session key. Not reversible."""
    return hashlib.sha256(normalize_session_key(session_key).encode("utf-8")).hexdigest()


def scope_token(namespace: str, profile_id: str) -> str:
    """Unambiguous scope prefix. A local filesystem path is not an input."""
    raw = namespace.strip() + "\0" + profile_id.strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _slot(scope: str, conversation_id: str) -> str:
    return f"{scope}:conv:{conversation_id}"


def conversation_redis_keys(scope: str, conversation_id: str) -> tuple[str, str, str]:
    """Epoch, lease, and manifest keys. The hash tag is the whole slot."""
    tag = _slot(scope, conversation_id)
    base = f"hermes:{{{tag}}}"
    return f"{base}:epoch", f"{base}:lease", f"{base}:manifest"


def node_address_key(scope: str, node_id: str, incarnation: str) -> str:
    """Address of one process incarnation. Not part of the conversation Lua slot."""
    return f"hermes:{{{scope}}}:node:{_key_token(node_id)}:{_key_token(incarnation)}"


def object_key(scope: str, conversation_id: str, owner_epoch: int, snapshot_revision: int, sha256: str) -> str:
    """Immutable blob name. The content hash is in the key; a new body is a new object."""
    return (
        f"hermes/{scope}/conversations/{conversation_id}/"
        f"snapshot.{owner_epoch}.{snapshot_revision}.{sha256}.json.zst"
    )


def _key_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._~-]", "_", value or "")
    return cleaned[:128] or "x"


def resolve_node_id(configured: str = "") -> str:
    """``cluster.node_id``, else ``POD_NAME``, else the hostname. Read at call time."""
    chosen = (configured or "").strip() or os.environ.get("POD_NAME", "").strip()
    return chosen or socket.gethostname()


def process_incarnation() -> str:
    """Random id for this process start. A restart does not keep the old lease."""
    global _INCARNATION
    if _INCARNATION is None:
        _INCARNATION = uuid.uuid4().hex
    return _INCARNATION


def reset_process_incarnation() -> None:
    """Test hook. Production code does not call this."""
    global _INCARNATION
    _INCARNATION = None
