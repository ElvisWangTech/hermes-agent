"""Phase 3: instance lifecycle through the lease, snapshot, and provider contract."""

import pytest

from gateway.coordination.errors import ClusterError
from gateway.coordination.instances import Started


def test_bare_url_endpoints_carry_no_host_header(make_manager, provider):
    instance = make_manager().ensure_running("role-a")

    assert instance.host_header == ""


def test_provider_may_report_a_data_plane_host_header(make_manager, provider):
    """A provider whose data plane routes on Host (multi-tenant proxy) reports
    the header next to the endpoint; the manager records it verbatim."""

    class RoutedProvider:
        def start(self, instance_id, state_dir, env):
            return Started("http://node-1:80", f"49983-{instance_id}")

        def pause(self, instance_id):
            provider.pause(instance_id)

        def resume(self, instance_id):
            provider.resume(instance_id)

        def stop(self, instance_id):
            provider.stop(instance_id)

    instance = make_manager(prov=RoutedProvider()).ensure_running("role-a")

    assert instance.endpoint == "http://node-1:80"
    assert instance.host_header == f"49983-{instance.instance_id}"


def test_idle_instance_is_snapshotted_and_paused_then_resumed_on_demand(
    env, provider, make_manager
):
    manager = make_manager(idle_pause_sec=60)
    first = manager.ensure_running("role-a")
    (env.host.state_dir("role-a") / "memories").mkdir(parents=True, exist_ok=True)
    (env.host.state_dir("role-a") / "memories" / "MEMORY.md").write_text("kept")

    for _ in range(4):
        env.clock.now += 30
        assert manager.renew_leases() == []
    assert manager.pause_idle() == ["role-a"]
    assert manager.instance("role-a").state == "paused"
    assert env.authority.read_manifest("role-a").revision == 1

    again = manager.ensure_running("role-a")
    assert again.instance_id == first.instance_id
    assert ("resume", first.instance_id) in provider.calls


def test_instance_key_is_injected_and_differs_per_instance(make_manager, provider):
    manager = make_manager()
    instance = manager.ensure_running("role-a")

    env = provider.started_env[instance.instance_id]
    assert env["API_SERVER_KEY"] == instance.key
    assert len(instance.key) >= 32


def test_destroy_snapshots_then_stops_and_releases_the_lease(
    env, provider, make_manager
):
    manager = make_manager()
    instance = manager.ensure_running("role-a")
    (env.host.state_dir("role-a") / "x.txt").write_text("final")

    manager.destroy("role-a")

    assert manager.instance("role-a") is None
    assert env.authority.read_manifest("role-a") is not None
    assert ("stop", instance.instance_id) in provider.calls
    replacement = manager.ensure_running("role-a")
    assert replacement.lease.incarnation != instance.lease.incarnation


def test_snapshot_failure_keeps_the_instance_and_does_not_destroy(
    env, provider, make_manager
):
    manager = make_manager()
    instance = manager.ensure_running("role-a")
    env.objects.fail_put = True

    with pytest.raises(ClusterError):
        manager.destroy("role-a")

    assert manager.instance("role-a") is not None
    assert ("stop", instance.instance_id) not in provider.calls


def test_a_sandbox_killed_out_of_band_is_recycled(env, provider, make_manager):
    """An out-of-band kill (node reboot, manual sandbox delete) leaves the manager
    pointing at a dead sandbox id; once the instance is past its boot grace the
    next request must stop it, release its lease, and boot a fresh instance
    instead of relaying into 40x forever. A sandbox still inside the grace is
    never probed: the cold gateway answers later than the envd the provider waits for."""
    healthy: list[bool] = [True]
    manager = make_manager(alive=lambda instance: healthy[0])
    dead = manager.ensure_running("role-a")

    assert manager.ensure_running("role-a") is dead  # inside the boot grace: no probe

    env.clock.now += 601.0
    healthy[0] = False
    replacement = manager.ensure_running("role-a")

    assert replacement.instance_id != dead.instance_id
    assert ("stop", dead.instance_id) in provider.calls
    assert ("start", replacement.instance_id) in provider.calls
    assert manager.instance("role-a") is replacement


def test_an_instance_paused_whose_sandbox_died_is_recycled(env, provider, make_manager):
    """A pause hibernates the sandbox; if it dies while paused the wake-up must
    not 500 on the provider error — it recycles and starts a fresh instance."""
    manager = make_manager()  # default alive: everything answers /health
    manager.ensure_running("role-a")
    instance = manager.instance("role-a")
    instance.state = "paused"

    def broken_resume(instance_id):
        provider.calls.append(("resume", instance_id))
        raise RuntimeError("sandbox gone")

    provider.resume = broken_resume
    replacement = manager.ensure_running("role-a")

    assert replacement.instance_id != instance.instance_id
    assert ("stop", instance.instance_id) in provider.calls
    assert ("start", replacement.instance_id) in provider.calls


def test_a_paused_instance_is_never_probed(env, provider, make_manager):
    """The heartbeat must not kill a hibernating instance: a paused sandbox
    answers no /health yet is perfectly healthy."""
    manager = make_manager(alive=lambda instance: False)  # every probe reports dead
    instance = manager.ensure_running("role-a")
    instance.state = "paused"

    assert manager.renew_leases() == []
    assert ("stop", instance.instance_id) not in provider.calls
    assert manager.instance("role-a") is instance
