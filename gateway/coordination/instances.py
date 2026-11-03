"""Phase 3: instance lifecycle for sandbox-per-profile hosting.

The control plane owns each profile's instance: it takes the lease, restores the node
directory, starts a sandbox through a ``SandboxProvider``, pauses it after idle time
(snapshot first), resumes it on demand, and stops it on destroy. The provider is the
only place that knows about Cube; this module runs the same way against a local stub.

Every instance gets its own API key, injected at start. The gateway holds that key and
forwards it; the instance accepts no other bearer, so a client cannot reach the
instance directly.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from gateway.coordination.authority import Lease
from gateway.coordination.errors import ClusterError, LeaseError
from gateway.coordination.snapshots import SnapshotService

BOOT_GRACE_SEC = 300.0  # a cold sandbox boots envd first; the gateway answers some 40-75s later


@dataclass(frozen=True)
class Started:
    """What ``start`` returns: where to reach the instance, plus the optional
    Host header the provider's data plane routes on (multi-tenant proxies that
    pick the backend from ``Host`` need it; a plain IP endpoint does not)."""

    endpoint: str
    host_header: str = ""
    api_host_header: str = ""


class SandboxProvider(Protocol):
    def start(
        self, instance_id: str, state_dir: Path, env: dict[str, str]
    ) -> "str | Started":
        """Start an instance on ``state_dir`` and return its base URL once it answers health checks.

        May return a ``Started`` (or any object with ``endpoint`` / ``host_header``)
        to carry the data-plane Host header alongside the base URL.
        """
        ...

    def pause(self, instance_id: str) -> None: ...

    def resume(self, instance_id: str) -> None: ...

    def stop(self, instance_id: str) -> None: ...


@dataclass
class Instance:
    profile_id: str
    instance_id: str
    lease: Lease
    endpoint: str
    key: str
    state: str = "running"  # running | paused
    last_active: float = 0.0
    host_header: str = ""  # the profile's dashboard port: Pocket and the web API
    api_host_header: str = ""  # the OpenAI-compatible api_server port (/v1 only)
    started_at: float = 0.0  # boot-grace anchor: probes wait out the cold start


@dataclass
class _ProviderHost:
    """Adapts the provider to the snapshot service's ``NodeHost`` protocol."""

    provider: SandboxProvider
    root: Path

    def state_dir(self, profile_id: str) -> Path:
        return self.root / profile_id

    def pause(self, instance_id: str) -> None:
        self.provider.pause(instance_id)

    def resume(self, instance_id: str) -> None:
        self.provider.resume(instance_id)

    def stop(self, instance_id: str) -> None:
        self.provider.stop(instance_id)


