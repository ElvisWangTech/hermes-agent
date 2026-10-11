"""Cluster config, proxy policy, drain, and the log four-tuple."""

from __future__ import annotations

import logging

import pytest

from agent.log_context import reset_log_context, set_log_context
from gateway.coordination.config import load_cluster_config, parse_cluster_section, require_cluster_ready, reset_config_cache
from gateway.coordination.dirty import DirtyTracker, Watermarks
from gateway.coordination.drain import health_payload, reset_drain_flag, run_drain
from gateway.coordination.errors import ClusterConfigError
from gateway.coordination.protocol import MemoryCoordination, MemorySessionRouter
from gateway.coordination.proxy import address_allowed, same_destination
from gateway.coordination.session_sync import SessionSync
from gateway.coordination.snapshot_store import MemoryObjectStore, SnapshotBlobs
from gateway.platforms.api_server_cluster import logical_path
from hermes_cli.config import DEFAULT_CONFIG, load_config
from hermes_constants import get_hermes_home


def test_default_config_disables_the_cluster():
    assert DEFAULT_CONFIG["cluster"]["enabled"] is False
    assert load_config()["cluster"]["enabled"] is False


def test_gateway_loader_sees_raw_cluster_yaml(tmp_path, monkeypatch):
    del tmp_path
    home = get_hermes_home()
    (home / "config.yaml").write_text(
        "cluster:\n  enabled: true\n  namespace: lab\n  profile_id: default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("CLUSTER_INTERNAL_TOKEN", "token")
    reset_config_cache()
    cfg = load_cluster_config()
    assert cfg.enabled and cfg.namespace == "lab" and cfg.profile_id == "default"
    assert cfg.redis_url.startswith("redis://")
    assert cfg.internal_token == "token"


def test_enabled_cluster_names_missing_secrets():
    cfg = parse_cluster_section({"enabled": True, "namespace": "lab", "profile_id": "default"})
    with pytest.raises(ClusterConfigError, match="REDIS_URL"):
        require_cluster_ready(cfg)


def test_address_policy_rejects_public_targets():
    assert address_allowed("http://10.1.0.8:8642", ())
    assert address_allowed("http://gateway-b:8642", ("gateway-b",))
    assert not address_allowed("http://gateway-b:8642", ())
    assert not address_allowed("http://8.8.8.8:8642", ())
    assert not address_allowed("http://user:pass@10.0.0.1:8642", ())
    assert same_destination("http://gateway-a:8642/v1/chat/completions", "http://gateway-a:8642")
    assert logical_path("/p/alice/v1/chat/completions") == "/v1/chat/completions"


def test_log_four_tuple_is_absent_until_a_cluster_request_sets_it():
    import hermes_logging  # installs the session_tag record factory

    del hermes_logging
    logger = logging.getLogger("hermes.cluster.test")
    quiet = logger.makeRecord("hermes.cluster.test", logging.INFO, __file__, 1, "plain", (), None)
    assert "u=" not in quiet.session_tag
    tokens = set_log_context(user_id="", agent_id="default", session_id="s1", node_id="node-a")
    try:
        tagged = logger.makeRecord("hermes.cluster.test", logging.INFO, __file__, 1, "turn", (), None)
    finally:
        reset_log_context(tokens)
    assert tagged.session_tag == " [u= a=default s=s1 n=node-a]"


@pytest.mark.asyncio
async def test_drain_releases_a_clean_lease_and_reports_a_failed_flush(tmp_path):
    reset_drain_flag()
    store = MemoryCoordination()
    router = MemorySessionRouter(store, scope="scope", node_id="a", incarnation="ia")
    sync = SessionSync(
        router=router, blobs=SnapshotBlobs(MemoryObjectStore()), dirty=DirtyTracker(),
        watermarks=Watermarks(tmp_path / "watermarks.json"), profile_id="default", scope="scope",
    )
    clean = router.claim("chat", "a", 30)
    result = await run_drain(
        router=router, sync=sync, timeout_sec=1, inflight=lambda: 0, sessions=[(None, clean)],
    )
    assert result["ok"] and result["status"] == "drained" and result["released"] == 1
    assert health_payload()["ready"] is False

    reset_drain_flag()
    store2 = MemoryCoordination()
    router2 = MemorySessionRouter(store2, scope="scope", node_id="a", incarnation="ia")
    sync2 = SessionSync(
        router=router2, blobs=SnapshotBlobs(MemoryObjectStore()), dirty=DirtyTracker(),
        watermarks=Watermarks(tmp_path / "w2.json"), profile_id="default", scope="scope",
    )
    dirty = router2.claim("chat", "a", 30)
    sync2.mark_dirty(dirty.conversation_id)
    sync2.fence(dirty.conversation_id)
    failed = await run_drain(
        router=router2, sync=sync2, timeout_sec=1, inflight=lambda: 0, sessions=[(None, dirty)],
    )
    assert not failed["ok"] and failed["status"] == "incomplete" and failed["dirty"] == 1
    assert router2.lookup("chat") == "a"
    reset_drain_flag()
