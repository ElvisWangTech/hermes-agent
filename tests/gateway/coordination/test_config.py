"""``cluster.*`` must fail closed: disabled is inert, enabled without its secrets refuses to start."""

from pathlib import Path

import pytest

from gateway.coordination import create_snapshot_service
from gateway.coordination.config import parse_cluster_section, require_cluster_ready
from gateway.coordination.errors import ClusterConfigError, ClusterError


def test_absent_section_is_disabled_and_cannot_build_a_service(tmp_path: Path):
    cfg = parse_cluster_section(None)

    assert cfg.enabled is False
    with pytest.raises(ClusterError):
        create_snapshot_service(cfg, authority=tmp_path / "a.sqlite", host=object())


def test_enabled_without_object_store_secrets_refuses_to_start():
    cfg = parse_cluster_section(
        {
            "enabled": True,
            "namespace": "lab",
            "object_store": {"backend": "s3", "endpoint": "http://x", "bucket": "b"},
        },
        secrets={},
    )

    with pytest.raises(ClusterConfigError, match="OBJECT_STORE_ACCESS_KEY"):
        require_cluster_ready(cfg)
