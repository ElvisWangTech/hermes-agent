"""Profile-bound admission for Pocket at the shared HTTP and RPC boundaries."""

from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi.responses import JSONResponse

from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

_HTTP_METHODS = {
    "/api/pocket/name": "POST",
    "/api/pocket/device": "DELETE",
    "/api/pocket/identity": "GET",
    "/api/audio/transcribe": "POST",
    "/api/audio/speak": "POST",
}
_RPC_METHODS = frozenset({
    "client.capabilities", "gateway.ping", "session.create", "session.resume",
    "session.list", "prompt.submit", "session.interrupt", "session.events.since",
})


def authority() -> PocketAuthority:
    from hermes_cli.config import get_process_hermes_home

    return PocketAuthority(Path(get_process_hermes_home()) / "pocket-auth.sqlite")


async def admit_http(request, call_next):
    token = request.headers.get("X-Hermes-Session-Token", "")
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer pd_"):
        token = bearer[7:]
    if request.url.path == "/api/pocket/exchange" and request.method == "POST":
        request.state.token_authenticated = True
        return await call_next(request)
    if not token.startswith("pd_"):
        return None
    identity = authority().verify(token)
    if identity is None:
        return JSONResponse({"detail": "Device credential is invalid or revoked"}, status_code=401)
    if _HTTP_METHODS.get(request.url.path) != request.method:
        return JSONResponse({"detail": "Device credential cannot access this endpoint"}, status_code=403)
    requested = request.query_params.getlist("profile")
    if requested and requested != [identity["profile"]]:
        return JSONResponse({"detail": "Profile access denied"}, status_code=403)
    query = [(key, value) for key, value in parse_qsl(request.scope["query_string"].decode()) if key != "profile"]
    query.append(("profile", identity["profile"]))
    request.scope["query_string"] = urlencode(query).encode()
    request.state.pocket_identity = identity
    request.state.token_authenticated = True
    return await call_next(request)


def admit_rpc(req: dict, transport, server) -> dict:
    identity = getattr(transport, "auth_identity", None) or {}
    if identity.get("provider") != "pocket":
        return req
    current = authority().verify(identity["credential"])
    if current is None:
        raise PermissionError("Device credential is revoked")
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
    profile = current["profile"]
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
    current = authority().verify(identity["credential"])
    if current is None:
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