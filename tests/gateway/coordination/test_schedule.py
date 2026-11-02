"""Phase 4: a due occurrence wakes its profile exactly once; a crash mid-claim is not retried."""

import json

from gateway.coordination.schedule import (
    FileScheduleSource,
    Occurrence,
    OccurrenceLedger,
    WakeScheduler,
)


def _write_jobs(env, profile: str, jobs: list[dict]) -> None:
    cron = env.host.state_dir(profile) / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def test_due_job_wakes_a_paused_profile_once_and_not_again(env, provider, make_manager):
    env.clock.now = 1_700_000_000.0
    manager = make_manager(idle_pause_sec=1)
    instance = manager.ensure_running("role-a")
    env.clock.now += 5
    assert manager.pause_idle() == ["role-a"]
    _write_jobs(
        env,
        "role-a",
        [{"id": "job-1", "next_run_at": "2023-11-14T00:00:00+00:00", "enabled": True}],
    )
    ledger = OccurrenceLedger(env.authority)
    source = FileScheduleSource(env.host.root, lambda: ["role-a"])
    scheduler = WakeScheduler(
        ledger=ledger, source=source, manager=manager, clock=env.clock
    )

    first = scheduler.tick()
    second = scheduler.tick()

    assert [occurrence.job_id for occurrence in first] == ["job-1"]
    assert second == []
    assert provider.calls.count(("resume", instance.instance_id)) == 1


def test_disabled_and_future_jobs_do_not_wake_anything(env, make_manager):
    make_manager().ensure_running("role-a")
    _write_jobs(
        env,
        "role-a",
        [
            {"id": "off", "next_run_at": "2023-11-14T00:00:00+00:00", "enabled": False},
            {
                "id": "later",
                "next_run_at": "2099-01-01T00:00:00+00:00",
                "enabled": True,
            },
        ],
    )
    source = FileScheduleSource(env.host.root, lambda: ["role-a"])

    assert source.due(env.clock.now) == []


def test_claimed_occurrence_left_open_by_a_crash_is_marked_interrupted(env):
    ledger = OccurrenceLedger(env.authority)
    occurrence = Occurrence("role-a", "job-9", "2023-11-14T00:00:00+00:00")

    assert ledger.claim(occurrence, env.clock.now) is True
    assert ledger.claim(occurrence, env.clock.now) is False
    assert ledger.interrupt_claimed() == 1
    assert ledger.claim(occurrence, env.clock.now) is False