class InstanceManager:
    def __init__(
        self,
        *,
        provider: SandboxProvider,
        node_root: Path,
        make_service: Callable[[_ProviderHost], SnapshotService],
        env_for: Callable[[str], dict[str, str]],
        idle_pause_sec: float,
        clock: Callable[[], float] = time.time,
        alive: "Callable[[Instance], bool] | None" = None,
    ):
        self._provider = provider
        self._host = _ProviderHost(provider, node_root)
        self._service = make_service(self._host)
        self._env_for = env_for
        self._idle = idle_pause_sec
        self._clock = clock
        self._alive = alive or _probe_alive
        self._instances: dict[str, Instance] = {}

    @property
    def service(self) -> SnapshotService:
        return self._service

    def instance(self, profile_id: str) -> Instance | None:
        return self._instances.get(profile_id)

    def ensure_running(self, profile_id: str) -> Instance:
        """Return a running instance for the profile, starting or waking it as needed."""
        current = self._instances.get(profile_id)
        if current is not None and current.state == "paused":
            try:
                self._provider.resume(current.instance_id)
            except Exception:
                # Resume failing (sandbox deleted while paused, bad snapshot) is
                # recovered the same way as an out-of-band kill: clean up and
                # fall through to a fresh start instead of failing the request.
                self._provider.stop(current.instance_id)
                self._service.release(current.lease)
                current = None
            else:
                current.state = "running"
                current.last_active = self._clock()
                return current
        if current is not None and self._aged(current) and not self._alive(current):
            # A sandbox that died out of band (node reboot, manual DELETE) leaves the
            # record pointing at a dead sandbox id and every request would 40x through
            # the proxy. A freshly started sandbox gets a boot grace — the provider only
            # waits for the envd, the gateway answers after — but once aged and
            # unresponsive it is cleaned up and started again.
            self._provider.stop(current.instance_id)
            self._service.release(current.lease)
            del self._instances[profile_id]
            current = None
        if current is not None:
            current.last_active = self._clock()
            return current
        instance_id = f"{profile_id}-{secrets.token_hex(6)}"
        lease = self._service.acquire(profile_id, instance_id)
        try:
            self._service.restore(lease)
            key = secrets.token_urlsafe(32)
            env = {**self._env_for(profile_id), "API_SERVER_KEY": key}
            state_dir = self._host.state_dir(profile_id)
            state_dir.mkdir(parents=True, exist_ok=True)
            started = _as_started(self._provider.start(instance_id, state_dir, env))
        except Exception:
            self._service.release(lease)
            raise
        instance = Instance(
            profile_id,
            instance_id,
            lease,
            started.endpoint,
            key,
            "running",
            self._clock(),
            started.host_header,
            api_host_header=started.api_host_header,
            started_at=self._clock(),
        )
        self._instances[profile_id] = instance
        return instance

    def touch(self, profile_id: str) -> None:
        """Record activity and renew the lease: a request is the only tick some
        deployments get, so an about-to-expire lease extends here rather than
        failing the request."""
        instance = self._require(profile_id)
        instance.last_active = self._clock()
        if not self._service.renew(instance.lease):
            self._instances.pop(profile_id, None)
            raise ClusterError(f"instance lease for profile {profile_id!r} expired")

    def pause_idle(self) -> list[str]:
        """Snapshot and pause every running instance idle for ``idle_pause_sec``."""
        now = self._clock()
        paused: list[str] = []
        for profile_id, instance in list(self._instances.items()):
            if instance.state != "running" or now - instance.last_active < self._idle:
                continue
            try:
                self._service.snapshot(instance.lease, resume=False)
            except LeaseError:
                self._instances.pop(profile_id, None)
                continue
            except ClusterError:
                continue
            instance.state = "paused"
            paused.append(profile_id)
        return paused

    def renew_leases(self) -> list[str]:
        """Renew every lease and probe the running sandboxes: an expired lease or a
        sandbox that died out of band (node reboot, manual DELETE) is stopped and
        forgotten. Paused instances skip the probe — a hibernating sandbox answers
        no /health, and its wake-up path recovers a bad resume on its own."""
        lost: list[str] = []
        for profile_id, instance in list(self._instances.items()):
            dead = not self._service.renew(instance.lease) or (
                instance.state == "running"
                and self._aged(instance)
                and not self._alive(instance)
            )
            if dead:
                self._provider.stop(instance.instance_id)
                self._service.release(instance.lease)
                del self._instances[profile_id]
                lost.append(profile_id)
        return lost

    def _aged(self, instance: Instance) -> bool:
        """Past the boot grace: long enough that an unresponsive sandbox is dead,
        not cold-starting (the provider waits only for the envd, not the gateway)."""
        return self._clock() - instance.started_at > BOOT_GRACE_SEC

    def destroy(self, profile_id: str) -> None:
        """Commit the final state, then stop and release. A failed snapshot keeps the instance."""
        instance = self._require(profile_id)
        try:
            self._service.snapshot(instance.lease, resume=False)
        except LeaseError:
            self._instances.pop(profile_id, None)
            return
        self._provider.stop(instance.instance_id)
        self._service.release(instance.lease)
        del self._instances[profile_id]

    def _require(self, profile_id: str) -> Instance:
        instance = self._instances.get(profile_id)
        if instance is None:
            raise ClusterError(f"profile {profile_id!r} has no instance")
        return instance


def _as_started(result: "str | object") -> Started:
    """Accept a bare URL, a ``Started``, or any provider-owned object with the
    same two attributes (providers are plugin packages and share no base class)."""
    if isinstance(result, str):
        return Started(result)
    endpoint = getattr(result, "endpoint", None)
    if endpoint is None:
        raise ClusterError(f"provider start() returned no endpoint: {result!r}")
    return Started(
        endpoint,
        getattr(result, "host_header", "") or "",
        getattr(result, "api_host_header", "") or "",
    )


def _probe_alive(instance: Instance) -> bool:
    """Health-check the running sandbox through the data plane.

    The api_server's ``/health`` is unauthenticated by design, so one GET answers
    "is this sandbox really up" with no key in the request. Any error status,
    connection failure or timeout means the id is dead from the client's point of
    view, which is what the router would learn one relay attempt later anyway.
    """
    import urllib.request

    request = urllib.request.Request(
        instance.endpoint.rstrip("/") + "/health",
        headers={"Host": instance.api_host_header},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status == 200
    except Exception:
        return False
