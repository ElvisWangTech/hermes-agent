"""Serve the handheld client at /pocket, ahead of the dashboard catch-all.

``hermes serve`` installs a catch-all that answers every other path. These
routes are registered first so /pocket still resolves, including on a headless
serve. The session token is written into the page only when the auth gate is
off, matching the desktop handshake on ``/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

_NO_STORE = {"Cache-Control": "no-store"}
_TOKEN_SLOT = 'window.__POCKET_SESSION_TOKEN__ = ""'


def pocket_dist_dir() -> Path | None:
    """First directory that actually contains a built index.html."""
    here = Path(__file__).resolve().parent
    candidates = []
    override = os.environ.get("HERMES_POCKET_DIST", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(here / "pocket_dist")
    candidates.append(here.parent / "apps" / "pocket" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def _index_html(dist: Path, token: str) -> str:
    html = (dist / "index.html").read_text(encoding="utf-8")
    if token:
        html = html.replace(_TOKEN_SLOT, f"window.__POCKET_SESSION_TOKEN__ = {json.dumps(token)}", 1)
    return html


def _asset(dist: Path, rel: str) -> Path | None:
    if not rel or rel.endswith("/"):
        return None
    root = dist.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


def mount_pocket(application: FastAPI) -> None:
    """Register /pocket before any catch-all. Missing dist answers 404 per request."""

    @application.get("/pocket")
    @application.get("/pocket/")
    async def pocket_index():
        dist = pocket_dist_dir()
        if dist is None:
            return JSONResponse({"error": "页面还没准备好"}, status_code=404)
        gated = bool(getattr(application.state, "auth_required", False))
        token = ""
        if not gated:
            from hermes_cli.web_deps import _server

            token = _server()._SESSION_TOKEN
        try:
            html = _index_html(dist, token)
        except OSError:
            return JSONResponse({"error": "页面还没准备好"}, status_code=404)
        return HTMLResponse(html, headers=_NO_STORE)

    @application.get("/pocket/{asset_path:path}")
    async def pocket_asset(asset_path: str):
        dist = pocket_dist_dir()
        if dist is None:
            return JSONResponse({"error": "页面还没准备好"}, status_code=404)
        found = _asset(dist, asset_path)
        if found is None:
            return JSONResponse({"error": "找不到"}, status_code=404)
        return FileResponse(found)
