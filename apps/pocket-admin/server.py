"""Independent Pocket administration service; never mount in the client server."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
import urllib.request
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr

from hermes_constants import get_process_hermes_home
from hermes_cli.dashboard_auth.pocket_store import AUTHORITY_FILENAME, PocketAuthority

COOKIE = "pocket_admin_session"
SESSION_SECONDS = 3600
_cluster: "ClusterHomes | None" = None


class ClusterHomes:
    """Cluster profile source. Roles come from the control plane's authority, a
    profile's home is ``root/<profile>`` (the directory the sandbox mounts), and the
    edit window is read from the sandbox state, never from local profile dirs."""

    def __init__(self, authority: PocketAuthority, root: Path, sandbox_states: Callable[[], dict[str, str]]):
        self.authority = authority
        self.root = root
        self.sandbox_states = sandbox_states

    def roles(self) -> list[str]:
        return [row["profile"] for row in self.authority.management_snapshot()["roles"]]

    def home(self, profile: str) -> Path:
        return self.root / profile


def cube_sandbox_states(api_url: str) -> dict[str, str]:
    """Sandbox state per profile from the Cube API. An unreachable API raises, so
    every edit is refused: the edit window fails closed."""
    try:
        with urllib.request.urlopen(api_url.rstrip("/") + "/sandboxes", timeout=10) as response:
            boxes = json.loads(response.read() or b"[]")
    except OSError as failure:
        raise ValueError("Sandbox state unavailable; refusing") from failure
    states: dict[str, str] = {}
    for box in boxes:
        profile = box.get("metadata", {}).get("hermes.profile_id", "")
        if profile:
            states[profile] = str(box.get("state", "?")).lower()
    return states


def password_record(password: str) -> dict:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Use a password between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return {"salt": salt.hex(), "digest": digest.hex()}


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class IssueBody(BaseModel):
    profile: str
    ttl: int = 600
    create_profile: bool = False


class ProfileConfigBody(BaseModel):
    model: str = Field(max_length=512)
    provider: str = Field(default="", max_length=128)
    context_length: int = Field(default=0, ge=0, le=10_000_000)
    stt: dict | None = None
    tts: dict | None = None


class CredentialBody(BaseModel):
    value: SecretStr = Field(default_factory=lambda: SecretStr(""), max_length=4096)


def _credential_slots() -> set[str]:
    from hermes_cli.auth import PROVIDER_REGISTRY

    return {key for provider in PROVIDER_REGISTRY.values() for key in provider.api_key_env_vars} | {
        "VOICE_TOOLS_OPENAI_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
        "ELEVENLABS_API_KEY", "DEEPINFRA_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY",
        "MINIMAX_API_KEY",
    }


def _profile_credentials(profile: str) -> dict:
    from hermes_cli.config import get_env_path
    from agent.secret_scope import load_env_file

    _, scope = _profile_config_scope(profile)
    with scope:
        values = load_env_file(get_env_path())
    return {key: bool(values.get(key)) for key in sorted(_credential_slots())}


def _write_profile_credential(profile: str, key: str, value: str | None) -> dict:
    from hermes_cli.credential_lifecycle import save_provider_env_credential, remove_provider_env_credential
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    if key not in _credential_slots():
        raise ValueError("Unsupported credential slot")
    if value is not None and (not value.strip() or any(character in value for character in "\r\n")):
        raise ValueError("Enter a nonempty single-line credential")
    _, scope = _profile_config_scope(profile, write=True)
    with scope, _CONFIG_MUTATION_LOCK:
        if value is None:
            remove_provider_env_credential(key)
        else:
            save_provider_env_credential(key, value.strip())
    return _profile_credentials(profile)


def _speech_settings(settings: dict) -> dict:
    allowed = {"provider", "enabled", "language", "model", "model_id", "voice", "voice_id", "speed"}
    providers = {"local", "groq", "openai", "xai", "elevenlabs", "deepinfra", "mistral",
                 "edge", "gemini", "minimax", "neutts", "kittentts", "piper"}
    result = {key: value for key, value in settings.items()
              if key in allowed and isinstance(value, (str, bool, int, float))}
    for key in providers:
        value = settings.get(key)
        if isinstance(value, dict):
            result[key] = {field: entry for field, entry in value.items()
                           if field in allowed and isinstance(entry, (str, bool, int, float))}
    return result


def _pocket_tts_settings(settings: dict, raw: dict) -> dict:
    result = _speech_settings(settings)
    if not raw.get("edge", {}).get("voice"):
        result.setdefault("edge", {})["voice"] = "zh-CN-XiaoxiaoNeural"
    return result


def create_app(home: Path, *, secure_cookies: bool = False, cluster: "ClusterHomes | None" = None) -> FastAPI:
    global _cluster
    _cluster = cluster
    record = json.loads((home / "pocket-admin-auth.json").read_text())
    salt, expected = bytes.fromhex(record["salt"]), bytes.fromhex(record["digest"])
    if len(salt) != 16 or len(expected) != 64:
        raise ValueError("Invalid administrator password record")
    authority = cluster.authority if cluster is not None else PocketAuthority(home / AUTHORITY_FILENAME)
    sessions: dict[str, float] = {}
    attempts: deque[float] = deque()
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, failure: RequestValidationError):
        return JSONResponse({"detail": "Invalid administrator request"}, status_code=422)

    @application.middleware("http")
    async def protect(request: Request, call_next):
        now = time.time()
        for token, expiry in list(sessions.items()):
            if expiry <= now:
                del sessions[token]
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            expected_origin = str(request.base_url).rstrip("/")
            if request.headers.get("X-Pocket-Admin") != "1" or origin and origin != expected_origin:
                return JSONResponse({"detail": "Same-origin administrator request required"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/login":
            if request.cookies.get(COOKIE) not in sessions:
                return JSONResponse({"detail": "Administrator login required"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.post("/api/login")
    async def login(body: LoginBody, request: Request):
        now = time.time()
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise HTTPException(429, "Too many login attempts")
        attempts.append(now)
        actual = hashlib.scrypt(body.password.encode(), salt=salt, n=16384, r=8, p=1)
        if not hmac.compare_digest(actual, expected):
            raise HTTPException(401, "Invalid administrator credentials")
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        token = secrets.token_urlsafe(32)
        sessions[token] = now + SESSION_SECONDS
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=secure_cookies, samesite="strict", path="/")
        return response

    @application.get("/api/session")
    def session():
        return {"ok": True}

    @application.post("/api/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @application.get("/api/pocket/admin")
    def snapshot():
        from hermes_cli.profiles import list_profiles

        result = authority.management_snapshot()
        if _cluster is not None:
            result["profiles"] = [role["profile"] for role in result["roles"]]
            return result
        result["profiles"] = [item.name for item in list_profiles(lazy_skill_count=True) if item.name != "default"]
        return result

    @application.post("/api/pocket/codes")
    def issue(body: IssueBody):
        from hermes_cli.profiles import create_profile, get_profile_dir, normalize_profile_name

        try:
            profile = normalize_profile_name(body.profile)
            if profile in {"default", "current"}:
                raise ValueError("Choose an independent named profile")
            if _cluster is not None:
                if not body.create_profile and profile not in _cluster.roles():
                    raise ValueError("Profile does not exist; choose create profile")
                return authority.issue_code(profile, ttl=body.ttl)
            profile_dir = get_profile_dir(profile)
            if body.create_profile:
                if profile_dir.exists():
                    raise ValueError(f"Profile '{profile}' already exists")
                create_profile(profile, no_alias=True)
                _copy_default_model_config(profile_dir)
            elif not profile_dir.is_dir():
                raise ValueError("Profile does not exist; choose create profile")
            return authority.issue_code(profile, ttl=body.ttl)
        except ValueError as failure:
            raise HTTPException(400, str(failure)) from failure

    @application.get("/api/pocket/profiles/{profile}/config")
    def profile_config(profile: str):
        try:
            return _read_profile_model_config(profile)
        except ValueError as failure:
            raise HTTPException(400, str(failure)) from failure

    @application.get("/api/pocket/profiles/{profile}/credentials")
    def profile_credentials(profile: str):
        try:
            return _profile_credentials(profile)
        except ValueError:
            raise HTTPException(400, "Invalid credential target") from None

    @application.put("/api/pocket/profiles/{profile}/credentials/{key}")
    def update_credential(profile: str, key: str, body: CredentialBody):
        try:
            return _write_profile_credential(profile, key, body.value.get_secret_value())
        except ValueError:
            raise HTTPException(400, "Credential could not be saved; check target and value") from None

    @application.delete("/api/pocket/profiles/{profile}/credentials/{key}")
    def delete_credential(profile: str, key: str):
        try:
            return _write_profile_credential(profile, key, None)
        except ValueError:
            raise HTTPException(400, "Credential could not be removed") from None

    @application.get("/api/pocket/profiles/{profile}/options")
    def profile_options(profile: str):
        from hermes_cli.inventory import build_model_options_payload, load_picker_context
        from hermes_cli.config import DEFAULT_CONFIG

        try:
            _, scope = _profile_config_scope(profile)
            with scope:
                options = build_model_options_payload(load_picker_context(), include_unconfigured=True)
        except ValueError as failure:
            raise HTTPException(400, str(failure)) from failure
        return {"providers": [{"slug": row["slug"], "name": row.get("name", row["slug"]),
                               "models": row.get("models", [])} for row in options["providers"]],
                "stt": _speech_settings(DEFAULT_CONFIG["stt"]),
                "tts": _pocket_tts_settings(DEFAULT_CONFIG["tts"], {})}

    @application.put("/api/pocket/profiles/{profile}/config")
    def update_profile_config(profile: str, body: ProfileConfigBody):
        try:
            return _write_profile_model_config(profile, body)
        except ValueError as failure:
            raise HTTPException(400, str(failure)) from failure

    @application.delete("/api/pocket/codes/{code_id}")
    def revoke_code(code_id: int):
        if not authority.revoke_code(code_id):
            raise HTTPException(404, "Connection code not found")
        return {"ok": True}

    @application.delete("/api/pocket/roles/{role_id}/devices/{device_id}")
    def revoke_device(role_id: str, device_id: str):
        authority.revoke_device(device_id, role_id)
        return {"ok": True}

    @application.delete("/api/pocket/roles/{role_id}/authorization")
    def disable(role_id: str):
        profile = authority.role_profile(role_id)
        if _cluster is not None and profile and _cluster.home(profile).is_dir():
            if _cluster.sandbox_states().get(profile, "absent") != "paused":
                raise HTTPException(409, "Sandbox must be idle-paused before disabling: its schedules are held inside it")
        authority.disable_role(role_id)
        held = bool(profile) and _hold_profile_schedules(profile)
        return {"ok": True, "schedules_held": held}

    @application.get("/{asset_path:path}")
    def assets(asset_path: str):
        from hermes_cli.pocket_pages import _asset

        dist = Path(__file__).parent / "dist"
        target = _asset(dist, asset_path or "index.html")
        if target is None:
            raise HTTPException(404, "Not found")
        return FileResponse(target)

    return application


def _hold_profile_schedules(profile: str) -> bool:
    """Pause this profile's schedules when authorization is turned off.

    A later authorization does not resume them; the user continues each one.
    Returns False when there is nothing to hold (no profile home yet).
    """
    try:
        _normalized, scope = _profile_config_scope(profile, write=True)
    except ValueError:
        return False
    with scope:
        from hermes_cli.pocket_schedule import hold_for_authorization

        hold_for_authorization()
    return True


def _profile_config_scope(profile: str, *, write: bool = False):
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name
    from hermes_cli.web_server_profiles import _config_profile_scope

    normalized = normalize_profile_name(profile)
    if normalized in {"default", "current"}:
        raise ValueError("Choose an independent named profile")
    if _cluster is not None:
        return normalized, _cluster_scope(normalized, write=write)
    profile_dir = get_profile_dir(normalized)
    if not profile_dir.is_dir():
        raise ValueError("Profile does not exist")
    return normalized, _config_profile_scope(normalized)


def _cluster_scope(profile: str, *, write: bool):
    """Scope onto the profile's node home. Writes need the sandbox idle-paused: a
    paused sandbox resumes and re-reads the home, while a running or absent one
    would clobber the edit or be restored over it (SnapshotService semantics)."""
    if profile not in _cluster.roles():
        raise ValueError("Profile does not exist")
    home = _cluster.home(profile)
    if not home.is_dir():
        raise ValueError("Profile home does not exist yet: the profile has never run")
    if write and _cluster.sandbox_states().get(profile, "absent") != "paused":
        raise ValueError("Sandbox is not paused: edits wait for the idle pause")
    return _scoped_home(home)


@contextmanager
def _scoped_home(home: Path):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_cli.web_server_profiles import _hermes_home_scope
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting

    activate_multi_profile_hosting()
    hydrate_profile_secret_sources(home)
    secrets = build_profile_secret_scope(home)
    with _hermes_home_scope(home):
        token = set_secret_scope(secrets, profile_home=str(home))
        try:
            yield
        finally:
            reset_secret_scope(token)


def _read_profile_model_config(profile: str) -> dict:
    from hermes_cli.config import load_config, read_raw_config

    normalized, scope = _profile_config_scope(profile)
    with scope:
        config = load_config()
        raw_tts = read_raw_config().get("tts", {})
    model = config.get("model", "")
    if isinstance(model, dict):
        provider = str(model.get("provider", ""))
        model_name = str(model.get("default", model.get("name", model.get("model", ""))))
    else:
        provider = ""
        model_name = str(model or "")
    return {
        "profile": normalized,
        "model": model_name,
        "provider": provider,
        "context_length": int(model.get("context_length", config.get("model_context_length", 0)) if isinstance(model, dict) else 0),
        "stt": _speech_settings(config.get("stt", {})),
        "tts": _pocket_tts_settings(config.get("tts", {}), raw_tts),
    }


def _write_profile_model_config(profile: str, body: ProfileConfigBody) -> dict:
    from hermes_cli.config import load_config, save_config
    from hermes_cli.web_server_config import _normalize_main_model_assignment, _validated_main_model_selection, _apply_main_model_assignment
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    normalized, scope = _profile_config_scope(profile, write=True)
    with scope:
        config = load_config()
        current = config.get("model", {})
        current_name = current.get("default", current.get("name", current.get("model", ""))) if isinstance(current, dict) else current
        current_provider = current.get("provider", "") if isinstance(current, dict) else ""
        result = None
        if (body.provider, body.model) != (current_provider, current_name):
            provider, model = _normalize_main_model_assignment(body.provider, body.model)
            result = _validated_main_model_selection(config, provider, model)
    _, scope = _profile_config_scope(normalized, write=True)
    with scope, _CONFIG_MUTATION_LOCK:
        config = load_config()
        current = config.get("model", {})
        if result is not None:
            config["model"] = _apply_main_model_assignment(current, result)
        elif not isinstance(current, dict):
            config["model"] = {"default": current_name, "provider": current_provider}
        config["model"]["context_length"] = body.context_length
        config.pop("model_context_length", None)
        preserve_keys = set()
        for section in ("stt", "tts"):
            changes = getattr(body, section)
            if changes is not None:
                safe = _speech_settings(changes)
                if safe != changes:
                    raise ValueError("Speech settings must not contain credentials or unsupported fields")
                existing = config.setdefault(section, {})
                for key, value in safe.items():
                    if isinstance(value, dict):
                        existing.setdefault(key, {}).update(value)
                        preserve_keys.update((section, key, field) for field in value)
                    else:
                        existing[key] = value
                        preserve_keys.add((section, key))
        save_config(config, preserve_keys=preserve_keys)
    return _read_profile_model_config(normalized)


def _copy_default_model_config(profile_dir: Path) -> None:
    from hermes_cli.config import load_config, read_raw_config, save_config
    from hermes_cli.web_server_profiles import _config_profile_scope
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    with _config_profile_scope("default"):
        source = load_config()
        source["tts"] = _pocket_tts_settings(source.get("tts", {}), read_raw_config().get("tts", {}))
    model = source.get("model", "")
    profile = profile_dir.name
    with _config_profile_scope(profile), _CONFIG_MUTATION_LOCK:
        target = load_config()
        target["model"] = ({key: value for key, value in model.items()
                            if key in {"default", "name", "provider", "context_length", "api_mode"}}
                           if isinstance(model, dict) else model)
        for section in ("stt", "tts"):
            target[section] = _speech_settings(source.get(section, {}))
        preserve_keys = set()
        for section in ("stt", "tts"):
            for key, value in target[section].items():
                if isinstance(value, dict):
                    preserve_keys.update((section, key, field) for field in value)
                else:
                    preserve_keys.add((section, key))
        save_config(target, preserve_keys=preserve_keys)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9122)
    parser.add_argument("--secure-cookies", action="store_true")
    parser.add_argument("--cluster-root", default="", help="node profile homes, e.g. /data/shared/hermes-homes")
    parser.add_argument("--cluster-cube-api", default="http://127.0.0.1:3000")
    options = parser.parse_args()
    home = Path(get_process_hermes_home())
    if options.setup:
        password = getpass.getpass("Administrator password (12+ characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
        record = password_record(password)
        home.mkdir(parents=True, exist_ok=True)
        target = home / "pocket-admin-auth.json"
        with open(target, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            target.chmod(0o600)
            json.dump(record, output)
        print("Administrator password configured. Restart the admin service to invalidate sessions.")
        return
    if options.host not in {"127.0.0.1", "::1", "localhost"} and not options.secure_cookies:
        parser.error("Remote deployment requires HTTPS and --secure-cookies")
    import uvicorn

    cluster = None
    if options.cluster_root:
        cluster = ClusterHomes(
            PocketAuthority(home / AUTHORITY_FILENAME),
            Path(options.cluster_root),
            lambda: cube_sandbox_states(options.cluster_cube_api),
        )
    uvicorn.run(create_app(home, secure_cookies=options.secure_cookies, cluster=cluster),
                host=options.host, port=options.port)


if __name__ == "__main__":
    main()