"""Failures raised by the control plane's state base."""

from __future__ import annotations


class ClusterError(RuntimeError):
    """Base class for control-plane failures."""


class ClusterConfigError(ClusterError):
    """``cluster.*`` is enabled but unusable. Startup fails instead of running half-open."""


class LeaseError(ClusterError):
    """The caller does not hold a live instance lease for the profile."""


class SnapshotError(ClusterError):
    """A snapshot could not be taken, uploaded, verified, or restored."""
