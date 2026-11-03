"""Phase 3+: the process Pocket connects to in sandbox-per-profile hosting.

Pocket's traffic mixes two owners: connection-code exchange, device identity,
and device revocation are control-plane data (the shared authority), while
conversation and per-profile state belong to the profile's instance. This app
serves the first three in-process and hands everything else to the router,
which wakes and forwards with the instance's internal key.
"""

from __future__ import annotations

from typing import Callable

from aiohttp import web

from gateway.coordination.access import (
    AccessControl,
    AccessDenied,
    Principal,
    RateLimited,
    Unauthenticated,
)
from gateway.coordination.instances import InstanceManager
from gateway.coordination.router import Gateway, _client_credential

_EXCHANGE_PATH = "/api/pocket/exchange"
_IDENTITY_PATH = "/api/pocket/identity"
_DEVICE_PATH = "/api/pocket/device"


class ClusterServer:
    """Route table: control-plane pocket endpoints, then the router."""

    def __init__(
        self,
        access: AccessControl,
        manager: InstanceManager,
        exchange_code: Callable[[str], dict],
        revoke_device: Callable[[str, str], None],
        static_root: str = "",
    ):
        self._access = access
        self._router = Gateway(access, manager)
        self._exchange_code = exchange_code
        self._revoke_device = revoke_device
        self._static_root = static_root

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_post(_EXCHANGE_PATH, self._exchange)
        application.router.add_get(_IDENTITY_PATH, self._identity)
        application.router.add_delete(_DEVICE_PATH, self._revoke)
        application.router.add_route("*", "/{tail:.*}", self._route)
        return application

    async def _route(self, request: web.Request) -> web.StreamResponse:
        """The catch-all: matching SPA assets first, then data-plane routing.

        ``?token=`` and friends never touch a static file, so checking the
        filesystem before the router cannot leak a credential anywhere.
        """
        if self._static_root and request.method in ("GET", "HEAD"):
            static = self._static_response(request)
            if static is not None:
                return static
        return await self._router.handle(request)

    def _static_response(self, request: web.Request) -> web.StreamResponse | None:
        from pathlib import Path

        try:
            root = Path(self._static_root).resolve()
            for name in self._static_names(request.path):
                path = (root / name).resolve()
                if path.is_dir():
                    path = path / "index.html"
                if path.is_relative_to(root) and path.is_file():
                    return web.FileResponse(path, status=200)
            return None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _static_names(path: str) -> list[str]:
        """Filesystem candidates for a URL: the literal path first, then — when
        the URL carries the client's ``/pocket/`` prefix — the same file in the
        flat build output. The client references its assets as ``/pocket/...``
        (vite base) while its dist is deployed at the static root itself; a
        literal-only match leaves every subresource falling through to the
        authenticated catch-all, which blanks the page."""
        literal = path.lstrip("/")
        if path.startswith("/pocket/"):
            return [literal, literal[len("pocket/"):]]
        return [literal]

    async def _exchange(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        code = str(body.get("code", "")) if isinstance(body, dict) else ""
        if not code or len(code) > 128:
            return web.json_response({"detail": "Invalid connection code"}, status=400)
        try:
            issued = self._exchange_code(code)
        except ValueError as exc:
            return web.json_response({"detail": str(exc)}, status=401)
        return web.json_response(issued)

    async def _identity(self, request: web.Request) -> web.StreamResponse:
        principal, error = self._admitted_device(request)
        if error is not None:
            return error
        return web.json_response(
            {"profile_id": principal.profile_id, "device_id": principal.credential_id}
        )

    async def _revoke(self, request: web.Request) -> web.StreamResponse:
        principal, error = self._admitted_device(request)
        if error is not None:
            return error
        self._revoke_device(principal.credential_id, principal.profile_id)
        return web.json_response({"ok": True})

    def _admitted_device(
        self, request: web.Request
    ) -> tuple[Principal | None, web.StreamResponse | None]:
        try:
            principal = self._access.resolve(_client_credential(request))
            self._access.admit(principal)
            if principal.kind != "device":
                raise AccessDenied("This endpoint takes a device credential")
        except Unauthenticated as exc:
            return None, web.json_response({"detail": str(exc)}, status=401)
        except AccessDenied as exc:
            return None, web.json_response({"detail": str(exc)}, status=403)
        except RateLimited as exc:
            return None, web.json_response({"detail": str(exc)}, status=429)
        return principal, None

    def _device_principal(self, request: web.Request) -> Principal:
        principal = self._access.resolve(_client_credential(request))
        self._access.admit(principal)
        if principal.kind != "device":
            raise AccessDenied("This endpoint takes a device credential")
        return principal


def build_cluster_server(
    access: AccessControl,
    manager: InstanceManager,
    *,
    exchange_code: Callable[[str], dict],
    revoke_device: Callable[[str, str], None],
    static_root: str = "",
) -> web.Application:
    return ClusterServer(
        access, manager, exchange_code, revoke_device, static_root=static_root
    ).app()
