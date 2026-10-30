"""Pocket schedules are confirmed proposals on the profile's own cron store.

A device speaks a stored result. A notifier may observe that result, and hearing
it stays a separate step. Pausing for a lost authorization does not resume later.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cron.jobs import get_job, update_job, use_cron_store
from hermes_cli.pocket_schedule import (
    claim,
    clear_schedules,
    confirm,
    decline,
    deliver_to_pocket,
    draft,
    hold_for_authorization,
    interpret,
    list_schedules,
    parse_utterance,
    register_schedule_notifier,
    schedule_limit,
    settle,
    unregister_schedule_notifier,
)


def _now() -> datetime:
    return datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)


def test_unclear_clock_does_not_become_a_job(tmp_path):
    with use_cron_store(tmp_path):
        morning = interpret("早上提醒我出门", "role-a")
        bare = interpret("八点提醒我出门", "role-a")
        chat = interpret("停一下", "role-a")

        assert morning["type"] == "clarify" and morning["question"] == "几点？"
        assert bare["type"] == "clarify" and bare["question"] == "早上还是晚上？"
        assert chat == {"type": "chat"}
        assert list_schedules()["items"] == []


def test_an_evening_digit_alarm_is_a_reminder():
    parsed = parse_utterance("定一个晚上8点的闹钟", [], zone="UTC", now=_now())

    assert parsed["type"] == "proposal"
    assert parsed["payload"]["kind"] == "remind"
    assert parsed["payload"]["phrase"] == "闹钟"
    assert parsed["payload"]["schedule"].startswith("2026-10-24T20:00:00")
    assert "几点" not in parsed.get("question", "")


def test_spoken_clock_keeps_its_hour():
    spaced = parse_utterance("定一个晚上 8 点的闹钟", [], zone="UTC", now=_now())
    shi = parse_utterance("定一个晚上八时的闹钟", [], zone="UTC", now=_now())
    tonight = parse_utterance("今晚8点叫我", [], zone="UTC", now=_now())

    for parsed in (spaced, shi, tonight):
        assert parsed["type"] == "proposal"
        assert parsed["payload"]["kind"] == "remind"
        assert parsed["payload"]["phrase"] == "闹钟"
        assert parsed["payload"]["schedule"].startswith("2026-10-24T20:00:00")


def test_a_passed_today_is_refused_and_an_implied_clock_rolls_forward():
    passed = parse_utterance("今天早上八点提醒我出门", [], zone="UTC", now=_now())
    rolled = parse_utterance("早上八点提醒我出门", [], zone="UTC", now=_now())

    assert passed["type"] == "clarify" and passed["question"] == "这个时间已经过了"
    assert rolled["payload"]["schedule"].startswith("2026-10-25T08:00:00")
    assert rolled["payload"]["when"].startswith("明天 08:00")


def test_half_hour_later_is_thirty_minutes_and_decline_writes_nothing(tmp_path):
    parsed = parse_utterance("半小时后提醒我喝水", [], zone="服务器本地时间", now=_now())
    task = parse_utterance("每个工作日晚上九点跟我说今天天气", [], zone="Asia/Shanghai", now=_now())

    assert parsed["payload"]["schedule"] == "in 30m"
    assert parsed["payload"]["kind"] == "remind"
    assert task["payload"]["kind"] == "task"
    assert task["payload"]["schedule"] == "0 21 * * 1-5"
    assert task["payload"]["task"] == "今天天气"
    assert "Hermes" not in task["body"]
    assert "cron" not in task["body"].lower()
    with use_cron_store(tmp_path):
        proposal = interpret("半小时后提醒我喝水", "role-a")
        assert decline(proposal["id"])["speech"] == "没记下。"
        with pytest.raises(ValueError, match="已经处理过了"):
            confirm(proposal["id"])
        assert list_schedules()["items"] == []


def test_one_confirm_writes_one_job(tmp_path):
    with use_cron_store(tmp_path):
        proposal = interpret("30分钟后提醒我带钥匙", "role-a")
        assert proposal["allow"] == "记下"
        created = confirm(proposal["id"])
        with pytest.raises(ValueError, match="已经处理过了"):
            confirm(proposal["id"])
        with pytest.raises(ValueError, match="过期"):
            confirm("not-a-proposal")

        board = list_schedules()
        assert [item["name"] for item in board["items"]] == ["带钥匙"]
        assert "带钥匙" in interpret("我的日程", "role-a")["speech"]
        job = get_job(created["id"])
        assert job["deliver"] == "pocket"
        assert job["origin"]["chat_id"] == "role-a"
        assert job["origin"]["pocket_kind"] == "remind"
        assert job["origin"]["pocket_phrase"] == "带钥匙"


def test_profiles_keep_separate_cron_stores(tmp_path):
    left, right = tmp_path / "a", tmp_path / "b"
    with use_cron_store(left):
        confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])
    with use_cron_store(right):
        assert list_schedules()["items"] == []
        confirm(interpret("30分钟后提醒我喝水", "b")["id"])
    with use_cron_store(left):
        assert [item["name"] for item in list_schedules()["items"]] == ["带钥匙"]
    with use_cron_store(right):
        assert [item["name"] for item in list_schedules()["items"]] == ["喝水"]


def test_schedule_limit_follows_profile_config_and_keeps_the_oldest():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    home = Path(os.environ["HERMES_HOME"])
    path = home / "config.yaml"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        assert schedule_limit() == int(DEFAULT_CONFIG["pocket"]["schedule_limit"])
        path.write_text("pocket:\n  schedule_limit: 1\n", encoding="utf-8")
        assert schedule_limit() == 1
        with use_cron_store(home):
            confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])
            confirm(interpret("停掉带钥匙", "a")["id"])
            extra = interpret("30分钟后提醒我喝水", "a")
            with pytest.raises(ValueError, match="先停掉或取消一条"):
                confirm(extra["id"])
            assert [item["name"] for item in list_schedules()["items"]] == ["带钥匙"]
            assert list_schedules()["items"][0]["paused"] is True
            path.write_text("pocket:\n  schedule_limit: 2\n", encoding="utf-8")
            confirm(extra["id"])
            assert {item["name"] for item in list_schedules()["items"]} == {"带钥匙", "喝水"}
        path.write_text("pocket:\n  schedule_limit: no\n", encoding="utf-8")
        assert schedule_limit() == int(DEFAULT_CONFIG["pocket"]["schedule_limit"])
    finally:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous, encoding="utf-8")


def test_resume_does_not_catch_up_a_missed_slot(tmp_path):
    with use_cron_store(tmp_path):
        created = confirm(interpret("每两小时提醒我带钥匙", "a")["id"])
        past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        update_job(created["id"], {
            "enabled": False,
            "state": "paused",
            "paused_at": past,
            "paused_reason": "user",
            "next_run_at": past,
        })
        speech = confirm(interpret("继续带钥匙提醒", "a")["id"])["speech"]
        job = get_job(created["id"])
        nxt = datetime.fromisoformat(job["next_run_at"])
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)

        assert speech.startswith("继续了")
        assert job["enabled"] is True
        assert job["paused_at"] is None
        assert job["paused_reason"] is None
        assert nxt > datetime.now(timezone.utc)


def test_authorization_hold_pauses_without_resuming(tmp_path):
    with use_cron_store(tmp_path):
        created = confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])
        assert hold_for_authorization() == 1
        assert get_job(created["id"])["paused_reason"] == "authorization"
        assert hold_for_authorization() == 0
        assert list_schedules()["items"][0]["paused"] is True


def test_cancel_and_destroy_remove_unheard_lines(tmp_path):
    with use_cron_store(tmp_path):
        created = confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])
        deliver_to_pocket(get_job(created["id"]), "带钥匙", ok=True)
        confirm(interpret("取消带钥匙", "a")["id"])
        assert list_schedules() == {"items": [], "unheard": [], "said": []}
        again = confirm(interpret("30分钟后提醒我喝水", "a")["id"])
        deliver_to_pocket(get_job(again["id"]), "喝水", ok=True)
        clear_schedules()
        assert list_schedules()["items"] == []
        assert list_schedules()["unheard"] == []


def test_first_speaker_leases_the_line_and_a_notifier_does_not_hear_it(tmp_path):
    seen = []

    def note(item):
        seen.append(dict(item))

    register_schedule_notifier(note)
    try:
        with use_cron_store(tmp_path):
            job = get_job(confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])["id"])
            deliver_to_pocket(job, "带钥匙", ok=True)
            item_id = list_schedules()["unheard"][0]["id"]
            assert seen == [{"id": item_id, "job_id": job["id"], "text": "带钥匙", "ok": True}]
            assert list_schedules()["said"] == []

            def take(device):
                with use_cron_store(tmp_path):
                    return claim(device, [item_id])

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(take, ("device-1", "device-2")))
            assert sum(len(result) for result in results) == 1
            winner = "device-1" if results[0] else "device-2"
            loser = "device-2" if winner == "device-1" else "device-1"
            with pytest.raises(ValueError):
                settle(item_id, loser, delivered=True)
            assert claim(loser, [item_id]) == []
            settle(item_id, winner, delivered=False)
            assert claim(loser, [item_id])[0]["text"] == "带钥匙"
            settle(item_id, loser, delivered=True)
            board = list_schedules()
            assert board["unheard"] == []
            assert board["said"] == [{"text": "带钥匙", "ok": True}]
    finally:
        unregister_schedule_notifier(note)


def test_a_repeated_failure_is_one_line(tmp_path):
    with use_cron_store(tmp_path):
        job = get_job(confirm(interpret("30分钟后提醒我天气", "a")["id"])["id"])
        deliver_to_pocket(job, "provider exploded", ok=False)
        deliver_to_pocket(job, "provider exploded again", ok=False)
        deliver_to_pocket(job, "", ok=True)
        assert [item["text"] for item in list_schedules()["unheard"]] == ["刚才没能完成天气"]
        deliver_to_pocket(job, "晴", ok=True)
        deliver_to_pocket(job, "still down", ok=False)
        assert [item["text"] for item in list_schedules()["unheard"]] == [
            "刚才没能完成天气",
            "晴",
            "刚才没能完成天气",
        ]


def test_delivery_writes_the_inbox_and_a_reminder_does_not_start_a_model(tmp_path):
    from cron.scheduler import _prepare_job_prompt
    from cron.scheduler_delivery import _deliver_result
    from cron.scheduler_preflight import _preflight_check_delivery

    with use_cron_store(tmp_path):
        reminder = get_job(confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])["id"])
        task = get_job(confirm(interpret("30分钟后跟我说今天天气", "a")["id"])["id"])

        assert _preflight_check_delivery(reminder) is None
        assert _deliver_result(reminder, "带钥匙") is None
        early, prompt = _prepare_job_prompt(reminder, reminder["id"], reminder["name"], None, None)
        assert early[2] == "带钥匙" and prompt is None
        assert task["origin"]["pocket_kind"] == "task"
        assert "不要提问" in task["prompt"] and "今天天气" in task["prompt"]
        assert _deliver_result(task, "english failure", for_failure=True) is None
        texts = [item["text"] for item in list_schedules()["unheard"]]
        assert texts == ["带钥匙", "刚才没能完成今天天气"]


def test_devices_cannot_cross_profiles_or_skip_the_card(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hermes_cli import profiles, web_server
    from hermes_cli.dashboard_auth import pocket_access
    from hermes_cli.dashboard_auth.pocket_store import PocketAuthority

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    profiles.create_profile("role-a", no_alias=True, no_skills=True)
    profiles.create_profile("role-b", no_alias=True, no_skills=True)
    authority = PocketAuthority(home / "pocket-auth.sqlite")
    monkeypatch.setattr(pocket_access, "authority", lambda: authority)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    token_a = authority.exchange(authority.issue_code("role-a")["code"])["token"]
    token_b = authority.exchange(authority.issue_code("role-b")["code"])["token"]
    headers_a = {"X-Hermes-Session-Token": token_a}
    headers_b = {"X-Hermes-Session-Token": token_b}

    with TestClient(web_server.app) as client:
        assert client.post("/api/pocket/schedules/interpret", json={"text": "你好"}).status_code == 401
        forged = client.post(
            "/api/pocket/schedules/interpret?profile=role-b",
            json={"text": "30分钟后提醒我带钥匙"},
            headers=headers_a,
        )
        assert forged.status_code == 403
        chat = client.post("/api/pocket/schedules/interpret", json={"text": "你好"}, headers=headers_a)
        assert chat.status_code == 200 and chat.json()["type"] == "chat"
        asked = client.post("/api/pocket/schedules/interpret", json={"text": "八点提醒我出门"}, headers=headers_a)
        assert asked.status_code == 200 and asked.json()["question"] == "早上还是晚上？"
        created = client.post(
            "/api/pocket/schedules/interpret",
            json={"text": "30分钟后提醒我带钥匙"},
            headers=headers_a,
        )
        assert created.status_code == 200 and created.json()["type"] == "proposal"
        proposal_id = created.json()["id"]
        assert client.post("/api/pocket/schedules/confirm", json={"id": proposal_id}, headers=headers_b).status_code == 400
        assert client.post("/api/pocket/schedules/confirm", json={"id": proposal_id}, headers=headers_a).status_code == 200
        assert client.get("/api/pocket/schedules", headers=headers_b).json()["items"] == []
        assert [item["name"] for item in client.get("/api/pocket/schedules", headers=headers_a).json()["items"]] == ["带钥匙"]
        assert client.get("/api/pocket/schedules", headers=headers_b).json()["items"] == []
        assert [item["name"] for item in client.get("/api/pocket/schedules", headers=headers_a).json()["items"]] == ["带钥匙"]


def test_losing_authorization_pauses_one_profile(tmp_path, monkeypatch):
    import importlib.util

    from hermes_cli.profiles import create_profile, get_profile_dir

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    create_profile("role-a", no_alias=True, no_skills=True)
    create_profile("role-b", no_alias=True, no_skills=True)
    with use_cron_store(get_profile_dir("role-a")):
        confirm(interpret("30分钟后提醒我带钥匙", "role-a")["id"])
    with use_cron_store(get_profile_dir("role-b")):
        confirm(interpret("30分钟后提醒我喝水", "role-b")["id"])
    specification = importlib.util.spec_from_file_location(
        "pocket_admin_hold",
        Path(__file__).resolve().parents[2] / "apps/pocket-admin/server.py",
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module._hold_profile_schedules("missing-role")
    module._hold_profile_schedules("role-a")
    module._hold_profile_schedules("role-a")
    with use_cron_store(get_profile_dir("role-a")):
        item = list_schedules()["items"][0]
        assert item["paused"] is True
        assert get_job(item["id"])["paused_reason"] == "authorization"
    with use_cron_store(get_profile_dir("role-b")):
        assert list_schedules()["items"][0]["paused"] is False


def test_a_resolved_create_is_saved_without_a_confirm_card(tmp_path):
    """The model resolves language. A complete time is saved immediately; an unresolved clock is not."""
    with use_cron_store(tmp_path):
        refused = draft({
            "action": "create", "kind": "remind", "name": "alarm", "resolved": False,
            "when": "2027-01-01T20:00:00+00:00",
        }, "role-a")
        assert refused["ok"] is False
        assert list_schedules()["items"] == []

        saved = draft({
            "action": "create", "kind": "remind", "name": "alarm", "resolved": True,
            "repeat": "weekly", "weekday": 1, "hour": 20, "minute": 0,
            "when_label": "Mondays at 20:00",
        }, "role-a")
        assert saved["ok"] is True
        assert saved["type"] == "saved"
        assert "Mondays at 20:00" in saved["speech"]
        assert get_job(saved["id"])["schedule"]["expr"] == "0 20 * * 1"


def test_a_late_reminder_stays_folded_and_is_delivered_once(tmp_path):
    """A Pocket reminder past the grace window is not run, stays in the list as expired, and is stored once."""
    from cron.scheduler import tick

    with use_cron_store(tmp_path):
        saved = draft({
            "action": "create", "kind": "remind", "name": "带钥匙", "resolved": True,
            "repeat": "once", "when": "2027-01-01T20:00:00+00:00",
            "when_label": "2027-01-01 20:00",
        }, "role-a")
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        update_job(saved["id"], {"next_run_at": past})
        tick(verbose=False, sync=True)
        tick(verbose=False, sync=True)
        board = list_schedules()
        assert [item["name"] for item in board["items"]] == ["带钥匙"]
        assert board["items"][0]["expired"] is True
        assert [item["text"] for item in board["unheard"]] == ["带钥匙"]


def test_newer_todos_lead_and_expired_ones_are_not_announced(tmp_path):
    """A past next run is folded out of the announcement. Newer current todos come first."""
    with use_cron_store(tmp_path):
        older = confirm(interpret("30分钟后提醒我带钥匙", "a")["id"])["id"]
        sooner = confirm(interpret("两小时后提醒我喝水", "a")["id"])["id"]
        newer = confirm(interpret("三小时后提醒我散步", "a")["id"])["id"]
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        update_job(older, {"next_run_at": past, "created_at": "2020-01-01T00:00:00+00:00"})
        update_job(sooner, {"created_at": "2026-01-01T00:00:00+00:00"})
        update_job(newer, {"created_at": "2026-10-30T00:00:00+00:00"})

        items = list_schedules()["items"]
        assert [item["name"] for item in items] == ["散步", "喝水", "带钥匙"]
        assert [item["expired"] for item in items] == [False, False, True]
        speech = interpret("我的日程", "a")["speech"]
        assert speech.index("散步") < speech.index("喝水")
        assert "带钥匙" not in speech
