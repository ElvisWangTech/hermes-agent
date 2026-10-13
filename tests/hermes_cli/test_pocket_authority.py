from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli.dashboard_auth.pocket_store import PocketAuthority


def test_codes_are_single_use_across_authority_instances(tmp_path):
    path = tmp_path / "auth.sqlite"
    authority = PocketAuthority(path)
    code = authority.issue_code("role-a")["code"]

    def exchange():
        try:
            return PocketAuthority(path).exchange(code)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: exchange(), range(2)))
    assert sum(result is not None for result in results) == 1
    result = next(result for result in results if result is not None)
    assert PocketAuthority(path).verify(result["token"])["profile"] == "role-a"


def test_devices_revoke_independently_and_role_disable_blocks_codes(tmp_path):
    authority = PocketAuthority(tmp_path / "auth.sqlite")
    first = authority.exchange(authority.issue_code("role-a")["code"])
    second = authority.exchange(authority.issue_code("role-a")["code"])
    other = authority.exchange(authority.issue_code("role-b")["code"])
    assert first["profile_id"] == second["profile_id"] != other["profile_id"]
    assert first["token"] != second["token"]
    authority.revoke_device(first["device_id"], other["profile_id"])
    assert authority.verify(first["token"])
    authority.revoke_device(first["device_id"], first["profile_id"])
    assert authority.verify(first["token"]) is None
    assert authority.verify(second["token"])
    unused = authority.issue_code("role-a")["code"]
    authority.disable_role(first["profile_id"])
    assert authority.verify(second["token"]) is None
    assert authority.verify(other["token"])
    with pytest.raises(ValueError):
        authority.exchange(unused)


