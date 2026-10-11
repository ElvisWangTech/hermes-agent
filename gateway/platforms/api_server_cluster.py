"""api_server cluster admission: claim, restore, proxy, and drain.

Installed as middleware by ``APIServerAdapter.connect``. When the cluster is
disabled the middleware calls the handler and returns, so the single-node
routes keep their current behavior. Runs, the response store, artifacts, and
browser-control are refused while the cluster is on; they have no owner index.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re

from gateway.coordination.config import load_cluster_config
from gateway.coordination.drain import draining, health_payload, run_drain
from gateway.coordination.errors import ClusterError, RoutingError, SnapshotError
from gateway.coordination.proxy import (
    HOP_HEADER,
    PROFILE_HEADER,
    TOKEN_HEADER,
    address_allowed,
    join_owner_url,
    same_destination,
)

logger = logging.getLogger("gateway.platforms.api_server")

_PROFILE_PREFIX = re.compile(r"^/p/[^/]+")
_UNSUPPORTED = (
    ("/v1/runs", 501, "cluster_unsupported", "runs are not available across cluster nodes"),
    ("/v1/responses", 501, "cluster_unsupported", "the response store is not available across cluster nodes"),
    ("/v1/artifacts", 501, "cluster_unsupported", "artifacts are not available across cluster nodes"),
    ("/v1/browser-control", 501, "cluster_unsupported", "browser-control is not migrated across nodes"),
)
_periodic_started = False


def logical_path(path: str) -> str:
    """Strip a multiplex ``/p/<profile>`` prefix so route checks see the real path."""
    stripped = _PROFILE_PREFIX.sub("", path or "")
    return stripped or "/"


def cluster_route_table(adapter) -> list[tuple]:
    return [
        ("POST", "/admin/drain", lambda request: handle_drain(adapter, request)),
        ("GET", "/admin/cluster", lambda request: handle_metrics(adapter, request)),
    ]


def ensure_cluster_started() -> None:
    """Validate an enabled cluster before the listener binds. Disabled is a no-op."""
    from gateway.coordination.config import require_cluster_ready

    cfg = require_cluster_ready()
    if not cfg.enabled:
        return
    from gateway.coordination import get_session_sync

    get_session_sync()
    _arm_periodic(cfg)


def cluster_http_middleware():
    from aiohttp import web

    @web.middleware
    async def _middleware(request, handler):
        cfg = load_cluster_config()
        if not cfg.enabled:
            return await handler(request)
        path = logical_path(request.path)
        if request.method == "GET" and path in {"/health", "/v1/health"}:
            body = health_payload()
            if body is not None:
                return web.json_response(body, status=503)
        blocked = _unsupported(path)
        if blocked is not None:
            return blocked
        if request.method != "POST" or path != "/v1/chat/completions":
            return await handler(request)
        return await _route_chat(request, handler, cfg)

    return _middleware


async def handle_drain(adapter, request):
    from aiohttp import web

    cfg = load_cluster_config()
    if not cfg.enabled:
        return _error("cluster is disabled", 404, "cluster_disabled")
    if not _token_ok(cfg.internal_token, request.headers.get(TOKEN_HEADER, "")):
        return _error("cluster token rejected", 401, "cluster_unauthorized")
    from gateway.coordination import get_session_sync

    sync = get_session_sync()
    db = await adapter._ensure_session_db_async()
    pairs = [(db, claim) for claim in sync.router.owned_claims()] if db is not None else []
    result = await run_drain(
        router=sync.router,
        sync=sync,
        timeout_sec=cfg.drain_timeout_sec,
        inflight=lambda: int(getattr(adapter, "_pending_agent_requests", 0) or 0),
        sessions=pairs,
    )
    return web.json_response(result, status=200 if result["ok"] else 503)


async def handle_metrics(adapter, request):
    from aiohttp import web

    del adapter
    cfg = load_cluster_config()
    if not cfg.enabled:
        return _error("cluster is disabled", 404, "cluster_disabled")
    if not _token_ok(cfg.internal_token, request.headers.get(TOKEN_HEADER, "")):
        return _error("cluster token rejected", 401, "cluster_unauthorized")
    from gateway.coordination.metrics import snapshot

    return web.json_response(snapshot())


def _unsupported(path: str):
    if path.rstrip("/").endswith("/fork"):
        return _error(
            "reset/branch is not available on the cluster entry",
            409,
            "cluster_reset_unsupported",
        )
    for prefix, status, code, message in _UNSUPPORTED:
        if path == prefix or path.startswith(prefix + "/"):
            return _error(message, status, code)
    return None


async def _route_chat(request, handler, cfg):
    from gateway.coordination import get_session_sync
    hop = request.headers.get(HOP_HEADER, "").strip()
    if hop and hop != "1":
        return _error("cluster proxy is single-hop", 508, "cluster_proxy_loop")
    if hop and not _token_ok(cfg.internal_token, request.headers.get(TOKEN_HEADER, "")):
        return _error("cluster token rejected", 401, "cluster_unauthorized")
    profile = request.headers.get(PROFILE_HEADER, "").strip()
    if hop and profile and profile != cfg.profile_id:
        return _error("cluster profile mismatch", 409, "cluster_profile_mismatch")
    session_key = request.headers.get("X-Hermes-Session-Key", "").strip()
    session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
    if not session_key:
        if session_id:
            return _error(
                "X-Hermes-Session-Id requires X-Hermes-Session-Key when the cluster is enabled",
                400,
                "cluster_session_key_required",
            )
        return await _call_local(request, handler, cfg, claim=None, db=None)
    if draining() and not hop:
        return _error("node is draining", 503, "cluster_draining")
    try:
        sync = get_session_sync()
        claim = await asyncio.to_thread(sync.router.claim, session_key, cfg.resolved_node_id(), cfg.affinity.lease_ttl_sec)
    except ClusterError as exc:
        return _from_error(exc)
    except ValueError as exc:
        return _error(str(exc), 400, "cluster_bad_session_key")
    if not claim.is_mine:
        if hop:
            return _error("this node is not the conversation owner", 409, "cluster_not_owner")
        if not claim.acquired and claim.owner is None:
            return _error("node is not accepting new conversations", 503, "cluster_draining")
        body = await request.read()
        return await _proxy(request, cfg, claim, body)
    if sync.fenced(claim.conversation_id):
        return _error("lease renewal failed; conversation is fenced", 503, "cluster_fenced")
    adapter = request.app.get("api_server_adapter")
    db = await adapter._ensure_session_db_async() if adapter is not None else None
    try:
        sync.check_session_id(claim, session_id or None)
        if claim.took_over and db is not None:
            await asyncio.to_thread(sync.restore, db, claim)
    except ClusterError as exc:
        return _from_error(exc)
    _arm_renew(sync, claim, cfg)
    return await _call_local(request, handler, cfg, claim=claim, db=db, sync=sync)


async def _call_local(request, handler, cfg, *, claim, db, sync=None):
    from agent.log_context import reset_log_context, set_log_context
    from gateway.coordination.heartbeat import unwatch_turn, watch_turn

    session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
    if claim is not None:
        session_id = session_id or claim.session_key
    tokens = set_log_context(
        user_id="",
        agent_id=cfg.profile_id,
        session_id=session_id,
        node_id=cfg.resolved_node_id(),
    )
    task = asyncio.current_task()
    if claim is not None:
        watch_turn(claim.conversation_id, task)
    try:
        response = await handler(request)
    finally:
        if claim is not None:
            unwatch_turn(claim.conversation_id, task)
        reset_log_context(tokens)
    if claim is not None and sync is not None and _should_snapshot(cfg, response):
        echoed = response.headers.get("X-Hermes-Session-Id", "").strip()
        if echoed:
            sync.note_tip(claim.conversation_id, echoed)
        sync.attach_db(db)
        sync.mark_dirty(claim.conversation_id)
        if db is not None:
            asyncio.create_task(_writeback(sync, db, claim))
    return response


def _should_snapshot(cfg, response) -> bool:
    if not cfg.snapshot.on_turn_end:
        return False
    status = getattr(response, "status", 500)
    return int(status) < 500


async def _writeback(sync, db, claim) -> None:
    try:
        await asyncio.to_thread(sync.writeback, db, claim)
    except Exception:
        logger.warning("cluster writeback failed conversation=%s", claim.conversation_id, exc_info=True)


def _arm_renew(sync, claim, cfg) -> None:
    from gateway.coordination.heartbeat import arm

    def _renew() -> bool:
        return bool(sync.router.renew(claim, cfg.affinity.lease_ttl_sec))

    def _fail() -> None:
        sync.fence(claim.conversation_id)

    try:
        arm(claim.conversation_id, _renew, cfg.affinity.heartbeat_sec, _fail)
    except RuntimeError:
        # No running loop (a sync caller). The request path still holds the lease
        # until TTL; the periodic scanner is best-effort.
        return


def _arm_periodic(cfg) -> None:
    global _periodic_started
    if _periodic_started:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _periodic_started = True
    loop.create_task(_periodic(cfg), name="cluster-dirty-scan")


async def _periodic(cfg) -> None:
    from gateway.coordination import get_session_sync

    while True:
        await asyncio.sleep(cfg.snapshot.dirty_periodic_sec)
        try:
            sync = get_session_sync()
        except ClusterError:
            continue
        # Turn-end writeback is primary. This scan flushes a dirty bit left
        # behind when that task was cancelled, using the store the last turn attached.
        db = sync.bound_db()
        for claim in list(sync.router.owned_claims()):
            if not sync.dirty.is_dirty(claim.conversation_id):
                continue
            from gateway.coordination.metrics import note
            note("snapshot_dirty_age", value=1)
            if db is None:
                continue
            try:
                await asyncio.to_thread(sync.writeback, db, claim)
            except Exception:
                logger.warning(
                    "cluster dirty scan failed conversation=%s", claim.conversation_id, exc_info=True,
                )


async def _proxy(request, cfg, claim, body: bytes):
    import aiohttp

    from gateway.coordination.metrics import note

    address = claim.address or ""
    if not address or not address_allowed(address, cfg.proxy.allow_hosts):
        note("proxy_errors")
        return _error("owner address is not an allowed internal target", 502, "cluster_owner_unreachable")
    if same_destination(address, cfg.resolved_advertise_url()):
        note("proxy_errors")
        return _error("refusing to proxy to this node", 508, "cluster_proxy_loop")
    url = join_owner_url(address, request.rel_url.path, request.rel_url.query_string)
    headers = {
        "Content-Type": request.content_type or "application/json",
        HOP_HEADER: "1",
        TOKEN_HEADER: cfg.internal_token,
        PROFILE_HEADER: cfg.profile_id,
    }
    for name in ("Authorization", "X-Hermes-Session-Key", "X-Hermes-Session-Id", "Accept"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    timeout = aiohttp.ClientTimeout(total=cfg.proxy.timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body, headers=headers) as upstream:
                if "text/event-stream" in (upstream.headers.get("Content-Type") or ""):
                    return await _pipe_sse(request, upstream)
                payload = await upstream.read()
                out = {}
                for name in ("X-Hermes-Session-Id", "X-Hermes-Session-Key", "Content-Type"):
                    if name in upstream.headers:
                        out[name] = upstream.headers[name]
                from aiohttp import web
                return web.Response(status=upstream.status, body=payload, headers=out)
    except asyncio.CancelledError:
        raise
    except Exception:
        note("proxy_errors")
        logger.warning("cluster proxy failed", exc_info=True)
        return _error("cluster proxy failed", 502, "cluster_proxy_failed")


async def _pipe_sse(request, upstream):
    from aiohttp import web

    headers = {
        name: upstream.headers[name]
        for name in ("X-Hermes-Session-Id", "X-Hermes-Session-Key")
        if name in upstream.headers
    }
    response = web.StreamResponse(status=upstream.status, headers=headers)
    response.content_type = "text/event-stream"
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(4096):
            await response.write(chunk)
            if request.transport is None or request.transport.is_closing():
                break
    finally:
        upstream.close()
        try:
            await response.write_eof()
        except Exception:
            pass
    return response


def _token_ok(expected: str, presented: str) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(
        hashlib.sha256(expected.encode("utf-8")).digest(),
        hashlib.sha256(presented.encode("utf-8")).digest(),
    )


def _error(message: str, status: int, code: str):
    from aiohttp import web

    return web.json_response(
        {"error": {"message": message, "type": "cluster_error", "code": code}},
        status=status,
    )


def _from_error(exc: ClusterError):
    return _error(str(exc), exc.http_status, exc.code)


def reset_cluster_http_for_tests() -> None:
    global _periodic_started
    _periodic_started = False
