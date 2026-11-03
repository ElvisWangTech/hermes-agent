"""Phase 3+ cluster entry: Pocket control-plane endpoints are served locally,
everything else is routed, and static hosting is optional."""

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.coordination.access import AccessControl
from gateway.coordination.server import build_cluster_server
from gateway.coordination.instances import InstanceManager


@pytest.fixture
def access(env):
    def verify_device(token: str) -> dict | None:
        if token.startswith("pd_"):
            return {"profile_id": "role-a", "device_id": "device-7"}
        return None

    return AccessControl(
        env.authority,
        pepper="test-pepper",
        verify_device=verify_device,
        clock=env.clock,
        monotonic=env.clock,
    )


@pytest.fixture
def control_plane():
    state = {"exchanged": [], "revoked": []}

    def exchange_code(code: str) -> dict:
        state["exchanged"].append(code)
        if code == "bad-code":
            raise ValueError("Connection code invalid")
        return {"token": f"pd_{code}", "profile_id": "role-a"}

    def revoke_device(device_id: str, profile_id: str) -> None:
        state["revoked"].append((device_id, profile_id))

    state.update(exchange_code=exchange_code, revoke_device=revoke_device)
    return state


@pytest.fixture
def make_cluster(access, control_plane, make_manager):
    def build(*, static_root: str = ""):
        manager = make_manager()

        application = build_cluster_server(
            access,
            manager,
            exchange_code=control_plane["exchange_code"],
            revoke_device=control_plane["revoke_device"],
            static_root=static_root,
        )
        return application

    return build


@pytest_asyncio.fixture
async def client(make_cluster):
    server = TestServer(make_cluster())
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exchange_serves_the_control_plane_not_the_instance(
    client, control_plane, provider
):
    response = await client.post("/api/pocket/exchange", json={"code": "good-code"})

    assert response.status == 200
    assert (await response.json())["token"] == "pd_good-code"
    assert control_plane["exchanged"] == ["good-code"]
    assert provider.calls == []  # no instance was ever started


@pytest.mark.asyncio
async def test_invalid_connection_code_is_rejected(client, control_plane, provider):
    response = await client.post("/api/pocket/exchange", json={"code": "bad-code"})

    assert response.status == 401
    assert control_plane["exchanged"] == ["bad-code"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_identity_answers_from_the_device_credential(client, control_plane):
    response = await client.get(
        "/api/pocket/identity", headers={"X-Hermes-Session-Token": "pd_device-token"}
    )

    assert response.status == 200
    body = await response.json()
    assert body == {"profile_id": "role-a", "device_id": "device-7"}


@pytest.mark.asyncio
async def test_identity_with_a_cluster_api_key_is_rejected(client):
    response = await client.get(
        "/api/pocket/identity", headers={"Authorization": "Bearer hk_not-a-device"}
    )

    assert response.status == 401


@pytest.mark.asyncio
async def test_device_disconnect_revokes_on_the_control_plane(client, control_plane):
    response = await client.delete(
        "/api/pocket/device", headers={"X-Hermes-Session-Token": "pd_device-token"}
    )

    assert response.status == 200
    assert control_plane["revoked"] == [("device-7", "role-a")]


@pytest.mark.asyncio
async def test_other_paths_still_reach_the_router(client):
    response = await client.post("/v1/chat/completions", json={})

    assert response.status == 401  # no credential: the router handled it


@pytest.mark.asyncio
async def test_static_hosting_is_optional(tmp_path, make_cluster):
    (tmp_path / "index.html").write_text("<html>portrait</html>")
    server = TestServer(make_cluster(static_root=str(tmp_path)))
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/")
        assert response.status == 200
        assert "portrait" in (await response.text())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pocket_prefixed_assets_resolve_into_the_flat_dist(tmp_path, make_cluster):
    """The client HTML references assets as ``/pocket/assets/<hash>.js`` (vite
    base) while the deployed dist is flat. If the prefix is not stripped, every
    subresource falls to the authenticated catch-all and the page renders
    blank — which is exactly what a browser user saw on the node."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc.js").write_text("console.log(1)")
    (tmp_path / "manifest.webmanifest").write_text("{}")
    server = TestServer(make_cluster(static_root=str(tmp_path)))
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    try:
        for url in ("/pocket/assets/index-abc.js", "/pocket/manifest.webmanifest"):
            response = await client.get(url)
            assert response.status == 200, url
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pocket_prefix_without_a_file_never_becomes_a_static_hit(tmp_path, make_cluster):
    """Prefix stripping is a map, not a sitemap: a URL with no backing file
    must still fall through to the router (which 401s unauthenticated)."""
    (tmp_path / "index.html").write_text("<html>portrait</html>")
    server = TestServer(make_cluster(static_root=str(tmp_path)))
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/pocket/no-such-bundle.js")
        assert response.status == 401
    finally:
        await client.close()
