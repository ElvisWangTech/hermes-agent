import runpy
from pathlib import Path

import pytest


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "apps/pocket-admin/server.py"))
    return home, module


def test_profile_snapshot_and_speech_writes_are_isolated(admin_env):
    home, admin = admin_env
    from hermes_cli.config import load_config, save_config
    from hermes_cli.profiles import create_profile, get_profile_dir
    from hermes_cli.web_server_profiles import _config_profile_scope

    original = {"model": {"default": "uncatalogued-model", "provider": "deepseek", "api_key": "secret"},
                "stt": {"provider": "openai", "openai": {"model": "whisper-1", "api_key": "secret"}},
                "tts": {"provider": "edge", "edge": {"voice": "en-US-AriaNeural"}}}
    with _config_profile_scope("default"):
        save_config(original)
    for name in ("role-a", "role-b"):
        create_profile(name, no_alias=True)
        admin["_copy_default_model_config"](get_profile_dir(name))
    first = admin["_read_profile_model_config"]("role-a")
    first["context_length"] = 32768
    first["stt"]["enabled"] = False
    first["tts"]["edge"]["voice"] = "zh-CN-YunxiNeural"
    body = admin["ProfileConfigBody"](**first)
    updated = admin["_write_profile_model_config"]("role-a", body)
    assert updated["context_length"] == 32768
    assert updated["stt"]["enabled"] is False
    assert updated["tts"]["edge"]["voice"] == "zh-CN-YunxiNeural"
    other = admin["_read_profile_model_config"]("role-b")
    assert other["context_length"] != updated["context_length"]
    assert other["tts"]["edge"]["voice"] != updated["tts"]["edge"]["voice"]
    assert admin["_read_profile_model_config"]("role-a") == updated
    with _config_profile_scope("role-a"):
        saved = load_config()
        assert "api_key" not in saved["model"]
        assert "api_key" not in saved["stt"]["openai"]
        assert saved["model"]["context_length"] == updated["context_length"]
    with _config_profile_scope("default"):
        assert load_config()["model"]["api_key"] == "secret"
    from hermes_yaml import safe_load
    from hermes_cli.config import DEFAULT_CONFIG
    current = admin["_read_profile_model_config"]("role-b")
    current["tts"] = {"provider": DEFAULT_CONFIG["tts"]["provider"],
                      "edge": {"voice": DEFAULT_CONFIG["tts"]["edge"]["voice"]}}
    admin["_write_profile_model_config"]("role-b", admin["ProfileConfigBody"](**current))
    raw = safe_load((get_profile_dir("role-b") / "config.yaml").read_text())
    assert raw["tts"]["provider"] == current["tts"]["provider"]
    assert raw["tts"]["edge"]["voice"] == current["tts"]["edge"]["voice"]


def test_profile_config_rejects_secrets_and_default_target(admin_env):
    _, admin = admin_env
    from hermes_cli.profiles import create_profile
    create_profile("role-a", no_alias=True)
    current = admin["_read_profile_model_config"]("role-a")
    assert current["tts"]["edge"]["voice"] == "zh-CN-XiaoxiaoNeural"
    from hermes_cli.config import load_config, save_config
    from hermes_cli.web_server_profiles import _config_profile_scope
    with _config_profile_scope("role-a"):
        config = load_config()
        config["tts"]["edge"]["voice"] = "en-US-AriaNeural"
        save_config(config, preserve_keys={("tts", "edge", "voice")})
    assert admin["_read_profile_model_config"]("role-a")["tts"]["edge"]["voice"] == "en-US-AriaNeural"
    body = admin["ProfileConfigBody"](**{**current, "tts": {"openai": {"api_key": "secret"}}})
    with pytest.raises(ValueError, match="credentials"):
        admin["_write_profile_model_config"]("role-a", body)
    with pytest.raises(ValueError, match="independent"):
        admin["_read_profile_model_config"]("default")


def test_profile_credentials_rotate_without_cross_profile_leaks(admin_env):
    import json
    import os
    import subprocess
    import sys
    from unittest.mock import MagicMock
    from run_agent import AIAgent
    from agent.secret_scope import build_profile_secret_scope, get_secret, set_secret_scope, reset_secret_scope
    from hermes_cli.profiles import create_profile, get_profile_dir

    _, admin = admin_env
    for name in ("role-a", "role-b"):
        create_profile(name, no_alias=True)
    key = "OPENAI_API_KEY"
    status = admin["_write_profile_credential"]("role-a", key, "dummy-first")
    assert status[key] is True
    assert "dummy-first" not in json.dumps(status)
    home = get_profile_dir("role-a")
    frozen = build_profile_secret_scope(home)
    agent = object.__new__(AIAgent)
    agent.provider = agent.requested_provider = "openai-api"
    agent.api_mode = "chat_completions"
    agent.base_url = "https://api.openai.com/v1"
    agent.api_key = "dummy-first"
    agent._client_kwargs = {"base_url": agent.base_url, "api_key": agent.api_key}
    agent._fallback_activated = False
    agent._env_creds_seen = (agent.base_url, agent.api_key)
    agent._replace_primary_openai_client = MagicMock(return_value=True)
    agent._reapply_route_client_config = MagicMock()
    script = "from agent.secret_scope import load_env_file; from pathlib import Path; import sys; print(load_env_file(Path(sys.argv[1])).get('OPENAI_API_KEY', ''))"
    child_env = {**os.environ, "HERMES_HOME": str(home)}
    assert subprocess.check_output([sys.executable, "-c", script, str(home / ".env")], env=child_env, text=True).strip() == "dummy-first"
    admin["_write_profile_credential"]("role-a", key, "dummy-second")
    assert subprocess.check_output([sys.executable, "-c", script, str(home / ".env")], env=child_env, text=True).strip() == "dummy-second"
    assert frozen[key] == "dummy-first"
    from hermes_cli.web_server_profiles import _config_profile_scope
    with _config_profile_scope("role-a"):
        assert agent._try_refresh_env_client_credentials() is True
    assert agent.api_key == agent._client_kwargs["api_key"] == "dummy-second"
    agent._replace_primary_openai_client.assert_called_once_with(reason="env_credential_refresh")
    for name, expected in (("role-a", "dummy-second"), ("role-b", None), ("role-a", "dummy-second")):
        target = get_profile_dir(name)
        token = set_secret_scope(build_profile_secret_scope(target), profile_home=str(target))
        try:
            assert get_secret(key) == expected
        finally:
            reset_secret_scope(token)
    assert admin["_write_profile_credential"]("role-a", key, None)[key] is False
    assert key not in build_profile_secret_scope(home)
    with pytest.raises(ValueError, match="Unsupported"):
        admin["_write_profile_credential"]("role-a", "PATH", "invalid")


