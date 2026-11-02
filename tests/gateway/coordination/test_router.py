"""Phase 3 routing: the gateway forwards with the instance key, never the client credential."""

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.coordination.access import AccessControl
from gateway.coordination.instances import Started
from gateway.coordination.router import Gateway


def _instance_app(state: dict, seen: list):
    async def chat(request):
        state["queries"].append(request.rel_url.query_string)
        seen.append(dict(request.headers))
        if request.headers.get("Authorization") != f"Bearer {state['key']}":
            return web.json_response({"detail": "instance refused"}, status=401)
        return web.json_response({"ok": True, "path": request.path_qs})

    async def stream(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for index in range(2):
            await response.write(f"data: {index}\n\n".encode())
        await response.write_eof()
        return response

    async def echo_ws(request):
        state["queries"].append(request.rel_url.query_string)
        seen.append(dict(request.headers))
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            if message.type == aiohttp.WSMsgType.TEXT:
                await socket.send_str("echo:" + message.data)
        return socket

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/stream", stream)
    app.router.add_get("/api/ws", echo_ws)
    return app


async def _build_stack(env, provider, make_manager, access=None):
    state: dict = {"key": None, "queries": []}
    seen: list = []
    instance_server = TestServer(_instance_app(state, seen))
    await instance_server.start_server()
    try:
        if hasattr(provider, "endpoint"):
            provider.endpoint = str(instance_server.make_url("")).rstrip("/")
        manager = make_manager(prov=provider)
        if access is None:
            access = AccessControl(
                env.authority,
                pepper="test-pepper",
                verify_device=lambda token: None,
                clock=env.clock,
                monotonic=env.clock,
            )
        key = access.create_api_key("role-a")
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", Gateway(access, manager).handle)
        client = TestClient(TestServer(app))
        await client.start_server()
        state["key"] = manager.ensure_running("role-a").key
        try:
            yield SimpleStack(client, key, state, seen)
        finally:
            await client.close()
    finally:
        await instance_server.close()


@pytest_asyncio.fixture
async def stack(env, provider, make_manager):
    async for step in _build_stack(env, provider, make_manager):
        yield step


class RoutedProvider:
    """A provider whose data plane routes on Host (as the Cube one does)."""

    endpoint = ""

    def start(self, instance_id, state_dir, env):
        return Started(self.endpoint, "9119-instance-host", "8642-instance-host")

    def pause(self, instance_id): ...

    def resume(self, instance_id): ...

    def stop(self, instance_id): ...


@pytest_asyncio.fixture
async def routed_stack(env, make_manager):
    async for step in _build_stack(env, RoutedProvider(), make_manager):
        yield step

@pytest_asyncio.fixture
async def pocket_stack(env, make_manager):
    """A stack whose gateway accepts Pocket device credentials."""

    def verify_device(token: str) -> dict | None:
        if token.startswith("pd_"):
            return {"profile_id": "role-a", "device_id": "device-7"}
        return None

    from gateway.coordination.access import AccessControl

    access = AccessControl(
        env.authority,
        pepper="test-pepper",
        verify_device=verify_device,
        clock=env.clock,
        monotonic=env.clock,
    )
    async for step in _build_stack(env, RoutedProvider(), make_manager, access=access):
        yield step


class SimpleStack:
    def __init__(self, client, key, state, seen):
        self.client = client
        self.key = key
        self.state = state
        self.seen = seen


@pytest.mark.asyncio
async def test_request_is_forwarded_with_the_instance_key_only(stack):
    response = await stack.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {stack.key}"},
        json={"model": "x"},
    )

    assert response.status == 200
    assert (await response.json())["ok"] is True
    forwarded = stack.seen[-1]["Authorization"]
    assert forwarded == f"Bearer {stack.state['key']}"
    assert stack.key not in forwarded


@pytest.mark.asyncio
async def test_missing_credential_never_reaches_the_instance(stack):
    response = await stack.client.post("/v1/chat/completions", json={})

    assert response.status == 401
    assert stack.seen == []


@pytest.mark.asyncio
async def test_server_sent_events_are_streamed_through(stack):
    response = await stack.client.get(
        "/v1/stream", headers={"Authorization": f"Bearer {stack.key}"}
    )

    assert await response.text() == "data: 0\n\ndata: 1\n\n"


@pytest.mark.asyncio
async def test_api_key_cannot_reach_the_dashboard_websocket(stack):
    """An API key is for /v1 only; the dashboard socket belongs to Pocket devices."""
    response = await stack.client.get(
        "/api/ws", headers={"Authorization": f"Bearer {stack.key}"}
    )

    assert response.status == 403
    assert stack.seen == []


@pytest.mark.asyncio
async def test_profile_header_for_another_profile_is_forbidden(stack):
    response = await stack.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {stack.key}", "X-Hermes-Profile": "role-b"},
        json={},
    )

    assert response.status == 403
    assert stack.seen == []


@pytest.mark.asyncio
async def test_routed_endpoints_forward_the_data_plane_host_header(routed_stack):
    """Host-routed data planes (the Cube proxy) need the header untouched."""
    response = await routed_stack.client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {routed_stack.key}"},
        json={"model": "x"},
    )

    assert response.status == 200
    assert routed_stack.seen[-1]["Host"] == "8642-instance-host"


@pytest.mark.asyncio
async def test_api_key_websocket_upgrade_is_refused_before_the_instance_wakes(routed_stack):
    response = await routed_stack.client.get(
        "/api/ws", headers={"Authorization": f"Bearer {routed_stack.key}"}
    )

    assert response.status == 403
    assert routed_stack.seen == []


@pytest.mark.asyncio
async def test_pocket_device_token_in_a_header_authenticates_and_is_never_forwarded(
    pocket_stack,
):
    """Pocket sends its device token as a session header — the instance sees
    only the instance key and the gateway-asserted principal."""
    response = await pocket_stack.client.post(
        "/v1/chat/completions",
        headers={"X-Hermes-Session-Token": "pd_device-token"},
        json={"model": "x"},
    )

    assert response.status == 200
    forwarded = pocket_stack.seen[-1]
    assert forwarded["Authorization"] == f"Bearer {pocket_stack.state['key']}"
    assert forwarded["X-Hermes-Principal"] == "device:device-7"
    assert forwarded["Host"] == "9119-instance-host"
    assert forwarded["X-Hermes-Cluster-Profile"] == "role-a"
    assert forwarded.get("X-Hermes-Session-Token") is None
    assert "pd_device-token" not in str(forwarded)


@pytest.mark.asyncio
async def test_pocket_websocket_token_query_is_stripped_from_the_forwarded_url(
    pocket_stack,
):
    socket = await pocket_stack.client.ws_connect(
        "/api/ws?token=pd_device-token&extra=1"
    )
    await socket.send_str("hi")
    reply = await socket.receive_str()
    await socket.close()

    assert reply == "echo:hi"
    forwarded = pocket_stack.seen[-1]
    assert forwarded["Authorization"] == f"Bearer {pocket_stack.state['key']}"
    assert forwarded["X-Hermes-Principal"] == "device:device-7"
    assert forwarded["Host"] == "9119-instance-host"
    assert "pd_device-token" not in pocket_stack.state["queries"][-1]
    assert "extra=1" in pocket_stack.state["queries"][-1]
