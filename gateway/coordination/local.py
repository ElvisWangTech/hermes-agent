"""Router used when ``cluster.enabled`` is false.

Every claim belongs to this process. There is no lease expiry and no manifest,
so the api_server path that consults it still short-circuits before calling it.
"""

from __future__ import annotations

from gateway.coordination.base import Claim, Manifest, Owner
from gateway.coordination.identity import conversation_id_for, normalize_session_key, process_incarnation, resolve_node_id


class LocalSessionRouter:
    def __init__(self, node_id: str | None = None):
        self._node_id = node_id or resolve_node_id()
        self._incarnation = process_incarnation()
        self._accepting = True
        self._held: dict[str, Claim] = {}

    def claim(self, session_key: str, node_id: str, ttl: float) -> Claim:
        del ttl
        key = normalize_session_key(session_key)
        conversation_id = conversation_id_for(key)
        existing = self._held.get(conversation_id)
        if not self._accepting and existing is None:
            return Claim(
                session_key=key,
                conversation_id=conversation_id,
                is_mine=False,
                owner=None,
                manifest=None,
                acquired=False,
            )
        owner = Owner(node_id or self._node_id, self._incarnation, 1)
        claim = Claim(
            session_key=key,
            conversation_id=conversation_id,
            is_mine=True,
            owner=owner,
            manifest=None,
            acquired=True,
        )
        self._held[conversation_id] = claim
        return claim

    def lookup(self, session_key: str) -> str | None:
        conversation_id = conversation_id_for(normalize_session_key(session_key))
        claim = self._held.get(conversation_id)
        return None if claim is None or claim.owner is None else claim.owner.node_id

    def renew(self, claim: Claim, ttl: float) -> bool:
        del ttl
        held = self._held.get(claim.conversation_id)
        return held is not None and held.owner is not None and claim.owner is not None and held.owner.matches(claim.owner)

    def release(self, claim: Claim) -> bool:
        held = self._held.get(claim.conversation_id)
        if held is None or held.owner is None or claim.owner is None or not held.owner.matches(claim.owner):
            return False
        del self._held[claim.conversation_id]
        return True

    def stop_new_claims(self) -> None:
        self._accepting = False

    def commit_manifest(
        self,
        claim: Claim,
        expected_snapshot_revision: int,
        expected_reset_revision: int,
        manifest: Manifest,
    ) -> bool:
        del claim, expected_snapshot_revision, expected_reset_revision, manifest
        return False

    def read_manifest(self, conversation_id: str) -> Manifest | None:
        del conversation_id
        return None

    def register_address(self, node_id: str, incarnation: str, address: str, ttl: float) -> None:
        del node_id, incarnation, address, ttl

    def lookup_address(self, node_id: str, incarnation: str) -> str | None:
        del node_id, incarnation
        return None

    def owned_claims(self) -> list[Claim]:
        return list(self._held.values())