def test_credential_api_requires_login_and_never_echoes_inputs(admin_env):
    import json
    from fastapi.testclient import TestClient
    from hermes_cli.profiles import create_profile

    home, admin = admin_env
    password = "test-admin-password"
    (home / "pocket-admin-auth.json").write_text(json.dumps(admin["password_record"](password)))
    create_profile("role-a", no_alias=True)
    client = TestClient(admin["create_app"](home))
    path = "/api/pocket/profiles/role-a/credentials/OPENAI_API_KEY"
    headers = {"X-Pocket-Admin": "1"}
    assert client.put(path, json={"value": "dummy-key"}, headers=headers).status_code == 401
    assert client.post("/api/login", json={"password": password}, headers=headers).status_code == 200
    assert client.put(path, json={"value": "dummy-key"}).status_code == 403
    response = client.put(path, json={"value": "dummy-key"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["OPENAI_API_KEY"] is True
    assert "dummy-key" not in response.text
    invalid = "sensitive-input" * 400
    response = client.put(path, json={"value": invalid}, headers=headers)
    assert response.status_code == 422
    assert "sensitive-input" not in response.text
    assert client.delete(path, headers=headers).json()["OPENAI_API_KEY"] is False

PASSWORD = "correct horse battery"


@pytest.fixture
def cluster_admin(tmp_path, monkeypatch):
    import json

    from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    admin = runpy.run_path(str(Path(__file__).resolve().parents[2] / "apps/pocket-admin/server.py"))
    (home / "pocket-admin-auth.json").write_text(json.dumps(admin["password_record"](PASSWORD)))
    nodes = tmp_path / "hermes-homes"
    nodes.mkdir()
    authority = PocketAuthority(home / "pocket-auth.sqlite")
    for profile in ("role-a", "role-b"):
        authority.issue_code(profile)
        (nodes / profile).mkdir()
    states = {"role-a": "paused", "role-b": "running"}
    cluster = admin["ClusterHomes"](authority, nodes, lambda: dict(states))
    return home, admin, cluster, states, authority, nodes


def test_cluster_writes_land_in_the_node_home_only_while_paused(cluster_admin):
    home, admin, cluster, states, authority, nodes = cluster_admin
    admin["create_app"](home, cluster=cluster)
    from hermes_yaml import safe_load

    paused = admin["_read_profile_model_config"]("role-a")
    paused["context_length"] = 32768
    updated = admin["_write_profile_model_config"]("role-a", admin["ProfileConfigBody"](**paused))
    assert updated["context_length"] == 32768
    assert safe_load((nodes / "role-a" / "config.yaml").read_text())["model"]["context_length"] == 32768

    running = admin["_read_profile_model_config"]("role-b")
    assert running["profile"] == "role-b"
    with pytest.raises(ValueError, match="not paused"):
        admin["_write_profile_model_config"]("role-b", admin["ProfileConfigBody"](**running))
    assert not (nodes / "role-b" / "config.yaml").exists()
    assert not (home / "profiles").exists()  # nothing created in the local profile tree


def test_cluster_refuses_unknown_profiles_and_homes_that_never_ran(cluster_admin):
    home, admin, cluster, states, authority, nodes = cluster_admin
    admin["create_app"](home, cluster=cluster)
    with pytest.raises(ValueError, match="does not exist"):
        admin["_read_profile_model_config"]("ghost")
    (nodes / "role-b").rmdir()
    with pytest.raises(ValueError, match="never run"):
        admin["_read_profile_model_config"]("role-b")


def test_cluster_api_lists_roles_and_defers_disable_while_the_sandbox_runs(cluster_admin):
    from fastapi.testclient import TestClient

    home, admin, cluster, states, authority, nodes = cluster_admin
    client = TestClient(admin["create_app"](home, cluster=cluster))
    assert client.post("/api/login", json={"password": PASSWORD}, headers={"X-Pocket-Admin": "1"}).status_code == 200

    snapshot = client.get("/api/pocket/admin").json()
    assert sorted(snapshot["profiles"]) == ["role-a", "role-b"]

    refused = client.post("/api/pocket/codes", json={"profile": "ghost"}, headers={"X-Pocket-Admin": "1"})
    assert refused.status_code == 400

    role_b_id = next(role["id"] for role in snapshot["roles"] if role["profile"] == "role-b")
    blocked = client.delete(f"/api/pocket/roles/{role_b_id}/authorization", headers={"X-Pocket-Admin": "1"})
    assert blocked.status_code == 409
    still = next(role for role in client.get("/api/pocket/admin").json()["roles"] if role["profile"] == "role-b")
    assert still["active"]
