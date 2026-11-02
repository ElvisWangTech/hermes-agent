"""Profile-bound admission for Pocket at the shared HTTP and RPC boundaries.

Cluster mode: a sandbox instance launched by the control plane trusts the
principal the cluster gateway injects. The proof of trust is the request
carrying the instance's own internal key (``API_SERVER_KEY``) — only the
gateway holds it — so the ``X-Hermes-Principal`` /
``X-Hermes-Cluster-Profile`` headers it sends alongside are the identity, and
no device row is consulted locally. Revocation stays authoritative at the
gateway, which re-verifies every connection it accepts.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi.responses import JSONResponse

from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

_HTTP_METHODS = {
    "/api/pocket/name": "POST",
    "/api/pocket/device": "DELETE",
    "/api/pocket/identity": "GET",
    "/api/pocket/schedules": "GET",
    "/api/pocket/schedules/interpret": "POST",
    "/api/pocket/schedules/confirm": "POST",
    "/api/pocket/schedules/decline": "POST",
    "/api/pocket/schedules/claim": "POST",
    "/api/pocket/schedules/heard": "POST",
    "/api/audio/transcribe": "POST",
    "/api/audio/speak": "POST",
}
_RPC_METHODS = frozenset({
    "client.capabilities", "gateway.ping", "session.create", "session.resume",
    "session.list", "prompt.submit", "session.interrupt", "session.events.since",
})


def authority() -> PocketAuthority:
    from hermes_cli.config import get_process_hermes_home
    from hermes_cli.dashboard_auth.pocket_store import AUTHORITY_FILENAME

    return PocketAuthority(Path(get_process_hermes_home()) / AUTHORITY_FILENAME)


_PRINCIPAL_HEADER = "X-Hermes-Principal"
_PROFILE_HEADER = "X-Hermes-Cluster-Profile"


def cluster_identity(authorization: str, principal: str, profile: str) -> dict | None:
    """The gateway-asserted device identity, or None when this request is not
    a cluster-routed one (the internal key is absent locally, or mismatched)."""
    internal_key = os.environ.get("API_SERVER_KEY", "")
    if not internal_key:
        return None
    if not hmac.compare_digest(authorization.encode(), f"Bearer {internal_key}".encode()):
        return None
    kind, _, device_id = principal.partition(":")
    if kind != "device" or not device_id or not profile:
        return None
    return {
        "profile": profile,
        "profile_id": profile,
        "device_id": device_id,
        "provider": "pocket",
        "cluster": True,
    }


def cluster_http_identity(request) -> dict | None:
    return cluster_identity(
        request.headers.get("authorization", ""),
        request.headers.get(_PRINCIPAL_HEADER, ""),
        request.headers.get(_PROFILE_HEADER, ""),
    )


async def admit_http(request, call_next):
    token = request.headers.get("X-Hermes-Session-Token", "")
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer pd_"):
        token = bearer[7:]
    if request.url.path == "/api/pocket/exchange" and request.method == "POST":
        request.state.token_authenticated = True
        return await call_next(request)
    identity = None
    if token.startswith("pd_"):
        identity = authority().verify(token)
        if identity is None:
            return JSONResponse({"detail": "Device credential is invalid or revoked"}, status_code=401)
    else:
        # Cluster-managed instance: the gateway asserted the device with the
        # internal key. No local authority consult and no revocation re-check —
        # the gateway re-verifies everything it routes.
        identity = cluster_http_identity(request)
    if identity is None:
        return None
    if _HTTP_METHODS.get(request.url.path) != request.method:
        return JSONResponse({"detail": "Device credential cannot access this endpoint"}, status_code=403)
    requested = request.query_params.getlist("profile")
    if requested and requested != [identity["profile"]]:
        return JSONResponse({"detail": "Profile access denied"}, status_code=403)
    query = [(key, value) for key, value in parse_qsl(request.scope["query_string"].decode()) if key != "profile"]
    if not identity.get("cluster"):
        # A cluster instance serves exactly one profile: its own home. Naming that
        # profile would send the dashboard looking for a profile directory that a
        # sandbox does not have.
        query.append(("profile", identity["profile"]))
    request.scope["query_string"] = urlencode(query).encode()
    request.state.pocket_identity = identity
    request.state.token_authenticated = True
    return await call_next(request)


def admit_rpc(req: dict, transport, server) -> dict:
    identity = getattr(transport, "auth_identity", None) or {}
    if identity.get("provider") != "pocket":
        return req
    if not identity.get("cluster"):
        # Local device: re-verify against this instance's authority on every
        # call. Cluster identities are verified by the gateway that routed them.
        current = authority().verify(identity["credential"])
        if current is None:
            raise PermissionError("Device credential is revoked")
        identity = {**identity, **current}
    if not isinstance(req, dict):
        raise PermissionError("Invalid device request")
    if "method" not in req:
        from tui_gateway import server_requests
        with server_requests._lock:
            pending = server_requests._open.get(req.get("id"))
            session_id = pending.sid if pending is not None else None
        if session_id is None:
            raise PermissionError("Unknown server request")
        admit_rpc({"method": "session.interrupt", "params": {"session_id": session_id}}, transport, server)
        return req
    method = req["method"]
    if method not in _RPC_METHODS:
        raise PermissionError("Method is not available to Pocket devices")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        raise PermissionError("Invalid device request")
    params = dict(params)
    profile = identity["profile"]
    if "profile" in params and params["profile"] != profile:
        raise PermissionError("Profile access denied")
    session_id = params.get("session_id")
    if method in {"prompt.submit", "session.interrupt", "session.events.since", "session.resume"} and not session_id:
        raise PermissionError("Session id required")
    allowed_params = {
        "session.resume": {"session_id", "source", "lazy", "inline_images", "omit_messages", "profile"},
        "session.list": {"limit", "profile"},
        "prompt.submit": {"session_id", "text"},
        "session.interrupt": {"session_id"},
        "session.events.since": {"session_id", "last_seen", "profile"},
    }.get(method)
    if allowed_params is not None and set(params) - allowed_params:
        raise PermissionError("Unsupported Pocket request parameters")
    if session_id:
        with server._sessions_lock:
            live = server._sessions.get(session_id)
        target = server._profile_home(profile) or Path(server._hermes_home)
        if live:
            home = Path(live.get("profile_home") or server._hermes_home)
            if home.resolve() != target.resolve():
                raise PermissionError("Session access denied")
        else:
            with server._profile_db({"profile": profile}) as database:
                if database is None or database.get_session(session_id) is None:
                    raise PermissionError("Session access denied")
    if method in {"session.create", "session.resume", "session.list"}:
        if identity.get("cluster"):
            params.pop("profile", None)
        else:
            params["profile"] = profile
    if method == "session.create":
        allowed = {"source", "title", "profile"}
        if set(params) - allowed:
            raise PermissionError("Pocket cannot override agent configuration")
        params["source"] = "pocket"
    return {**req, "params": params}


def admit_output(frame: dict, transport) -> bool:
    identity = getattr(transport, "auth_identity", None) or {}
    if identity.get("provider") != "pocket":
        return True
    if not identity.get("cluster") and authority().verify(identity["credential"]) is None:
        return False
    if "method" not in frame:
        return True
    params = frame.get("params") or {}
    if params.get("type") == "gateway.ready":
        return True
    session_id = params.get("session_id")
    if not session_id:
        return False
    from tui_gateway import server
    try:
        admit_rpc({"method": "session.interrupt", "params": {"session_id": session_id}}, transport, server)
    except PermissionError:
        return False
    return True