def test_http_devices_cannot_select_profiles_or_admin_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import pocket_access

    authority = PocketAuthority(tmp_path / "auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    first_code = authority.issue_code("role-a")["code"]
    with TestClient(web_server.app) as client:
        first = client.post("/api/pocket/exchange", json={"code": first_code})
        assert first.status_code == 200
        assert client.post("/api/pocket/exchange", json={"code": first_code}).status_code == 401
        headers = {"X-Hermes-Session-Token": first.json()["token"]}
        assert client.get("/api/pocket/identity", headers=headers).json()["profile_id"] == first.json()["profile_id"]
        assert client.get("/api/pocket/identity?profile=role-b", headers=headers).status_code == 403
        assert client.get("/api/config", headers=headers).status_code == 403
        assert client.get("/api/pocket/admin", headers=headers).status_code == 403
        second = authority.exchange(authority.issue_code("role-a")["code"])
        assert client.delete("/api/pocket/device", headers=headers).status_code == 200
        assert client.get("/api/pocket/identity", headers=headers).status_code == 401
        assert client.get("/api/pocket/identity", headers={"X-Hermes-Session-Token": second["token"]}).status_code == 200


def test_admin_setup_creates_private_credentials_and_supports_password_reset(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from fastapi.testclient import TestClient

    specification = importlib.util.spec_from_file_location("pocket_admin_setup", Path(__file__).resolve().parents[2] / "apps/pocket-admin/server.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["server.py", "--setup"])
    target = tmp_path / "pocket-admin-auth.json"
    previous_password = None
    for password in ("initial-test-password", "replacement-test-password"):
        if target.exists():
            target.chmod(0o644)
        monkeypatch.setattr(module.getpass, "getpass", lambda prompt: password)
        module.main()
        assert target.stat().st_mode & 0o777 == 0o600
        assert password not in target.read_text()
        with TestClient(module.create_app(tmp_path)) as client:
            headers = {"X-Pocket-Admin": "1"}
            if previous_password is not None:
                assert client.post("/api/login", json={"password": previous_password}, headers=headers).status_code == 401
            assert client.post("/api/login", json={"password": password}, headers=headers).status_code == 200
        previous_password = password


def test_admin_listing_and_code_revocation_use_real_http(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from fastapi.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import pocket_access
    from hermes_cli import profiles

    specification = importlib.util.spec_from_file_location("pocket_admin_server", Path(__file__).resolve().parents[2] / "apps/pocket-admin/server.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import json
    (tmp_path / "pocket-admin-auth.json").write_text(json.dumps(module.password_record("test-password-only")))
    authority = PocketAuthority(tmp_path / "pocket-auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    with TestClient(module.create_app(tmp_path)) as client, TestClient(web_server.app) as handheld:
        assert client.get("/api/pocket/admin").status_code == 401
        device = authority.exchange(authority.issue_code("role-a")["code"])
        assert client.get("/api/pocket/admin", headers={"X-Hermes-Session-Token": device["token"]}).status_code == 401
        client.headers["X-Pocket-Admin"] = "1"
        assert client.post("/api/login", json={"password": "test-password-only"}, headers={"Origin": "https://foreign.invalid"}).status_code == 403
        assert client.post("/api/login", json={"password": "incorrect"}).status_code == 401
        login = client.post("/api/login", json={"password": "test-password-only"})
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert not profiles.get_profile_dir("role-a").exists()
        issued_response = client.post("/api/pocket/codes", json={"profile": "role-a", "create_profile": True})
        assert issued_response.status_code == 200
        assert profiles.get_profile_dir("role-a").is_dir()
        issued = issued_response.json()
        assert handheld.post("/api/pocket/codes", json={"profile": "role-a"}, headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN}).status_code in {404, 405}
        response = client.get("/api/pocket/admin")
        assert response.status_code == 200
        body = response.json()
        assert body["roles"][0]["id"] == issued["profile_id"]
        assert issued["code"] not in response.text
        assert "digest" not in response.text
        code_id = body["codes"][0]["id"]
        assert client.delete(f"/api/pocket/codes/{code_id}").status_code == 200
        assert handheld.post("/api/pocket/exchange", json={"code": issued["code"]}).status_code == 401
        cookie = client.cookies.get(module.COOKIE)
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/pocket/admin", headers={"Cookie": f"{module.COOKIE}={cookie}"}).status_code == 401
        for attempt in range(3):
            assert client.post("/api/login", json={"password": "incorrect"}).status_code == 401
        assert client.post("/api/login", json={"password": "incorrect"}).status_code == 429


def test_rpc_real_profile_stores_reject_foreign_sessions(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from hermes_cli import profiles
    from hermes_cli.dashboard_auth import pocket_access
    from hermes_state import SessionDB
    from tui_gateway import server

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", str(home))
    authority = PocketAuthority(home / "auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    transports = []
    for profile, session_id in (("role-a", "session-a"), ("role-b", "session-b")):
        target = profiles.get_profile_dir(profile)
        target.mkdir(parents=True)
        database = SessionDB(target / "state.db")
        database.create_session(session_id, source="pocket")
        database.close()
        device = authority.exchange(authority.issue_code(profile)["code"])
        transports.append(SimpleNamespace(auth_identity={"provider": "pocket", "credential": device["token"]}))
    for transport, own, foreign in ((transports[0], "session-a", "session-b"),
                                     (transports[1], "session-b", "session-a"),
                                     (transports[0], "session-a", "session-b")):
        accepted = pocket_access.admit_rpc({"method": "session.resume", "params": {"session_id": own}}, transport, server)
        assert accepted["params"]["profile"] == authority.verify(transport.auth_identity["credential"])["profile"]
        with pytest.raises(PermissionError):
            pocket_access.admit_rpc({"method": "session.resume", "params": {"session_id": foreign}}, transport, server)
        with pytest.raises(PermissionError):
            pocket_access.admit_rpc({"method": "config.get", "params": {}}, transport, server)
        assert not pocket_access.admit_output({"method": "event", "params": {"session_id": foreign}}, transport)
        from tui_gateway import server_requests
        monkeypatch.setitem(server_requests._open, "foreign-question", SimpleNamespace(sid=foreign))
        with pytest.raises(PermissionError):
            pocket_access.admit_rpc({"id": "foreign-question", "result": {}}, transport, server)


def test_device_ws_upgrade_and_existing_transport_revocation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.dashboard_auth import pocket_access
    from hermes_cli.web_server_chat import _ws_auth_reason

    authority = PocketAuthority(tmp_path / "auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    device = authority.exchange(authority.issue_code("role-a")["code"])
    websocket = SimpleNamespace(query_params={"token": device["token"]}, url=SimpleNamespace(path="/api/ws"))
    assert _ws_auth_reason(websocket) == (None, "device")
    transport = SimpleNamespace(auth_identity=websocket._hermes_auth_identity)
    assert pocket_access.admit_output({"id": 1, "result": {}}, transport)
    for path in ("/api/console", "/api/pub", "/api/events"):
        websocket.url.path = path
        assert _ws_auth_reason(websocket)[0] == "device_denied"
    authority.revoke_device(device["device_id"], device["profile_id"])
    assert not pocket_access.admit_output({"id": 1, "result": {}}, transport)
    with pytest.raises(PermissionError):
        pocket_access.admit_rpc({"id": 2, "method": "client.capabilities", "params": {}}, transport, None)