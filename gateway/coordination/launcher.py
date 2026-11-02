"""Cluster gateway entry point: ``python -m gateway.coordination.launcher``.

One process serves Pocket's control-plane endpoints (connection-code exchange, device
identity, device revocation), the Pocket client files, and routes everything else to
the profile's sandbox instance. Sandboxes come from the Cube provider (``CUBE_*``
variables, ``hermes_cube_provider`` must be importable), so this process must run on
a node where the host mount paths exist.

Configuration lives in ``--home`` (a HERMES_HOME): ``config.yaml`` carries the
``cluster`` section, and ``.env`` carries ``OBJECT_STORE_ACCESS_KEY``,
``OBJECT_STORE_SECRET_KEY`` and ``CLUSTER_KEY_PEPPER``. The control-plane database is
``pocket-auth.sqlite`` in the same directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from pathlib import Path

from aiohttp import web

from gateway.coordination.access import AccessControl
from gateway.coordination.authority import ControlPlaneAuthority
from gateway.coordination.config import load_cluster_config, require_cluster_ready
from gateway.coordination import create_snapshot_service
from gateway.coordination.instances import InstanceManager
from gateway.coordination.server import build_cluster_server
from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

AUTHORITY_FILE = "pocket-auth.sqlite"
IDLE_PAUSE_SEC = 900.0
_LOOP_MIN_INTERVAL_SEC = 15.0


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _secret(name: str, file_values: dict[str, str]) -> str:
    return os.environ.get(name) or file_values.get(name, "")


def build_app(home: Path, node_root: Path, static_root: str) -> web.Application:
    from hermes_cube_provider.provider import create as create_provider

    file_values = _read_env_file(home / ".env")
    cfg = require_cluster_ready(_with_secrets(load_cluster_config(home), file_values))
    pepper = _secret("CLUSTER_KEY_PEPPER", file_values)
    if not pepper:
        raise SystemExit("CLUSTER_KEY_PEPPER is not set in the environment or in .env")
    authority_path = home / AUTHORITY_FILE
    pocket = PocketAuthority(authority_path)

    def verify_device(token: str) -> dict | None:
        identity = pocket.verify(token)
        if identity is None:
            return None
        return {"profile_id": identity["profile"], "device_id": identity["device_id"]}

    authority = ControlPlaneAuthority(authority_path)
    access = AccessControl(
        authority,
        pepper=pepper,
        verify_device=verify_device,
        clock=time.time,
        monotonic=time.monotonic,
    )
    provider = create_provider(home / "cube")
    manager = InstanceManager(
        provider=provider,
        node_root=node_root,
        make_service=lambda host: create_snapshot_service(
            cfg, authority=authority_path, host=host
        ),
        env_for=lambda profile_id: {},
        idle_pause_sec=IDLE_PAUSE_SEC,
        clock=time.time,
    )

    async def _run_instance_tasks() -> None:
        """Re-lease and idle-pause on a timer: requests should not be the only
        thing keeping a lease alive, and snapshots must happen while idle."""
        interval = max(_LOOP_MIN_INTERVAL_SEC, cfg.lease.ttl_sec / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                for step in (manager.renew_leases, manager.pause_idle):
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(step)
        except asyncio.CancelledError:
            pass

    async def _app_lifecycle(app: web.Application) -> None:
        task = asyncio.create_task(_run_instance_tasks())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app = build_cluster_server(
        access,
        manager,
        exchange_code=pocket.exchange,
        revoke_device=pocket.revoke_device,
        static_root=static_root,
    )
    app.cleanup_ctx.append(_app_lifecycle)
    return app


def _with_secrets(cfg, file_values: dict[str, str]):
    """Object-store keys come from the environment or .env, never from config.yaml."""
    import dataclasses

    store = dataclasses.replace(
        cfg.object_store,
        access_key=_secret("OBJECT_STORE_ACCESS_KEY", file_values),
        secret_key=_secret("OBJECT_STORE_SECRET_KEY", file_values),
    )
    return dataclasses.replace(cfg, object_store=store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes cluster gateway")
    parser.add_argument("--home", required=True, help="control-plane HERMES_HOME")
    parser.add_argument("--node-root", required=True, help="host directory holding profile homes")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9330)
    parser.add_argument("--static", default="", help="built Pocket client directory")
    args = parser.parse_args(argv)

    app = build_app(Path(args.home), Path(args.node_root), args.static)
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
