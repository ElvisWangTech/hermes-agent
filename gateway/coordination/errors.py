"""Failures of the optional cluster path.

Disabled clusters never raise these. Startup and request paths map them to a
fatal config error or an HTTP response; they are not a fallback onto local cache.
"""

from __future__ import annotations


class ClusterError(Exception):
    """Base for cluster failures that must not be served as a successful turn."""

    code = "cluster_error"
    http_status = 503

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class ClusterConfigError(ClusterError):
    """Enabled cluster is missing a required setting or optional dependency."""

    code = "cluster_not_ready"
    http_status = 503


class SnapshotError(ClusterError):
    """Blob, checksum, or replace-import failed. The conversation stays closed."""

    code = "snapshot_failed"
    http_status = 503


class RoutingError(ClusterError):
    """The request cannot be claimed or forwarded."""

    code = "cluster_routing"
    http_status = 409
