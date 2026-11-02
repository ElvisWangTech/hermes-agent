"""Phase 3: gateway routing to a profile's instance.

Order per request: resolve the credential, rate-limit, check the optional profile
header against the principal, run the quota preflight, wake or start the instance,
then forward. The client's credential never reaches the instance; the gateway sends
the instance's own key instead.
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from gateway.coordination.access import (
    AccessControl,
    AccessDenied,
    Principal,
    QuotaExceeded,
    RateLimited,
    Unauthenticated,
)
from gateway.coordination.errors import ClusterError
from gateway.coordination.instances import InstanceManager

PROFILE_HEADER = "X-Hermes-Profile"
DEVICE_SESSION_HEADER = "X-Hermes-Session-Token"
_STRIP = frozenset({
    "authorization",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    PROFILE_HEADER.lower(),
    DEVICE_SESSION_HEADER.lower(),
})


class Gateway:
    def __init__(self, access: AccessControl, manager: InstanceManager):
        self._access = access
        self._manager = manager

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            principal = self._access.resolve(_client_credential(request))
            self._access.admit(principal)
            self._check_profile_header(request, principal)
            port = _instance_port(principal, request)
            self._access.preflight(principal.profile_id)
        except Unauthenticated as exc:
            return web.json_response({"detail": str(exc)}, status=401)
        except AccessDenied as exc:
            return web.json_response({"detail": str(exc)}, status=403)
        except (RateLimited, QuotaExceeded) as exc:
            return web.json_response({"detail": str(exc)}, status=429)

        try:
            instance = await asyncio.to_thread(
                self._manager.ensure_running, principal.profile_id
            )
        except ClusterError as exc:
            return web.json_response(
                {"detail": f"instance unavailable: {exc}"},
                status=503,
                headers={"Retry-After": "5"},
            )
        try:
            self._manager.touch(principal.profile_id)
        except ClusterError as exc:
            return web.json_response(
                {"detail": f"instance unavailable: {exc}"},
                status=503,
                headers={"Retry-After": "5"},
            )

        host_header = instance.host_header if port == "dashboard" else instance.api_host_header
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _relay_websocket(
                request, instance.endpoint, instance.key, principal, host_header
            )
        return await _relay_http(
            request, instance.endpoint, instance.key, principal, host_header
        )

    def _check_profile_header(self, request: web.Request, principal: Principal) -> None:
        requested = request.headers.get(PROFILE_HEADER)
        if requested is not None:
            self._access.authorize(principal, requested)


def _client_credential(request: web.Request) -> str:
    """The caller's credential in the order a client may send it.

    API clients use ``Authorization: Bearer``; Pocket devices send their device
    token as a session header on HTTP and inside the ``?token=`` upgrade URL.
    """
    bearer = request.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        return bearer[len("Bearer ") :].strip()
    session = request.headers.get("X-Hermes-Session-Token", "")
    if session:
        return session.strip()
    return request.query.get("token", "").strip()


def _forward_path(request: web.Request) -> str:
    """The request path with the client's credential params removed — a Pocket
    WS upgrade carries its device token in ``?token=``, which must not reach
    the instance."""
    if "token" not in request.rel_url.query:
        return str(request.rel_url)
    query = {k: v for k, v in request.rel_url.query.items() if k != "token"}
    return str(request.rel_url.with_query(query))


def _instance_port(principal: Principal, request: web.Request) -> str:
    """Which port of the profile's instance serves this request.

    A Pocket device reaches the dashboard, which owns Pocket, the web API and /api/ws.
    An API key reaches only the OpenAI-compatible api_server under /v1/. Anything
    else would hand the instance's internal key to a credential that has no business
    with that surface.
    """
    if principal.kind == "device":
        return "dashboard"
    if principal.kind == "api_key" and request.path.startswith("/v1/"):
        return "api"
    raise AccessDenied("This credential cannot reach that endpoint")


def _principal_headers(principal: Principal) -> dict[str, str]:
    """The verified principal forwarded to the instance. The instance trusts it
    because the request itself carries the instance's internal key, which only
    the control plane holds."""
    return {
        "X-Hermes-Principal": f"{principal.kind}:{principal.credential_id}",
        "X-Hermes-Cluster-Profile": principal.profile_id,
    }


def _forward_headers(
    request: web.Request, key: str, principal: Principal
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _STRIP
    }
    headers["Authorization"] = f"Bearer {key}"
    headers.update(_principal_headers(principal))
    return headers


async def _relay_http(
    request: web.Request,
    endpoint: str,
    key: str,
    principal: Principal,
    host_header: str = "",
) -> web.StreamResponse:
    body = await request.read()
    url = endpoint.rstrip("/") + _forward_path(request)
    headers = _forward_headers(request, key, principal)
    if host_header:
        headers["Host"] = host_header
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_read=600)
    ) as session:
        async with session.request(
            request.method,
            url,
            data=body or None,
            headers=headers,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status, reason=upstream.reason
            )
            for name, value in upstream.headers.items():
                if name.lower() not in {
                    "content-length",
                    "transfer-encoding",
                    "connection",
                }:
                    response.headers[name] = value
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response


async def _relay_websocket(
    request: web.Request,
    endpoint: str,
    key: str,
    principal: Principal,
    host_header: str = "",
) -> web.WebSocketResponse:
    server = web.WebSocketResponse()
    await server.prepare(request)
    url = endpoint.replace("http", "ws", 1).rstrip("/") + _forward_path(request)
    headers = {"Authorization": f"Bearer {key}", **_principal_headers(principal)}
    if host_header:
        headers["Host"] = host_header
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, headers=headers) as client:
            await asyncio.gather(
                _pump(server, client), _pump(client, server), return_exceptions=True
            )
    return server


async def _pump(source, sink) -> None:
    async for message in source:
        if message.type == aiohttp.WSMsgType.TEXT:
            await sink.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await sink.send_bytes(message.data)
        else:
            break
    await sink.close()
