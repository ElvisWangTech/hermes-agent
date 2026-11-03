"""Cluster-managed instance admission: the gateway-asserted principal is
trusted because the request carries the instance's own internal key, and no
local device rows are consulted or assumed."""

from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers


@pytest.fixture
def cluster_env(monkeypatch, tmp_path):
    """A managed instance: internal key in the env, authority rigged to blow up
    if anything consults local device rows."""
    from hermes_cli.dashboard_auth import pocket_access

    monkeypatch.setenv("API_SERVER_KEY", "instance-internal-key")
    monkeypatch.setattr(
        pocket_access, "authority", lambda: pytest.fail("local device rows consulted")
    )
    return {"key": "instance-internal-key"}


def _cluster_headers(key: str, *, path: str = "/api/ws") -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "X-Hermes-Principal": "device:device-7",
        "X-Hermes-Cluster-Profile": "role-a",
    }


def test_http_request_from_the_gateway_binds_the_asserted_identity(
    cluster_env, monkeypatch
):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import pocket_access

    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    with TestClient(web_server.app) as client:
        response = client.get("/api/pocket/identity", headers=_cluster_headers(cluster_env["key"]))

    assert response.status_code == 200
    assert response.json() == {"profile_id": "role-a", "device_id": "device-7"}


def test_a_wrong_internal_key_is_not_admitted(cluster_env, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server

    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    with TestClient(web_server.app) as client:
        response = client.get("/api/pocket/identity", headers=_cluster_headers("wrong-key"))

    assert response.status_code == 401


def test_missing_principal_header_is_not_admitted(cluster_env):
    from hermes_cli.dashboard_auth.pocket_access import cluster_identity

    assert cluster_identity(f"Bearer {cluster_env['key']}", "", "role-a") is None
    assert cluster_identity(
        f"Bearer {cluster_env['key']}", "api_key:some-id", "role-a"
    ) is None


def test_without_an_internal_key_the_instance_is_unmanaged():
    from hermes_cli.dashboard_auth.pocket_access import cluster_identity

    assert cluster_identity("Bearer anything", "device:device-7", "role-a") is None


def test_ws_upgrade_asserts_the_identity_on_the_api_path_only(cluster_env):
    from hermes_cli.web_server_chat import _ws_auth_reason

    websocket = SimpleNamespace(
        query_params={"token": ""},
        url=SimpleNamespace(path="/api/ws"),
        headers=Headers(_cluster_headers(cluster_env["key"])),
    )
    assert _ws_auth_reason(websocket) == (None, "device")
    identity = websocket._hermes_auth_identity
    assert identity["provider"] == "pocket" and identity["cluster"] is True
    assert identity["profile"] == "role-a" and identity["device_id"] == "device-7"

    websocket.url.path = "/api/console"
    assert _ws_auth_reason(websocket)[0] == "device_denied"


def test_cluster_identity_skips_local_revocation_checks(rpc_spy):
    from hermes_cli.dashboard_auth import pocket_access

    transport = SimpleNamespace(
        auth_identity={
            "provider": "pocket",
            "cluster": True,
            "credential": "device-7",
            "profile": "role-a",
            "profile_id": "role-a",
            "device_id": "device-7",
        }
    )

    accepted = pocket_access.admit_rpc(
        {"method": "session.list", "params": {"limit": 5}}, transport, rpc_spy
    )
    assert "profile" not in accepted["params"]  # the instance is already this profile

    outgone = {"id": 1, "result": {}}
    assert pocket_access.admit_output(outgone, transport) is True


def test_local_identity_still_reverifies_against_the_authority(rpc_spy, monkeypatch, tmp_path):
    from hermes_cli.dashboard_auth import pocket_access
    from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

    authority = PocketAuthority(tmp_path / "auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    device = authority.exchange(authority.issue_code("role-a")["code"])
    identity = {**authority.verify(device["token"]), "provider": "pocket",
                "credential": device["token"]}
    transport = SimpleNamespace(auth_identity=identity)

    assert pocket_access.admit_output({"id": 1, "result": {}}, transport) is True
    authority.revoke_device(device["device_id"], device["profile_id"])
    assert pocket_access.admit_output({"id": 1, "result": {}}, transport) is False


def test_revocation_accepts_the_profile_name_the_cluster_sends(rpc_spy, monkeypatch, tmp_path):
    """The cluster gateway routes with the profile NAME; a revoke addressed that
    way must still land (the single-machine path passes the role's internal id)."""
    from hermes_cli.dashboard_auth import pocket_access
    from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

    authority = PocketAuthority(tmp_path / "auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    device = authority.exchange(authority.issue_code("role-a")["code"])
    identity = {**authority.verify(device["token"]), "provider": "pocket",
                "credential": device["token"]}
    transport = SimpleNamespace(auth_identity=identity)

    assert pocket_access.admit_output({"id": 1, "result": {}}, transport) is True
    authority.revoke_device(device["device_id"], "role-a")
    assert pocket_access.admit_output({"id": 1, "result": {}}, transport) is False


@pytest.fixture
def rpc_spy(tmp_path):
    """A stand-in server exposing only the attributes admit_rpc touches."""
    from contextlib import nullcontext

    return SimpleNamespace(
        _sessions_lock=nullcontext(),
        _sessions={},
        _profile_home=lambda profile: tmp_path / profile,
    )
